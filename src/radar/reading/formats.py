"""Bounded, offline document reading; no Office automation, extraction paths, or URL fetches."""

import io
import re
import stat
import struct
import zipfile
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from radar.domain.text import squash
from radar.reading.segments import DocumentSegment
from radar.reading.workers import run_bounded

if TYPE_CHECKING:
    from radar.reading.pdf import PdfContent

FORMAT_VERSION = "office-html-zip:1"
WORKER_MEMORY_BYTES = 512 * 1024 * 1024


def limit_worker_memory(max_memory_bytes: int = WORKER_MEMORY_BYTES) -> None:
    """Call only inside an owned child; POSIX adds an address-space cap to structural input limits."""
    try:
        import resource
    except ImportError:
        return
    if max_memory_bytes <= 0:
        raise ValueError("Worker memory limit must be positive")
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    cap = min(max_memory_bytes, hard) if hard != resource.RLIM_INFINITY else max_memory_bytes
    if soft == resource.RLIM_INFINITY or soft > cap:
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard))


@dataclass(frozen=True)
class FormatLimits:
    max_bytes: int = 32 * 1024 * 1024
    max_uncompressed_bytes: int = 64 * 1024 * 1024
    max_members: int = 100
    max_ratio: int = 200
    max_depth: int = 3
    max_xml_bytes: int = 8 * 1024 * 1024
    max_xml_nodes: int = 200_000
    max_xml_depth: int = 64
    max_rows: int = 20_000
    max_cells: int = 100_000
    max_text_chars: int = 2_000_000

    def __post_init__(self) -> None:
        if min(self.__dict__.values()) <= 0:
            raise ValueError("Document format limits must be positive")


DEFAULT_FORMAT_LIMITS = FormatLimits()


def _check_directory(data: bytes, max_members: int) -> None:
    """Bound central-directory allocation before ZipFile builds its in-memory entry objects."""
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65_557))
    if end < 0 or len(data) - end < 22:
        raise ValueError("Missing ZIP central directory")
    count = struct.unpack_from("<H", data, end + 10)[0]
    size, offset = struct.unpack_from("<II", data, end + 12)
    if count == 65_535 or offset + size != end:
        raise ValueError("ZIP64, multipart or prefixed archives are unsupported")
    if count > max_members:
        raise ValueError("Archive exceeds member limit")
    cursor = offset
    actual = 0
    while cursor < end:
        if cursor + 46 > end or data[cursor : cursor + 4] != b"PK\x01\x02":
            raise ValueError("Invalid ZIP central directory")
        actual += 1
        if actual > max_members:
            raise ValueError("Archive exceeds member limit")
        filename, extra, comment = struct.unpack_from("<HHH", data, cursor + 28)
        cursor += 46 + filename + extra + comment
    if cursor != end or actual != count:
        raise ValueError("Inconsistent ZIP central directory")


def safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not (
        path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or "\x00" in name
    )


def checked_zip(data: bytes, limits: FormatLimits = DEFAULT_FORMAT_LIMITS) -> zipfile.ZipFile:
    """Validate every member before reading any; never write a member to the filesystem."""
    if len(data) > limits.max_bytes:
        raise ValueError("Archive exceeds compressed byte limit")
    _check_directory(data, limits.max_members)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
        if len(entries) > limits.max_members:
            raise ValueError("Archive exceeds member limit")
        if len({entry.filename for entry in entries}) != len(entries):
            raise ValueError("Archive contains duplicate member names")
        total = 0
        for entry in entries:
            if not safe_member(entry.filename) or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Archive contains unsafe member path or symbolic link")
            if entry.flag_bits & 1:
                raise ValueError("Encrypted archive members are unsupported")
            total += entry.file_size
            if total > limits.max_uncompressed_bytes:
                raise ValueError("Archive exceeds uncompressed byte limit")
            if entry.file_size > max(1, entry.compress_size) * limits.max_ratio:
                raise ValueError("Archive exceeds compression ratio limit")
        return archive
    except (zipfile.BadZipFile, NotImplementedError) as exc:
        raise ValueError("Invalid or unsupported ZIP archive") from exc
    except Exception:
        if "archive" in locals():
            archive.close()
        raise


def member_bytes(archive: zipfile.ZipFile, name: str, maximum: int) -> bytes:
    with archive.open(name) as member:
        result = member.read(maximum + 1)
    if len(result) > maximum:
        raise ValueError("Archive member exceeds byte limit")
    return result


def detect_format(data: bytes) -> str:
    if data.lstrip().startswith(b"%PDF-"):
        return "pdf"
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        # Reading the bounded central directory does not decompress members.
        if len(data) > DEFAULT_FORMAT_LIMITS.max_bytes:
            raise ValueError("Document exceeds byte limit")
        _check_directory(data, DEFAULT_FORMAT_LIMITS.max_members)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
                if "word/document.xml" in names:
                    return "docx"
                if "xl/workbook.xml" in names:
                    return "xlsx"
                return "zip"
        except zipfile.BadZipFile as exc:
            raise ValueError("Invalid ZIP document") from exc
    if b"<" in data[:512] and any(
        tag in data[:4096].lower() for tag in (b"<html", b"<!doctype html", b"<body", b"<p", b"<table", b"<div")
    ):
        return "html"
    return "unsupported"


def _xml(archive: zipfile.ZipFile, name: str, limits: FormatLimits) -> ET.Element:
    data = member_bytes(archive, name, limits.max_xml_bytes)
    if b"\x00" in data:
        raise ValueError("Unsupported XML encoding or null bytes")
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML declarations and entities are unsupported")
    nodes = depth = 0
    root: ET.Element | None = None
    try:
        iterator = ET.iterparse(io.BytesIO(data), events=("start", "end"))
        for event, element in iterator:
            if event == "start":
                if root is None:
                    root = element
                nodes += 1
                depth += 1
                if nodes > limits.max_xml_nodes or depth > limits.max_xml_depth:
                    raise ValueError("XML node or nesting limit exceeded")
            else:
                depth -= 1
        if root is None:
            raise ValueError("Empty Office XML")
        return root
    except ET.ParseError as exc:
        raise ValueError("Invalid Office XML") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class _Segments:
    def __init__(self, limits: FormatLimits) -> None:
        self.limits = limits
        self.items: list[DocumentSegment] = []
        self.characters = 0
        self.locators: set[str] = set()

    def add(
        self, locator: str, text: str, *, uncertain: bool = False, reason: str = "", header: str = "", context: str = ""
    ) -> None:
        text = squash(text)
        if not text and not uncertain:
            return
        if locator in self.locators:
            raise ValueError("Document contains ambiguous duplicate locations")
        self.locators.add(locator)
        self.characters += len(text) + len(header) + len(context)
        if self.characters > self.limits.max_text_chars or len(self.items) >= self.limits.max_cells:
            raise ValueError("Document text or segment limit exceeded")
        self.items.append(DocumentSegment(locator, text, uncertain, reason, header, context))


class _Html(HTMLParser):
    """Text only: active tags are ignored, and linked resources are never opened."""

    def __init__(self, segments: _Segments) -> None:
        super().__init__(convert_charrefs=True)
        self.segments = segments
        self.ignored = 0
        self.depth = self.nodes = self.paragraph = self.table = self.row = self.cell = 0
        self.in_cell = False
        self.in_table = False
        self.part_characters = 0
        self.omitted_resources = False
        self.parts: list[str] = []

    def flush(self) -> None:
        if self.parts:
            self.paragraph += 1
            locator = (
                f"table:{self.table}:row:{self.row}:cell:{self.cell}" if self.in_cell else f"paragraph:{self.paragraph}"
            )
            self.segments.add(locator, " ".join(self.parts))
            self.parts = []
            self.part_characters = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"img", "object", "iframe", "video", "canvas"}:
            self.omitted_resources = True
        self.nodes += 1
        if self.nodes > self.segments.limits.max_xml_nodes:
            raise ValueError("HTML node limit exceeded")
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.depth += 1
            if self.depth > self.segments.limits.max_xml_depth:
                raise ValueError("HTML nesting limit exceeded")
        if tag in {"script", "style", "iframe", "object", "noscript", "template"}:
            self.ignored += 1
        if not self.ignored:
            if tag in {"table", "tr", "td", "th"} or (
                not self.in_cell and tag in {"p", "div", "li", "br", "h1", "h2", "h3"}
            ):
                self.flush()
            if tag == "table":
                if self.in_table:
                    raise ValueError("Nested HTML tables are unsupported")
                self.in_table = True
                self.table += 1
                self.row = self.cell = 0
            elif tag == "tr":
                self.row += 1
                self.cell = 0
                if self.row > self.segments.limits.max_rows:
                    raise ValueError("HTML table row limit exceeded")
            elif tag in {"td", "th"}:
                self.cell += 1
                self.in_cell = True

    def handle_endtag(self, tag: str) -> None:
        self.depth = max(0, self.depth - 1)
        if tag in {"script", "style", "iframe", "object", "noscript", "template"} and self.ignored:
            self.ignored -= 1
        elif not self.ignored and (
            tag in {"table", "tr", "td", "th"} or (not self.in_cell and tag in {"p", "div", "li", "h1", "h2", "h3"})
        ):
            self.flush()
            if tag == "table":
                self.row = self.cell = 0
                self.in_table = False
            elif tag in {"td", "th"}:
                self.in_cell = False

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)
            self.part_characters += len(data)
            if self.part_characters > self.segments.limits.max_text_chars:
                raise ValueError("HTML text limit exceeded")


def _docx(archive: zipfile.ZipFile, segments: _Segments) -> None:
    root = _xml(archive, "word/document.xml", segments.limits)
    body = next((node for node in root if _local(node.tag) == "body"), root)
    paragraph = table = rows = cells = 0
    for node in body:
        if _local(node.tag) == "p":
            paragraph += 1
            segments.add(f"paragraph:{paragraph}", " ".join(n.text or "" for n in node.iter() if _local(n.tag) == "t"))
        elif _local(node.tag) == "tbl":
            table += 1
            headers: list[str] = []
            for row_index, row in enumerate((n for n in node if _local(n.tag) == "tr"), 1):
                rows += 1
                if rows > segments.limits.max_rows:
                    raise ValueError("DOCX table row limit exceeded")
                values = [
                    squash(" ".join(n.text or "" for n in cell.iter() if _local(n.tag) == "t"))
                    for cell in row
                    if _local(cell.tag) == "tc"
                ]
                if row_index == 1:
                    headers = values
                for cell_index, value in enumerate(values, 1):
                    cells += 1
                    if cells > segments.limits.max_cells:
                        raise ValueError("DOCX table cell limit exceeded")
                    segments.add(
                        f"table:{table}:row:{row_index}:cell:{cell_index}",
                        value,
                        header=headers[cell_index - 1] if cell_index <= len(headers) else "",
                        context=" ".join(values),
                    )


def _html_context(segments: _Segments) -> None:
    rows: dict[tuple[str, str], list[DocumentSegment]] = {}
    headers: dict[tuple[str, str], str] = {}
    for segment in segments.items:
        parts = segment.locator.split(":")
        if len(parts) == 6 and parts[0] == "table":
            rows.setdefault((parts[1], parts[3]), []).append(segment)
            if parts[3] == "1":
                headers[(parts[1], parts[5])] = segment.text
    for index, segment in enumerate(segments.items):
        parts = segment.locator.split(":")
        if len(parts) != 6 or parts[0] != "table":
            continue
        header = headers.get((parts[1], parts[5]), "")
        context = " ".join(item.text for item in rows[(parts[1], parts[3])])
        segments.characters += len(header) + len(context)
        if segments.characters > segments.limits.max_text_chars:
            raise ValueError("HTML table context exceeds text limit")
        segments.items[index] = replace(segment, header=header, context=context)


def _xlsx(archive: zipfile.ZipFile, segments: _Segments) -> None:
    limits = segments.limits
    names = set(archive.namelist())
    shared: list[str] = []
    shared_characters = 0
    if "xl/sharedStrings.xml" in names:
        for item in _xml(archive, "xl/sharedStrings.xml", limits):
            shared.append("".join(n.text or "" for n in item.iter() if _local(n.tag) == "t"))
            shared_characters += len(shared[-1])
            if len(shared) > limits.max_cells or shared_characters > limits.max_text_chars:
                raise ValueError("XLSX shared string limit exceeded")
    workbook = _xml(archive, "xl/workbook.xml", limits)
    relations = _xml(archive, "xl/_rels/workbook.xml.rels", limits)
    targets: dict[str, str] = {}
    for relation in relations:
        if relation.attrib.get("TargetMode") == "External":
            continue
        target = relation.attrib.get("Target", "")
        path = target.removeprefix("/") if target.startswith("/xl/") else "xl/" + target
        if not safe_member(path):
            raise ValueError("Unsafe XLSX sheet relationship")
        targets[relation.attrib.get("Id", "")] = path
    rows = cells = 0
    for sheet_index, sheet in enumerate((node for node in workbook.iter() if _local(node.tag) == "sheet"), 1):
        relation_id = next((value for key, value in sheet.attrib.items() if _local(key) == "id"), "")
        sheet_target = targets.get(relation_id)
        if not sheet_target or sheet_target not in names:
            raise ValueError("Unavailable or external XLSX sheet")
        root = _xml(archive, sheet_target, limits)
        headers: dict[str, str] = {}
        for row in (node for node in root.iter() if _local(node.tag) == "row"):
            rows += 1
            if rows > limits.max_rows:
                raise ValueError("XLSX row limit exceeded")
            row_values: list[tuple[str, str, bool]] = []
            for cell in (node for node in row if _local(node.tag) == "c"):
                cells += 1
                if cells > limits.max_cells:
                    raise ValueError("XLSX cell limit exceeded")
                address = cell.attrib.get("r", "")
                if re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", address) is None:
                    raise ValueError("Invalid XLSX cell address")
                children = {_local(node.tag): node for node in cell}
                value = children.get("v")
                text = value.text or "" if value is not None else ""
                if cell.attrib.get("t") == "s":
                    try:
                        index = int(text)
                        if index < 0:
                            raise ValueError("Negative shared string index")
                        text = shared[index]
                    except (ValueError, IndexError) as exc:
                        raise ValueError("Invalid XLSX shared string reference") from exc
                elif cell.attrib.get("t") == "inlineStr":
                    text = "".join(node.text or "" for node in cell.iter() if _local(node.tag) == "t")
                formula = "f" in children
                row_values.append((address, text, formula))
            if not headers:
                headers = {
                    "".join(filter(str.isalpha, address)): text for address, text, formula in row_values if not formula
                }
            row_context = " ".join(text for _, text, formula in row_values if not formula)
            for address, text, formula in row_values:
                reason = "Formula result is unverified; formula evaluation is disabled" if formula else ""
                segments.add(
                    f"sheet:{sheet_index}:cell:{address}",
                    text,
                    uncertain=formula,
                    reason=reason,
                    header=headers.get("".join(filter(str.isalpha, address)), ""),
                    context=row_context,
                )


def read_document(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_FORMAT_LIMITS.max_bytes,
    max_text_chars: int = DEFAULT_FORMAT_LIMITS.max_text_chars,
    limits: FormatLimits | None = None,
) -> "PdfContent":
    """Pure parsing for the existing bounded document worker; no implicit I/O or formula evaluation."""
    from radar.reading.pdf import PdfContent

    limits = limits or FormatLimits(max_bytes=max_bytes, max_text_chars=max_text_chars)
    if len(data) > min(max_bytes, limits.max_bytes):
        raise ValueError("Document exceeds byte limit")
    kind = detect_format(data)
    segments = _Segments(limits)
    omitted_resources = False
    if kind == "html":
        parser = _Html(segments)
        parser.feed(data.decode("utf-8-sig", errors="replace"))
        parser.close()
        parser.flush()
        _html_context(segments)
        omitted_resources = parser.omitted_resources
    elif kind in {"docx", "xlsx", "zip"}:
        with checked_zip(data, limits) as archive:
            if kind == "docx":
                _docx(archive, segments)
            elif kind == "xlsx":
                _xlsx(archive, segments)
    else:
        raise ValueError("Unsupported document format")
    errors = tuple(sorted({segment.reason for segment in segments.items if segment.uncertain}))
    if omitted_resources:
        errors += ("Embedded resources require manual review; no resource was fetched",)
    if kind in {"docx", "xlsx"}:
        with checked_zip(data, limits) as archive:
            names = archive.namelist()
            omitted = [
                name
                for name in names
                if name.startswith(
                    (
                        "word/media/",
                        "word/footnotes",
                        "word/endnotes",
                        "word/header",
                        "word/footer",
                        "xl/drawings/",
                        "xl/charts/",
                        "xl/media/",
                    )
                )
            ]
            if omitted:
                errors += ("Embedded images, headers, notes or drawings require manual review",)
    return PdfContent(
        pages=0,
        fields={},
        page_texts=(),
        scanned_pages=(),
        format=kind,
        segments=tuple(segments.items),
        format_errors=errors,
    )


def read_document_bounded(
    data: bytes, *, timeout_s: float = 30, limits: FormatLimits = DEFAULT_FORMAT_LIMITS
) -> "PdfContent":
    _, result = next(run_bounded(_read, [("document", (data, limits))], max_workers=1, timeout_s=timeout_s))
    if result.error or result.value is None:
        raise ValueError(result.error or "Document worker returned no content")
    return result.value


def _read(data: bytes, limits: FormatLimits) -> "PdfContent":
    limit_worker_memory()
    return read_document(data, max_bytes=limits.max_bytes, max_text_chars=limits.max_text_chars, limits=limits)


def archive_members_bounded(
    data: bytes, *, timeout_s: float = 30, limits: FormatLimits = DEFAULT_FORMAT_LIMITS
) -> tuple[tuple[str, bytes], ...]:
    """Decompression is native work too; it shares the terminable document-worker contract."""
    _, result = next(run_bounded(_archive_members, [("archive", (data, limits))], max_workers=1, timeout_s=timeout_s))
    if result.error or result.value is None:
        raise ValueError(result.error or "Archive worker returned no members")
    return result.value


def _archive_members(data: bytes, limits: FormatLimits) -> tuple[tuple[str, bytes], ...]:
    limit_worker_memory()
    with checked_zip(data, limits) as archive:
        return tuple(
            (entry.filename, member_bytes(archive, entry.filename, limits.max_bytes))
            for entry in archive.infolist()
            if not entry.is_dir()
        )
