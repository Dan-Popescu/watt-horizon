"""Constructed format/protocol fixtures; none is a human annotation or a live coverage observation."""

import io
import json
import stat
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from radar.config import Settings
from radar.domain.enums import SourceKind
from radar.evaluation.campaign import load_campaign, prepare
from radar.evaluation.campaign_review import validate
from radar.net.http import PoliteHttp
from radar.pipeline.filings import Services
from radar.pipeline.ingest import ingest_document
from radar.pipeline.run import process_filings
from radar.reading.formats import FormatLimits, checked_zip, detect_format, limit_worker_memory, read_document
from radar.reading.ocr import NullOcr
from radar.reading.pdf import PdfReader
from radar.reading.workers import run_bounded
from radar.sources.dossier import collect_dossier, is_document_url
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import StaticLookups
from radar.web.campaign_app import build_app

QUOTE = "Le projet de stockage par batteries propose une puissance de 30 MW et une capacité de 60 MWh."


def _memory_probe() -> int:
    limit_worker_memory(8 * 1024 * 1024)
    return len(bytearray(32 * 1024 * 1024))


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS unavailable; structural bounds still apply")
def test_native_worker_memory_failure_is_explicit() -> None:
    _, result = next(run_bounded(_memory_probe, [("low-memory", ())], max_workers=1, timeout_s=10))
    assert result.value is None and result.error and "MemoryError" in result.error


def archive(files: dict[str, str | bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, content in files.items():
            zipped.writestr(name, content)
    return output.getvalue()


def docx(text: str = QUOTE) -> bytes:
    return archive(
        {
            "word/document.xml": '<w:document xmlns:w="urn:word"><w:body><w:p><w:r><w:t>'
            + text
            + "</w:t></w:r></w:p></w:body></w:document>"
        }
    )


def xlsx(*, formula: bool = True) -> bytes:
    formula_cell = '<c r="B2"><f>SUM(20,10)</f><v>30</v></c>' if formula else '<c r="B2"><v>30</v></c>'
    return archive(
        {
            "xl/workbook.xml": '<workbook xmlns:r="urn:relationship"><sheets><sheet name="Projet" r:id="r1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": '<worksheet><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Projet</t></is></c><c r="B1" t="inlineStr"><is><t>Puissance MW</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Stockage proposé</t></is></c>'
            + formula_cell
            + "</row></sheetData></worksheet>",
        }
    )


def test_html_and_docx_have_honest_locators_and_no_active_content() -> None:
    html = read_document(
        (
            "<html><body><p>"
            + QUOTE
            + '</p><script>fetch("https://forbidden.test")</script><img src="https://forbidden.test/x"><table><tr><th>Puissance MW</th><th>Projet</th></tr><tr><td>30</td><td>Stockage proposé</td></tr></table></body></html>'
        ).encode()
    )
    assert html.pages == 0 and not html.page_texts and html.format == "html"
    assert html.segments[0].locator == "paragraph:1" and QUOTE in html.text
    assert "forbidden" not in html.text
    cell = next(item for item in html.segments if item.locator == "table:1:row:2:cell:1")
    assert cell.text == "30" and cell.header == "Puissance MW" and "Stockage" in cell.context
    content = read_document(docx())
    assert content.format == "docx" and content.pages == 0 and content.text == QUOTE
    assert content.segments[0].locator == "paragraph:1"


def test_xlsx_formula_is_visible_and_never_a_reliable_number() -> None:
    content = read_document(xlsx())
    cell = next(item for item in content.segments if item.locator == "sheet:1:cell:B2")
    assert cell.uncertain and cell.text == "30" and cell.header == "Puissance MW"
    assert "30" not in content.text and content.format_errors
    assert content.pages == 0


def test_docx_content_controls_keep_body_table_and_cell_locations() -> None:
    body = (
        "<w:sdt><w:sdtPr/><w:sdtContent><w:p><w:r><w:t>" + QUOTE + "</w:t></w:r></w:p></w:sdtContent></w:sdt>"
        "<w:tbl><w:tblPr/><w:sdt><w:sdtContent><w:tr>"
        "<w:sdt><w:sdtContent><w:tc><w:p><w:r><w:t>30 MW</w:t></w:r></w:p></w:tc>"
        "</w:sdtContent></w:sdt></w:tr></w:sdtContent></w:sdt></w:tbl>"
    )
    content = read_document(
        archive({"word/document.xml": '<w:document xmlns:w="urn:word"><w:body>' + body + "</w:body></w:document>"})
    )
    assert content.text == QUOTE + " 30 MW" and not content.format_errors
    assert [segment.locator for segment in content.segments] == ["paragraph:1", "table:1:row:1:cell:1"]


@pytest.mark.parametrize(
    "body",
    [
        "<w:ins><w:p><w:r><w:t>30 MW</w:t></w:r></w:p></w:ins>",
        "<w:p><w:ins><w:r><w:t>30 MW</w:t></w:r></w:ins></w:p>",
        '<w:p><w:fldSimple w:instr="CALCULATE"><w:r><w:t>30 MW</w:t></w:r></w:fldSimple></w:p>',
        '<w:altChunk w:id="r1"/>',
        "<w:unknown><w:p><w:r><w:t>30 MW</w:t></w:r></w:p></w:unknown>",
        "<w:p><w:unknown><w:r><w:t>30 MW</w:t></w:r></w:unknown></w:p>",
        '<mc:AlternateContent xmlns:mc="urn:compatibility"><mc:Choice><w:p><w:r><w:t>30 MW</w:t></w:r></w:p></mc:Choice></mc:AlternateContent>',
    ],
)
def test_docx_unsupported_or_changed_structures_are_explicitly_incomplete(body: str) -> None:
    content = read_document(
        archive({"word/document.xml": '<w:document xmlns:w="urn:word"><w:body>' + body + "</w:body></w:document>"})
    )
    assert content.format_errors and "incomplete" in content.read_by
    assert "30 MW" not in content.text


def test_empty_non_pdf_text_is_incomplete_but_archive_container_is_distinct() -> None:
    assert read_document(docx("")).format_errors
    assert read_document(b"<html><body></body></html>").format_errors
    assert not read_document(archive({"unread.bin": b"not a document"})).format_errors


def test_docx_preserves_split_run_numbers_and_flags_drawing_textboxes() -> None:
    content = read_document(
        archive(
            {
                "word/document.xml": '<w:document xmlns:w="urn:word"><w:body><w:p><w:r><w:t>1</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>00 MW</w:t></w:r></w:p><w:p><w:r><w:drawing><w:p><w:r><w:t>30 MW</w:t></w:r></w:p></w:drawing></w:r></w:p></w:body></w:document>'
            }
        )
    )
    assert content.text == "100 MW" and content.format_errors
    assert content.segments[1].uncertain and "30 MW" in content.segments[1].text


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/escape", "folder\\escape"])
def test_archive_paths_are_rejected_without_writing(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        checked_zip(archive({name: "private"}))


def test_archive_symlinks_bombs_members_and_xml_entities_are_bounded() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        info = zipfile.ZipInfo("symlink")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(info, "/outside")
    with pytest.raises(ValueError, match="symbolic"):
        checked_zip(output.getvalue())
    with pytest.raises(ValueError, match="ratio"):
        checked_zip(archive({"bomb": "a" * 100_000}))
    with pytest.raises(ValueError, match="member limit"):
        checked_zip(archive({"a": "1", "b": "2"}), replace(FormatLimits(), max_members=1))
    with pytest.raises(ValueError, match="XML declarations"):
        read_document(archive({"word/document.xml": '<!DOCTYPE x [<!ENTITY a "EXPAND">]><document>&a;</document>'}))
    with pytest.raises(ValueError, match="node or nesting"):
        read_document(docx(), limits=replace(FormatLimits(), max_xml_nodes=2))
    with pytest.raises(ValueError, match="text or segment"):
        read_document(docx(), max_text_chars=10)
    with pytest.raises(ValueError, match="cell limit"):
        read_document(xlsx(), limits=replace(FormatLimits(), max_cells=1))


def test_archive_ingestion_retains_originals_lineage_and_real_extraction(db: Database, blobs: BlobStore) -> None:
    controlled_document = archive(
        {
            "word/document.xml": '<w:document xmlns:w="urn:word"><w:body><w:sdt><w:sdtContent><w:p><w:r><w:t>'
            + QUOTE
            + "</w:t></w:r></w:p></w:sdtContent></w:sdt></w:body></w:document>"
        }
    )
    source = archive({"annexe.docx": controlled_document, "tableau.xlsx": xlsx(), "unread.bin": b"unsupported"})
    with db.transaction():
        root = ingest_document(
            db,
            blobs,
            data=source,
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="r",
            number="1",
            url="https://example.test/dossier.zip",
            title="Projet de stockage",
            filename="dossier.zip",
        )
    with pytest.raises(ValueError, match="XML encoding"):
        read_document(
            archive(
                {"word/document.xml": '<!DOCTYPE x [<!ENTITY a "EXPAND">]><document>&a;</document>'.encode("utf-16")}
            )
        )
    records = db.documents(root.filing_key)
    assert {record.format for record in records} == {"zip", "docx", "xlsx"}
    assert blobs.get(root.id) == source
    assert all(record.parent_document_id == root.id for record in records if record.id != root.id)
    assert any(edge["state"] == "unsupported" for edge in db.dossier_edges(root.filing_key))
    report = process_filings(db, Services(blobs=blobs, ocr=NullOcr(), classifier=None, lookups=StaticLookups()))
    assert not report.failed
    filing = db.filing(root.filing_key)
    assert filing and filing.machine and filing.machine.values["power_mw"] == 30
    assert filing.evidence["power"].locator == "paragraph:1"
    assert all(observation.page is None for observation in filing.quantity_observations)
    # A stable URL can acquire a different content hash and retains both versions.
    with db.transaction():
        newer = ingest_document(
            db,
            blobs,
            data=docx(QUOTE + " Version suivante."),
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="r",
            number="1",
            url="https://example.test/dossier.zip",
            title="Projet de stockage",
            filename="annexe.docx",
        )
    assert newer.id != root.id and len(db.documents(root.filing_key)) == 4


def test_reader_dispatch_and_cache_keep_structured_content(db: Database, blobs: BlobStore) -> None:
    data = docx()
    reader = PdfReader()
    content = reader.read("docx-hash", data)
    assert content.format == "docx" and content.pages == 0 and content.text == QUOTE
    assert reader.read("docx-hash", data) == content
    assert detect_format(xlsx()) == "xlsx"
    assert is_document_url("https://example.test/ANNEXE.DOCX?download=1")


def test_dossier_collects_office_and_reports_unread_archive_members(db: Database, blobs: BlobStore) -> None:
    body = archive({"annexe.docx": docx(), "unknown.bin": b"unread"})
    requests: list[str] = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return (
            httpx.Response(200, text='<a href="/bundle.zip">Annexe</a>')
            if request.url.path == "/case"
            else httpx.Response(200, content=body)
        )

    with PoliteHttp("test", default_interval_s=0, transport=httpx.MockTransport(response)) as http:
        result = collect_dossier(
            http,
            db,
            blobs,
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="r",
            number="1",
            root_url="https://example.test/case",
            title="Stockage",
        )
    assert result.state == "partial" and requests == ["/case", "/bundle.zip"]
    assert any("unread" in message for message in result.errors)
    assert len(db.documents()) == 2


@pytest.mark.parametrize(
    "data,locator,quotation",
    [(docx(), "paragraph:1", QUOTE), (xlsx(formula=False), "sheet:1:cell:B2", "30")],
)
def test_private_campaign_structured_citations_and_access(
    db: Database,
    blobs: BlobStore,
    settings: Settings,
    tmp_path: Path,
    data: bytes,
    locator: str,
    quotation: str,
) -> None:
    with db.transaction():
        record = ingest_document(
            db,
            blobs,
            data=data,
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="r",
            number="1",
            url="https://example.test/annexe.docx",
            title="Projet de stockage",
            filename="annexe.docx",
        )
    assert not process_filings(
        db, Services(blobs=blobs, ocr=NullOcr(), classifier=None, lookups=StaticLookups())
    ).failed
    directory = tmp_path / "campaign"
    prepare(db, directory, double_rate=0)
    row = json.loads((directory / "reviewer-a.jsonl").read_text())
    row["reviewer"] = "Synthetic protocol reader"
    row["verified"] = True
    row["reviewed_documents"] = [record.id]
    row["fields"]["power_mw"] = {
        "status": "present",
        "value": 30.0,
        "evidence": {"document_id": record.id, "page": None, "locator": locator, "quote": quotation},
    }
    packet = directory / "test-protocol.jsonl"
    packet.write_text(json.dumps(row))
    _, references = validate(load_campaign(directory), [packet], blobs)
    assert references[record.filing_key]["power_mw"] == 30
    row["fields"]["power_mw"]["evidence"]["page"] = 1
    packet.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="invented PDF page"):
        validate(load_campaign(directory), [packet], blobs)
    with TestClient(build_app(settings, directory, "Reader", "a"), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/documents/{record.id}/segments", params={"filing_key": record.filing_key})
        assert response.status_code == 200
        assert any(segment["locator"] == locator for segment in response.json()["segments"])
        assert response.headers["Cache-Control"] == "no-store"
        assert (
            client.get(f"/api/documents/{record.id}/pages/1/text", params={"filing_key": record.filing_key}).status_code
            == 400
        )
        assert (
            client.get(f"/api/documents/{'f' * 64}/segments", params={"filing_key": record.filing_key}).status_code
            == 404
        )
