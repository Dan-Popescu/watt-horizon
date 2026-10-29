"""Offline campaign contracts, including tiny quantities, partial reviews and held-out privacy.

The small PDF text is synthetic administrative-shaped data for protocol testing, not human gold.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from radar import cli
from radar.config import Settings
from radar.domain.enums import Category, SourceKind
from radar.domain.models import Classification, Filing, Link, LinkDecision, MachineOutput
from radar.evaluation.campaign import check_documents, load_campaign, prepare
from radar.evaluation.campaign_compare import comparison, load_snapshot, snapshot
from radar.evaluation.campaign_review import validate
from radar.pipeline.ingest import ingest_pdf
from radar.store.blobs import BlobStore
from radar.store.db import Database
from tests.conftest import FILLER, make_pdf

QUOTE = "une puissance de 0,05 MW"


def seed(db: Database, blobs: BlobStore, *, partition: str = "dev", number: str = "000001") -> Filing:
    key = f"dreal_case_by_case:hauts-de-france:{number}"
    with db.transaction():
        document = ingest_pdf(
            db,
            blobs,
            data=make_pdf([FILLER + number + " Le projet de stockage par batteries declare " + QUOTE + "."]),
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="hauts-de-france",
            number=number,
            url="https://example.org/form.pdf",
            title="Stockage",
            filename="form.pdf",
        )
        classification = Classification(category=Category.BESS, method="rules")
        filing = Filing(
            key=key,
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="hauts-de-france",
            number=number,
            title="Stockage",
            document_ids=(document.id,),
            classification=classification,
            machine=MachineOutput(classification=classification, values={"power_mw": 0.05}),
            strata={"read": "text", "template": "one", "evaluation_partition": partition},
        )
        db.upsert_filing(filing)
        db.note_processing_outcome(key, "complete")
    return filing


def packet(directory: Path, *, reviewer: str = "Alice", value: float = 0.05) -> Path:
    row = json.loads((directory / "reviewer-a.jsonl").read_text().splitlines()[0])
    row["reviewer"], row["verified"] = reviewer, True
    document_id = next(iter(row["document_relevance"]))
    row["reviewed_documents"] = [document_id]
    row["fields"]["power_mw"] = {
        "status": "present",
        "value": value,
        "evidence": {"document_id": document_id, "page": 1, "quote": QUOTE},
    }
    out = directory / f"{reviewer}.jsonl"
    out.write_text(json.dumps(row) + "\n")
    return out


def test_prepare_blind_packets_are_immutable(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    seed(db, blobs)
    directory = tmp_path / "campaign"
    report = prepare(db, directory, double_rate=1)
    campaign = load_campaign(directory)
    assert report["selected_groups"] == 1 and report["double_review_groups"] == 1
    assert report["independence"] == "unestablished" and not report["quality_measured"]
    assert report["strata"]["source"]["unrepresented"] == []
    row = json.loads((directory / "reviewer-a.jsonl").read_text())
    assert not row["verified"] and not row["reviewer"]
    assert {o["status"] for o in row["fields"].values()} == {"unreviewed"}
    assert all(o["value"] is None for o in row["fields"].values())
    assert campaign.records[0].documents[0].id in (directory / "manifest.json").read_text()
    with pytest.raises(ValueError, match="already exists"):
        prepare(db, directory)
    raw = json.loads((directory / "manifest.json").read_text())
    raw["records"][0]["partition"] = "test"
    (directory / "manifest.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="fingerprint"):
        load_campaign(directory)


def test_partial_double_reviews_need_distinct_consensus(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    seed(db, blobs)
    directory = tmp_path / "campaign"
    prepare(db, directory, double_rate=1)
    campaign = load_campaign(directory)
    first = packet(directory)
    report, references = validate(campaign, [first], blobs)
    assert report["accepted_fields"] == 0 and not references
    second = packet(directory, reviewer="Bob")
    report, references = validate(campaign, [first, second], blobs)
    assert report["accepted_fields"] == 1
    assert references[campaign.records[0].filing_key] == {"power_mw": 0.05}
    assert report["pending_fields"] > 0
    with pytest.raises(ValueError, match="Duplicate reviewer"):
        validate(campaign, [first, first], blobs)
    for path, category in ((first, "bess"), (second, "other")):
        row = json.loads(path.read_text())
        row["fields"]["category"] = {
            "status": "present",
            "value": category,
            "evidence": row["fields"]["power_mw"]["evidence"],
        }
        path.write_text(json.dumps(row))
    report, references = validate(campaign, [first, second], blobs)
    assert report["disagreements"] == 1
    assert "category" not in references[campaign.records[0].filing_key]


def test_unknown_unreviewed_and_unverified_never_become_absent(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    seed(db, blobs)
    directory = tmp_path / "campaign"
    prepare(db, directory, double_rate=0)
    campaign = load_campaign(directory)
    blank = directory / "reviewer-a.jsonl"
    report, refs = validate(campaign, [blank], blobs)
    assert report["status"] == "unmeasured" and not refs
    reviewed = packet(directory)
    row = json.loads(reviewed.read_text())
    row["fields"]["substation"]["status"] = "unknown"
    row["fields"]["siret"]["status"] = "absent"
    reviewed.write_text(json.dumps(row))
    _, refs = validate(campaign, [reviewed], blobs)
    values = refs[campaign.records[0].filing_key]
    assert values == {"power_mw": 0.05, "siret": None}
    row["reviewed_documents"] = []
    row["fields"]["power_mw"] = {"status": "unreviewed"}
    reviewed.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="every campaign document"):
        validate(campaign, [reviewed], blobs)


@pytest.mark.parametrize(
    "mutation", ["quote", "page", "value", "reviewer", "blind", "fingerprint", "fields", "date", "siret"]
)
def test_invalid_human_claims_are_refused(mutation: str, db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    seed(db, blobs)
    directory = tmp_path / "campaign"
    prepare(db, directory, double_rate=0)
    path = packet(directory)
    row = json.loads(path.read_text())
    if mutation == "quote":
        row["fields"]["power_mw"]["evidence"]["quote"] = "Invented 0.05 MW"
    elif mutation == "page":
        row["fields"]["power_mw"]["evidence"]["page"] = 2
    elif mutation == "value":
        row["fields"]["power_mw"]["value"] = 5.0
    elif mutation == "reviewer":
        row["reviewer"] = " "
    elif mutation == "blind":
        row["blind"] = False
    elif mutation == "fingerprint":
        row["campaign_fingerprint"] = "different"
    elif mutation in {"date", "siret"}:
        name = "filed_on" if mutation == "date" else "siret"
        row["fields"][name] = {
            "status": "present",
            "value": "tomorrow" if mutation == "date" else "123",
            "evidence": row["fields"]["power_mw"]["evidence"],
        }
    else:
        del row["fields"]["siret"]
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError):
        validate(load_campaign(directory), [path], blobs)


def test_snapshot_requires_raw_machine_and_unchanged_sources(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    filing = seed(db, blobs)
    directory = tmp_path / "campaign"
    prepare(db, directory)
    campaign = load_campaign(directory)
    baseline = tmp_path / "before.json"
    snapshot(db, campaign, baseline, "before")
    assert load_snapshot(baseline, campaign).predictions[0].values["power_mw"] == 0.05
    with db.transaction():
        db.upsert_filing(filing.model_copy(update={"machine": None}))
    with pytest.raises(ValueError, match="raw machine"):
        snapshot(db, campaign, tmp_path / "bad.json", "after")
    with db.transaction():
        db.upsert_filing(filing.model_copy(update={"document_ids": ()}))
    with pytest.raises(ValueError, match="stale"):
        check_documents(db, campaign)


def test_exact_vs_tolerance_and_heldout_error_privacy(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    filing = seed(db, blobs, partition="test")
    directory = tmp_path / "campaign"
    prepare(db, directory, double_rate=0)
    campaign = load_campaign(directory)
    _, refs = validate(campaign, [packet(directory)], blobs)
    baseline, candidate = tmp_path / "before.json", tmp_path / "after.json"
    snapshot(db, campaign, baseline, "before")
    assert filing.machine is not None
    with db.transaction():
        db.upsert_filing(
            filing.model_copy(update={"machine": filing.machine.model_copy(update={"values": {"power_mw": 0.1}})})
        )
        db.note_processing_outcome(filing.key, "complete")
    snapshot(db, campaign, candidate, "after")
    report = comparison(campaign, refs, load_snapshot(baseline, campaign), load_snapshot(candidate, campaign))
    scores = report["partitions"]["test"]["power_mw"]
    assert scores["candidate"]["exact_accuracy"] == 0
    assert scores["candidate"]["business_tolerance_accuracy"] == 1
    assert scores["paired_groups"]["regressed_exact"] == 1
    assert not report["dev_errors"]
    assert filing.key not in json.dumps(report)


def test_empty_campaign_comparison_is_unmeasured(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    directory = tmp_path / "campaign"
    assert prepare(db, directory)["status"] == "empty"
    campaign = load_campaign(directory)
    report, refs = validate(campaign, [directory / "reviewer-a.jsonl"], blobs)
    assert report["status"] == "unmeasured" and not refs
    path = tmp_path / "empty.json"
    snapshot(db, campaign, path, "empty")
    result = comparison(campaign, refs, load_snapshot(path, campaign), load_snapshot(path, campaign))
    assert result["status"] == "unmeasured" and not result["quality_measured"]


def test_grouped_sampling_keeps_shared_documents_together(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    filing = seed(db, blobs)
    other_key = "mrae_opinion:hauts-de-france:000002"
    with db.transaction():
        document = db.documents(filing.key)[0]
        db.upsert_document(document.model_copy(update={"filing_key": other_key, "source": SourceKind.MRAE_OPINION}))
        db.upsert_filing(
            filing.model_copy(update={"key": other_key, "number": "000002", "source": SourceKind.MRAE_OPINION})
        )
        db.note_processing_outcome(filing.key, "complete")
        db.note_processing_outcome(other_key, "complete")
    directory = tmp_path / "campaign"
    report = prepare(db, directory, limit=1)
    records = load_campaign(directory).records
    assert report["selected_groups"] == 1 and len(records) == 2
    assert len({r.group for r in records}) == 1
    assert len({r.partition for r in records}) == 1
    campaign = load_campaign(directory)
    snap_path = tmp_path / "group.json"
    snapshot(db, campaign, snap_path, "group")
    snap = load_snapshot(snap_path, campaign)
    result = comparison(campaign, {r.filing_key: {"power_mw": 0.05} for r in records}, snap, snap)
    metrics = result["partitions"][records[0].partition]["power_mw"]["baseline"]
    assert metrics["n"] == 2 and metrics["independent_n"] == 1


def test_snapshot_integrity_and_category_gold_exclusion(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    filing = seed(db, blobs)
    assert filing.machine is not None
    with db.transaction():
        db.upsert_filing(
            filing.model_copy(
                update={
                    "machine": filing.machine.model_copy(
                        update={"classification": Classification(category=Category.BESS, method="gold")}
                    )
                }
            )
        )
    directory = tmp_path / "campaign"
    prepare(db, directory)
    campaign = load_campaign(directory)
    path = tmp_path / "snapshot.json"
    snapshot(db, campaign, path, "gold")
    snap = load_snapshot(path, campaign)
    result = comparison(campaign, {filing.key: {"category": "bess"}}, snap, snap)
    assert result["excluded_nonindependent_categories"] == 1 and not result["quality_measured"]
    raw = json.loads(path.read_text())
    raw["predictions"] = []
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="fingerprint"):
        load_snapshot(path, campaign)


def test_cli_unverified_campaign_is_unmeasured_and_strict_fails(
    db: Database,
    blobs: BlobStore,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(db, blobs)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    directory = tmp_path / "campaign"
    assert cli.main(["campaign", "prepare", "--out", str(directory), "--double-rate", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "awaiting_human_review"
    snap_path = tmp_path / "snapshot.json"
    assert cli.main(["campaign", "snapshot", str(directory), "--name", "base", "--out", str(snap_path)]) == 0
    capsys.readouterr()
    args = [
        "campaign",
        "compare",
        str(directory),
        "--baseline",
        str(snap_path),
        "--candidate",
        str(snap_path),
        "--annotations",
        str(directory / "reviewer-a.jsonl"),
        "--strict",
    ]
    assert cli.main(args) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "unmeasured" and "error" in result


@pytest.mark.parametrize(
    "field,new_value", [("title", "Changed title"), ("filename", "changed.pdf"), ("audit", "random")]
)
def test_snapshot_binds_processing_metadata(
    field: str, new_value: str, db: Database, blobs: BlobStore, tmp_path: Path
) -> None:
    filing = seed(db, blobs)
    directory = tmp_path / "campaign"
    prepare(db, directory)
    manifest = load_campaign(directory)
    document = db.documents(filing.key)[0]
    with db.transaction():
        db.upsert_document(document.model_copy(update={field: new_value}))
    with pytest.raises(ValueError, match="metadata changed"):
        snapshot(db, manifest, tmp_path / "stale.json", "stale")


def test_incomplete_processing_blocks_snapshot(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    filing = seed(db, blobs)
    directory = tmp_path / "campaign"
    prepare(db, directory)
    with db.transaction():
        db.note_processing_outcome(filing.key, "waiting")
    with pytest.raises(ValueError, match="processing outcomes"):
        snapshot(db, load_campaign(directory), tmp_path / "stale.json", "stale")


def test_cli_comparison_rechecks_groups_after_snapshot_export(
    db: Database,
    blobs: BlobStore,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = seed(db, blobs)
    second = seed(db, blobs, number="000002", partition="test")
    directory = tmp_path / "campaign"
    prepare(db, directory)
    snapshot_path = tmp_path / "before.json"
    snapshot(db, load_campaign(directory), snapshot_path, "before")
    with db.transaction():
        db.add_link_decision(
            LinkDecision(
                a=first.key, b=second.key, verdict="same_project", author="Reviewer", created_at=datetime.now(UTC)
            )
        )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert (
        cli.main(
            [
                "campaign",
                "compare",
                str(directory),
                "--baseline",
                str(snapshot_path),
                "--candidate",
                str(snapshot_path),
                "--annotations",
                str(directory / "reviewer-a.jsonl"),
            ]
        )
        == 2
    )
    report = json.loads(capsys.readouterr().out)
    assert "cross frozen" in report["error"]


@pytest.mark.parametrize("partition", ["dev", "test"])
@pytest.mark.parametrize("decision_only", [False, True])
def test_new_same_project_relationship_invalidates_frozen_groups(
    partition: str, decision_only: bool, db: Database, blobs: BlobStore, tmp_path: Path
) -> None:
    first = seed(db, blobs)
    second = seed(db, blobs, number="000002", partition=partition)
    directory = tmp_path / "campaign"
    prepare(db, directory)
    manifest = load_campaign(directory)
    assert len({r.group for r in manifest.records}) == 2
    with db.transaction():
        if decision_only:
            db.add_link_decision(
                LinkDecision(
                    a=first.key, b=second.key, verdict="same_project", author="Reviewer", created_at=datetime.now(UTC)
                )
            )
        else:
            db.replace_links([Link(a=first.key, b=second.key, reasons=("manual",), verdict="same_project")])
    with pytest.raises(ValueError, match=r"group membership changed|cross frozen"):
        snapshot(db, manifest, tmp_path / "bad.json", "bad")


@pytest.mark.parametrize("missing", [False, True])
def test_absent_only_review_requires_intact_document_bytes(
    missing: bool, db: Database, blobs: BlobStore, tmp_path: Path
) -> None:
    filing = seed(db, blobs)
    directory = tmp_path / "campaign"
    prepare(db, directory, double_rate=0)
    manifest = load_campaign(directory)
    path = packet(directory)
    row = json.loads(path.read_text())
    row["fields"]["power_mw"] = {"status": "unreviewed"}
    row["fields"]["siret"] = {"status": "absent"}
    path.write_text(json.dumps(row))
    raw = blobs.path_for(filing.document_ids[0])
    if missing:
        raw.unlink()
    else:
        raw.write_bytes(b"corrupt")
    with pytest.raises((ValueError, OSError)):
        validate(manifest, [path], blobs)
