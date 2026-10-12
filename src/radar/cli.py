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
import json
import sys
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from radar.api import digest, exports, queries
from radar.classification.gold import load_gold
from radar.config import Settings, load_settings
from radar.evaluation.classification import evaluate_classifier
from radar.evaluation.gold import draft as draft_field_gold
from radar.evaluation.gold import load_field_gold, write_field_gold
from radar.evaluation.ocr import evaluate_ocr
from radar.evaluation.privacy import audit_privacy
from radar.evaluation.report import build_report
from radar.evaluation.report import compare as compare_reports
from radar.evaluation.report import metrics as eval_metrics
from radar.extraction import EXTRACTION_VERSION
from radar.net.http import PoliteHttp, SiteBlockedError
from radar.pipeline import importers
from radar.pipeline.corrections import reviewed_references
from radar.pipeline.run import link_and_build, process_filings
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
    return {"collect": collected, **cmd_run(args, s)}


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
        report = build_report(filings, labels, field_gold, audit_privacy(db, blobs), ocr, classifier)
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
    review = None
    if args.review:
        services = make_services(s, make_http(s))

        def rebuild(db: Database, key: str) -> None:
            process_filings(db, services, only={key})
            link_and_build(db)

        review = (rebuild, lambda: load_gold(s.gold_path))
    app = build_app(s.db_path, token=s.api_token, mcp_host=args.public_host or args.host, review=review)
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
    sig.add_argument("kind", choices=["registry", "press"])
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
