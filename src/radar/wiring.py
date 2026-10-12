"""Composition root: the only place that builds concrete adapters from `Settings`."""

from radar.classification.base import CachedClassifier, ChainClassifier, Classifier
from radar.classification.gold import GoldClassifier, load_gold
from radar.config import Settings
from radar.net.http import PoliteHttp
from radar.pipeline.filings import Services
from radar.reading.ocr import make_ocr
from radar.store.blobs import BlobStore
from radar.store.cache import KeyValueCache
from radar.validation.lookups import ApiLookups, CompositeLookups, JsonCache, ReferenceCommunes


def make_http(settings: Settings) -> PoliteHttp | None:
    return None if settings.offline else PoliteHttp(settings.user_agent)


def make_classifier(
    settings: Settings, cache: KeyValueCache | None = None, *, with_gold: bool = True
) -> Classifier | None:
    classifiers: list[Classifier] = []
    if settings.llm_model and not settings.offline:  # offline means no network at all, LLM included
        from radar.classification.llm import LlmClassifier  # optional dependency

        llm = LlmClassifier(settings.llm_model)
        classifiers.append(CachedClassifier(llm, cache) if cache else llm)
    gold = load_gold(settings.gold_path) if with_gold else {}
    if gold:
        classifiers.append(GoldClassifier(gold))  # fallback, and the reference for `radar eval`
    return ChainClassifier(*classifiers) if classifiers else None


def make_lookups(settings: Settings, http: PoliteHttp | None) -> CompositeLookups:
    api = ApiLookups(http, JsonCache(settings.lookup_cache_path), offline=settings.offline)
    return CompositeLookups(ReferenceCommunes(settings.communes_path), api)


def make_services(settings: Settings, http: PoliteHttp | None) -> Services:
    cache = KeyValueCache(settings.cache_path)
    return Services(
        blobs=BlobStore(settings.blobs_dir),
        ocr=make_ocr(settings.transcripts_dir, use_tesseract=settings.use_tesseract, cache=cache),
        classifier=make_classifier(settings, cache),
        lookups=make_lookups(settings, http),
    )
