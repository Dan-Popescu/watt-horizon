"""Composition root: the only place that builds concrete adapters from `Settings`."""

import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from radar.classification.base import ChainClassifier, Classifier
from radar.classification.gold import GoldClassifier, load_gold
from radar.classification.terms import ScreenTerms
from radar.config import Settings
from radar.domain.references import canonical_reference_map
from radar.evaluation.stats import partition_of
from radar.extraction.llm import FieldExtractor
from radar.net.http import PoliteHttp
from radar.pipeline.filings import Services
from radar.pipeline.fingerprint import digest, files_fingerprint
from radar.pipeline.llm_batch import (
    DeferredClassifier,
    DeferredExtractor,
    DeferredVerifier,
    LlmQueue,
    TrackedClassifier,
    TrackedExtractor,
    TrackedVerifier,
)
from radar.quality.propagate import Aliases
from radar.quality.rulebook import Rulebook
from radar.quality.templates import Registry
from radar.reading.ocr import make_ocr
from radar.reading.pdf import PdfReader
from radar.reading.policy import ReadingPolicy
from radar.store.blobs import BlobStore
from radar.store.cache import KeyValueCache
from radar.store.db import Database
from radar.store.llm_jobs import BudgetLimits, TokenPricing
from radar.validation.lookups import ApiLookups, CompositeLookups, JsonCache, ReferenceCommunes


def make_http(settings: Settings) -> PoliteHttp | None:
    return (
        None
        if settings.offline
        else PoliteHttp(
            settings.user_agent,
            max_response_bytes=settings.http_max_bytes,
            max_response_seconds=settings.http_deadline_s,
        )
    )


LlmMode = Literal["live", "batch", "cached"]


@dataclass
class LlmSetup:
    """Model-backed services. Mode: live = direct calls (cached); batch = cache misses are queued for a Message
    Batch (half price, scheduled runs); cached = cache only, never a paid call (dry runs, checks, robustness)."""

    classifier: Classifier | None = None
    extractor: FieldExtractor | None = None
    verifier: Any = None
    queue: LlmQueue | None = None
    tasks: tuple[Any, ...] = ()


def make_llm(settings: Settings, cache: KeyValueCache, mode: LlmMode = "live") -> LlmSetup:
    if not settings.llm_model or (settings.offline and mode != "cached"):
        return LlmSetup()
    from radar.classification.llm import LlmClassifier, TieredClassifier  # optional dependency
    from radar.extraction.llm import LlmExtractor
    from radar.quality.model_check import LlmVerifier

    strong = LlmClassifier(settings.llm_model)
    fast = LlmClassifier(settings.llm_fast_model) if settings.llm_fast_model else None
    extractor = LlmExtractor(settings.llm_model, passages=settings.llm_passages)
    verifier = LlmVerifier(settings.llm_fast_model or settings.llm_model) if settings.llm_check else None
    tasks = tuple(t for t in (strong, fast, extractor, verifier) if t is not None)
    pricing = {}
    if settings.llm_pricing_path is not None:
        raw = json.loads(settings.llm_pricing_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("LLM pricing must map model names to versioned token rates")
        for model, values in raw.items():
            if not isinstance(values, dict):
                raise ValueError(f"Invalid pricing entry for {model}")
            pricing[model] = TokenPricing(**values)
    queue = LlmQueue(
        cache,
        jobs_path=settings.llm_jobs_path,
        campaign=settings.llm_campaign,
        pricing=pricing,
        limits=BudgetLimits(
            daily_usd=settings.llm_daily_budget_usd,
            campaign_usd=settings.llm_campaign_budget_usd,
            filing_usd=settings.llm_filing_budget_usd,
            max_attempts=settings.llm_max_attempts,
        ),
    )
    if mode == "live":
        wrap_c: Any = lambda t: TrackedClassifier(t, cache, queue)  # noqa: E731
        wrap_e: Any = lambda t: TrackedExtractor(t, cache, queue)  # noqa: E731
        wrap_v: Any = lambda t: TrackedVerifier(t, cache, queue)  # noqa: E731
    else:  # batch queues misses; cached answers None on a miss (no queue, no call)
        wrap_c = lambda t: DeferredClassifier(t, cache, queue, cache_only=mode == "cached")  # noqa: E731
        wrap_e = lambda t: DeferredExtractor(t, cache, queue, cache_only=mode == "cached")  # noqa: E731
        wrap_v = lambda t: DeferredVerifier(t, cache, queue, cache_only=mode == "cached")  # noqa: E731
    classifier: Classifier = wrap_c(strong)
    if fast is not None:
        classifier = TieredClassifier(wrap_c(fast), classifier)
    return LlmSetup(classifier, wrap_e(extractor), wrap_v(verifier) if verifier else None, queue, tasks)


def make_classifier(
    settings: Settings, cache: KeyValueCache | None = None, *, with_gold: bool = True, llm: LlmSetup | None = None
) -> Classifier | None:
    classifiers: list[Classifier] = []
    if llm is None and cache is not None:
        llm = make_llm(settings, cache)
    if llm is not None and llm.classifier is not None:
        classifiers.append(llm.classifier)
    gold = load_gold(settings.gold_path) if with_gold else {}
    if gold:
        known_keys: list[str] = []
        if settings.db_path.is_file():
            with Database(settings.db_path, read_only=True) as db:
                known_keys = db.filing_keys_with_documents()
                filings = db.filings()
            dev_keys = {filing.key for filing in filings if partition_of(filing) == "dev"}
            gold = {key: label for key, label in canonical_reference_map(filings, gold).items() if key in dev_keys}
        else:
            gold = {}
        classifiers.append(GoldClassifier(gold, known_keys=known_keys))
    return ChainClassifier(*classifiers) if classifiers else None


def batch_client(settings: Settings) -> Any:
    """`anthropic.Anthropic().messages.batches`, or None without a model or offline."""
    if not settings.llm_model or settings.offline:
        return None
    import anthropic  # optional dependency

    return anthropic.Anthropic(max_retries=0).messages.batches


def make_lookups(settings: Settings, http: PoliteHttp | None) -> CompositeLookups:
    api = ApiLookups(http, JsonCache(settings.lookup_cache_path), offline=settings.offline)
    return CompositeLookups(ReferenceCommunes(settings.communes_path), api)


def make_services(settings: Settings, http: PoliteHttp | None, *, llm: LlmMode = "live") -> Services:
    cache = KeyValueCache(settings.cache_path)
    policy = (
        ReadingPolicy(
            mode=cast(Literal["adaptive", "full"], settings.reading_mode),
            max_deep_pages=settings.reading_max_deep_pages,
            max_ocr_pages=settings.reading_max_ocr_pages,
        )
        if settings.reading_mode != "economical"
        else None
    )
    reader = PdfReader(
        cache,
        max_workers=settings.pdf_workers,
        max_bytes=settings.pdf_max_bytes,
        max_pages=settings.pdf_max_pages,
        max_batch_bytes=settings.pdf_batch_bytes,
        max_memo_bytes=settings.pdf_batch_bytes,
        document_timeout_s=settings.pdf_timeout_s,
        read_tables=settings.pdf_read_tables,
        max_table_pages=settings.pdf_table_max_pages,
        max_table_rows=settings.pdf_table_max_rows,
        max_table_chars=settings.pdf_table_max_chars,
        policy=policy,
    )
    ocr = make_ocr(
        settings.transcripts_dir,
        use_tesseract=settings.use_tesseract,
        cache=cache,
        max_workers=settings.pdf_workers,
        max_pages=settings.ocr_max_pages,
        max_bytes=settings.pdf_max_bytes,
        max_pixels=settings.ocr_max_pixels,
        document_timeout_s=settings.ocr_timeout_s,
        secondary_pass=settings.ocr_secondary_pass,
        secondary_max_pages=settings.ocr_secondary_max_pages,
    )
    models = make_llm(settings, cache, llm)
    classifier = make_classifier(settings, cache, llm=models)
    extractor = models.extractor
    steering = (settings.gold_path, settings.rules_path, settings.aliases_path, settings.templates_path)
    setup = digest(
        files_fingerprint([*steering, settings.screen_terms_path, settings.communes_path]),
        ocr.label,
        classifier.name if classifier else None,
        extractor.name if extractor else None,
        models.verifier.name if models.verifier else None,
        settings.offline,
        settings.pdf_max_bytes,
        settings.pdf_max_pages,
        settings.ocr_max_pages,
        settings.ocr_max_pixels,
        settings.pdf_timeout_s,
        settings.ocr_timeout_s,
        settings.pdf_read_tables,
        settings.pdf_table_max_pages,
        settings.pdf_table_max_rows,
        settings.pdf_table_max_chars,
        settings.ocr_secondary_pass,
        settings.ocr_secondary_max_pages,
        reader.version,
    )
    return Services(
        blobs=BlobStore(settings.blobs_dir),
        ocr=ocr,
        classifier=classifier,
        extractor=extractor,
        aliases=Aliases.load(settings.aliases_path),
        templates=Registry.load(settings.templates_path),
        rules=Rulebook.load(settings.rules_path).effective(),
        labels={n: g.category for n, g in load_gold(settings.gold_path).items() if g.verified},
        screen_terms=ScreenTerms.load(settings.screen_terms_path).pattern(),
        fingerprint=setup,
        reader=reader,
        verifier=models.verifier,
        llm_queue=models.queue,
        llm_tasks=models.tasks,
        partitions_path=settings.partitions_path,
        partitions_test_after=settings.evaluation_test_after,
        lookups=make_lookups(settings, http),
    )
