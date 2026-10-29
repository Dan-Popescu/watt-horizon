#!/usr/bin/env python3
"""Compare bounded local readers on a corpus without network, models or human-quality claims.

Run with ``uv run python tools/compare_reading.py``. Each variant gets a fresh temporary
SQLite cache; its warm pass recreates the reader, so the measurement is of persistent
cache reuse rather than the reader's in-memory memo. No application data is modified.
"""

import argparse
import hashlib
import json
import os
import platform
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

import pymupdf

from radar.domain.enums import DocumentRole
from radar.domain.quantities import measurements
from radar.pipeline.filings import extract_facts
from radar.quality.corpus import CorpusFiling, load_manifest
from radar.reading.ocr import make_ocr, tesseract_version
from radar.reading.pdf import DEFAULT_MAX_BYTES, PdfContent, PdfReader
from radar.store.cache import KeyValueCache

VARIANTS = (("baseline", False, False), ("tables", True, False), ("secondary_ocr", False, True))
MAX_DOCUMENTS = 100
MAX_CORPUS_BYTES = 128 * 1024 * 1024


@contextmanager
def native_diagnostics_to_stderr() -> Iterator[None]:
    """Keep spawned native workers' informational messages out of the JSON stream."""
    sys.stdout.flush()
    saved = os.dup(sys.stdout.fileno())
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, sys.stdout.fileno())
        os.close(saved)


def fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def associations(content: PdfContent) -> set[tuple[str, str, float, float]]:
    """Deduplicate literal number/unit associations: tables can repeat page text.

    These are machine readings, not verified facts. Associating a number with its
    physical object, phase or scenario is outside this diagnostic.
    """
    text = content.text + " " + " ".join(content.fields.values())
    return {(m.dimension, m.basis, m.low, m.high) for m in measurements(text)}


def load_documents(directory: Path, filings: list[CorpusFiling]) -> dict[str, bytes]:
    names = sorted({doc.filename for filing in filings for doc in filing.documents})
    if not names or len(names) > MAX_DOCUMENTS:
        raise ValueError(f"Corpus must contain 1 to {MAX_DOCUMENTS} unique documents")
    root = (directory / "pdfs").resolve(strict=True)
    documents = {}
    total = 0
    for name in names:
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("Corpus document escapes its PDF directory")
        with path.open("rb") as stream:
            data = stream.read(min(DEFAULT_MAX_BYTES, MAX_CORPUS_BYTES - total) + 1)
        if len(data) > DEFAULT_MAX_BYTES or total + len(data) > MAX_CORPUS_BYTES:
            raise ValueError("Corpus exceeds byte budget")
        documents[name] = data
        total += len(data)
    return documents


def measure_pass(
    filings: list[CorpusFiling],
    documents: dict[str, bytes],
    cache: KeyValueCache,
    *,
    tables: bool,
    secondary: bool,
    use_ocr: bool,
) -> dict[str, Any]:
    reader = PdfReader(cache, read_tables=tables)
    engine = make_ocr(None, use_tesseract=use_ocr, cache=cache, secondary_pass=secondary)
    hits, misses = cache.hits, cache.misses
    counters: Counter[str] = Counter()
    readings: dict[str, dict[str, Any]] = {}
    literals: dict[str, set[tuple[str, str, float, float]]] = {}
    values: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    durations = []
    started = perf_counter()
    for filing in filings:
        contents = []
        descriptors = []
        for document in filing.documents:
            data = documents[document.filename]
            document_id = hashlib.sha256(data).hexdigest()
            tick = perf_counter()
            try:
                content = engine.complete(document_id, data, reader.read(document_id, data))
            except Exception as exc:
                failures[document.filename] = f"{type(exc).__name__}: {exc}"
                break
            contents.append(content)
            descriptors.append((document_id, DocumentRole(document.role), document.title))
            if document.filename not in readings:
                duration = perf_counter() - tick
                durations.append(duration)
                summary = content.coverage_summary()
                counters.update(summary)
                literals[document.filename] = associations(content)
                readings[document.filename] = {
                    "elapsed_s": round(duration, 6),
                    "coverage": summary,
                    "text_sha256": fingerprint(content.text),
                    "fields_sha256": fingerprint(content.fields),
                    "tables_sha256": fingerprint([asdict(table) for table in content.tables]),
                    "unique_literal_associations": len(literals[document.filename]),
                }
        else:
            try:
                facts = asdict(extract_facts(descriptors, contents).facts)
                values[filing.number] = {key: str(value) for key, value in facts.items() if key != "evidence"}
            except Exception as exc:
                failures[f"filing:{filing.number}"] = f"{type(exc).__name__}: {exc}"
    return {
        "elapsed_s": round(perf_counter() - started, 6),
        "cache_hits": cache.hits - hits,
        "cache_misses": cache.misses - misses,
        "documents": len(readings),
        "filings_extracted": len(values),
        "coverage": dict(counters),
        "document_elapsed_median_s": round(median(durations), 6) if durations else None,
        "document_elapsed_max_s": round(max(durations), 6) if durations else None,
        "readings": readings,
        "failures": failures,
        "engine": engine.label,
        "_literals": literals,
        "_facts": values,
    }


def compare(reference: dict[str, Any], treatment: dict[str, Any]) -> dict[str, object]:
    comparable = set(reference["readings"]) & set(treatment["readings"])
    return {
        "comparable_documents": len(comparable),
        "incomparable_documents": sorted(set(reference["readings"]) ^ set(treatment["readings"])),
        "text_changed": sorted(
            name
            for name in comparable
            if reference["readings"][name]["text_sha256"] != treatment["readings"][name]["text_sha256"]
        ),
        "fields_changed": sorted(
            name
            for name in comparable
            if reference["readings"][name]["fields_sha256"] != treatment["readings"][name]["fields_sha256"]
        ),
        "literal_associations_added": sum(
            len(treatment["_literals"][name] - reference["_literals"][name]) for name in comparable
        ),
        "literal_associations_removed": sum(
            len(reference["_literals"][name] - treatment["_literals"][name]) for name in comparable
        ),
        "fact_fields_changed": {
            number: changed
            for number in sorted(reference["_facts"].keys() & treatment["_facts"].keys())
            if (
                changed := sorted(
                    field
                    for field in reference["_facts"][number].keys() | treatment["_facts"][number].keys()
                    if reference["_facts"][number].get(field) != treatment["_facts"][number].get(field)
                )
            )
        },
        "incomparable_filings": sorted(reference["_facts"].keys() ^ treatment["_facts"].keys()),
    }


def run(directory: Path, *, use_ocr: bool = True) -> dict[str, Any]:
    if (directory / "manifest.json").stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Corpus manifest exceeds byte budget")
    filings, _ = load_manifest(directory)
    if not filings or len(filings) > MAX_DOCUMENTS:
        raise ValueError(f"Corpus must contain 1 to {MAX_DOCUMENTS} filings")
    documents = load_documents(directory, filings)
    variants: dict[str, dict[str, Any]] = {}
    for name, tables, secondary in VARIANTS:
        with tempfile.TemporaryDirectory(prefix="watt-reading-") as temporary:
            cache = KeyValueCache(Path(temporary) / "derived.sqlite")
            try:
                cold = measure_pass(filings, documents, cache, tables=tables, secondary=secondary, use_ocr=use_ocr)
                warm = measure_pass(filings, documents, cache, tables=tables, secondary=secondary, use_ocr=use_ocr)
            finally:
                cache.close()
        variants[name] = {"cold": cold, "warm": warm, "warm_vs_cold": compare(cold, warm)}
    comparisons = {name: compare(variants["baseline"]["cold"], runs["cold"]) for name, runs in variants.items()}
    for runs in variants.values():
        for pass_name in ("cold", "warm"):
            runs[pass_name].pop("_literals")
            runs[pass_name].pop("_facts")
    return {
        "schema": 1,
        "offline": True,
        "human_quality_measured": False,
        "ocr_enabled": use_ocr,
        "readers_complete": all(
            not result["failures"]
            and result["documents"] == len(documents)
            and result["filings_extracted"] == len(filings)
            and not any(result["coverage"].get(key, 0) for key in ("unread_pages", "ocr_errors", "table_errors"))
            for passes in variants.values()
            for name, result in passes.items()
            if name in {"cold", "warm"}
        ),
        "scope": "reader and rule extraction only; no classification, validation or project grouping",
        "environment": {
            "python": platform.python_version(),
            "pymupdf": pymupdf.VersionBind,
            "tesseract": tesseract_version(),
        },
        "corpus": {
            "filings": len(filings),
            "documents": len(documents),
            "bytes": sum(map(len, documents.values())),
            "sha256": fingerprint({name: hashlib.sha256(data).hexdigest() for name, data in documents.items()}),
        },
        "variants": variants,
        "vs_baseline": comparisons,
        "limitations": [
            "Single sequential cold and persistent-cache pass, not a production throughput estimate.",
            "Literal associations and rule-output stability are diagnostics, not accuracy or recall.",
            "Scanned documents were excluded from the bundled redacted corpus; OCR diversity is not representative.",
            "Table limits and OCR errors stay visible in coverage counters; no human reference is created.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path(__file__).resolve().parents[1] / "tests" / "corpus")
    parser.add_argument("--no-ocr", action="store_true", help="Disable both local OCR passes explicitly.")
    args = parser.parse_args()
    try:
        with native_diagnostics_to_stderr():
            report = run(args.corpus, use_ocr=not args.no_ocr)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["readers_complete"] else 2


if __name__ == "__main__":
    sys.exit(main())
