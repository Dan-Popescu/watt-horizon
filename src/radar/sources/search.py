"""Bounded web discovery: search snippets are leads, never extracted project facts."""

import hashlib
import json
import re
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urldefrag, urlencode, urljoin, urlparse, urlunparse

from radar.domain.models import Filing
from radar.domain.privacy import MASKED_PERSON, PersonScrubber
from radar.domain.text import fold, squash
from radar.net.http import PoliteHttp, RequestBudgetError, SiteBlockedError
from radar.store.db import Database
from radar.store.discovery import DiscoveryStore

DEFAULT_SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
SEARCH_POLICY_VERSION = "2026-10-29-v1"
SEARCH_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_OFFICIAL_DOMAINS = ("developpement-durable.gouv.fr", "gouv.fr")
_NEXT_FIELDS = frozenset({"q", "s", "dc", "v", "vqd", "nextParams", "o", "api"})


def clean_url(raw: str) -> str | None:
    try:
        parsed = urlparse(urldefrag(raw)[0])
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 80, 443}
        )
    except ValueError:
        return None
    if not valid:
        return None
    # Functional download/query parameters are retained; only known advertising tracking is dropped.
    query = [
        (key, value)
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        if not key.lower().startswith("utm_")
        for value in values
    ]
    return urlunparse(
        parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(), query=urlencode(query))
    )


def allowed_url(url: str, domains: Sequence[str]) -> bool:
    canonical = clean_url(url)
    if canonical is None:
        return False
    parsed = urlparse(canonical)
    host = parsed.hostname or ""
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def validate_domains(domains: Sequence[str]) -> tuple[str, ...]:
    result = tuple(sorted({domain.strip().lower() for domain in domains}))
    if not result or any(
        len(domain) > 253
        or "." not in domain
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in domain.split("."))
        for domain in result
    ):
        raise ValueError("Discovery requires explicit DNS domain names without URLs or wildcards")
    return result


def _term(raw: str) -> str:
    value = squash(raw).replace('"', " ")
    if not value or len(value) > 250 or PersonScrubber().scrub(value) != value or MASKED_PERSON in value:
        raise ValueError("Search terms must be bounded corporate or project context without person/contact data")
    return value


@dataclass(frozen=True)
class SearchQuery:
    key: str
    region: str
    query: str
    provider: str
    domains: tuple[str, ...]
    seed_keys: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class QueryPlan:
    queries: tuple[SearchQuery, ...]
    seed_filings: int
    truncated: bool
    campaign: str = ""


def plan_queries(
    filings: Iterable[Filing],
    *,
    region: str,
    domains: Sequence[str] = DEFAULT_OFFICIAL_DOMAINS,
    manual_terms: Sequence[str] = (),
    corporate_aliases: Mapping[str, str] | None = None,
    provider: str = DEFAULT_SEARCH_ENDPOINT,
    max_queries: int = 100,
    campaign: str | None = None,
    lead_contexts: Iterable[Mapping[str, Any]] = (),
) -> QueryPlan:
    """Generate only known corporate/project context. No model-invented names or inferred locations."""
    if max_queries <= 0 or max_queries > 5000 or clean_url(provider) is None or urlparse(provider).scheme != "https":
        raise ValueError("Use a positive bounded query count and an HTTPS search endpoint")
    official = validate_domains(domains)
    campaign_id = campaign or datetime.now(UTC).date().isoformat()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", campaign_id):
        raise ValueError("Search campaign must be a bounded identifier")
    records = [filing for filing in filings if filing.region == region]
    contexts: list[tuple[str, str, tuple[str, ...]]] = [(_term(term), "manual", ()) for term in manual_terms]
    for index, lead in enumerate(lead_contexts):
        if index >= max_queries * 2:
            break
        if lead.get("region") != region or lead.get("origin") != "press":
            continue
        try:
            contexts.append((_term(str(lead["title"])), "press-lead", ("lead:" + str(lead["id"]),)))
        except ValueError:
            continue
    for filing in records:
        identity = (filing.key,)
        names = {value for value in (filing.developer, filing.legal_name) if value and value != MASKED_PERSON}
        for variant, canonical in (corporate_aliases or {}).items():
            if any(fold(canonical) == fold(name) for name in names):
                names.add(variant)
        for name in sorted(names):
            try:
                contexts.append((f'"{_term(name)}" (stockage OR batteries OR datacenter)', "company", identity))
            except ValueError:
                continue
        if filing.siret and re.fullmatch(r"\d{14}", filing.siret):
            contexts.append((f'"{filing.siret}"', "siret", identity))
        if filing.location.commune:
            contexts.append((f'"{_term(filing.location.commune)}" (stockage OR datacenter)', "commune", identity))
        if filing.substation:
            contexts.append((f'"poste" "{_term(filing.substation)}" stockage', "substation", identity))
        if filing.number and len(filing.number) <= 80:
            contexts.append((f'"{_term(filing.number)}" dossier', "filing", identity))
    # Round-robin context kinds prevents a long company list consuming the entire plan.
    buckets: dict[str, deque[tuple[str, str, tuple[str, ...]]]] = defaultdict(deque)
    for entry in contexts:
        buckets[entry[1]].append(entry)
    ordered = []
    while any(buckets.values()):
        for bucket in sorted(buckets):
            if buckets[bucket]:
                ordered.append(buckets[bucket].popleft())
    result: dict[str, SearchQuery] = {}
    truncated = False
    for context, reason, seeds in ordered:
        domain_query = " OR ".join(f"site:{domain}" for domain in official)
        query = f"{context} ({domain_query})"
        key = hashlib.sha256(
            json.dumps([SEARCH_POLICY_VERSION, campaign_id, region, query, provider, official]).encode()
        ).hexdigest()
        if key in result:
            previous = result[key]
            result[key] = SearchQuery(
                key, region, query, provider, official, tuple(sorted(set(previous.seed_keys) | set(seeds))), reason
            )
        elif len(result) < max_queries:
            result[key] = SearchQuery(key, region, query, provider, official, seeds, reason)
        else:
            truncated = True
    return QueryPlan(tuple(result.values()), len(records), truncated, campaign_id)


def enqueue_plan(db: Database, plan: QueryPlan) -> dict[str, Any]:
    store = DiscoveryStore(db.conn)
    with db.transaction():
        added = sum(store.enqueue(**vars(query)) for query in plan.queries)
    return {
        "planned": len(plan.queries),
        "added": added,
        "seed_filings": plan.seed_filings,
        "truncated": plan.truncated,
        "campaign": plan.campaign,
    }


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str = ""
    observed_url: str = ""


@dataclass
class SearchPage:
    results: list[SearchResult] = field(default_factory=list)
    next_cursor: dict[str, str] | None = None
    recognized: bool = False
    challenge: bool = False
    unsupported_pagination: bool = False
    truncated: bool = False


class _SearchParser(HTMLParser):
    def __init__(self, endpoint: str, query: str) -> None:
        super().__init__(convert_charrefs=True)
        self.endpoint, self.query = endpoint, query
        self.page = SearchPage()
        self.current_result: SearchResult | None = None
        self.anchor: tuple[str, str] | None = None
        self.text: list[str] = []
        self.text_chars = 0
        self.snippet_depth = 0
        self.form: dict[str, str] | None = None
        self.form_action = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        classes = (data.get("class") or "").split()
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            self.anchor = (data.get("href") or "", "result")
            self.text = []
            self.text_chars = 0
        elif tag == "a" and data.get("rel") == "next":
            self.anchor = (data.get("href") or "", "next")
            self.text = []
            self.text_chars = 0
        if "result__snippet" in classes:
            self.snippet_depth = 1
            self.text = []
            self.text_chars = 0
        elif self.snippet_depth and tag not in {"br", "img", "input", "hr", "meta", "link"}:
            self.snippet_depth += 1
        if any(name.startswith("no-results") or name == "no_results" for name in classes):
            self.page.recognized = True
        if "challenge-form" in classes or data.get("id") in {"challenge-form", "anomaly-form"}:
            self.page.challenge = True
        if tag == "form":
            self.form, self.form_action = {}, urljoin(self.endpoint, data.get("action") or "")
        elif tag == "input" and self.form is not None:
            name, value = data.get("name"), data.get("value") or ""
            if name in _NEXT_FIELDS and len(value) <= 2000:
                self.form[name] = value

    def handle_data(self, data: str) -> None:
        if (self.anchor is not None or self.snippet_depth) and self.text_chars < 4000:
            self.text.append(data[: 4000 - self.text_chars])
            self.text_chars += len(self.text[-1])

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.anchor is not None:
            raw, kind = self.anchor
            absolute = urljoin(self.endpoint, raw)
            wrapped = parse_qs(urlparse(absolute).query).get("uddg", [])
            target = clean_url(wrapped[0] if wrapped else absolute)
            if kind == "result" and target:
                if len(self.page.results) < 1000:
                    self.current_result = SearchResult(target, squash(" ".join(self.text)), observed_url=absolute)
                    self.page.results.append(self.current_result)
                else:
                    self.page.truncated = True
                    self.current_result = None
                self.page.recognized = True
            elif kind == "next" and target and urlparse(target).netloc == urlparse(self.endpoint).netloc:
                cursor = {
                    key: values[0] for key, values in parse_qs(urlparse(target).query).items() if key in _NEXT_FIELDS
                }
                if cursor.get("q", self.query) == self.query:
                    self.page.next_cursor = cursor
            self.anchor = None
        if self.snippet_depth:
            self.snippet_depth -= 1
            if self.snippet_depth == 0 and self.current_result:
                self.current_result.snippet = squash(" ".join(self.text))
        if tag == "form" and self.form is not None:
            if self.form.get("s", "").isdecimal() and int(self.form["s"]) > 0:
                self.page.unsupported_pagination = True
                if (
                    self.form.get("q", self.query) == self.query
                    and urlparse(self.form_action).netloc == urlparse(self.endpoint).netloc
                ):
                    self.page.next_cursor = self.form
                    self.page.unsupported_pagination = False
            self.form = None


def parse_search(page: str, endpoint: str, query: str) -> SearchPage:
    parser = _SearchParser(endpoint, query)
    parser.feed(page)
    if re.search(r"anomaly\.js|unusual traffic|verify (?:that )?you are human|captcha", page, re.I):
        parser.page.challenge = True
    return parser.page


@dataclass(frozen=True)
class SearchLimits:
    queries: int = 3
    pages: int = 3
    results: int = 30
    requests: int = 10
    total_requests_per_query: int = 20
    interval_s: float = 2

    def __post_init__(self) -> None:
        if (
            any(
                value <= 0
                for value in (self.queries, self.pages, self.results, self.requests, self.total_requests_per_query)
            )
            or not 0 <= self.interval_s <= 60
        ):
            raise ValueError("Search budgets must be positive and pacing between zero and sixty seconds")


DEFAULT_SEARCH_LIMITS = SearchLimits()


def _run_search(
    http: PoliteHttp,
    db: Database,
    *,
    region: str,
    limits: SearchLimits = DEFAULT_SEARCH_LIMITS,
    retry_failed: bool = False,
) -> dict[str, Any]:
    """One serialized, bounded pass. Persist response ingestion and cursor advancement together."""
    store = DiscoveryStore(db.conn)
    requests = results = new_leads = 0
    errors: list[str] = []
    stopped = None
    scrubber = PersonScrubber()
    with db.transaction():
        store.recover_interrupted(region)
    for query in store.queries(region, limit=limits.queries, retry_failed=retry_failed):
        while requests < limits.pages and results < limits.results:
            if query["requests"] >= limits.total_requests_per_query:
                with db.transaction():
                    store.query_state(query["key"], "budget", error="cumulative_provider_request_budget")
                break
            parameters = {"q": query["query"], **query["cursor"]}
            request_url = query["provider"] + ("&" if "?" in query["provider"] else "?") + urlencode(parameters)
            with db.transaction():
                store.query_state(query["key"], "running")
                attempt = store.start_attempt("query", query["key"])
            started = monotonic()
            requests += 1

            def record_request(attempt_id: int = attempt, query_key: str = query["key"]) -> None:
                with db.transaction():
                    store.record_request(attempt_id, query_key)

            request_count = 0
            try:
                with http.request_budget(
                    limits.total_requests_per_query - query["requests"], on_request=record_request
                ) as page_budget:
                    previous_max_bytes = http.max_response_bytes
                    http.max_response_bytes = min(previous_max_bytes, SEARCH_MAX_BYTES)
                    try:
                        raw = http.get_bytes(request_url, min_interval_s=limits.interval_s, same_origin_redirects=True)
                    finally:
                        http.max_response_bytes = previous_max_bytes
                        request_count = page_budget.requests
                parsed = parse_search(raw.decode("utf-8", errors="replace"), query["provider"], query["query"])
                if parsed.challenge:
                    http.blocked_hosts.add(urlparse(query["provider"]).netloc)
                    raise SiteBlockedError(urlparse(query["provider"]).netloc, "search verification challenge")
                if not parsed.recognized:
                    raise ValueError("Unrecognized search response; no completeness inferred")
                available = limits.results - results
                accepted = [result for result in parsed.results if allowed_url(result.url, query["domains"])]
                overflow = len(accepted) > available
                with db.transaction():
                    for rank, result in enumerate(accepted[:available], 1):
                        store.add_lead(
                            region=region,
                            url=result.url,
                            title=scrubber.scrub(result.title) or "",
                            snippet=scrubber.scrub(result.snippet) or "",
                            origin="search",
                            source_url=request_url,
                            query_key=query["key"],
                            rank=rank,
                            observed_url=result.observed_url,
                        )
                        new_leads += 1
                    results += min(len(accepted), available)
                    store.page_done(query["key"], len(parsed.results))
                    reason: str | None
                    if overflow:
                        # Retry the same page with a larger result budget; lead keys deduplicate prior writes.
                        state, cursor, reason = "partial", query["cursor"], "result_budget"
                    elif parsed.unsupported_pagination or parsed.truncated:
                        state, cursor, reason = "partial", {}, "unsupported_pagination"
                    elif parsed.next_cursor:
                        state, cursor, reason = (
                            ("pending", parsed.next_cursor, None)
                            if parsed.next_cursor != query["cursor"]
                            else ("partial", query["cursor"], "nonadvancing_pagination")
                        )
                    else:
                        state, cursor, reason = "complete", {}, None
                    store.query_state(query["key"], state, cursor=cursor, error=reason)
                    store.finish_attempt(
                        attempt,
                        state,
                        requests=request_count,
                        results=min(len(accepted), available),
                        duration_s=monotonic() - started,
                    )
                current = store.query(query["key"])
                if current is None or state != "pending":
                    break
                query = current
            except RequestBudgetError as exc:
                with db.transaction():
                    store.query_state(query["key"], "partial", error=str(exc))
                    store.finish_attempt(
                        attempt, "partial", requests=request_count, error=str(exc), duration_s=monotonic() - started
                    )
                return {
                    "provider_fetches": requests,
                    "accepted_results": results,
                    "lead_observations": new_leads,
                    "stopped": "physical_request_budget",
                    "errors": errors,
                    "status": store.status(region),
                }
            except SiteBlockedError as exc:
                stopped = str(exc)
                with db.transaction():
                    store.query_state(query["key"], "blocked", error=stopped)
                    store.finish_attempt(
                        attempt, "blocked", requests=request_count, error=stopped, duration_s=monotonic() - started
                    )
                break
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                errors.append(reason)
                with db.transaction():
                    store.query_state(query["key"], "failed", error=reason)
                    store.finish_attempt(
                        attempt, "failed", requests=request_count, error=reason, duration_s=monotonic() - started
                    )
                break
        if stopped or requests >= limits.pages or results >= limits.results:
            break
    return {
        "provider_fetches": requests,
        "accepted_results": results,
        "lead_observations": new_leads,
        "stopped": stopped,
        "errors": errors,
        "status": store.status(region),
        "caveat": "Résultats du moteur limités au budget ; aucun rappel global d’Internet déduit.",
    }


def run_search(
    http: PoliteHttp,
    db: Database,
    *,
    region: str,
    limits: SearchLimits = DEFAULT_SEARCH_LIMITS,
    retry_failed: bool = False,
) -> dict[str, Any]:
    """Hold the CLI writer lock before calling: recovery assumes no previous worker remains alive."""
    with http.request_budget(limits.requests) as budget:
        result = _run_search(http, db, region=region, limits=limits, retry_failed=retry_failed)
    return result | {"physical_requests": budget.requests}
