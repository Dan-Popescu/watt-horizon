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
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from radar.api import digest, exports, queries, review
from radar.api.quality import quality_overview
from radar.classification.gold import load_gold
from radar.config import Settings, load_settings
from radar.domain.privacy import PersonScrubber
from radar.evaluation.classification import evaluate_classifier
from radar.evaluation.gold import draft as draft_field_gold
from radar.evaluation.gold import load_field_gold, write_field_gold
from radar.evaluation.ocr import evaluate_ocr
from radar.evaluation.privacy import audit_privacy
from radar.evaluation.report import ReportInputs, build_report
from radar.evaluation.report import compare as compare_reports
from radar.evaluation.report import metrics as eval_metrics
from radar.evaluation.stats import split_of
from radar.extraction import EXTRACTION_VERSION
from radar.net.http import PoliteHttp, SiteBlockedError
from radar.pipeline import importers
from radar.pipeline.corrections import reviewed_references
from radar.pipeline.filings import Services, read_documents
from radar.pipeline.health import collector_health
from radar.pipeline.run import link_and_build, process_filings
from radar.quality import cases, errors, improve, propagate, robustness, rulebook, shadow
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
from radar.validation.lookups import JsonCache, ReferenceCommunes
from radar.wiring import make_classifier, make_http, make_services


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
            report = dreal.collect_dreal(http, db, blobs, region, listings, args.max_offset, args.max_items)
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
    result = {"collect": collected, **cmd_run(args, s)}
    with Database(s.db_path) as db:
        health = collector_health(db.runs("collect:"), now=datetime.now(UTC))
        drifting = drift(db.filings(), _first_fetched(db), datetime.now(UTC))
    alerts = [h.source for h in health if h.status == "alert"] + [f"drift:{d.value}:{d.metric}" for d in drifting]
    return {**result, "health_alerts": alerts}


def cmd_process(args: argparse.Namespace, s: Settings) -> Any:
    with Database(s.db_path) as db:
        run_id = db.start_run("process")
        report = process_filings(db, make_services(s, make_http(s)), only=set(args.only) if args.only else None)
        db.finish_run(run_id, asdict(report))
        return report


def cmd_link(_: argparse.Namespace, s: Settings) -> Any:
    with Database(s.db_path) as db:
        run_id = db.start_run("link")
        result = link_and_build(db)
        db.finish_run(run_id, result)
        return result


def cmd_run(args: argparse.Namespace, s: Settings) -> Any:
    return {"process": cmd_process(args, s), "link": cmd_link(args, s)}


def cmd_eval(args: argparse.Namespace, s: Settings) -> Any:
    """Quality report, stored and compared with the previous one. --strict: exit 2 on any regression."""
    blobs = BlobStore(s.blobs_dir)
    with Database(s.db_path) as db:
        filings = db.filings()
        file_labels, file_fields = load_gold(s.gold_path), load_field_gold(s.field_gold_path)
        if args.draft_fields:  # the file keeps hand-written references + unverified drafts; reviews stay in the db
            drafts = draft_field_gold([f for f in filings if f.number in file_labels], file_fields, EXTRACTION_VERSION)
            write_field_gold(s.field_gold_path, [*file_fields.values(), *drafts])
            file_fields = load_field_gold(s.field_gold_path)
        reviewed_labels, reviewed_fields = reviewed_references(filings, db.corrections())
        labels = {**file_labels, **reviewed_labels}  # human review wins
        field_gold = {**file_fields, **reviewed_fields}
        ocr = None
        if TesseractOcr.available():
            ocr = evaluate_ocr(TesseractOcr(), blobs, s.transcripts_dir)
        classifier = None
        if args.classifier:
            services = make_services(s, make_http(s))
            llm = make_classifier(s, KeyValueCache(s.cache_path), with_gold=False)
            if llm is None:
                return {"error": "No model classifier: set RADAR_LLM_MODEL (and ANTHROPIC_API_KEY), not offline."}
            classifier = evaluate_classifier(llm, db, services, labels)
        report = build_report(
            ReportInputs(
                filings=filings,
                labels=labels,
                field_gold=field_gold,
                corrections=db.corrections(),
                privacy_leaks=audit_privacy(db, blobs),
                listing=db.listing_counts(),
                cases=cases.refresh(s.cases_dir, rulebook.Rulebook.load(s.rules_path).effective())
                if s.cases_dir.exists()
                else None,
                ocr=ocr,
                classifier=classifier,
            )
        )
        report["robustness"] = robustness.check_robustness(corpus_documents(db, make_services(s, None))).summary()
        previous = db.last_runs().get("eval", {}).get("stats", {}).get("metrics", {})
        current = eval_metrics(report)
        comparison = compare_reports(previous, current) if previous else {"regressions": [], "improvements": []}
        run_id = db.start_run("eval")
        db.finish_run(run_id, {"metrics": current, "report": report})
    result = {**report, "comparison": comparison}
    if args.strict and comparison["regressions"]:
        return {**result, "error": "regressions against the previous evaluation"}
    return result


def cmd_stats(_: argparse.Namespace, s: Settings) -> Any:
    with Database(s.db_path) as db:
        return queries.dataset_info(db)


def cmd_changes(args: argparse.Namespace, s: Settings) -> Any:
    with Database(s.db_path) as db:
        changes = queries.what_changed(
            db, args.since, category=args.category, department=args.department, limit=args.limit
        )
        return {"since": args.since, "count": len(changes), "changes": changes}


def cmd_export(args: argparse.Namespace, s: Settings) -> Any:
    with Database(s.db_path) as db:
        projects = queries.list_projects(db, category=args.category, department=args.department)
    out = Path(args.out) if args.out else s.data_dir / "exports" / f"projets.{args.format}"
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "csv":
        out.write_text(exports.to_csv(projects), encoding="utf-8-sig")
    else:
        out.write_text(json.dumps(exports.to_geojson(projects), ensure_ascii=False, indent=1), encoding="utf-8")
    return {"path": str(out), "projects": len(projects)}


def cmd_web(args: argparse.Namespace, s: Settings) -> Any:
    import uvicorn

    from radar.web.app import build_app

    if args.host not in {"127.0.0.1", "localhost", "::1"} and not s.api_token:
        return {"error": "Refusing to listen on a public interface without RADAR_API_TOKEN."}
    review_mode = None
    if args.review:
        services = make_services(s, make_http(s))

        def rebuild(db: Database, key: str) -> None:
            process_filings(db, services, only={key})
            link_and_build(db)

        review_mode = (rebuild, lambda: load_gold(s.gold_path))
    app = build_app(
        s.db_path,
        token=s.api_token,
        mcp_host=args.public_host or args.host,
        review=review_mode,
        quality=(lambda db: quality_overview(db, s.cases_dir)) if args.review else None,
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
        if args.action == "export":
            rows = review.export_corrections(db)
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = [json.dumps(r, ensure_ascii=False) for r in rows]
            path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")
            return {"path": str(path), "corrections": len(rows)}
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return {"path": str(path), "added": review.import_corrections(db, rows), "next": "radar run"}


def cmd_health(args: argparse.Namespace, s: Settings) -> Any:
    """Collector health from the run history. --strict: exit 2 on any alert (for the scheduler)."""
    with Database(s.db_path) as db:
        report = collector_health(db.runs("collect:"), now=datetime.now(UTC))
        drifting = drift(db.filings(), _first_fetched(db), datetime.now(UTC))
    alerts = [h.source for h in report if h.status == "alert"]
    alerts += [f"drift:{d.group}:{d.value}:{d.metric}" for d in drifting]
    result: dict[str, Any] = {"sources": report, "drift": drifting, "alerts": alerts}
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
            if filing.number not in verified or split_of(filing.number) == "test":  # test split: measure only
                continue
            documents = db.documents(filing.key)
            case = cases.make_case(
                filing, documents, read_documents(documents, services), verified[filing.number].expected
            )
            previous = s.cases_dir / f"{case.number}.json"
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
        report = shadow.shadow(db, make_services(s, make_http(s)), set(args.only) if args.only else None)
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
    if args.strict and breaks:
        result["error"] = "the current code breaks verified values: " + ", ".join(breaks)
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
        report = robustness.check_robustness(corpus_documents(db, make_services(s, make_http(s))))
    path = s.data_dir / "improve" / "robustness.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(robustness.render(report), encoding="utf-8", newline="\n")
    return {**report.summary(), "fragile": len(report.fragile), "report": str(path)}


def cmd_mcp(_: argparse.Namespace, s: Settings) -> Any:
    from radar.mcp_server import build_server

    build_server(s.db_path).run(transport="stdio")
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
    run = sub.add_parser("run", help="process + link + build projects")
    run.add_argument("--only", nargs="*", help="filing keys")
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
    ev = sub.add_parser("eval", help="quality report vs gold sets, stored and compared with the previous run")
    ev.add_argument("--strict", action="store_true", help="exit 2 if any metric regressed")
    ev.add_argument("--classifier", action="store_true", help="also re-run the model classifier alone (paid)")
    ev.add_argument("--draft-fields", action="store_true", help="add unverified field references from extractions")
    sub.add_parser("errors", help="error clusters from human corrections, detector precision / recall")
    cs = sub.add_parser("cases", help="regression cases from human-verified filings (gold/cases)")
    cs.add_argument("action", choices=["export", "check"])
    cs.add_argument("--strict", action="store_true", help="check: exit 2 on any regression")
    imp = sub.add_parser("improve", help="agent-ready tickets per failing field, with validated rule candidates")
    imp.add_argument("--field", help="only this field")
    sub.add_parser("robustness", help="perturb every stored document; list extracted values that move")
    ru = sub.add_parser("rules", help="adopted extraction rules (gold/rules.json): list, adopt, disable")
    ru.add_argument("action", nargs="?", choices=["list", "adopt", "disable"], default="list")
    ru.add_argument("--field", help="adopt: field (developer, siret, commune, power_mw, energy_mwh, ...)")
    ru.add_argument("--regex", help="adopt: explicit regex on fold_keep text (group 1 = value)")
    ru.add_argument("--rank", type=int, default=1, help="adopt: n-th validated suggestion (radar improve)")
    ru.add_argument("--id", help="disable: rule id")
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
    "robustness": cmd_robustness,
}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    result = COMMANDS[args.command](args, load_settings())
    if result is not None:
        _emit(result)
    return 2 if isinstance(result, dict) and "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
