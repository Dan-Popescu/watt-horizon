# AGENTS.md

Early detection of French grid-connection projects (battery storage "BESS", data centers) from public filings.
Output: a SQLite dataset of evidence-backed projects, served read-only over MCP (stdio and HTTP), a JSON API and a map.
Human docs (French): `README.md`, `docs/architecture.md`, `docs/deploiement.md`. This file is the agent contract.

## Commands

```bash
uv sync --all-extras                      # install (extra "llm" = anthropic SDK)
uv run pytest                             # offline tests (network tests: -m network)
uv run ruff format src tests && uv run ruff check src tests && uv run mypy   # must all pass
uv run radar --help                       # CLI; every command prints ONE JSON object on stdout
RADAR_OFFLINE=1 uv run radar run          # rebuild filings -> links -> projects, no network
uv run radar eval                         # gold-set report (screening recall, completeness)
uv run radar mcp                          # MCP server, stdio
uv run radar web                          # map + JSON API + MCP over HTTP on http://127.0.0.1:8765 (/mcp)
uv run radar changes --since 2026-10-01   # change log (new / changed / removed projects)
uv run radar export csv|geojson           # files in <data>/exports/
uv run radar update                       # scheduled job: polite collect of recent items + run (network!)
docker build -t radar-raccordement .      # image used in production (see compose.yaml)
```

Never run `collect`/`update` unless the task explicitly asks for new data: collection hits public sites.

Definition of done: format + ruff + mypy strict + pytest green, and `radar run` + `radar eval` still work on `data/`.

## Environment

| Variable | Default | Effect |
| --- | --- | --- |
| `RADAR_DATA_DIR` | `data` | all state: `radar.sqlite`, `blobs/`, `transcripts/`, `cache/lookups.json`, `reference/communes.json`, `gold_labels.jsonl` |
| `RADAR_OFFLINE` | `0` | `1` = no network at all: no HTTP, no LLM; lookups from cache + reference files only |
| `RADAR_LLM_MODEL` | unset | e.g. `claude-opus-5-5`: enables the LLM classifier (extra `llm` + `ANTHROPIC_API_KEY`); unset = gold set only |
| `RADAR_TESSERACT` | `1` | OCR scanned pages with local Tesseract when the binary is on PATH (after transcripts); `0` disables |
| `RADAR_USER_AGENT` | project UA | sent on every request |
| `RADAR_API_TOKEN` | unset | `radar web`: require `Authorization: Bearer <token>` on `/api/*` and `/mcp`; mandatory off localhost |

`data/` is git-ignored and rebuildable from `blobs/` (content-addressed raw files are the source of truth).

## Data flow (one direction, each layer imports only the ones above it)

```
domain/        pure types + rules (no I/O): enums, models, ids, geo, power, text, departments
reading/       bytes -> PdfContent (text layer, AcroForm fields, scanned pages) ; ocr.py = OcrEngine chain
extraction/    PdfContent/text -> Facts (value + Evidence per field): cerfa.py, decision.py, substation.py
classification/ rules.screen (layer 1, recall-first) -> Classifier chain (LLM, then gold set)
validation/    Facts + Lookups port -> Checks (GDPR, SIRET, commune, GPS, power, dates) + confidence
linking/       filings + signals -> proposed Links (same commune / developer word / substation)
pipeline/      orchestration: ingest, build_filing, process_filings, link_and_build, changes (log), importers, evaluate
store/         db.py (SQLite, migrations) + blobs.py (sha256 files)
net/http.py    PoliteHttp: per-host pacing, retries, SiteBlockedError
sources/       collectors (network -> store): dreal, mrae, georisques, registry, press, prefectures
api/           read-only queries -> pydantic views (the only thing MCP/CLI/web expose); exports.py = CSV/GeoJSON
mcp_server.py  MCP tools = thin wrappers over api/queries.py
web/app.py     Starlette: /api/* (same queries), / (static/index.html map), /mcp (streamable HTTP, stateless)
wiring.py      composition root (Settings -> concrete services). cli.py = argparse -> wiring
```

Core entities (`domain/models.py`): `DocumentRecord` (one PDF) -> `Filing` (one case number, facts merged from its
documents) + `Signal` (company, article, installation) -> `Link` -> `Project` (what users query).

## Invariants (do not break)

1. **Every extracted value carries `Evidence`** (document id, locator, verbatim quote). No evidence -> do not set it.
2. **Never invent data.** Transcripts in `data/transcripts/<document_id>.txt` are verbatim; estimates are
   `PowerStatus.ESTIMATED` ranges with a `basis`; unknown stays `None`.
3. **GDPR**: natural persons are masked (`MASKED_PERSON`) before storage; never log, store or return their names.
4. **Polite collection**: only through `PoliteHttp`. On `SiteBlockedError` stop that source for the run. Never
   parallelise requests to one host, never retry around a 403/429/disconnect, never bypass a block.
   Prefecture sites blocked us once (2026-10): keep `MIN_INTERVAL_S = 5` and a page budget.
5. **Writes**: `Database.upsert_*` do not commit; the caller wraps them in `with db.transaction():`.
6. **Idempotence**: `process`/`link`/`run` rebuild from stored documents; re-running gives the same result.
7. **Layer 1 screen is recall-first**: it may keep noise (layer 2 rejects it), it must not drop a target.
   Any regex change needs a test with the verbatim title that motivated it.
8. **Read API is read-only.** MCP tools and web endpoints open one short-lived connection per call; bad input ->
   `ToolError` (MCP) / HTTP 400 (web). New read features go in `api/queries.py` first, then MCP + web.
9. Code, identifiers, comments, docstrings: English. User-facing labels/caveats in `api/` and `web/`: French.
10. **Change log**: `link_and_build` diffs old vs new projects (`pipeline/changes.py`) and writes links, projects and
    `project_changes` in one transaction. Unchanged projects keep `updated_at`. Track a new field in `changes.TRACKED`.
11. **Web UI** (`web/static/index.html`): one file, no build step, escapes every value (`esc`, `safeUrl`). Basemap =
    OpenFreeMap positron; the self-hosted `departements.geojson` fallback is used only if the style itself fails.

## Recipes

- **New region**: add a `Region` in `sources/regions.py` (departments, DREAL base + listings, MRAe pages,
  prefecture roots). Nothing else should need changing; if it does, fix the coupling.
- **New source**: `sources/<name>.py` with pure `parse_*`/`to_signal` functions (unit-tested on small fixtures) and
  one `collect_<name>(http, db, ...) -> CollectReport`; add a `SourceKind`; wire it in `cli.cmd_collect`.
  PDFs go through `pipeline.ingest.ingest_pdf`; non-PDF findings are `Signal`s.
- **New extracted field**: add it to `Facts` + `Filing`, set it with `Evidence` in `extraction/`, map it in
  `pipeline/filings.build_filing`, expose it in `api/views.py`, add it to `evaluate.TRACKED_FIELDS` if useful.
- **New check**: pure function in `validation/checks.py` returning `Check`; call it in `validator.validate`.
- **New classifier** (e.g. cheaper model): implement `classification.base.Classifier`; add it to the chain in
  `wiring.make_classifier`. Measure with `radar eval` before/after.
- **New MCP tool**: add the query to `api/queries.py` returning a view from `api/views.py`, then a thin
  `@mcp.tool(annotations=READ_ONLY)` in `mcp_server.py`; test it in `tests/test_pipeline_api_mcp.py`.
- **New web endpoint**: a query in `api/queries.py`, then a `Route` in `web/app.py` wrapped by `endpoint(...)`;
  test it in `tests/test_web.py` with Starlette's `TestClient` (base_url `http://127.0.0.1:8765`).
- **Schema change**: append a migration to `store/db.py` (`user_version` increments); never edit old ones.

## Tests

- Offline by default; HTTP is mocked with `httpx.MockTransport`. Real network tests: `@pytest.mark.network`.
- Test text comes from real public documents (verbatim excerpts). Say so when values are combined.
- PDFs are generated in tests with `tests.conftest.make_pdf(pages, fields)`; no binary fixtures.
- Shared end-to-end dataset: fixture `loaded_db` in `tests/conftest.py` (2 projects, 1 signal).
- Lookups use `StaticLookups` (fixtures `AUBENTON`, `BONNEUIL`, `VERS_SUR_SELLE`, `MERU` in `conftest.py`).

## Gotchas

- Windows: stdout is reconfigured to UTF-8 by the CLI; ad-hoc scripts need `PYTHONIOENCODING=utf-8`.
- TLS: `truststore` uses the OS certificate store (the bundled one failed on government sites).
- PyMuPDF: `import pymupdf` (not `fitz`). Form fields matter: several cerfas only state power in fields.
- Cerfa 14734 has two field layouts plus flattened PDFs (see `extraction/cerfa.py` docstring).
- `Filing.key` = `"<source>:<region>:<normalized number>"`; gold labels are keyed by normalized number.
- MCP SDK v2 API: `mcp.server.mcpserver.MCPServer`; tests use `mcp.Client(server)` in memory.
- MCP over HTTP rejects Host headers other than localhost when bound locally (DNS-rebinding protection); behind a
  proxy pass `radar web --public-host <host>`.
- Line endings are LF (`.gitattributes`); the Docker image runs as a non-root user with `/data` as volume.
