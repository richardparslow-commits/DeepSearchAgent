# DeepSearchAgent — App Architecture & Development Prompt

You are working on **DeepSearchAgent**, a Python CLI and library that researches U.S. veterans compensation claims across court and board opinion sources, ranks the findings, and produces structured legal guidance.

## What the app does (pipeline, in order)

1. **Plan** — `planning.py` decomposes a claim issue into a `ResearchPlan`: 8 broad court-site queries (CAVC, Federal Circuit, Supreme Court, BVA) plus statute-anchored queries for detected claim elements. No network, no LLM — entirely deterministic.

2. **Search** — `providers.py` fans out 8+ queries across up to 5 pluggable backends (DuckDuckGo, CourtListener v4 REST API, BVA via search.usa.gov, BVA via va.gov sitemap, BVA via local on-disk index). Each backend has its own query adaptation, retry/backoff, pacing, and result parsing. Results are merged, deduplicated by title/URL and citation/date (with a same-URL fallback), and capped.

3. **Refine** — `agent.py` observes which claim elements are uncovered, generates gap-refinement queries via `refine_plan()`, and re-searches. Loops until coverage is complete or `SEARCH_MAX_REFINEMENT_ROUNDS` is hit. Opt-in `CITATION_TRAVERSAL` follows CourtListener citation trails.

4. **Enrich** — `fetch.py` pulls structured details (citation, date, holding, docket, judge, statutes, outcome) from each top case's source page or opinion text. CourtListener cases use the REST API (not the WAF'd frontend). BVA `.txt` decisions are parsed directly.

5. **Deep-read** (opt-in) — `deep_read.py` ingests full opinion bodies, chunked via map-reduce into per-case summaries surfaced as `deep_summaries`.

6. **Interpret** — `interpretation.py` builds structured guidance: detected legal elements with claimant guidance, case-backed principles, strengths/gaps with a coverage score, contradiction detection (deterministic + LLM-assisted), a statute-outcome matrix, and actionable next steps. With `OPENAI_API_KEY` set, the LLM enhances the narrative and reconciling reasoning pass; without it, a deterministic template runs.

7. **Rank** — `ranking.py` orders cases by authority tier (strictly dominant) then within-tier composite score (relevance 60%, recency 25%, completeness 15%), with a per-court representation floor.

8. **Output** — `__main__.py` renders JSON, text, or CSV. Analysis goes to stdout; logs and terminal events (`analysis_complete`/`analysis_failed`) go to stderr.

## Module map (~26,000 lines of package code, ~45,000 lines of tests)

| Module | Lines | Purpose |
|--------|-------|---------|
| `__main__.py` | 3,108 | CLI entry point: arg parsing, output rendering, batch/auto-run orchestration |
| `citator.py` | 3,822 | Citator: citing-opinion retrieval, treatment classification, source-validity checks, history status |
| `providers.py` | 2,803 | Search providers: DuckDuckGo, CourtListener, BVA (search.usa.gov), BVA sitemap, BVA local index; query adaptation/minimization; telemetry rollup; budget guards |
| `models.py` | 1,967 | Pydantic models: `LegalAnalysis`, `CaseRecord`, `SurfacedCaseRecord`, `CaseAbstract`, impact/adjudication records, and more |
| `va_domain.py` | 1,634 | VA domain layer: provision registry, case abstracts, provision impacts, RVSR/DRO guidance builders |
| `agent.py` | 1,560 | Core pipeline: `analyze_cases_for_claim`, `fetch_cases_for_issue`, research loop, citation traversal, enrichment |
| `authorities.py` | 1,308 | Authority verification: official-source checks, panel validation, GovInfo integration |
| `fetch.py` | 1,060 | Case detail extraction from HTML/PDF/TXT: citation, date, holding, docket, judge, statutes, outcome |
| `interpretation.py` | 759 | Structured guidance: element library, principle scanning, coverage scoring, contradiction detection, narrative building |
| `config.py` | 755 | Typed `Settings` dataclass: every runtime knob read from env, with parsing helpers |
| `regulations.py` | 613 | Regulations.gov client: dockets, documents, sampled public comments; provision-level impact wrapper |
| `congress.py` | 586 | Congress.gov client: public laws and bill actions (optional legislative history) |
| `llm.py` | 540 | OpenAI-compatible client: narrative interpretation, reconciling reasoning, schema-validated outputs |
| `adjudication.py` | 531 | RVSR / Senior Rater-DRO / QRS adjudication workflows and role guidance |
| `search.py` | 497 | Base DuckDuckGo provider, retry/backoff/pacing utilities, HTTP proxy support |
| `provenance_compare.py` | 450 | Provenance comparison between retrieved sources and model claims |
| `snapshots.py` | 426 | Analysis snapshots with content-hash manifests and source-URL identity |
| `authority_queue.py` | 407 | Human-review queue for authority verification |
| `appellate_history.py` | 341 | Appellate-history file ingestion and history-status merge |
| `extraction.py` | 329 | Quote-first LLM extraction of holdings from opinion text |
| `impact.py` | 325 | Per-case impact analysis: issue tags, procedural posture, statutory anchors, authority weight |
| `redaction.py` | 321 | Secret redaction for URLs, logs, and exception chains |
| `courtlistener_cache.py` | 320 | On-disk CourtListener response cache with TTL |
| `deep_read.py` | 304 | Chunked map-reduce full-text ingestion |
| `topics.py` | 262 | Topic vocabulary, statute hints, court constants, principle patterns |
| `planning.py` | 255 | Deterministic issue decomposition, query plan generation, gap-refinement |
| `ranking.py` | 190 | Authority-tier + composite-score ranking with per-court representation floor |
| `reliability.py` | 179 | Source classification (official/secondary/unknown) and authority tiers |
| `batch.py` | 168 | Batch state tracking for `--batch-size` / `--auto-run` |
| `queries.py` | 105 | Query variant derivation, provider-specific adaptation |
| `__init__.py` | 7 | `load_dotenv()` |

`webapp.py` (770 lines, project root, outside the package) is the Flask + Waitress web interface.

## Configuration surface

Every runtime knob is a typed field in `Settings` (`config.py`), driven by environment variables (auto-loaded from `.env` via python-dotenv). Key families:

- **Search pacing**: `SEARCH_MAX_WORKERS`, `SEARCH_DELAY_SECONDS`, `SEARCH_RETRY_ATTEMPTS`, `SEARCH_BACKOFF_BASE_SECONDS`, `SEARCH_BACKOFF_MAX_SECONDS`, `SEARCH_MIN_INTERVAL_SECONDS`, `SEARCH_MAX_RPM_BY_PROVIDER`, `SEARCH_MAX_WALL_SECONDS`, `SEARCH_MAX_REFINEMENT_ROUNDS`
- **Provider selection**: `SEARCH_PROVIDERS` (comma-separated: `duckduckgo,courtlistener,bva,bvasitemap,bvalocal`), with per-provider overrides for pages (`SEARCH_PAGES_PER_QUERY_BY_PROVIDER`) and variants (`SEARCH_QUERY_VARIANTS_BY_PROVIDER`)
- **CourtListener**: `COURTLISTENER_API_KEY` (required), `COURTLISTENER_USAGE_GUARD`, `CITATION_TRAVERSAL`, `CITATION_TRAVERSE_LIMIT`
- **BVA local**: `SEARCH_BVA_LOCAL_INDEX_DIR`, `SEARCH_BVA_LOCAL_INDEX_MAX_FILES`, `SEARCH_BVA_LOCAL_INDEX_MAX_AGE_HOURS`, `SEARCH_BVA_SITEMAP_SCAN_LIMIT`
- **LLM**: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL`, `OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_TOKENS`, `LLM_REASONING`, `LLM_REASONING_LIMIT`
- **Pagination**: `SEARCH_MAX_RESULTS`, `SEARCH_PAGES_PER_QUERY`
- **Other**: `REQUEST_TIMEOUT_SECONDS`, `MAX_FETCH_BYTES`, `SEARCH_HTTP_PROXY`, `DEEP_READ`, `DEEP_READ_LIMIT`, `DEEP_READ_PAGES`, `DEEP_CHUNK_CHARS`

## Quality gates (strict — every change must pass all)

- **Lint**: `ruff` (rules E, F, W; E501 ignored). `make lint`
- **Tests**: 2,434 tests, ~20s runtime. `make test` / `make test-w` (warnings as errors)
- **Coverage**: 100% line + branch, enforced by `--cov-fail-under=100`. `make coverage`
- **Mutation kill-gate**: mutmut pass per module against its test slice for the 18 modules paired in the Makefile's `MUTATE_MODULES`; survivors triaged into `.mutation-baseline.json`. `make mutate-check`

## Conventions when editing

- Python 3.11+, `from __future__ import annotations` everywhere
- Pydantic v2 models with `model_dump(mode="json")`
- Logging via `logging.getLogger(__name__)`, structured JSON log events for terminal signals
- No `os.environ` reads outside `config.py` — use `get_settings()` everywhere
- Provider fetch/retry pattern: `_get_json()` / `_get_text()` helpers with transient-error detection, `Retry-After` header parsing, exponential backoff with jitter, and `_throttle()` for global + per-provider pacing
- Test structure: one file per module (`tests/test_providers.py`, `tests/test_legal_agent.py`, etc.), mock external HTTP at the `requests.get` / `requests.Session.get` level, use the `conftest.py` env-var reset fixture
- Mutation baseline: when adding code that produces provably-equivalent survivors (log messages, default args, unreachable fallbacks), update `.mutation-baseline.json` in the same commit — the CI gate fails on any survivor count above baseline or any changed survivor-identity digest
- Commit format: conventional commits (`fix:`, `feat:`, `chore:`), usually naming the audit finding IDs they resolve (e.g. `fix(domain): resolve AUD4-002 …`)

## Key design choices to preserve

- **Deterministic fallback always available**: the interpretation layer works without an LLM; contradiction detection runs both paths
- **Provider isolation**: each backend is a separate class under the `SearchProvider` protocol; adding one never touches another's code
- **Telemetry on every run**: `search_telemetry` (per-provider queries/results/dedupes/failures + variant breakdown) and `search_flags` in every output
- **Event stream**: `analysis_complete` / `analysis_failed` / `batch_summary` events on stderr for pipeline integration
- **No secrets in the repo**: `.env` is gitignored; `.env.example` is the template
- **WAF immunity**: DuckDuckGo and BVA use `curl_cffi` browser impersonation; `bvasitemap` and `bvalocal` bypass WAFs entirely; CourtListener uses the REST API (not the frontend)
