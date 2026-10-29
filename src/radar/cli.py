"""Command line. Every command prints one JSON object on stdout (machine-readable); logs go to stderr.

radar import files|signals|gold|cache|communes ...   load data already on disk (no network)
radar collect dreal|mrae|georisques|registry|press|prefectures ...   polite network collection
radar process | link | run                           build filings, links, projects (idempotent)
radar update                                         scheduled job: polite collect of recent items + run
radar changes --since D | export csv|geojson | digest --since D   alerts and files for clients
radar web                                            map + JSON API + MCP over HTTP
radar eval | stats | mcp
"""

import argparse
import dataclasses
import json
import sqlite3
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from radar.api import digest, exports, queries, review
from radar.api.quality import quality_overview
from radar.classification.gold import load_gold
from radar.classification.terms import ScreenTerms
from radar.config import Settings, load_settings
from radar.domain.privacy import PersonScrubber
from radar.domain.references import canonical_reference_map
from radar.evaluation.baseline import (
    REQUIRED_FIELDS,
    approve_baseline,
    assess_report,
    dataset_fingerprint,
    load_baseline,
)
from radar.evaluation.classification import evaluate_classifier
from radar.evaluation.gold import draft as draft_field_gold
from radar.evaluation.gold import load_field_gold, write_field_gold
from radar.evaluation.ocr import evaluate_ocr
from radar.evaluation.privacy import audit_privacy
from radar.evaluation.report import ReportInputs, build_report
from radar.evaluation.report import metrics as eval_metrics
from radar.evaluation.stats import partition_of
from radar.extraction import EXTRACTION_VERSION
from radar.net.http import PoliteHttp, SiteBlockedError
from radar.pipeline import importers, llm_batch
from radar.pipeline.corrections import reviewed_references
from radar.pipeline.filings import Services, read_documents
from radar.pipeline.health import collector_health
from radar.pipeline.operations import (
    PublishedDataset,
    activate_dataset,
    dataset_status,
    operational_status,
    process_lock,
    publish_dataset,
)
from radar.pipeline.orchestration import execute_processing
from radar.pipeline.run import assign_partitions, link_and_build, process_filings
from radar.quality import cases, corpus, errors, improve, propagate, robustness, rulebook, screen_terms, shadow
from radar.quality.arbiter import learn
from radar.quality.drift import drift
from radar.quality.suggest import suggest
from radar.quality.templates import Registry
from radar.quality.triage import triage
from radar.reading.ocr import TesseractOcr
from radar.sources import dreal, georisques, mrae, prefectures, press, registry
from radar.sources.regions import get_region
from radar.store.blobs import BlobStore
from radar.store.cache import KeyValueCache
from radar.store.db import Database
from radar.store.llm_jobs import BudgetLimits
from radar.store.readiness import candidate_readiness
from radar.store.snapshots import backup_dataset, restore_dataset
from radar.validation.lookups import JsonCache, ReferenceCommunes
from radar.wiring import batch_client, make_classifier, make_http, make_services


def _emit(payload: Any) -> None:
    def default(o: Any) -> Any:
        if is_dataclass(o) and not isinstance(o, type):
            return asdict(o)
        if hasattr(o, "model_dump"):
            return o.model_dump(mode="json")
        return str(o)

    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=default) + "\n")


def _require_http(settings: Settings) -> PoliteHttp:
    http = make_http(settings)
    if http is None:
        raise SystemExit("RADAR_OFFLINE=1: network commands are disabled.")
    return http


# Commands ----------------------------------------------------------------------------------------------
def cmd_import(args: argparse.Namespace, s: Settings) -> Any:
    with Database(s.db_path) as db:
        if args.what == "files":
            return importers.import_files(
                db,
                BlobStore(s.blobs_dir),
                Path(args.index),
                Path(args.docs),
                args.region,
                Path(args.transcripts) if args.transcripts else None,
                s.transcripts_dir,
            )
        if args.what == "signals":
            if args.kind == "registry":
                return {"signals": importers.import_registry_signals(db, Path(args.path))}
            if args.kind == "energy-registry":
                return {"signals": importers.import_energy_registry(db, Path(args.path))}
            return {"signals": importers.import_press_signals(db, Path(args.path))}
    if args.what == "gold":
        return {"labels": importers.import_gold(Path(args.path), s.gold_path)}
    if args.what == "cache":
        return {"added": importers.import_cache(Path(args.path), JsonCache(s.lookup_cache_path))}
    return {"communes": importers.import_communes(Path(args.path), s.communes_path)}


def _collect_one(source: str, args: argparse.Namespace, s: Settings, http: PoliteHttp, db: Database) -> Any:
    """Run one collector; a site that blocks us ends this source for the run (never retried in a loop)."""
    region = get_region(args.region)
    blobs = BlobStore(s.blobs_dir)
    run_id = db.start_run(f"collect:{source}")
    try:
        if source == "dreal":
            listings = tuple(args.listing) if args.listing else region.dreal_listings[:2]
            terms = ScreenTerms.load(s.screen_terms_path).pattern()
            report = dreal.collect_dreal(
                http, db, blobs, region, listings, args.max_offset, args.max_items, screen_terms=terms
            )
        elif source == "mrae":
            report = mrae.collect_mrae(http, db, blobs, region, args.year, args.max_items)
        elif source == "georisques":
            report = georisques.collect_georisques(http, db, region.departments)
        elif source == "registry":
            report = registry.collect_registry(http, db, region.departments, date.fromisoformat(args.since))
        elif source == "press":
            ref = ReferenceCommunes(s.communes_path)
            names = [c.name for dep in region.departments for c in ref.communes_in(dep)]
            report = press.collect_press(http, db, names)
        else:
            report = prefectures.collect_prefectures(http, db, blobs, region, args.max_pages)
    except SiteBlockedError as exc:
        db.finish_run(run_id, {"stopped": str(exc)})
        return {"stopped": str(exc), "advice": "Site closed the connection: retry another day, never in a loop."}
    db.finish_run(run_id, asdict(report))
    return report


def cmd_collect(args: argparse.Namespace, s: Settings) -> Any:
    with _require_http(s) as http, Database(s.db_path) as db:
        return _collect_one(args.source, args, s, http, db)


UPDATE_SOURCES = ("dreal", "mrae", "registry", "press")  # prefectures: opt-in only (they blocked us once)


def cmd_update(args: argparse.Namespace, s: Settings) -> Any:
    """Scheduled job: light collection of recent items, then rebuild. Safe to run daily."""
    collected: dict[str, Any] = {}
    with _require_http(s) as http, Database(s.db_path) as db:
        for source in args.sources:
            collected[source] = _collect_one(source, args, s, http, db)
    args.llm_batch = True  # scheduled: model calls go through half-price batches, answers read at the next run
    result = {"collect": collected, **cmd_run(args, s)}
    with Database(s.db_path) as db:
        health = collector_health(db.runs("collect:"), now=datetime.now(UTC))
        drifting = drift(db.filings(), _first_fetched(db), datetime.now(UTC))
    alerts = [h.source for h in health if h.status == "alert"] + [f"drift:{d.value}:{d.metric}" for d in drifting]
    return {**result, "health_alerts": alerts}


def cmd_process(args: argparse.Namespace, s: Settings) -> Any:
    """Rebuild filings. With --llm-batch, model calls missing from the cache go into a Message Batch (half price):
    answers of ended batches are collected first, new requests are submitted last; --wait loops until done."""
    batch = bool(getattr(args, "llm_batch", False)) and bool(s.llm_model) and not s.offline
    with Database(s.db_path) as db:
        assign_partitions(db, s.partitions_path, s.evaluation_test_after)
        only = set(args.only) if args.only else None
        services = make_services(s, make_http(s), llm="batch" if batch else "live")
        return execute_processing(
            db,
            services,
            client=batch_client(s) if batch else None,
            only=only,
            full=getattr(args, "full", False),
            wait=bool(getattr(args, "wait", False)),
            timeout_s=s.llm_wait_timeout_s,
            poll_s=s.llm_poll_interval_s,
        )


def cmd_llm_batch(args: argparse.Namespace, s: Settings) -> Any:
    """Model batches: status (queued / in flight), collect (write the answers of ended batches into the cache)."""
    if args.action == "collect" and (not s.llm_model or s.offline):
        return {"error": "Collecting provider results requires a configured model and network access"}
    services = make_services(s, None, llm="batch" if args.action == "collect" else "cached")
    queue = services.llm_queue or llm_batch.LlmQueue(
        KeyValueCache(s.cache_path),
        jobs_path=s.llm_jobs_path,
        campaign=s.llm_campaign,
        limits=BudgetLimits(
            daily_usd=s.llm_daily_budget_usd,
            campaign_usd=s.llm_campaign_budget_usd,
            filing_usd=s.llm_filing_budget_usd,
            max_attempts=s.llm_max_attempts,
        ),
    )
    if args.action == "resolve":
        if not args.submission:
            return {"error": "--submission is required for reconciliation"}
        queue.resolve_uncertain(
            args.submission,
            provider_batch_id=args.provider_batch,
            confirm_not_accepted=args.confirm_not_accepted,
        )
        return queue.summary()
    if args.action == "settle-cost":
        if not args.submission or not args.request_key or args.actual_usd is None:
            return {"error": "--submission, --request-key and --actual-usd are required for external cost confirmation"}
        queue.jobs.settle_cost(args.submission, args.request_key, actual_usd=args.actual_usd)
        return queue.summary()
    if args.action == "collect":
        return {**llm_batch.collect(batch_client(s), queue, services.llm_tasks), "next": "radar run"}
    if args.action == "reparse":
        tasks = {task.name: task for task in services.llm_tasks}
        parsed = 0
        for key, entry in queue.jobs.iter_entries("received", "parse_failed", "await_parser"):
            if task := tasks.get(entry["task"]):
                parsed += queue.parse_saved(key, task)
        return {"parsed": parsed, "jobs": queue.summary(), "next": "radar run"}
    return queue.summary()


def cmd_link(_: argparse.Namespace, s: Settings) -> Any:
    with Database(s.db_path) as db:
        run_id = db.start_run("link")
        result = link_and_build(db)
        db.finish_run(run_id, result)
        return result


def cmd_run(args: argparse.Namespace, s: Settings) -> Any:
    processed = cmd_process(args, s)
    if "error" in processed:
        return {"process": processed, "error": "Processing incomplete; projects were not rebuilt"}
    if processed["status"] != "complete":
        return {"process": processed, "link": {"skipped": "waiting for model results"}}
    linked = cmd_link(args, s)
    with Database(s.db_path) as db:
        assign_partitions(db, s.partitions_path, s.evaluation_test_after)
    return {"process": processed, "link": linked}


def cmd_eval(args: argparse.Namespace, s: Settings) -> Any:
    """Quality report, stored and compared with the previous one. --strict: exit 2 on any regression."""
    blobs = BlobStore(s.blobs_dir)
    with Database(s.db_path) as db:
        filings, manifest = assign_partitions(db, s.partitions_path, s.evaluation_test_after)
        file_labels, file_fields = load_gold(s.gold_path), load_field_gold(s.field_gold_path)
        if args.draft_fields:  # the file keeps hand-written references + unverified drafts; reviews stay in the db
            known_labels = canonical_reference_map(filings, file_labels)
            drafts = draft_field_gold([f for f in filings if f.key in known_labels], file_fields, EXTRACTION_VERSION)
            write_field_gold(s.field_gold_path, [*file_fields.values(), *drafts])
            file_fields = load_field_gold(s.field_gold_path)
        reviewed_labels, reviewed_fields = reviewed_references(filings, db.corrections())
        labels = {**canonical_reference_map(filings, file_labels), **reviewed_labels}
        field_gold = {**canonical_reference_map(filings, file_fields), **reviewed_fields}
        ocr = None
        if TesseractOcr.available():
            ocr = evaluate_ocr(
                TesseractOcr(
                    max_workers=s.pdf_workers,
                    max_pages=s.ocr_max_pages,
                    max_bytes=s.pdf_max_bytes,
                    max_pixels=s.ocr_max_pixels,
                    document_timeout_s=s.ocr_timeout_s,
                ),
                blobs,
                s.transcripts_dir,
            )
        classifier = None
        if args.classifier:
            services = make_services(s, make_http(s))
            llm = make_classifier(s, KeyValueCache(s.cache_path), with_gold=False)
            if llm is None:
                return {"error": "No model classifier: set RADAR_LLM_MODEL (and ANTHROPIC_API_KEY), not offline."}
            classifier = evaluate_classifier(llm, db, services, labels)
        readiness = candidate_readiness(s.data_dir)
        failures = dict(db.last_runs().get("process", {}).get("stats", {}).get("failed", {}))
        failures.update({f"readiness:{reason}": reason for reason in readiness["reasons"]})
        eval_services = make_services(s, None, llm="cached")
        report = build_report(
            ReportInputs(
                filings=filings,
                labels=labels,
                field_gold=field_gold,
                corrections=db.corrections(),
                privacy_leaks=audit_privacy(db, blobs, eval_services.reader),
                listing=db.listing_counts(),
                cases=cases.refresh(s.cases_dir, rulebook.Rulebook.load(s.rules_path).effective())
                if s.cases_dir.exists()
                else None,
                ocr=ocr,
                classifier=classifier,
                links=db.links(),
                link_decisions=db.current_link_decisions(),
                partition_manifest=manifest,
                failures=failures,
            )
        )
        report["robustness"] = robustness.check_robustness(corpus_documents(db, eval_services)).summary()
        current = eval_metrics(report)
        baseline_path = Path(args.baseline) if getattr(args, "baseline", None) else s.baseline_path
        previous = load_baseline(baseline_path)
        profile: dict[str, Any] = {
            "min_samples": getattr(args, "min_samples", 30),
            "required_fields": tuple(getattr(args, "required_fields", ",".join(REQUIRED_FIELDS)).split(",")),
            "min_accuracy": getattr(args, "min_accuracy", 0.95),
            "min_screening_recall": getattr(args, "min_screening_recall", 0.95),
        }
        report["dataset_fingerprint"] = dataset_fingerprint(s.db_path)
        gate = assess_report(report, previous=previous, **profile)
        run_id = db.start_run("eval")
        db.finish_run(run_id, {"metrics": current, "report": report})
    result = {**report, "comparison": gate["comparison"], "gate": gate}
    if getattr(args, "new_campaign", False) and not getattr(args, "approve_baseline", False):
        return {**result, "error": "--new-campaign requires --approve-baseline"}
    if getattr(args, "approve_baseline", False):
        approved = approve_baseline(
            baseline_path,
            report,
            dataset_id=report["dataset_fingerprint"],
            new_campaign=getattr(args, "new_campaign", False),
            **profile,
        )
        result.update(approved_baseline=str(baseline_path), gate=approved["gate"])
    if args.strict and result["gate"]["status"] != "passed":
        return {**result, "error": "evaluation approval requirements are not satisfied"}
    return result


def cmd_stats(_: argparse.Namespace, s: Settings) -> Any:
    with Database(s.db_path) as db:
        return {**queries.dataset_info(db).model_dump(mode="json"), "operations": operational_status(s.data_dir)}


def cmd_backup(args: argparse.Namespace, s: Settings) -> Any:
    """Back up durable work and references without racing the local writer."""
    return backup_dataset(s.data_dir, Path(args.out), gold_dir=s.gold_dir, keep=args.keep)


def cmd_restore(args: argparse.Namespace, _: Settings) -> Any:
    return restore_dataset(
        Path(args.snapshot),
        Path(args.to),
        gold_destination=Path(args.gold_to) if args.gold_to else None,
    )


def cmd_dataset(args: argparse.Namespace, s: Settings) -> Any:
    directory = Path(args.dir)
    if args.action == "publish":
        return publish_dataset(
            s.db_path,
            directory,
            baseline_path=Path(args.baseline) if args.baseline else s.baseline_path,
        )
    if args.action == "activate":
        if not args.version:
            return {"error": "A dataset version is required for activation"}
        return activate_dataset(directory, args.version)
    return dataset_status(directory)


def cmd_changes(args: argparse.Namespace, s: Settings) -> Any:
    with Database(s.db_path) as db:
        changes = queries.what_changed(
            db, args.since, category=args.category, department=args.department, limit=args.limit
        )
        return {"since": args.since, "count": len(changes), "changes": changes}


def cmd_export(args: argparse.Namespace, s: Settings) -> Any:
    out = Path(args.out) if args.out else s.data_dir / "exports" / f"projets.{args.format}"
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    temporary: Path | None = None
    try:
        with Database(s.db_path, read_only=True) as db:

            def rows() -> Any:
                nonlocal count
                for project in queries.iter_project_summaries(db, category=args.category, department=args.department):
                    count += 1
                    yield project

            chunks = exports.iter_csv(rows()) if args.format == "csv" else exports.iter_geojson(rows())
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8-sig" if args.format == "csv" else "utf-8",
                dir=out.parent,
                prefix=f".{out.name}.",
                delete=False,
            ) as destination:
                temporary = Path(destination.name)
                for chunk in chunks:
                    destination.write(chunk)
            temporary.replace(out)
        return {"path": str(out), "projects": count}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def cmd_web(args: argparse.Namespace, s: Settings) -> Any:
    import uvicorn

    from radar.web.app import build_app

    if args.host not in {"127.0.0.1", "localhost", "::1"} and not s.api_token:
        return {"error": "Refusing to listen on a public interface without RADAR_API_TOKEN."}
    if s.published_dir is not None and args.review:
        return {"error": "Published datasets are read-only; review the candidate database separately"}
    review_mode = None
    if args.review:
        services = make_services(s, make_http(s))

        def rebuild(db: Database, key: str) -> None:
            process_filings(db, services, only={key})
            link_and_build(db)

        review_mode = (rebuild, lambda: load_gold(s.gold_path))
    database: Path | Callable[[], Path] = s.db_path
    if s.published_dir is not None:
        database = PublishedDataset(s.published_dir)
        database()  # fail before starting the server if no valid approved version is active
    app = build_app(
        database,
        token=s.api_token,
        mcp_host=args.public_host or args.host,
        review=review_mode,
        quality=(lambda db: quality_overview(db, s.cases_dir)) if args.review else None,
        document=partial(BlobStore(s.blobs_dir).get, max_bytes=s.pdf_max_bytes) if args.review else None,
        evidence_limits={
            "max_bytes": s.pdf_max_bytes,
            "max_pages": s.pdf_max_pages,
            "max_pixels": min(s.ocr_max_pixels, 8_000_000),
            "max_source_image_pixels": s.ocr_max_pixels,
            "document_timeout_s": min(s.pdf_timeout_s, 15),
        },
    )
    print(f"http://{args.host}:{args.port}  (MCP: /mcp)", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return None


def cmd_digest(args: argparse.Namespace, s: Settings) -> Any:
    with Database(s.db_path) as db:
        d = digest.build_digest(
            db, args.since, department=args.department, category=args.category, scope=args.scope, map_url=args.map_url
        )
    out = Path(args.out) if args.out else s.data_dir / "exports" / f"alerte-{d.since}.{args.format}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(digest.render_html(d) if args.format == "html" else digest.render_text(d), encoding="utf-8")
    return {"path": str(out), "new_projects": len(d.new_projects), "changes": len(d.changes)}


def cmd_review(args: argparse.Namespace, s: Settings) -> Any:
    """Back up / restore human corrections (gold/corrections.jsonl, versioned). Run `radar run` after an import."""
    path = Path(args.path) if args.path else s.corrections_path
    with Database(s.db_path) as db:
        links_path = s.link_decisions_path
        if args.action == "export":
            rows = review.export_corrections(db)
            _write_jsonl(path, rows)
            decisions = review.export_link_decisions(db)
            _write_jsonl(links_path, decisions)
            return {"path": str(path), "corrections": len(rows), "link_decisions": len(decisions)}
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        added = {"corrections": review.import_corrections(db, rows)}
        if links_path.exists():
            lines = links_path.read_text(encoding="utf-8").splitlines()
            added["link_decisions"] = review.import_link_decisions(db, [json.loads(x) for x in lines if x.strip()])
        return {"path": str(path), "added": added, "next": "radar run"}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")


def cmd_health(args: argparse.Namespace, s: Settings) -> Any:
    """Collector health from the run history. --strict: exit 2 on any alert (for the scheduler)."""
    operations = operational_status(s.data_dir)
    if not s.db_path.is_file():
        unavailable: dict[str, Any] = {
            "sources": [],
            "drift": [],
            "alerts": ["dataset_unavailable"],
            "operations": operations,
        }
        if args.strict:
            unavailable["error"] = "dataset database is unavailable"
        return unavailable
    with Database(s.db_path) as db:
        report = collector_health(db.runs("collect:"), now=datetime.now(UTC))
        drifting = drift(db.filings(), _first_fetched(db), datetime.now(UTC))
    alerts = [h.source for h in report if h.status == "alert"]
    alerts += [f"drift:{d.group}:{d.value}:{d.metric}" for d in drifting]
    result: dict[str, Any] = {"sources": report, "drift": drifting, "alerts": alerts, "operations": operations}
    if args.strict and alerts:
        result["error"] = "collector or drift alerts: " + ", ".join(alerts)
    return result


def _first_fetched(db: Database) -> dict[str, datetime]:
    first: dict[str, datetime] = {}
    for d in db.documents():
        if d.filing_key not in first or d.fetched_at < first[d.filing_key]:
            first[d.filing_key] = d.fetched_at
    return first


def cmd_templates(args: argparse.Namespace, s: Settings) -> Any:
    """Document templates seen (per filing strata), known or not; `export` accepts them into gold/templates.json."""
    with Database(s.db_path) as db:
        filings = db.filings()
    counts = Counter(t for f in filings for t in f.strata.get("template", "").split("+") if t)
    registry = Registry.load(s.templates_path)
    if args.action == "export":
        registry = registry or Registry()
        registry.accept(counts, date.today())
        registry.save(s.templates_path)
        return {"path": str(s.templates_path), "templates": len(registry.known), "next": "radar run"}
    return {
        "registry": str(s.templates_path) if registry else None,
        "templates": [
            {"template": t, "filings": n, "known": bool(registry and t in registry)} for t, n in counts.most_common()
        ],
    }


def cmd_triage(args: argparse.Namespace, s: Settings) -> Any:
    """The review queue: filings to look at first, with reasons (see quality/triage.py)."""
    with Database(s.db_path) as db:
        queue = triage(db.filings(), db.corrections(), date.today(), db.machine_changes())
    return {"queue": len(queue), "items": queue[: args.limit]}


def cmd_errors(_: argparse.Namespace, s: Settings) -> Any:
    """Where the machine fails, according to human corrections; and whether detectors saw it coming."""
    with Database(s.db_path) as db:
        return errors.analyse(db.filings(), db.corrections())


def cmd_cases(args: argparse.Namespace, s: Settings) -> Any:
    """export: human-verified filings -> gold/cases (replayable tests). check: replay all, update statuses."""
    if args.action == "check":
        result = cases.refresh(s.cases_dir, rulebook.Rulebook.load(s.rules_path).effective())
        summary: dict[str, Any] = {k: len(v) for k, v in result.items()} | {"regressed": result["regressed"]}
        if args.strict and result["regressed"]:
            summary["error"] = "regression: a case that passed fails again"
        return summary
    services = make_services(s, make_http(s))
    written = []
    with Database(s.db_path) as db:
        filings = db.filings()
        _, verified = reviewed_references(filings, db.corrections())
        for filing in filings:
            if filing.key not in verified or partition_of(filing) != "dev":
                continue
            documents = db.documents(filing.key)
            case = cases.make_case(
                filing, documents, read_documents(documents, services), verified[filing.key].expected
            )
            previous = cases.case_path(case, s.cases_dir)
            if previous.exists():  # never downgrade a fixed field: its failure must surface as a regression
                old = cases.load(previous)
                case.status |= {k: "fixed" for k, v in old.status.items() if v == "fixed" and k in case.status}
            cases.save(case, s.cases_dir)
            written.append(case.number)
    return {"written": written, "dir": str(s.cases_dir)}


def cmd_improve(args: argparse.Namespace, s: Settings) -> Any:
    """Agent-ready tickets from failing regression cases, with validated rule candidates."""
    found = improve.tickets(cases.load_all(s.cases_dir), rulebook.Rulebook.load(s.rules_path).effective())
    if args.field:
        found = [t for t in found if t.field == args.field]
    paths = improve.write(found, s.data_dir / "improve")
    return {
        "tickets": [
            {"field": t.field, "failing": len(t.failures), "candidates": len(t.suggestions), "path": str(p)}
            for t, p in zip(found, paths, strict=True)
        ],
        "next": "give a ticket to a coding agent, or fix by hand; then radar cases check --strict",
    }


def cmd_aliases(_: argparse.Namespace, s: Settings) -> Any:
    """Learn name spellings from corrections into gold/aliases.json (applied at every build). Then: radar run."""
    with Database(s.db_path) as db:
        learned = propagate.learn_aliases(db.filings(), db.corrections())
    merged = propagate.merge(propagate.Aliases.load(s.aliases_path), learned)
    merged.save(s.aliases_path)
    return {"path": str(s.aliases_path), "aliases": {k: len(v) for k, v in merged.table.items()}, "next": "radar run"}


def cmd_shadow(args: argparse.Namespace, s: Settings) -> Any:
    """Dry run of the current code on every stored filing: what would change, judged against verified values."""
    with Database(s.db_path) as db:
        report = shadow.shadow(db, make_services(s, make_http(s), llm="cached"), set(args.only) if args.only else None)
    order = {"breaks": 0, "fixes": 1, "changes-wrong-to-wrong": 2, "unknown": 3, "unchanged-verdict": 4}
    changes = sorted(report.changes, key=lambda c: (order.get(c.verdict, 9), c.field, c.number))
    lines = ["# Shadow run: effect of the current code on every filing", "", f"Summary: `{report.summary}`", ""]
    lines += ["| verdict | case | field | stored | new |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {c.verdict} | {c.number} | {c.field} | `{c.before}` | `{c.after}` |" for c in changes]
    path = s.data_dir / "improve" / "shadow.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")
    result: dict[str, Any] = {**report.summary, "report": str(path), "failed": report.failed}
    breaks = [f"{c.number}:{c.field}" for c in changes if c.verdict == "breaks"]
    if args.strict and (breaks or report.failed):
        result["error"] = "shadow validation failed: " + ", ".join([*breaks, *sorted(report.failed)])
    return result


def cmd_rules(args: argparse.Namespace, s: Settings) -> Any:
    """Adopted rules (gold/rules.json): list with learned reliability, adopt a suggestion, disable."""
    book = rulebook.Rulebook.load(s.rules_path)
    if args.action == "adopt":
        if not args.field:
            return {"error": "--field is required"}
        regex = args.regex
        origin = "manual"
        if not regex:
            adopted = rulebook.Rulebook.load(s.rules_path).effective()
            found = suggest(cases.load_all(s.cases_dir), args.field, rules=adopted)
            if len(found) < args.rank:
                return {"error": f"no validated suggestion #{args.rank} for {args.field}: run radar improve"}
            chosen = found[args.rank - 1]
            regex, origin = chosen.regex, f"suggestion: fixes {', '.join(chosen.fixes)} ({chosen.precision:.0%})"
        rule = book.adopt(args.field, regex, origin=origin)
        book.save(s.rules_path)
        return {"adopted": rule, "next": "radar shadow --strict, then radar run"}
    if args.action == "disable":
        book.disable(args.id or "")
        book.save(s.rules_path)
        return {"disabled": args.id}
    with Database(s.db_path) as db:
        learned = learn(db.filings(), db.corrections())
    rows = []
    for r in book.rules:
        correct, seen = next(
            (v for (f, src), v in learned.stats.items() if src == f"rule:{r.id}" and f == r.field), (0, 0)
        )
        effective = r in book.effective({r.id: (correct, seen)})
        rows.append({**dataclasses.asdict(r), "reviewed": seen, "correct": correct, "in_use": effective})
    return {"path": str(s.rules_path), "rules": rows}


def cmd_screen_terms(args: argparse.Namespace, s: Settings) -> Any:
    """Screen terms (gold/screen_terms.json): suggest from screen misses, list, adopt, disable."""
    book = ScreenTerms.load(s.screen_terms_path)
    if args.action in {"suggest", "adopt"} and not (args.action == "adopt" and args.term):
        with Database(s.db_path) as db:
            filings = db.filings()
            documents = {f.key: db.documents(f.key) for f in filings}
            found = screen_terms.suggest(filings, documents, db.rejected_titles(), book.pattern())
        if args.action == "suggest":
            return {
                "suggestions": [x.as_dict() for x in found[: args.limit]],
                "how": "radar screen-terms adopt --rank N (or --term ...), then radar run; collection uses it too",
            }
        if len(found) < args.rank:
            return {"error": f"no suggestion #{args.rank}"}
        chosen = found[args.rank - 1]
        origin = f"rescues {', '.join(chosen.rescues)} ({chosen.scope}); admits {chosen.admits}/{chosen.negatives}"
        term = book.adopt(chosen.term, origin)
    elif args.action == "adopt":
        term = book.adopt(args.term, "manual")
    elif args.action == "disable":
        book.disable(args.id or "")
        book.save(s.screen_terms_path)
        return {"disabled": args.id}
    else:
        return {"path": str(s.screen_terms_path), "terms": [dataclasses.asdict(t) for t in book.terms]}
    book.save(s.screen_terms_path)
    return {"adopted": dataclasses.asdict(term), "next": "radar run (the full-text screen uses it at every build)"}


def corpus_documents(db: Database, services: Services) -> list[tuple[str, list[robustness.Document]]]:
    """Every stored filing as (number, documents with GDPR-scrubbed text), for corpus-wide checks."""
    items = []
    for key in db.filing_keys_with_documents():
        documents = db.documents(key)
        contents = read_documents(documents, services)
        privacy = PersonScrubber.for_filing([c.fields for c in contents])
        docs = [
            robustness.Document(d.id, d.role, d.title, privacy.scrub(c.text) or "", c.fields)
            for d, c in zip(documents, contents, strict=True)
        ]
        items.append((key.rsplit(":", 1)[-1], docs))
    return items


def cmd_robustness(_: argparse.Namespace, s: Settings) -> Any:
    """Perturb every stored document like real PDFs differ; list values that move (brittle rules)."""
    with Database(s.db_path) as db:
        report = robustness.check_robustness(corpus_documents(db, make_services(s, make_http(s), llm="cached")))
    path = s.data_dir / "improve" / "robustness.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(robustness.render(report), encoding="utf-8", newline="\n")
    return {**report.summary(), "fragile": len(report.fragile), "report": str(path)}


def cmd_corpus(args: argparse.Namespace, s: Settings) -> Any:
    """build: redacted real PDFs of the given filings -> tests/corpus (+ snapshot). snapshot: re-record expected.json
    after checking the diff is intended (the corpus test fails on any difference)."""
    directory = Path(args.dir)
    if args.action == "build":
        written, filings_out, communes_out = [], [], {}
        reference = ReferenceCommunes(s.communes_path)
        blobs = BlobStore(s.blobs_dir)
        with Database(s.db_path) as db:
            for number in args.numbers or []:
                filing = next((f for f in db.filings() if f.number == number), None)
                if filing is None:
                    return {"error": f"unknown filing {number}"}
                documents = db.documents(filing.key)
                if any(d.scanned_pages for d in documents):
                    return {"error": f"{number}: scanned pages cannot be redacted reliably, choose another filing"}
                docs_out = []
                for d in documents:
                    original = blobs.get(d.id)
                    redacted = corpus.redact(original)
                    if leaks := corpus.audit(original, redacted):
                        return {"error": f"{number}: redaction incomplete ({len(leaks)} leak(s)), not written"}
                    name = f"{number}--{d.role.value}.pdf"
                    (directory / "pdfs").mkdir(parents=True, exist_ok=True)
                    (directory / "pdfs" / name).write_bytes(redacted)
                    docs_out.append({"filename": name, "role": d.role.value, "title": d.title, "url": d.url})
                    written.append(name)
                filings_out.append(
                    {"number": number, "category": filing.classification.category.value, "documents": docs_out}
                )
                info = reference.commune(filing.location.commune or "", filing.location.department)
                if info is not None:
                    communes_out[info.insee_code] = dataclasses.asdict(info)
        manifest = {"filings": filings_out, "communes": list(communes_out.values())}
        (directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
        )
        (directory / "README.md").write_text(CORPUS_README, encoding="utf-8", newline="\n")
    with tempfile.TemporaryDirectory() as work:
        snapshot = corpus.replay(directory, Path(work))
    previous = directory / "expected.json"
    changes = corpus.diff(json.loads(previous.read_text(encoding="utf-8")), snapshot) if previous.exists() else []
    previous.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return {
        "dir": str(directory),
        "filings": len(snapshot["filings"]),
        "projects": snapshot["projects"],
        "changed": changes,
    }


CORPUS_README = """# Corpus de test (PDF réels caviardés)

Documents publics de la DREAL Hauts-de-France (examens au cas par cas), réutilisés sous Licence Ouverte (Etalab).
Caviardés avant d'entrer dans le dépôt : champs de personnes vidés, noms de personnes, civilités, courriels,
téléphones et blocs de signature effacés de la couche texte, toutes les images retirées ; documents scannés exclus.

- `manifest.json` : dossiers, documents, catégorie de référence, communes utiles aux contrôles.
- `expected.json` : résultat attendu de toute la chaîne, vérifié par `tests/test_corpus.py`.

Mettre à jour après un changement voulu : `uv run radar corpus snapshot`, puis relire le diff de `expected.json`.
"""


def cmd_mcp(_: argparse.Namespace, s: Settings) -> Any:
    from radar.mcp_server import build_server

    selected: Path | Callable[[], Path] = s.db_path
    if s.published_dir is not None:
        selected = PublishedDataset(s.published_dir)
        selected()
    build_server(selected).run(transport="stdio")
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radar", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="load data already on disk (no network)")
    isub = imp.add_subparsers(dest="what", required=True)
    files = isub.add_parser("files", help="PDFs + index.json from a previous run")
    files.add_argument("index")
    files.add_argument("docs")
    files.add_argument("--region", default="hauts-de-france")
    files.add_argument("--transcripts", help="directory of '<pdf file>.txt' verbatim transcriptions")
    sig = isub.add_parser("signals")
    sig.add_argument("kind", choices=["registry", "press", "energy-registry"])
    sig.add_argument("path")
    for name in ("gold", "cache", "communes"):
        isub.add_parser(name).add_argument("path")

    col = sub.add_parser("collect", help="polite network collection (one request per second per host or slower)")
    col.add_argument("source", choices=["dreal", "mrae", "georisques", "registry", "press", "prefectures"])
    col.add_argument("--region", default="hauts-de-france")
    col.add_argument("--listing", action="append", help="DREAL listing page (repeatable); default: two most recent")
    col.add_argument("--max-offset", type=int, default=400, help="DREAL: last pagination offset (8 articles per page)")
    col.add_argument("--max-items", type=int, default=None)
    col.add_argument("--max-pages", type=int, default=30, help="prefectures: page budget per site")
    col.add_argument("--year", type=int, default=date.today().year)
    col.add_argument("--since", default=f"{date.today().year - 2}-01-01", help="registry: creation date lower bound")

    upd = sub.add_parser("update", help="scheduled job: collect recent items (polite), then run")
    upd.add_argument(
        "--sources", nargs="*", default=list(UPDATE_SOURCES), choices=[*UPDATE_SOURCES, "georisques", "prefectures"]
    )
    upd.add_argument("--region", default="hauts-de-france")
    upd.add_argument("--listing", action="append", help=argparse.SUPPRESS)
    upd.add_argument("--max-offset", type=int, default=16, help="DREAL: newest pages only (8 articles per page)")
    upd.add_argument("--max-items", type=int, default=None)
    upd.add_argument("--max-pages", type=int, default=10)
    upd.add_argument("--year", type=int, default=date.today().year)
    upd.add_argument("--since", default=(date.today() - timedelta(days=90)).isoformat())
    upd.add_argument("--only", nargs="*", help=argparse.SUPPRESS)

    proc = sub.add_parser("process", help="(re)build filings from stored documents")
    proc.add_argument("--only", nargs="*", help="filing keys")
    proc.add_argument("--full", action="store_true", help="rebuild every filing (ignore fingerprints)")
    run = sub.add_parser("run", help="process + link + build projects")
    run.add_argument("--only", nargs="*", help="filing keys")
    run.add_argument("--full", action="store_true", help="rebuild every filing (ignore fingerprints)")
    for rebuild in (proc, run):
        rebuild.add_argument("--llm-batch", action="store_true", help="model calls via Message Batches (half price)")
        rebuild.add_argument("--wait", action="store_true", help="with --llm-batch: wait for answers and rebuild")
    lb = sub.add_parser(
        "llm-batch", help="inspect durable jobs, collect/reparse saved answers or reconcile uncertain sends"
    )
    lb.add_argument(
        "action", nargs="?", choices=["status", "collect", "reparse", "resolve", "settle-cost"], default="status"
    )
    lb.add_argument("--submission", help="resolve: uncertain local submission id")
    lb.add_argument("--provider-batch", help="resolve: confirmed provider batch id")
    lb.add_argument("--request-key", help="settle-cost: request whose charge was confirmed externally")
    lb.add_argument("--actual-usd", type=float, help="settle-cost: externally confirmed total USD charge")
    lb.add_argument(
        "--confirm-not-accepted",
        action="store_true",
        help="resolve only after confirming that the provider did not accept the submission",
    )
    chg = sub.add_parser("changes", help="change log since a date (new / changed / removed projects)")
    chg.add_argument("--since", required=True, help="ISO date, e.g. 2026-10-01")
    exp = sub.add_parser("export", help="write projects to CSV (Excel, ';') or GeoJSON (GIS)")
    exp.add_argument("format", choices=["csv", "geojson"])
    exp.add_argument("--out", help="default: <data>/exports/projets.<format>")
    dig = sub.add_parser("digest", help="alert e-mail (HTML or text): new filings + changes since a date")
    dig.add_argument("--since", required=True, help="ISO date, e.g. 2026-10-05")
    dig.add_argument("--format", choices=["html", "text"], default="html")
    dig.add_argument("--scope", default="Hauts-de-France", help="label shown in the e-mail")
    dig.add_argument("--map-url", help="public map URL for links (none: no map links)")
    dig.add_argument("--out", help="default: <data>/exports/alerte-<since>.<format>")
    for p in (chg, exp, dig):
        p.add_argument("--category", help="bess | bess_hybrid | storage_substation | datacenter")
        p.add_argument("--department", help='code, e.g. "60"')
    chg.add_argument("--limit", type=int, default=200)
    ev = sub.add_parser("eval", help="quality report compared with an explicitly approved reference")
    ev.add_argument("--strict", action="store_true", help="exit 2 if independent evaluation requirements fail")
    ev.add_argument("--classifier", action="store_true", help="also re-run the model classifier alone (paid)")
    ev.add_argument("--draft-fields", action="store_true", help="add unverified field references from extractions")
    ev.add_argument("--baseline", help="approved reference path; default: <data>/evaluation/approved.json")
    ev.add_argument(
        "--approve-baseline",
        action="store_true",
        help="explicitly approve this evaluated dataset if all requirements pass",
    )
    ev.add_argument(
        "--new-campaign",
        action="store_true",
        help="with approval: archive the previous reference before a changed evaluation campaign",
    )
    ev.add_argument(
        "--min-samples",
        type=int,
        default=30,
        help="minimum independently verified observations per required field (default: 30)",
    )
    ev.add_argument(
        "--required-fields", default=",".join(REQUIRED_FIELDS), help="comma-separated fields required for approval"
    )
    ev.add_argument(
        "--min-accuracy",
        type=float,
        default=0.95,
        help="minimum observed field accuracy, not a statistical guarantee (default: .95)",
    )
    ev.add_argument(
        "--min-screening-recall",
        type=float,
        default=0.95,
        help="minimum observed independent screening recall (default: .95)",
    )
    sub.add_parser("errors", help="error clusters from human corrections, detector precision / recall")
    cs = sub.add_parser("cases", help="regression cases from human-verified filings (gold/cases)")
    cs.add_argument("action", choices=["export", "check"])
    cs.add_argument("--strict", action="store_true", help="check: exit 2 on any regression")
    imp = sub.add_parser("improve", help="agent-ready tickets per failing field, with validated rule candidates")
    imp.add_argument("--field", help="only this field")
    sub.add_parser("robustness", help="perturb every stored document; list extracted values that move")
    co = sub.add_parser("corpus", help="redacted real-PDF test corpus (tests/corpus): build, snapshot")
    co.add_argument("action", choices=["build", "snapshot"])
    co.add_argument("--numbers", nargs="*", help="build: filing numbers to include (no scanned pages)")
    co.add_argument("--dir", default="tests/corpus")
    ru = sub.add_parser("rules", help="adopted extraction rules (gold/rules.json): list, adopt, disable")
    ru.add_argument("action", nargs="?", choices=["list", "adopt", "disable"], default="list")
    ru.add_argument("--field", help="adopt: field (developer, siret, commune, power_mw, energy_mwh, ...)")
    ru.add_argument("--regex", help="adopt: explicit regex on fold_keep text (group 1 = value)")
    ru.add_argument("--rank", type=int, default=1, help="adopt: n-th validated suggestion (radar improve)")
    ru.add_argument("--id", help="disable: rule id")
    st = sub.add_parser("screen-terms", help="layer-1 screen terms learned from misses: suggest, list, adopt")
    st.add_argument("action", nargs="?", choices=["suggest", "list", "adopt", "disable"], default="suggest")
    st.add_argument("--term", help="adopt: explicit term (words, accents and case ignored)")
    st.add_argument("--rank", type=int, default=1, help="adopt: n-th suggestion")
    st.add_argument("--id", help="disable: term id")
    st.add_argument("--limit", type=int, default=20)
    tp = sub.add_parser("templates", help="document templates seen; export = accept them (gold/templates.json)")
    tp.add_argument("action", nargs="?", choices=["list", "export"], default="list")
    sh = sub.add_parser("shadow", help="dry run on the whole corpus: changed values, fixes / breaks vs verified")
    sh.add_argument("--only", nargs="*", help="filing keys")
    sh.add_argument("--strict", action="store_true", help="exit 2 if a verified value would break")
    al = sub.add_parser("aliases", help="learn verified name spellings from corrections (gold/aliases.json)")
    al.add_argument("action", choices=["export"])
    tri = sub.add_parser("triage", help="review queue: what a human should check first, and why")
    tri.add_argument("--limit", type=int, default=20)
    hl = sub.add_parser("health", help="collector health (blocked / empty / stale / crashed sources)")
    hl.add_argument("--strict", action="store_true", help="exit 2 on any alert")
    rev = sub.add_parser("review", help="export/import human corrections (gold/corrections.jsonl)")
    rev.add_argument("action", choices=["export", "import"])
    rev.add_argument("--path", help="default: gold/corrections.jsonl")
    web = sub.add_parser("web", help="map UI + JSON API + MCP over HTTP (read-only)")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--public-host", help="host name clients use (behind a proxy); default: --host")
    web.add_argument("--review", action="store_true", help="internal review mode: corrections and validations")
    backup = sub.add_parser("backup", help="consistent backup of database, durable jobs, files and human references")
    backup.add_argument("--out", required=True, help="private backup root, outside the data directory")
    backup.add_argument("--keep", type=int, help="retain this many recognized snapshots; preserve unrelated files")
    restore = sub.add_parser("restore", help="verify and restore a backup into an empty destination")
    restore.add_argument("snapshot")
    restore.add_argument("--to", required=True, help="empty destination directory")
    restore.add_argument("--gold-to", help="optional empty external directory for human references")
    dataset = sub.add_parser("dataset", help="publish, activate or inspect an approved local dataset snapshot")
    dataset.add_argument("action", choices=["publish", "activate", "status"])
    dataset.add_argument("version", nargs="?", help="activate: existing immutable version")
    dataset.add_argument("--dir", required=True, help="private dataset publication directory")
    dataset.add_argument("--baseline", help="publish: approved evaluation reference path")
    for name, text in (
        ("link", "propose links, rebuild projects"),
        ("stats", "dataset counts, freshness, caveats"),
        ("mcp", "serve the MCP server over stdio"),
    ):
        sub.add_parser(name, help=text)
    return parser


COMMANDS: dict[str, Callable[[argparse.Namespace, Settings], Any]] = {
    "import": cmd_import,
    "collect": cmd_collect,
    "update": cmd_update,
    "process": cmd_process,
    "link": cmd_link,
    "run": cmd_run,
    "eval": cmd_eval,
    "stats": cmd_stats,
    "changes": cmd_changes,
    "export": cmd_export,
    "digest": cmd_digest,
    "mcp": cmd_mcp,
    "web": cmd_web,
    "review": cmd_review,
    "health": cmd_health,
    "triage": cmd_triage,
    "errors": cmd_errors,
    "cases": cmd_cases,
    "improve": cmd_improve,
    "aliases": cmd_aliases,
    "shadow": cmd_shadow,
    "templates": cmd_templates,
    "rules": cmd_rules,
    "screen-terms": cmd_screen_terms,
    "llm-batch": cmd_llm_batch,
    "corpus": cmd_corpus,
    "robustness": cmd_robustness,
    "backup": cmd_backup,
    "restore": cmd_restore,
    "dataset": cmd_dataset,
}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
        mutating = args.command not in {
            "web",
            "mcp",
            "stats",
            "changes",
            "digest",
            "export",
            "health",
            "triage",
            "errors",
        }
        if mutating:
            with process_lock(settings.data_dir):
                result = COMMANDS[args.command](args, settings)
        else:
            result = COMMANDS[args.command](args, settings)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        result = {"error": str(exc), "type": type(exc).__name__}
    if result is not None:
        _emit(result)
    return 2 if isinstance(result, dict) and "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
