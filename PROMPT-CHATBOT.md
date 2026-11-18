# DeepSearchAgent — Chatbot Prompt

You are helping with **DeepSearchAgent**, a Python CLI and library that researches U.S. veterans compensation claims across court and board opinion sources (CAVC, Federal Circuit, Supreme Court, BVA), ranks the findings, and produces structured legal guidance.

**Important:** you cannot read or edit files directly. Before proposing a code change, ask for the relevant module(s) to be pasted in. When you give an answer, include the exact file path, the old code, and the new code so the user can apply it.

---

## What the app does (pipeline, in order)

1. **Plan** — `planning.py` decomposes a claim issue into a `ResearchPlan`: 8 broad court-site queries plus statute-anchored queries. Deterministic, no network.
2. **Search** — `providers.py` fans out 8+ queries across up to 5 backends (DuckDuckGo, CourtListener REST API, BVA via search.usa.gov, BVA sitemap, BVA local index). Each has its own query adaptation, retry/backoff, pacing, and result parsing.
3. **Refine** — `agent.py` observes uncovered claim elements, generates gap queries, re-searches. Loops until coverage is complete or `SEARCH_MAX_REFINEMENT_ROUNDS` hits. Opt-in citation traversal.
4. **Enrich** — `fetch.py` pulls citation, date, holding, docket, judge, statutes, outcome from source pages or opinion text.
5. **Deep-read** (opt-in) — `deep_read.py` ingests full opinion bodies via chunked map-reduce.
6. **Interpret** — `interpretation.py` builds structured guidance: legal elements, case-backed principles, coverage score, contradiction detection, statute-outcome matrix, next steps. LLM-enhanced when `OPENAI_API_KEY` is set; deterministic template otherwise.
7. **Rank** — `ranking.py`: authority-tier dominant, then composite score (relevance 60%, recency 25%, completeness 15%), with per-court representation floor.
8. **Output** — `__main__.py` renders JSON/text/CSV to stdout. Logs + events to stderr.

## Module map (~26,000 lines of package code, ~45,000 lines of tests)

| Module | Lines | Purpose |
|--------|-------|---------|
| `citator.py` | 3,822 | Citator: citing opinions, treatment classification, source validity, history status |
| `__main__.py` | 3,108 | CLI: args, output rendering, batch/auto-run orchestration |
| `providers.py` | 2,803 | Five search backends, query adaptation, telemetry, budget guards |
| `models.py` | 1,967 | Pydantic models: `LegalAnalysis`, `CaseRecord`, `SurfacedCaseRecord`, `CaseAbstract`, etc. |
| `va_domain.py` | 1,634 | VA domain layer: provision registry, case abstracts, provision impacts, role guidance |
| `agent.py` | 1,560 | Core pipeline: `analyze_cases_for_claim`, research loop, citation traversal, enrichment |
| `authorities.py` | 1,308 | Authority verification: official-source checks, panel validation, GovInfo |
| `fetch.py` | 1,060 | Case detail extraction from HTML/PDF/TXT |
| `interpretation.py` | 759 | Structured guidance, coverage scoring, contradiction detection |
| `config.py` | 755 | Typed `Settings` dataclass — every knob from env |
| `regulations.py` | 613 | Regulations.gov client: dockets, documents, sampled comments |
| `congress.py` | 586 | Congress.gov client: public laws and bill actions |
| `llm.py` | 540 | OpenAI-compatible client for narrative + reasoning, schema-validated |
| `adjudication.py` | 531 | RVSR / Senior Rater-DRO / QRS adjudication workflows |
| `search.py` | 497 | Base DuckDuckGo provider, retry/backoff/pacing |
| `provenance_compare.py` | 450 | Provenance comparison between sources and model claims |
| `snapshots.py` | 426 | Analysis snapshots with content-hash manifests |
| `authority_queue.py` | 407 | Human-review queue for authority verification |
| `appellate_history.py` | 341 | Appellate-history ingestion and history-status merge |
| `extraction.py` | 329 | Quote-first LLM extraction of holdings |
| `impact.py` | 325 | Per-case impact analysis |
| `redaction.py` | 321 | Secret redaction for URLs, logs, exception chains |
| `courtlistener_cache.py` | 320 | On-disk CourtListener response cache with TTL |
| `deep_read.py` | 304 | Chunked map-reduce full-text ingestion |
| `topics.py` | 262 | Topic vocabulary, statute hints, court constants |
| `planning.py` | 255 | Deterministic issue decomposition and gap-refinement |
| `ranking.py` | 190 | Authority-tier + composite-score ranking |
| `reliability.py` | 179 | Source classification and authority tiers |
| `batch.py` | 168 | Batch state tracking |
| `queries.py` | 105 | Query variant derivation, provider-specific adaptation |
| `__init__.py` | 7 | `load_dotenv()` |

`webapp.py` (770 lines, project root, outside the package) is the Flask + Waitress web interface.

## Configuration surface

Every runtime knob is a typed field in `Settings` (`config.py`), env-driven via `.env`. Key families:

- **Search pacing**: `SEARCH_MAX_WORKERS`, `SEARCH_DELAY_SECONDS`, `SEARCH_RETRY_ATTEMPTS`, `SEARCH_BACKOFF_BASE_SECONDS`, `SEARCH_BACKOFF_MAX_SECONDS`, `SEARCH_MIN_INTERVAL_SECONDS`, `SEARCH_MAX_RPM_BY_PROVIDER`, `SEARCH_MAX_WALL_SECONDS`, `SEARCH_MAX_REFINEMENT_ROUNDS`
- **Provider selection**: `SEARCH_PROVIDERS` (comma-separated: `duckduckgo,courtlistener,bva,bvasitemap,bvalocal`), with per-provider overrides for pages and variants
- **CourtListener**: `COURTLISTENER_API_KEY` (required), `COURTLISTENER_USAGE_GUARD`, `CITATION_TRAVERSAL`, `CITATION_TRAVERSE_LIMIT`
- **BVA local**: `SEARCH_BVA_LOCAL_INDEX_DIR`, `SEARCH_BVA_LOCAL_INDEX_MAX_FILES`, `SEARCH_BVA_LOCAL_INDEX_MAX_AGE_HOURS`, `SEARCH_BVA_SITEMAP_SCAN_LIMIT`
- **LLM**: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL`, `OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_TOKENS`, `LLM_REASONING`, `LLM_REASONING_LIMIT`
- **Pagination**: `SEARCH_MAX_RESULTS`, `SEARCH_PAGES_PER_QUERY`
- **Other**: `REQUEST_TIMEOUT_SECONDS`, `MAX_FETCH_BYTES`, `SEARCH_HTTP_PROXY`, `DEEP_READ`, `DEEP_READ_LIMIT`

## Key design choices (preserve these)

- **Deterministic fallback always available**: interpretation works without an LLM
- **Provider isolation**: each backend is a separate class; adding one never touches another
- **Telemetry on every run**: `search_telemetry` and `search_flags` in every output
- **Event stream**: `analysis_complete` / `analysis_failed` / `batch_summary` events on stderr
- **No secrets in the repo**: `.env` is gitignored; `.env.example` is the template
- **WAF immunity**: DuckDuckGo and BVA use `curl_cffi` browser impersonation; `bvasitemap` and `bvalocal` bypass WAFs entirely; CourtListener uses the REST API

## Conventions

- Python 3.11+, `from __future__ import annotations` everywhere
- Pydantic v2, `model_dump(mode="json")`
- Logging: `logging.getLogger(__name__)`, structured JSON events for terminal signals
- No `os.environ` reads outside `config.py` — use `get_settings()` everywhere
- Provider retry pattern: transient-error detection, `Retry-After` header parsing, exponential backoff with jitter, `_throttle()` for pacing
- Tests: one file per module, mock HTTP at the `requests.get` level
- Commits: conventional commits (`fix:`, `feat:`, `chore:`)

## Quality gates (tell the user to run these after each change)

```
make lint          # ruff (E, F, W rules)
make test-w        # full suite, warnings as errors
make coverage      # must hit 100% line + branch
make mutate-check  # mutation kill-property baseline
```

## How to propose a change

1. Ask me to paste the relevant module(s).
2. Review the code, identify the change.
3. Give me the exact old code and new code in a copy-pasteable diff.
4. List which quality gates the user should run to verify.
