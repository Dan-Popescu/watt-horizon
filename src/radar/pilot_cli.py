"""Noninteractive, offline pilot planning and private annotation orchestration."""

import argparse
from pathlib import Path
from typing import Any

from radar.config import Settings
from radar.evaluation.partitions import load_manifest
from radar.quality import pilot
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.wiring import make_services


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "pilot",
        help="plan a private quality pilot, freeze its campaign and inspect gaps",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  radar pilot plan --spec data/pilot-spec.json\n  radar pilot status data/pilots/pilot-2026",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "prepare"):
        command = actions.add_parser(
            action,
            help="inspect scope and budgets"
            if action == "plan"
            else "freeze complete groups and blank double-review packets",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="Examples:\n  radar pilot "
            + action
            + " --spec data/pilot-spec.json"
            + (" --dir data/pilots/pilot-2026" if action == "prepare" else ""),
        )
        command.add_argument(
            "--spec", required=True, help="private JSON scope, budgets and independent inventory provenance"
        )
        if action == "prepare":
            command.add_argument(
                "--dir", required=True, help="new private directory; existing campaigns are never overwritten"
            )
    command = actions.add_parser(
        "status",
        help="show coverage, references, grouping errors and declared human effort",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  radar pilot status data/pilots/pilot-2026\n  radar pilot status data/pilots/pilot-2026 --annotations data/reader-a.jsonl data/reader-b.jsonl",
    )
    command.add_argument("directory", help="prepared private pilot directory")
    command.add_argument(
        "--annotations", nargs="+", help="explicit blind annotations; defaults to the two blank packet paths"
    )
    command.add_argument("--effort", help="private JSONL effort sessions; defaults to pilot effort.jsonl")


def cmd_pilot(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    blobs = BlobStore(settings.blobs_dir)
    with Database(settings.db_path) as db:
        if args.action in {"plan", "prepare"}:
            spec = pilot.load_spec(Path(args.spec))
            previous = load_manifest(settings.partitions_path)
            if args.action == "plan":
                return pilot.plan(db, spec, previous=previous, blobs=blobs)
            return pilot.prepare(db, Path(args.dir), spec, blobs, previous=previous)
        services = make_services(settings, None, llm="cached")
        return pilot.status(
            db,
            Path(args.directory),
            blobs,
            annotations=[Path(path) for path in args.annotations] if args.annotations is not None else None,
            effort_path=Path(args.effort) if args.effort else None,
            read=lambda key, data: services.ocr.complete(key, data, services.reader.read(key, data)),
        )
