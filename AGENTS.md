# AGENTS.md

Early detection of French grid-connection projects (battery storage "BESS", data centers) from public filings.
Output: a SQLite dataset of evidence-backed projects, served read-only over MCP (stdio and HTTP), a JSON API and a map;
a measured quality loop (evaluation + human review) improves it over time.
Human docs (French): `README.md`, `docs/architecture.md`, `docs/deploiement.md`, `demo/`. This file is the agent contract.

## Commands

```bash
uv sync --all-extras                      # install (extra "llm" = anthropic SDK)
uv run pytest                             # offline tests (network tests: -m network)
uv run ruff format src tests && uv run ruff check src tests && uv run mypy   # must all pass
uv run radar --help                       # CLI; every command prints ONE JSON object on stdout
RADAR_OFFLINE=1 uv run radar run          # rebuild changed filings -> links -> projects, no network (--full: all)
RADAR_OFFLINE=1 uv run radar eval         # quality report, stored and compared with the previous run
uv run radar eval --strict                # exit 2 on any regression (CI / before merging an extractor change)
uv run radar web [--review]               # map + JSON API + MCP over HTTP (127.0.0.1:8765); --review = correction UI
uv run radar review export|import         # back up / restore corrections + link decisions (gold/*.jsonl)
uv run radar changes --since 2026-10-01   # change log (new / changed / removed projects)
uv run radar export csv|geojson           # files in <data>/exports/
uv run radar digest --since 2026-10-05    # weekly alert e-mail (HTML or --format text)
uv run radar health [--strict]            # collector health (blocked / empty / stale / crashed)
uv run radar triage                       # review queue: doubtful filings first, with reasons
uv run radar errors                       # error clusters from corrections, detector precision/recall
uv run radar cases export|check [--strict] # regression cases from human-verified filings (gold/cases)
uv run radar improve [--field F]          # agent-ready tickets: failing cases + validated rule candidates
uv run radar shadow [--strict]            # dry run of current code on ALL filings: fixes / breaks vs verified
uv run radar aliases export               # learn name spellings from corrections (gold/aliases.json)
uv run radar templates [export]           # document templates seen / accept them (gold/templates.json)
uv run radar rules list|adopt|disable     # regex rules adopted without code (gold/rules.json), learned reliability
uv run radar screen-terms [suggest|adopt] # screen terms learned from screen misses (gold/screen_terms.json)
uv run radar robustness                  # metamorphic check: text perturbations must not change values
uv run radar corpus build|snapshot        # redacted real-PDF corpus (tests/corpus), replayed by the tests
uv run radar mcp                          # MCP server, stdio
uv run radar update                       # scheduled job: polite collect of recent items + run (network!)
docker build -t radar-raccordement .      # production image (see compose.yaml); also runs the OCR evaluation
```

Never run `collect`/`update` unless the task explicitly asks for new data: collection hits public sites.
Never run `eval --classifier` or set `RADAR_LLM_MODEL` without being asked: model calls are paid.

Definition of done: format + ruff + mypy strict + pytest green, and `radar run` + `radar eval` show no regression on
`data/` (or every regression is explained, checked in the document, and the reference fixed).

## Environment

| Variable | Default | Effect |
| --- | --- | --- |
| `RADAR_DATA_DIR` | `data` | state: `radar.sqlite`, `blobs/`, `transcripts/`, `cache/` (lookups, `derived.sqlite`), `reference/`, `exports/` |
| `RADAR_GOLD_DIR` | `gold` | versioned references: `labels.jsonl`, `fields.jsonl`, `corrections.jsonl`, `link_decisions.jsonl`, adopted `rules.json` / `screen_terms.json` / `aliases.json` / `templates.json` (human work: in git) |
| `RADAR_OFFLINE` | `0` | `1` = no network at all: no HTTP, no LLM; lookups from cache + reference files only |
| `RADAR_LLM_MODEL` | unset | e.g. `claude-opus-5-5`: enables LLM classification + fallback extraction (extra `llm` + `ANTHROPIC_API_KEY`) |
| `RADAR_TESSERACT` | `1` | OCR scanned pages with local Tesseract when on PATH (after transcripts); `0` disables |
| `RADAR_USER_AGENT` | project UA | sent on every request |
| `RADAR_API_TOKEN` | unset | `radar web`: require `Authorization: Bearer <token>` on `/api/*` and `/mcp`; mandatory off localhost |

`data/` is git-ignored and rebuildable from `blobs/` (content-addressed raw files are the source of truth), except the
corrections table: export it (`radar review export`) to keep human work in git.

## Data flow (one direction, each layer imports only the ones above it)

```
domain/         pure types + rules (no I/O): enums, models, ids, geo, power, text, departments,
                privacy (person scrubber), fields (comparable values: machine vs current)
store/          db.py (SQLite, migrations), blobs.py (sha256 files), cache.py (disposable derived results)
reading/        bytes -> PdfContent (text, AcroForm fields, scanned pages); ocr.py = engines + provenance + cache
extraction/     PdfContent/text -> Facts (value + Evidence): cerfa, decision, substation; llm.py = verified fallback
classification/ rules.screen (layer 1, recall-first; + adopted terms.py) -> Classifier chain (LLM cached, then
                gold labels); bayes = local second opinion (never decides, flags confident disagreements)
validation/     Facts + Lookups port -> Checks (GDPR, SIRET, commune, GPS, power, dates) + confidence
linking/        linker (related links), resolve (same-project groups, must/cannot-link), registry
                (commissioned installations), review (human link verdicts: queue, linker/grouping metrics)
pipeline/       filings (build), corrections (human overrides), run (process / link_and_build), changes (log),
                projects, health (collectors), importers, fingerprint (incremental: skip unchanged filings)
evaluation/     gold (field references), fields, ocr, classification, privacy, stats (Wilson, dev/test split),
                rigor (strata, calibration, blind, recall estimate), report (run-to-run comparison)
quality/        flags (doubt detectors), audit (fetch although rejected), triage (review queue), errors (learn
                from corrections), cases (regression cases), locate + suggest + improve (bad example -> ticket),
                arbiter (learned source reliabilities), confidence (calibrated per-field), propagate (same-error
                suggestions, aliases), shadow (whole-corpus dry run), templates (layout fingerprints), drift,
                rulebook (adopted regex rules), robustness (metamorphic), corpus (redacted real PDFs),
                pageview (evidence highlighted on its PDF page), screen_terms (screen misses -> terms),
                agreement (double annotation, kappa)
net/http.py     PoliteHttp: per-host pacing, retries, SiteBlockedError
sources/        collectors: dreal, mrae, georisques, registry (companies), energy_registry, press, prefectures
api/            read queries -> pydantic views (only thing MCP/CLI/web expose); exports, digest, review
mcp_server.py   MCP tools = thin wrappers over api/queries.py
web/app.py      Starlette: /api/*, / (static/index.html), /mcp (streamable HTTP), review endpoints (--review only)
wiring.py       composition root (Settings -> services, caches). cli.py = argparse -> wiring
```

Core entities (`domain/models.py`): `DocumentRecord` (one PDF) -> `Filing` (one case number; `machine` keeps the raw
machine output, corrections apply on top) + `Signal` -> `Link` -> `Project` (a group of filings: what users query).

## Invariants (do not break)

1. **Every extracted value carries `Evidence`** (document id, locator, verbatim quote). No evidence -> do not set it.
   LLM values are kept only if the quote is in the text AND the value is in the quote (`extraction/llm.verify`).
2. **Never invent data.** Transcripts in `data/transcripts/<document_id>.txt` are verbatim (excerpts allowed);
   estimates are `PowerStatus.ESTIMATED` ranges with a `basis`; unknown stays `None`.
3. **GDPR**: no natural person's name in anything stored or sent to a model. `build_filing` scrubs quotes,
   descriptions, titles and LLM inputs (`domain/privacy.PersonScrubber`); `radar eval` audits it (`privacy.leaks` = 0).
   Reviewer names stay in the corrections table, never in API/MCP output.
4. **Polite collection**: only through `PoliteHttp`. On `SiteBlockedError` stop that source for the run. Never
   parallelise requests to one host, never retry around a 403/429/disconnect, never bypass a block.
   Prefecture sites blocked us once (2026-10): keep `MIN_INTERVAL_S = 5` and a page budget.
5. **Writes**: `Database` writes do not commit; the caller wraps them in `with db.transaction():`.
6. **Idempotence**: `process`/`link`/`run` rebuild from stored documents; re-running changes nothing. A filing
   is skipped when its input fingerprint is unchanged (`pipeline/fingerprint.py`: documents, code hash, steering
   gold files, setup, corrections, learned state); `--full` forces it. Each filing records `versions.code`/`git`.
7. **Layer 1 screen is recall-first**: it may keep noise, it must not drop a target. Regex changes need a test with
   the verbatim title that motivated them.
8. **Read API is read-only.** MCP tools and web endpoints open one short-lived connection per call; bad input ->
   `ToolError` (MCP) / HTTP 400 (web). Writes exist only in review mode (`radar web --review`).
9. Code, identifiers, comments, docstrings: English. User-facing labels/caveats in `api/` and `web/`: French.
10. **Change log**: `link_and_build` diffs old vs new projects and writes links, projects and `project_changes` in one
    transaction. A pipeline bug that produces spurious changes must be cleaned from the log (it is client-facing).
11. **Machine vs human**: evaluation always measures `Filing.machine` (raw output), never corrected values.
    Corrections are append-only (`active = 0` to withdraw); the latest per field wins at every rebuild.
12. **Versions and caches**: bump `extraction.EXTRACTION_VERSION` when extractor output can change, and the
    `PROMPT_VERSION` of `classification/llm.py` or `extraction/llm.py` on any prompt/schema change. Cache keys include
    them, so a bump never reads a stale answer. `data/cache/derived.sqlite` can be deleted at any time.
13. **Same-project grouping is conservative**: same commune AND (same SIRET or same developer). A SIRET alone is not
    enough (one company files several projects). Registry "in service" needs the strict match in `linking/registry.py`.
    Human link verdicts (`LinkDecision`: same_project / related / unrelated) override grouping at every build.
14. **Web UI** (`web/static/index.html`): one file, no build step, escapes every value (`esc`, `safeUrl`).
    `tests/test_web.py` checks its JavaScript with `node --check`.
    Design (Dan's rules): never the browser's native select or checkbox look. Every `<select>` is enhanced
    automatically into the custom dropdown (`enhanceSelect`; the hidden select stays the source of truth); a
    checkbox is `<label class="cbx"><input type="checkbox" class="sr-only"><span class="box">…svg…</span>text</label>`
    (modelled on AKA-MEDIAS `portals-shared/src/ui/Checkbox.tsx`). Stripe-like look: small radii (tokens
    `--r-sm/--r/--r-lg` = 4/6/8 px), thin borders, light shadows, no purple, quiet motion with reduced-motion support.

## Quality loop (how the system improves) — method in `docs/qualite.md`

1. **Detect**: `quality/flags.detect` at every build (`Filing.flags`); unknown layouts via `quality/templates`
   (`new_template`); collectors fetch near-miss and ~3 % random rejected items (`quality/audit`).
2. **Choose and score**: every source keeps its value (`Filing.candidates`); `quality/arbiter` picks by reliability
   learned from reviews (priors = hand-written precedence); `quality/confidence` gives each field a calibrated
   probability (`Filing.field_confidence`) and auto-accepts only proven-reliable fields. Both re-learn each run.
3. **Prioritise**: `quality/triage` (doubt incl. uncertain fields and unreviewed machine changes, x stakes).
4. **Correct fast**: review UI per filing or per field (batch, keyboard); `cause`; blind mode; one-click
   propagation of a fix to filings with the same wrong value; spelling-only fixes become aliases
   (`radar aliases export`, applied at every build). A corrected value found in a document gets that passage as
   evidence (`quality/locate`).
5. **Learn**: `radar errors` (clusters, unflagged errors); `radar improve` writes `data/improve/<field>.md`: failing
   cases with the verified value in context + regex candidates validated on all verified cases (never applied
   automatically). Implement the fix (+ test with the verbatim text), bump `EXTRACTION_VERSION`.
6. **Check before adopting**: `radar shadow --strict` (whole corpus: fixes / breaks / unknown), `radar cases check
   --strict`, `radar eval --strict`. After `radar run`, changed machine values are logged and queued for review.
7. **Watch**: `radar health` (collectors + drift per source / template), `radar eval` (intervals, dev/test,
   strata incl. template, calibration diagram + ECE, auto-accept share, arbitration table, robustness, second
   opinion, linker precision + grouping, reviewer agreement + kappa).
8. **Without code**: `radar rules adopt` (regex rule from a validated `radar improve` candidate; dropped
   automatically if reviews prove it unreliable), `radar screen-terms adopt` (term rescuing screen misses, with
   its cost in extra fetches), aliases, templates. Each is a versioned file in `gold/`.
9. **Project level**: review mode « Liens entre dossiers » (pairs ordered by consequence: machine merges first).
10. **References**: ~10 % of reviewed filings come back for a blind second review by another reviewer
   (`quality/agreement.py`); accuracy above the agreement rate is not meaningful.
11. **Robustness**: `radar robustness` (perturbed texts) and the redacted real-PDF corpus replay
   (`tests/test_corpus.py`) catch reading/library changes. Review shows each value on its PDF page
   (`/api/review/{key}/evidence/{field}.png`, rendered in memory, names blacked out).

Rules: never look at test-split errors or tune on them (`evaluation/stats.split_of`; they never become cases).
Drafts (`verified: false`) only catch regressions. OCR quality runs where Tesseract exists (Docker).

## Recipes

- **New region**: add a `Region` in `sources/regions.py`. Nothing else should need changing.
- **New source**: `sources/<name>.py` with pure `parse_*`/`to_signal` (tested on small fixtures) and
  `collect_<name>(http, db, ...) -> CollectReport`; add a `SourceKind`; wire it in `cli._collect_one`.
- **New extracted field**: `Facts` + `Filing`, set it with `Evidence` in `extraction/`, map it in
  `pipeline/filings.build_filing`, add it to `domain/fields.field_values`, `evaluation/gold.FIELDS`,
  `pipeline/corrections.CORRECTABLE`, `api/views.py` and, if clients care, `pipeline/changes.TRACKED`.
- **New OCR engine** (cloud OCR, vision model): implement `label` + `complete` in `reading/ocr.py` (fill only
  unread pages, record `ocr_engines`), wrap it in `CachedOcr`, add it to `make_ocr`; compare with `radar eval` in Docker.
- **New classifier or model**: implement `Classifier`; wrap it in `CachedClassifier` in `wiring.make_classifier`;
  measure with `radar eval --classifier` before switching.
- **New check**: pure function in `validation/checks.py` returning `Check`; call it in `validator.validate`.
- **New doubt detector** (when `radar errors` lists unflagged errors): a function in `quality/flags.py` added to
  `DETECTORS`, a `FLAGS` entry (severity, French label, action), and a test in `tests/test_quality_flags.py`.
  Then check its precision in `radar errors` (`by_flag`): a detector that is rarely right only adds noise.
- **New MCP tool / web endpoint**: query in `api/queries.py` -> view in `api/views.py` -> thin wrapper in
  `mcp_server.py` (`READ_ONLY`) and/or a `Route` in `web/app.py` via `endpoint(...)`; test in `tests/`.
- **Schema change**: append a migration to `store/db.py` (`user_version` increments); never edit old ones.

## Tests

- Offline by default; HTTP is mocked with `httpx.MockTransport`; LLMs with a fake client (`tests/test_llm_extraction.py`).
- Test text comes from real public documents (verbatim excerpts). Say so when values are combined.
- PDFs are generated with `tests.conftest.make_pdf(pages, fields)`. Only binary exception: `tests/corpus/`, real
  public PDFs redacted by `radar corpus build` (persons, images, digital signatures, metadata removed; scanned
  documents refused; the audit must be empty). Never add an unredacted PDF. After an intended output change:
  `radar corpus snapshot` and review the `expected.json` diff.
- Shared end-to-end dataset: fixture `loaded_db` in `tests/conftest.py`. The `settings` fixture points `gold_dir`
  to a temp dir: tests must never read or write the repository's `gold/`.

## Gotchas

- A dataclass attribute named `field` shadows `dataclasses.field` inside the class body: write
  `import dataclasses` and `dataclasses.field(...)` in such classes (mypy reports "str not callable").
- **Never edit regexes or `\n` through shell heredocs / Python string replacements**: escapes get mangled (`\b`
  became a backspace twice). Use the editor tool. `tests/test_llm_extraction.py` rejects control characters.
- Windows: the CLI reconfigures stdout to UTF-8; ad-hoc scripts need `PYTHONIOENCODING=utf-8`. In Git Bash, prefix
  `docker run -v /x:/y` commands with `MSYS_NO_PATHCONV=1`.
- TLS: `truststore` uses the OS certificate store (the bundled one failed on government sites).
- PyMuPDF: `import pymupdf`. Form fields matter: several cerfas state power only in fields.
- Cerfa 14734 has two field layouts plus flattened PDFs (see `extraction/cerfa.py`); person fields are listed by
  `domain/privacy.is_person_field`.
- `Filing.key` = `"<source>:<region>:<normalized number>"`; gold files are keyed by normalized number.
- MCP SDK v2: `mcp.server.mcpserver.MCPServer`; tests use `mcp.Client(server)` in memory. Over HTTP, Host headers
  other than localhost are rejected when bound locally; behind a proxy pass `radar web --public-host <host>`.
- The national register (`energy_registry`) lists installations in service ~1 year late; 745 storage rows imported
  from the prototype extract (2026-10-12), none matching a tracked project yet.
