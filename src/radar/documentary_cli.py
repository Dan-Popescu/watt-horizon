"""Bounded documentary operations, with local plans before external requests."""

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Any

from radar.config import Settings
from radar.net.http import PoliteHttp
from radar.quality.propagate import Aliases
from radar.sources import catalog, leads, search
from radar.sources.dossier import DossierLimits
from radar.sources.regions import get_region
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.store.discovery import DiscoveryStore
from radar.wiring import make_http


def _online(settings: Settings) -> PoliteHttp:
    http = make_http(settings)
    if http is None:
        raise ValueError("This command requires RADAR_OFFLINE=0 and approved network destinations")
    return http


def catalog_command(args: argparse.Namespace, settings: Settings) -> Any:
    path = Path(args.catalog) if args.catalog else settings.catalog_path
    if args.action == "status":
        return catalog.status(path)
    if args.action == "import":
        return catalog.import_regions(path, Path(args.path))
    with _online(settings) as http:
        if args.action == "discover":
            return catalog.discover(
                http, path, args.region, args.source, years=tuple(args.years), max_pages=args.max_pages
            )
        return catalog.adopt(http, path, args.id)


def discovery_command(args: argparse.Namespace, settings: Settings) -> Any:
    with Database(settings.db_path) as db:
        if args.action == "status":
            return DiscoveryStore(db.conn).status(args.region, limit=args.limit)
        region = get_region(args.region, settings.catalog_path)
        if args.action == "plan":
            plan = search.plan_queries(
                db.filings(),
                region=region.slug,
                domains=args.domain or search.DEFAULT_OFFICIAL_DOMAINS,
                manual_terms=args.term or (),
                corporate_aliases=Aliases.load(settings.aliases_path).table.get("developer", {}),
                provider=args.provider,
                max_queries=args.max_queries,
                campaign=args.campaign,
                lead_contexts=DiscoveryStore(db.conn).press_leads(region.slug, limit=20),
            )
            if args.enqueue:
                return search.enqueue_plan(db, plan)
            return {
                "queries": [asdict(query) for query in plan.queries],
                "truncated": plan.truncated,
                "seed_filings": plan.seed_filings,
                "campaign": plan.campaign,
                "network_requests": 0,
                "next": "Add --enqueue to save the plan, then radar discover search",
            }
        with _online(settings) as http:
            if args.action == "search":
                if not DiscoveryStore(db.conn).status(region.slug)["queries"]:
                    raise ValueError(
                        "No query plan. First run: radar discover plan --term 'stockage batteries' --enqueue"
                    )
                return search.run_search(
                    http,
                    db,
                    region=region.slug,
                    retry_failed=args.retry_failed,
                    limits=search.SearchLimits(
                        queries=args.max_queries,
                        pages=args.max_pages,
                        results=args.max_results,
                        requests=args.max_requests,
                        total_requests_per_query=args.total_requests,
                        interval_s=args.interval,
                    ),
                )
            return leads.resolve_leads(
                http,
                db,
                BlobStore(settings.blobs_dir),
                region=region,
                official_domains=args.domain or search.DEFAULT_OFFICIAL_DOMAINS,
                source=None,
                retry_failed=args.retry_failed,
                max_leads=args.max_leads,
                max_requests=args.max_requests,
                limits=DossierLimits(
                    pages=args.dossier_pages, documents=args.dossier_documents, depth=args.dossier_depth
                ),
            )


def quality_command(args: argparse.Namespace, settings: Settings) -> Any:
    from radar.quality.documentary_metrics import build_quality_loop

    with Database(settings.db_path, read_only=True) as db:
        return build_quality_loop(db, jobs_path=settings.llm_jobs_path, region=args.region)


def add_parsers(sub: Any) -> None:
    catalog_parser = sub.add_parser(
        "catalog",
        help="qualify observed sources and annual pages before adoption",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  radar catalog status\n  RADAR_OFFLINE=0 radar catalog discover --source mrae --years 2025",
    )
    cat = catalog_parser.add_subparsers(dest="action", required=True)
    for action in ("status", "import", "discover", "adopt"):
        examples = {
            "status": "radar catalog status",
            "import": "radar catalog import --path regional-seeds.json",
            "discover": "RADAR_OFFLINE=0 radar catalog discover --source mrae --years 2024 2025 --max-pages 4",
            "adopt": "RADAR_OFFLINE=0 radar catalog adopt --id <observed-candidate-id>",
        }
        command = cat.add_parser(
            action,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=f"Examples:\n  {examples[action]}",
        )
        command.add_argument("--catalog", help="default: RADAR_SOURCE_CATALOG or data/sources/catalog.json")
        if action == "import":
            command.add_argument("--path", required=True, help="validated regional seed configuration JSON")
        elif action == "discover":
            command.add_argument("--region", default="hauts-de-france")
            command.add_argument("--source", choices=["dreal", "mrae"], required=True)
            command.add_argument("--years", type=int, nargs="+", required=True)
            command.add_argument("--max-pages", type=int, default=8)
        elif action == "adopt":
            command.add_argument("--id", required=True, help="observed candidate ID returned by catalog discover")
    discovery = sub.add_parser(
        "discover",
        help="plan searches, queue leads and corroborate official documents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  radar discover plan --term 'stockage batteries' --enqueue\n  radar discover status",
    )
    actions = discovery.add_subparsers(dest="action", required=True)
    for action in ("plan", "search", "resolve", "status"):
        command = actions.add_parser(
            action,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="Examples:\n  radar discover plan --term 'stockage batteries' --enqueue\n"
            "  RADAR_OFFLINE=0 radar discover search --max-requests 10\n"
            "  RADAR_OFFLINE=0 radar discover resolve --max-leads 5\n  radar discover status",
        )
        command.add_argument("--region", default="hauts-de-france")
        if action == "plan":
            command.add_argument("--term", action="append", help="explicit known search term; repeatable")
            command.add_argument("--domain", action="append", help="approved official result domain; repeatable")
            command.add_argument("--provider", default=search.DEFAULT_SEARCH_ENDPOINT)
            command.add_argument("--max-queries", type=int, default=100)
            command.add_argument("--campaign", help="query campaign ID; default: UTC date for bounded daily revisits")
            command.add_argument("--enqueue", action="store_true", help="save the previewed plan; no network")
        elif action == "search":
            command.add_argument("--max-queries", type=int, default=3)
            command.add_argument("--max-pages", type=int, default=3)
            command.add_argument("--max-results", type=int, default=30)
            command.add_argument(
                "--max-requests", type=int, default=10, help="physical HTTP requests including redirects/retries"
            )
            command.add_argument("--total-requests", type=int, default=20, help="cumulative budget per query")
            command.add_argument("--interval", type=float, default=2)
            command.add_argument("--retry-failed", action="store_true")
        elif action == "resolve":
            command.add_argument("--max-leads", type=int, default=5)
            command.add_argument("--max-requests", type=int, default=30)
            command.add_argument("--domain", action="append")
            command.add_argument("--dossier-pages", type=int, default=8)
            command.add_argument("--dossier-documents", type=int, default=24)
            command.add_argument("--dossier-depth", type=int, default=2)
            command.add_argument(
                "--retry-failed", action="store_true", help="resume partial or failed leads; never bypass a block"
            )
        else:
            command.add_argument("--limit", type=int, default=20)
    quality = sub.add_parser(
        "quality-loop",
        help="source audits, reading coverage, feedback and recorded costs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  radar quality-loop --region hauts-de-france",
    )
    quality.add_argument("--region", help="omit to report all configured dataset sources")
