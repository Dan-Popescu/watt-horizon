"""Composition root: the only place that builds concrete adapters from `Settings`."""

from dataclasses import dataclass
from typing import Any, Literal

from radar.classification.base import CachedClassifier, ChainClassifier, Classifier
from radar.classification.gold import GoldClassifier, load_gold
from radar.classification.terms import ScreenTerms
from radar.config import Settings
from radar.extraction.llm import FieldExtractor
from radar.net.http import PoliteHttp
from radar.pipeline.filings import Services
from radar.pipeline.fingerprint import digest, files_fingerprint
from radar.pipeline.llm_batch import DeferredClassifier, DeferredExtractor, DeferredVerifier, LlmQueue
from radar.quality.propagate import Aliases
from radar.quality.rulebook import Rulebook
from radar.quality.templates import Registry
from radar.reading.ocr import make_ocr
from radar.reading.pdf import PdfReader
from radar.store.blobs import BlobStore
from radar.store.cache import KeyValueCache
from radar.validation.lookups import ApiLookups, CompositeLookups, JsonCache, ReferenceCommunes


def make_http(settings: Settings) -> PoliteHttp | None:
    return None if settings.offline else PoliteHttp(settings.user_agent)


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
    if not settings.llm_model or settings.offline:  # offline means no network at all, LLM included
        return LlmSetup()
    from radar.classification.llm import LlmClassifier, TieredClassifier  # optional dependency
    from radar.extraction.llm import CachedExtractor, LlmExtractor
    from radar.quality.model_check import CachedVerifier, LlmVerifier

    strong = LlmClassifier(settings.llm_model)
    fast = LlmClassifier(settings.llm_fast_model) if settings.llm_fast_model else None
    extractor = LlmExtractor(settings.llm_model, passages=settings.llm_passages)
    verifier = LlmVerifier(settings.llm_fast_model or settings.llm_model) if settings.llm_check else None
    tasks = tuple(t for t in (strong, fast, extractor, verifier) if t is not None)
    queue = LlmQueue(cache) if mode == "batch" else None
    if mode == "live":
        wrap_c: Any = lambda t: CachedClassifier(t, cache)  # noqa: E731
        wrap_e: Any = lambda t: CachedExtractor(t, cache)  # noqa: E731
        wrap_v: Any = lambda t: CachedVerifier(t, cache)  # noqa: E731
    else:  # batch queues misses; cached answers None on a miss (no queue, no call)
        wrap_c = lambda t: DeferredClassifier(t, cache, queue)  # noqa: E731
        wrap_e = lambda t: DeferredExtractor(t, cache, queue)  # noqa: E731
        wrap_v = lambda t: DeferredVerifier(t, cache, queue)  # noqa: E731
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
        classifiers.append(GoldClassifier(gold))  # fallback, and the reference for `radar eval`
    return ChainClassifier(*classifiers) if classifiers else None


def batch_client(settings: Settings) -> Any:
    """`anthropic.Anthropic().messages.batches`, or None without a model or offline."""
    if not settings.llm_model or settings.offline:
        return None
    import anthropic  # optional dependency

    return anthropic.Anthropic().messages.batches


def make_lookups(settings: Settings, http: PoliteHttp | None) -> CompositeLookups:
    api = ApiLookups(http, JsonCache(settings.lookup_cache_path), offline=settings.offline)
    return CompositeLookups(ReferenceCommunes(settings.communes_path), api)


def make_services(settings: Settings, http: PoliteHttp | None, *, llm: LlmMode = "live") -> Services:
    cache = KeyValueCache(settings.cache_path)
    ocr = make_ocr(settings.transcripts_dir, use_tesseract=settings.use_tesseract, cache=cache)
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
        reader=PdfReader(cache),
        verifier=models.verifier,
        llm_queue=models.queue,
        llm_tasks=models.tasks,
        lookups=make_lookups(settings, http),
    )
