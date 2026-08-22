# DeepSearchAgent

A focused legal research and case-analysis agent for U.S. veterans compensation claims.

[![CI](https://github.com/richardparslow-commits/DeepSearchAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/richardparslow-commits/DeepSearchAgent/actions/workflows/ci.yml)
[![Mutation kill gate](https://github.com/richardparslow-commits/DeepSearchAgent/actions/workflows/mutation.yml/badge.svg)](https://github.com/richardparslow-commits/DeepSearchAgent/actions/workflows/mutation.yml)

## Purpose

This project is designed to search widely for relevant court and board decisions affecting veterans compensation cases, then explain the impact of those rulings on the VA claims process.

## Included authority sources

- U.S. Court of Veterans Appeals (CAVC)
- U.S. Court of Appeals for the Federal Circuit
- U.S. Supreme Court
- Board of Veterans' Appeals (BVA)

## Project status (handoff)

State as of August 2026: **864 tests at 100% line + branch coverage**, ruff clean, and the mutation kill-property enforced on every module. CI is green on Python 3.11–3.14; the mutation gate runs on every code push and nightly on the Linux runner. The desktop checkout and GitHub are the same repository, tracking `origin/main`.

### Feature surface

- **Pluggable search providers** — `SEARCH_PROVIDERS` selects DuckDuckGo (default), CourtListener (structured CAVC/Federal Circuit/SCOTUS metadata via the v4 REST API), and/or BVA (Board decisions via the search.usa.gov index, query-minimized to the issue phrase to pass its WAF). Queries are expanded issue-aware into variant queries, paged per provider, paced by RPM budgets, retried with backoff, and merged with URL-level dedupe; each provider reports per-query telemetry.
- **Deep multi-round research loop** — plan → execute → observe → refine: uncovered claim elements trigger gap-refinement rounds (bounded by `SEARCH_MAX_REFINEMENT_ROUNDS`, optionally by `SEARCH_MAX_WALL_SECONDS`), and opt-in `CITATION_TRAVERSAL` follows CourtListener citation trails from the strongest cases.
- **Enrichment + deep-read** — top cases get full source-page details; `DEEP_READ=1` ingests whole opinion bodies via chunked map-reduce summaries surfaced as `deep_summaries`.
- **Reasoning and synthesis** — deterministic template analysis is always on; with `OPENAI_API_KEY` set, an LLM narrative and a reconciling reasoning pass add contradiction flags and per-claim citations. Deterministic contradiction detection (opposite outcomes on the same statute) runs even without the LLM.
- **Observability** — structured `analysis_complete` / `analysis_failed` events on stderr carrying run ids, `search_telemetry` + `search_flags` in the JSON output, `{json,text,csv}` output formats, and `--batch-size` `batch_summary` aggregation.
- **Operational controls** — every runtime knob is a typed `Settings` field; `--show-config` prints the resolved dump with secrets masked (proxy credentials included), and per-provider overrides, budgets, retry/backoff, wall-time, round-cap, and worker caps are all env-driven.

### Quality gates

| Gate | Command | Status |
|---|---|---|
| Unit + integration suite | `make test` / `make test-w` (warnings as errors) | 864 passing |
| Coverage, line + branch | `make coverage` | 100%, enforced in CI (`--cov-fail-under=100`) |
| Lint | `make lint` | ruff clean |
| Mutation kill-property | `make mutate-check` | every module at/under `.mutation-baseline.json`; timeouts and vacuous passes hard-fail |
| CI (pushes + PRs) | `.github/workflows/ci.yml`, Python 3.11–3.14 | green |
| Mutation gate | `.github/workflows/mutation.yml`, on every code push + cron 03:00 UTC + `workflow_dispatch` | green |

Two gate scripts carry the mutation enforcement: `scripts/mutmut_pass.py` runs a scoped pass over one module (survivor diffs land in `/tmp/mutmut_survivors_<module>.txt`) and hard-fails if mutmut reports no checked mutants, so a failed stats-collection run can never masquerade as a pass; `scripts/check_mutation_baseline.py` fails on any count above baseline or any timeout. When extending the app, keep coverage at 100% and hold the baselines — triage new survivors with the pass script before bumping `.mutation-baseline.json`.

### Operating

```bash
cp .env.example .env                 # edit: API keys, providers, budgets
python -m va_legal_agent "service connection for tinnitus" --output-format text
make smoke                           # one real query per configured provider
make mutate-check                    # full mutation pass + baseline gate in one shot
```

See Configuration for the full settings table, the Retry and exhaustion chain section for worst-case timing, and Troubleshooting for provider-specific failures.

## Quick start

1. Create a virtual environment.
2. Install dependencies (use `requirements-dev.txt` instead if you also want the test/dev tooling):
   ```bash
   pip install -r requirements.txt
   ```
   Alternatively, install as a package (also provides the `va-legal-agent` command):
   ```bash
   pip install -e .          # add "[llm]" for the optional OpenAI feature
   ```
3. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
4. Run the agent:
   ```bash
   python -m va_legal_agent "service connection for tinnitus"
   ```
   Use `--no-enrich` to skip fetching case source pages for citation/date details,
   `--output-format {json,text,csv}` to choose the analysis output format,
   `--output-file PATH` to write the analysis to a file, `--show-config` to
   print the resolved settings as JSON (useful for debugging; it includes
   `effective_search_providers`, the post-validation provider list next to the
   raw `search_providers` value),
   `--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}` to control diagnostic log
   verbosity, and `--log-format {text,json}` to emit log lines as plain text or
   one JSON object per line (handy for machine parsing). Log output always goes
   to stderr, so stdout stays clean for the analysis result. Set `LOG_JSON=1`
   in the environment (or `.env`) to default to JSON logs without a flag.
   Pass `--deep-read` to ingest the full opinion text of the top cases for a
   run (overriding `DEEP_READ=0`), or `--no-deep-read` to force it off when
   `DEEP_READ=1` is set in the environment; `--deep-read-limit N` tunes how
   many top cases are ingested for that run (overriding `DEEP_READ_LIMIT`,
   still capped by the number of cases found). `--show-config` prints the
   resolved `deep_read` / `deep_read_limit` / `deep_read_pages` /
   `deep_chunk_chars` values so operators can see the effective deep-read
   configuration.

   When a run finishes, an `analysis_complete` event is always logged on stderr
   (even if `--log-level` would suppress INFO diagnostics) carrying a run id,
   coverage score, and interpretation source as structured fields:
   `{"event": "analysis_complete", "run_id": "...", "coverage_score": 0.85, "interpretation_source": "template"}`.
   If analysis raises, an `analysis_failed` event is emitted instead
   (`{"event": "analysis_failed", "run_id": "...", "issue": "...", "error": "..."}`)
   and the exception is re-raised, so pipelines get a definitive terminal
   signal on both success and failure without parsing the analysis output.
   Set `RUN_ID` in the environment (or pass `--run-id ID`, which takes
   precedence) to correlate events across invocations — e.g. one batch id
   shared by many per-issue runs; otherwise a fresh id is generated per run.
   The same id is stamped onto the analysis output as a top-level `run_id`
   field (and a `Run id:` line in text / `run_id` column in CSV), so the
   stdout result and the stderr log events can be joined on it.    Each analysis also carries a `search_telemetry` field (per-provider
    queries issued, results returned, duplicates dropped, failures, plus a
    `variants` breakdown showing which expanded query variants actually
    returned results) so the output JSON shows the recall picture behind the
    findings; the same numbers are aggregated into `batch_summary` events for
    `--batch-size` runs. The JSON analysis also carries a computed
    `search_flags` list flagging low-recall or failing providers (e.g.
    “returned no results across 8
   queries”), and text output shows the same flags as gaps while productive
   providers appear as strengths.

   Pass `--batch-size N` (with the same `--run-id` on each run) to have the
   CLI track a batch locally: once `N` runs for that id have completed or
   failed, it emits a `batch_summary` event aggregating completions, failures,
   and coverage statistics (mean/min/max) for fleet monitoring, then cleans up
   its state file (stored under `BATCH_STATE_DIR`, defaulting to a temp dir).

## Running tests

The full suite (864 tests) runs in about seven seconds, so run it after
every change:

```bash
make test            # full suite (or: python -m pytest -q)
make test-search     # search parsing, providers, query expansion
make test-fetch      # fetching, enrichment, agent pipeline
make test-cli        # CLI output formats, events, batch tracking
make test-config     # environment parsing and settings
make test-core       # ranking, impact, interpretation, llm, topics
make test-w          # full suite with warnings promoted to errors
make lint            # ruff static checks (pyflakes + pycodestyle rules)
make lint-fix        # auto-fix what ruff can
make coverage        # full suite with a per-module coverage report
make mutate          # mutation-testing pass (mutmut) over every module
make mutate-check    # full mutation pass + the kill-property baseline gate
make test ARGS="-k telemetry"   # pass pytest args through
make smoke             # one real query per configured provider (manual network check)
make smoke QUERY="tinnitus"      # ... with a custom query
```

The suite holds 100% line and branch coverage across the package (`make
coverage` measures both). The optional LLM path (`va_legal_agent/llm.py`) is
covered via a mocked `openai` client, so it needs no network or API key. CI
(GitHub Actions, on every push and PR) runs `make lint`, `make test-w`, and
`make coverage ARGS="--cov-fail-under=100"` so the coverage baseline is
enforced, not just measured.

Coverage measures *execution*; `make mutate` checks whether the tests would
actually *catch* a fault. It runs a mutmut pass per module (each module
mutated against the test slice that exercises it) and writes every surviving
mutant's diff to `/tmp/mutmut_survivors_<module>.txt`. A survivor is either a
provably-equivalent mutation (log-message text, default args, unreachable
fallbacks — expected and acceptable) or a real gap that needs a stronger
test. The current suite kills every non-equivalent mutant: survivors are
limited to equivalent ones. Run it after adding features or tests to keep
that property, and triage survivors with `python scripts/mutmut_pass.py
<module> <test_file>` for a single module.

The kill-property is enforced, not assumed: a GitHub Actions job
(`mutation-kill-gate`) runs `make mutate` on every push to `main` that
touches code/tests (docs-only pushes are excluded) and fails when any
module's survivor count exceeds its entry in `.mutation-baseline.json` (the
triaged, provably-equivalent survivors per module; a module at `0` fails on
*any* survivor). Because it runs on push, the baseline bump must land in the
*same commit* as the code change that introduced the survivors — a feature
commit that adds a new equivalent without moving the baseline goes red
immediately instead of being caught up to 24h later. When the gate reports a
violation, triage the diff in `/tmp/mutmut_survivors_<module>.txt` — kill
real gaps with stronger tests, and bump the baseline only for a
newly-proven equivalent (e.g. a new log message). Run the same gate locally
in one shot with `make mutate-check` (full pass + baseline check; it exits
non-zero with a triage pointer on any untriaged survivor), or trigger the
job on demand via `workflow_dispatch`.

## Example usage

```python
from va_legal_agent.agent import analyze_cases_for_claim

analysis = analyze_cases_for_claim("service connection for tinnitus", max_results=5)
print(analysis.model_dump_json(indent=2))
```

## What this agent does

- Searches across the major veterans law sources: CAVC, Federal Circuit, Supreme Court, and BVA (queries run concurrently).
- Finds candidate cases based on claim issue, benefit type, and legal topic.
- Ranks results with an explainable ranking layer: court-authority tiers (Supreme Court > Federal Circuit > CAVC > BVA) are strictly dominant, and within a tier cases are scored by issue relevance, decision recency, and extracted-detail completeness.
- Fetches the top cases' source pages (HTML or PDF) to populate citation, decision date, holding, docket number, judge attribution, cited statutes, and procedural outcome where they can be extracted.
- Analyzes each case's impact with a nuanced profile: claim topics touched, procedural posture, statutory anchors, and weight of authority.
- Converts findings into structured VA claims guidance via the interpretive analysis layer: detected legal elements, case-backed principles, strengths/gaps, a coverage score, and actionable next steps. The narrative can be enhanced by OpenAI/Azure OpenAI when `OPENAI_API_KEY` is set; otherwise a deterministic template is used.
- Produces a structured analysis suitable for legal research and issue-spotting.

## How case ranking works

The ranking layer (`va_legal_agent/ranking.py`) orders candidates in two steps:

1. **Authority tiers are strictly dominant.** A Federal Circuit decision always outranks any CAVC or BVA decision, and so on up the hierarchy, because binding authority matters more than signal scores.
2. **Within a tier, a weighted composite score decides:**
   - relevance (60%): issue keyword relevance, normalized against the best match in the batch
   - recency (25%): decision year scaled from 1985 to today; unknown dates get a neutral score
   - completeness (15%): share of citation/decision date/holding extracted during enrichment

Before the final result cap is applied, a **per-court representation floor** reserves two slots for every authority tier that is present, so a full slate of Federal Circuit/CAVC cases can't starve the Board (BVA) out of the output entirely; the remaining slots then fill best-first. Each case carries a `composite_score` (authority tier + within-tier score) and a `ranking_explanation` describing the factor breakdown, and the analysis summary reports the score alongside authority and relevance.

## Case detail extraction

The enrichment step (`va_legal_agent/fetch.py`) pulls structured details from each top case's source page or PDF opinion. CourtListener cases are enriched from the REST opinion detail endpoint's full text instead of the frontend page — the `courtlistener.com/opinion/…` page serves an AWS WAF JavaScript challenge that no non-browser client (curl_cffi included) can solve, while the API carries the same body unchallenged. BVA `.txt` decisions, CAVC/CAFC pages, and other URLs use the generic page/PDF fetch.

- **Citation** — reporter citations (Vet.App., F.2d/F.3d/F.4th, U.S., S. Ct., Westlaw) and BVA citation numbers
- **Decision date** — ISO date, preferring dates marked "Decided"/"Filed"/"Issued"
- **Holding** — the first explicit holding sentence ("we hold that...", "the Court holds that..."), falling back to the page's meta description for HTML
- **Docket** — docket/case/appeal numbers (e.g. `19-4433`)
- **Judge** — judge attribution ("Judge Mary J. Smith", "Chief Judge ...") or "Per Curiam"
- **Statutes** — unique cited VA statutes, normalized (e.g. `38 U.S.C. § 5107(b)`, `38 C.F.R. § 3.303`)
- **Outcome** — procedural posture detected from disposition language (e.g. `vacated and remanded`)

All fields are best-effort: unparseable pages leave fields empty rather than failing the research run.

## Impact analysis layer

`va_legal_agent/impact.py` turns each case record into an `ImpactProfile` with a nuanced narrative (`agent.summarize_case_impact` delegates to it):

- **Issue tags** — claim topics the ruling touches (service connection, benefit of the doubt, reasons and bases, evidence evaluation, nexus, rating), in priority order, falling back to the claim issue itself.
- **Procedural posture** — the outcome (from enrichment, or detected from the title/holding) plus a nuance note explaining what vacatur, remand, affirmance, etc. can mean for a claim.
- **Statutory anchors** — statutes the ruling engages, mapped to doctrine hints (e.g. § 5107(b) benefit-of-the-doubt, § 7104(d)(1) reasons-and-bases).
- **Weight of authority** — binding appellate precedent vs. persuasive Board-level decisions vs. unidentified sources.

Output is deterministic research support derived from public decisions, not legal advice.

## Interpretive analysis layer

`va_legal_agent/interpretation.py` turns the ranked cases plus the claim issue into structured VA claims guidance:

- **Detected elements** — legal elements mentioned in the issue (service connection, nexus, benefit of the doubt, reasons and bases, presumptions, rating, aggravation, duty to assist, evidence evaluation), each with claimant guidance; `covered_by` lists which retrieved cases address the element.
- **Case-backed principles** — known veterans-law principles (e.g. 38 U.S.C. § 5107(b) benefit of the doubt, § 7104(d)(1) reasons and bases, nexus, evidence rules) scanned out of the case text with source-case attribution.
- **Strengths, gaps, and coverage score** — the share of detected elements that retrieved authority actually covers; gaps suggest where targeted research is needed.
- **Actionable next steps** — derived from the detected elements rather than static boilerplate.
- **Narrative** (`how_it_affects_va_claims`) — LLM-enhanced when `OPENAI_API_KEY` is set (`interpretation_source: "llm"`), deterministic template otherwise (`"template"`). All output states it is research support, not legal advice.
- **LLM reasoning pass** — with `OPENAI_API_KEY` set, the agent reconciles holdings across all ranked cases, flags contradictions between decisions (`contradictions` in the JSON/text/CSV output), and cites each claim; the deterministic template remains the always-on fallback.
- **Deterministic contradiction detection** — always on, even without the LLM: pairs of retrieved authorities that cite the same statute but reach opposite outcomes (favorable vs. unfavorable to the claimant, e.g. `granted` vs. `denied` on the same 38 U.S.C. §) are surfaced as `contradictions` on the template path too. Only explicit, single-direction outcomes trigger a flag; unknown or mixed postures never do. When the LLM reasoning pass is active, its contradictions are merged with the deterministic ones (deduped by case pair, LLM wins on a tie).
- **Deep-read mode** — with `DEEP_READ=1`, the agent ingests the full body of the top cases (every PDF page, the whole HTML page, or the verbatim plain-text decision) instead of the snippet and the first few pages, splits it into chunks, digests each chunk deterministically (holding / outcome / statutes), and synthesizes the digests into one per-case summary (LLM when `OPENAI_API_KEY` is set, a plain join otherwise). The reasoning pass then cross-references these full-text summaries across the whole corpus. Off by default because it fetches the entire body of every top case; a case whose body cannot be fetched keeps its snippet-based path. Each top case's summary is surfaced in the output as `deep_summaries` (a list of `{"case": ..., "summary": ...}` entries in JSON, a `Deep summaries` block in text, and a `deep_summaries` column in CSV), so you can see exactly what full-text ingestion produced.

## Configuration

Environment variables (see `.env.example`; loaded automatically via python-dotenv):

| Variable | Default | Purpose |
|---|---|---|
| `REQUEST_TIMEOUT_SECONDS` | `20` | Timeout for each outbound search/fetch request |
| `MAX_FETCH_BYTES` | `20971520` | Cap on downloaded response size in bytes (20 MiB) |
| `USER_AGENT` | `Mozilla/5.0 (compatible; VA-Legal-Agent/1.0; …)` | HTTP User-Agent sent by the plain-`requests` clients on WAF-free endpoints — the CourtListener REST provider and the BVA sitemap / local-index fetches; **not** sent to DuckDuckGo, the BVA search index, or the fetch layer (enrichment / deep-read), which all use `curl_cffi` browser impersonation (whose Chrome User-Agent must not be overridden) |
| `SEARCH_MAX_WORKERS` | `4` | Number of court-site search queries run concurrently |
| `SEARCH_DELAY_SECONDS` | `0.5` | Stagger between starting consecutive search queries (`0` disables) |
| `SEARCH_RETRY_ATTEMPTS` | `2` | Extra retries for throttled/transient DuckDuckGo responses (`0` disables) |
| `SEARCH_BACKOFF_BASE_SECONDS` | `1.0` | Base delay (seconds) for retry backoff, doubled per attempt |
| `SEARCH_BACKOFF_MAX_SECONDS` | `10.0` | Cap on the retry backoff delay (seconds) |
| `SEARCH_MIN_INTERVAL_SECONDS` | `0` | Global minimum seconds between any two search requests, across all providers (`0` disables) |
| `SEARCH_HTTP_PROXY` | – | Optional HTTP(S) proxy URL that every DuckDuckGo/CourtListener/BVA request routes through (e.g. a residential proxy when the local IP is flagged); empty connects directly |
| `SEARCH_MAX_RPM_BY_PROVIDER` | – | Per-provider requests-per-minute budget, e.g. `courtlistener=5,bva=3`; unlisted providers have no budget (`0` disables for that provider) |
| `SEARCH_BVA_SITEMAP_SCAN_LIMIT` | `200` | How many of the most recent BVA decision files the `bvasitemap` provider scans per query — the va.gov sitemap enumerates tens of thousands of decisions per year, so the provider bounds its file fetches to the newest N and greps those for the issue phrase |
| `SEARCH_BVA_LOCAL_INDEX_DIR` | `.bva_index` | On-disk directory where the `bvalocal` provider downloads the current year's full BVA decision corpus, builds a concatenated `corpus.txt` + byte-offset `manifest.json`, and runs subsequent queries against the local index (WAF-free, sub-second). The first query triggers the download if the index is absent or stale; set to empty to disable the `bvalocal` provider |
| `SEARCH_BVA_LOCAL_INDEX_MAX_FILES` | `0` | How many of the most recent decision files the `bvalocal` provider downloads on first build. `0` means download every current-year file (typically 40k+). Lower it to trade recall for faster first builds |
| `SEARCH_BVA_LOCAL_INDEX_MAX_AGE_HOURS` | `24` | Maximum age (hours) before the `bvalocal` provider re-checks the va.gov sitemap for newer decisions and auto-rebuilds the index. `0` disables auto-rebuild — the index is only built on first use |
| `SEARCH_MAX_RESULTS` | `10` | Cap on results merged per search query; raise it (e.g. `30`) for multi-provider runs so a backend listed first can't fill the cap alone and starve the others (override per run with `--max-results`) |
| `SEARCH_MAX_WALL_SECONDS` | `0` | Cap on total wall time (seconds) spent searching for one issue (`0` disables; override per run with `--max-wall-time`) |
| `SEARCH_MAX_REFINEMENT_ROUNDS` | `3` | Deterministic cap on gap-refinement rounds in the research loop — the re-search loop stops after this many rounds even with `SEARCH_MAX_WALL_SECONDS=0`, so a broken ready-filter or a `refine_plan` that mints fresh tasks can't spin forever (`0` disables gap refinement entirely) |
| `SEARCH_PROVIDERS` | `duckduckgo` | Comma-separated search providers (`duckduckgo`, `courtlistener`, `bva`, `bvasitemap`, `bvalocal`) |
| `SEARCH_PAGES_PER_QUERY` | `1` | Pages of results fetched per query per provider (`>1` deepens recall) |
| `SEARCH_PAGES_PER_QUERY_BY_PROVIDER` | – | Per-provider overrides, e.g. `bva=1,duckduckgo=4`; unlisted providers fall back to the global (each provider still fetches at least 1 page) |
| `SEARCH_QUERY_VARIANTS` | `3` | Additional variant queries derived from topic synonyms and statute hints (`0` disables) |
| `SEARCH_QUERY_VARIANTS_BY_PROVIDER` | – | Per-provider overrides, e.g. `bva=0,courtlistener=5`; unlisted providers fall back to the global (`0` disables expansion for that provider) |
| `SEARCH_EXCLUDE_TERMS` | – | Comma-separated terms (case-insensitive) that suppress results whose title/snippet contain them, e.g. `knee,back` to exclude decisions where the issue appears only in the procedural history. DDG appends `-term` operators; the post-fetch filter catches every provider (`""` = no filter) |

| `COURTLISTENER_API_KEY` | – | CourtListener API token (free account; now required — v4 returns 401 for anonymous requests) |
| `COURTLISTENER_USAGE_GUARD` | `1` | Pre-flight check against CourtListener's `api-usage` endpoint before each run; aborts with the daily-window reset time when the remaining budget can't cover the run (`0` disables) |
| `CITATION_TRAVERSAL` | `0` | Multi-hop citation traversal: follow CourtListener citation trails from the strongest cases found and merge the discovered opinions before final ranking (`1`/`true`/`yes`/`on` enables). Off by default because it makes extra authenticated API calls per run |
| `CITATION_TRAVERSE_LIMIT` | `3` | How many top cases seed the citation traversal |
| `ENRICH_CASE_LIMIT` | `5` | Top cases enriched with full source-page details |
| `INTERPRET_CASE_LIMIT` | `3` | Top cases fed into the interpretation narrative / LLM |
| `PRINCIPLE_SCAN_LIMIT` | `5` | Cases scanned for case-backed principle findings |
| `OPENAI_API_KEY` | – | Enables optional LLM interpretation of the top cases |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model used for LLM interpretation |
| `OPENAI_TIMEOUT_SECONDS` | `60` | Timeout (seconds) for each OpenAI completion request |
| `OPENAI_MAX_TOKENS` | `700` | Maximum completion tokens for the LLM narrative |
| `OPENAI_BASE_URL` | – | Endpoint override for Azure OpenAI / compatible APIs |
| `LLM_REASONING` | `1` | Reconcile holdings across all ranked cases via the LLM, flag contradictions, and cite each claim (`0` keeps only the lighter single-call narrative) |
| `LLM_REASONING_LIMIT` | `10` | Ranked cases fed into the reconciling reasoning pass |
| `DEEP_READ` | `0` | Deep-read mode: fetch the full opinion text of the top cases and summarize it via chunked map-reduce, so the reasoning pass cross-references holdings across the whole corpus instead of truncated snippets (`1`/`true`/`yes`/`on` enables) |
| `DEEP_READ_LIMIT` | `3` | Top cases ingested in deep-read mode |
| `DEEP_READ_PAGES` | `0` | PDF page cap for deep-read fetches (`0` reads every page) |
| `DEEP_CHUNK_CHARS` | `6000` | Approximate character size of each chunk in the map-reduce pass |
| `LOG_JSON` | `0` | Emit diagnostic logs as JSON on stderr (`1`/`true`/`yes`/`on`); overridden by `--log-format` |
| `RUN_ID` | – | Correlation id attached to terminal events; a fresh id is generated per run when unset |
| `BATCH_STATE_DIR` | temp dir | Directory for per-run_id batch state files used by `--batch-size` |

## Notes

This is a strong research foundation for legal analysis. For production use, a paid legal database or a more structured case-indexing layer can improve completeness and precision.

Web search is performed against DuckDuckGo's HTML endpoint without an API key; heavy use may be rate-limited or blocked by the provider. Throttled or transient responses (a 202/challenge page, a 429, or a 5xx) are retried with exponential backoff and jitter (`SEARCH_RETRY_ATTEMPTS`, `SEARCH_BACKOFF_BASE_SECONDS`, `SEARCH_BACKOFF_MAX_SECONDS`). A global minimum interval between requests can be enforced with `SEARCH_MIN_INTERVAL_SECONDS`. If every query still fails, the agent raises a `SearchError` describing the last underlying error. Request timeout can be tuned via `REQUEST_TIMEOUT_SECONDS` in `.env`.

Search runs through a pluggable provider layer (`SEARCH_PROVIDERS`). By default only DuckDuckGo is used; adding `courtlistener` queries the CourtListener v4 REST **search** endpoint for CAVC/Federal Circuit/SCOTUS opinions with structured metadata (case name, citation, decision date, docket, judge) that skips HTML enrichment entirely. (Full-text search lives on `/api/rest/v4/search/` — the `/opinions/` list endpoint rejects `q`/`court` as unknown filter params — and pagination is cursor-based: `SEARCH_PAGES_PER_QUERY` > 1 follows the `next` cursor links.) CourtListener's v4 API now requires a token — set `COURTLISTENER_API_KEY` (free account) or every query fails with 401; the free tier rate-limits the search endpoint aggressively, so keep `SEARCH_QUERY_VARIANTS_BY_PROVIDER=courtlistener=0` (or a low value) and `SEARCH_PAGES_PER_QUERY_BY_PROVIDER=courtlistener=1` to stay inside it. The citation-traversal feature fetches each opinion's cluster and docket records for its metadata (the opinions detail endpoint carries only the text). Adding `bva` searches the Board of Veterans' Appeals decisions index (search.usa.gov, `bvadecisions` affiliate), returning plain-text decision files that the fetch layer parses directly. Adding `bvasitemap` goes straight to the official `va.gov/sitemap_bva.xml` index (the `Described By` target of the data.va.gov catalog entry): it fetches the sitemap index and the current year's leaf sitemap once per run, then scans the most recent `SEARCH_BVA_SITEMAP_SCAN_LIMIT` decision files for the issue phrase — WAF-free, no search.usa.gov challenge pages — but it covers only recent current-year decisions, so it complements (not replaces) `bva`. Adding `bvalocal` takes this further: on first use it downloads every decision file the sitemap enumerates into a local on-disk index (`corpus.txt` + `manifest.json` under `SEARCH_BVA_LOCAL_INDEX_DIR`), and every subsequent query is sub-second — no per-query HTTP at all, no throttling, fully WAF-immune. Unknown or typo'd provider names in `SEARCH_PROVIDERS` are warned about at startup (visible in `--show-config` runs too) and skipped rather than failing the run, so the raw value stays in the config dump for inspection. Setting `SEARCH_PAGES_PER_QUERY` above `1` fetches multiple result pages per query for deeper recall (subject to the same throttling rules); backends that rate-limit pagination can be pinned to a single page per provider with `SEARCH_PAGES_PER_QUERY_BY_PROVIDER` (e.g. `bva=1`) while others keep deeper paging.

Each query is expanded into up to `SEARCH_QUERY_VARIANTS` variant queries derived from the shared topic vocabulary and the statute/doctrine table (fragments like `5107`, `3.303`), so one issue surfaces results that use different phrasing. Expansion is issue-aware: only topics implicated by the issue contribute synonyms (a rating issue expands with `evaluation`/`schedular`, not `nexus`), and only the statute fragments anchored to those topics are appended (a rating issue adds `4.1`, not `5107`/`1110`), so an unrelated issue like `tinnitus` gets no statute variants at all. Terms already in the issue are skipped. `site:` tokens are stripped automatically for CourtListener, which has its own court filter; DuckDuckGo keeps them. Backends that throttle extra queries can be tuned per provider with `SEARCH_QUERY_VARIANTS_BY_PROVIDER` — e.g. `bva=0` runs only the base query against search.usa.gov while DuckDuckGo keeps the global expansion; unlisted providers fall back to `SEARCH_QUERY_VARIANTS`.

### Retry and exhaustion chain (worst-case timing)

A single run nests three retry loops, and each is governed by its own setting. If you are tuning for wall time — e.g. to bound how long a batch takes when a provider is throttling — reason about them bottom-up:

1. **Inside each provider call** (`DuckDuckGoProvider.search` / `CourtListenerProvider._get_json`). A throttled/transient response is retried up to `SEARCH_RETRY_ATTEMPTS` times, so a call makes at most `SEARCH_RETRY_ATTEMPTS + 1` HTTP requests. Before each retry the provider sleeps `min(SEARCH_BACKOFF_BASE_SECONDS × 2ⁱ × jitter, SEARCH_BACKOFF_MAX_SECONDS)` (jitter is a random 0.75–1.25 multiplier; the `i`-th retry backoff doubles the base). CourtListener additionally honors the server's `Retry-After` header (never sleeping *less* than the exponential floor, still capped at `SEARCH_BACKOFF_MAX_SECONDS`). Every provider paces each attempt through `_throttle`: the global `SEARCH_MIN_INTERVAL_SECONDS` gap between any two requests, plus — when set — a per-provider `SEARCH_MAX_RPM_BY_PROVIDER` budget (60/N seconds apart for that backend, e.g. `courtlistener=5` forces ≥12 s between CourtListener requests). If every attempt hangs to its timeout, a call's worst case is roughly `(SEARCH_RETRY_ATTEMPTS + 1) × REQUEST_TIMEOUT_SECONDS +` the sum of those backoffs. BVA (search.usa.gov) does not retry — it fails fast on the first request error (one HTTP request, no backoff) — but it is still paced.
2. **Inside each query** (`search_all`). For every provider, every expanded variant runs on every page: up to `(1 + SEARCH_QUERY_VARIANTS) × SEARCH_PAGES_PER_QUERY` provider calls per query (the original query is always included; per-provider overrides `SEARCH_QUERY_VARIANTS_BY_PROVIDER` / `SEARCH_PAGES_PER_QUERY_BY_PROVIDER` replace the globals for that backend). Each of those calls is a step-1 call, so a fully-throttled query multiplies the step-1 worst case by that count.
3. **Inside each issue** (`fetch_cases_for_issue`). The agent builds 8 court-site queries and runs them on `SEARCH_MAX_WORKERS` threads, submitting them staggered by `SEARCH_DELAY_SECONDS`. Because the pool overlaps execution, the wall time is roughly `(8 − 1) × SEARCH_DELAY_SECONDS` plus the *slowest* query's step-2 time — not the sum of all eight.

When every level fails, the run raises a `SearchError` describing the **last** underlying error (the last retry's error inside a call, the last query×page's error in `search_all`, and the last query's error in `fetch_cases_for_issue`) — see the exhaustion tests for the exact contract.

**Worked example (defaults, all DuckDuckGo, everything throttled to the worst case):** a call makes 3 requests and sleeps 1.25 + 2.5 s ≈ **63.75 s** worst case; a query makes 4 calls ≈ **255 s**; an issue runs 8 queries concurrently with a 0.5 s stagger ≈ **4.3 minutes** of wall time; and with `SEARCH_MAX_REFINEMENT_ROUNDS=3` the research loop can then run up to three gap-refinement rounds on top, each another 4.3-minute fan-out, so a fully-throttled issue with gaps that never close reaches ≈ **4 × 4.3 min ≈ 17 minutes** before the round cap stops it (the loop usually exits far earlier — each round re-searches only the still-uncovered elements and stops as soon as the gaps close or the refined plan offers nothing new). Realistic partial failures are far cheaper — each successful call returns immediately — and `SEARCH_MIN_INTERVAL_SECONDS` (if set) adds pacing waits on top. To bound wall time when a backend is throttling: lower `SEARCH_RETRY_ATTEMPTS` (or `SEARCH_BACKOFF_MAX_SECONDS`), pin pages/variants down with the per-provider overrides, reduce `REQUEST_TIMEOUT_SECONDS` — or stop reasoning about the loops entirely and set a budget: `SEARCH_MAX_WALL_SECONDS` (or `--max-wall-time` per run) caps the total search time for one issue. When the budget is exhausted the remaining queries are abandoned — results already found are returned (with a warning), an in-flight query stops cooperatively between provider calls, and if nothing was found a `SearchError` is raised. The bound is not a hard abort of a request already in flight, but the run never starts new work past the deadline and returns at it.

**Free-tier CourtListener worked example (the recommended reliability settings):** with `SEARCH_PROVIDERS=courtlistener`, `SEARCH_QUERY_VARIANTS_BY_PROVIDER=courtlistener=0`, `SEARCH_PAGES_PER_QUERY_BY_PROVIDER=courtlistener=1`, `SEARCH_MAX_WORKERS=1`, `SEARCH_DELAY_SECONDS=3`, `SEARCH_MIN_INTERVAL_SECONDS=1`, `SEARCH_MAX_RPM_BY_PROVIDER=courtlistener=5`, and `SEARCH_BACKOFF_MAX_SECONDS=60`, the request count collapses to **one call per query** — 8 requests per issue, ≈ **15 issues/day** inside the 125/day window (which the usage guard enforces). The point of these settings is *pacing*, not speed: at 5 RPM CourtListener requests are spaced ≥12 s apart, so a healthy-but-paced issue takes ≈ 8 × 12 s ≈ **1.6 minutes** (fully serial under one worker; the 3 s stagger never adds because each query already exceeds it). When the backend is throttling hard, the worst case is worse than the default example *per call*: every retry honors `Retry-After` up to the 60 s cap, so a call is 3 × 20 s + 60 + 60 s = **180 s**; with one call per query that is **180 s per query** and, serialized across 8 queries, **24 minutes per issue** — **≈ 1.6 hours** for the research loop's four fan-outs, **≈ 80 hours** for a 50-issue batch. That ceiling is exactly why the reliability block also pairs these knobs with a budget: `SEARCH_MAX_WALL_SECONDS` (or `--max-wall-time`) returns each issue at the deadline instead of grinding through the capped `Retry-After` waits.

Independent of the clock, `SEARCH_MAX_REFINEMENT_ROUNDS` caps how many gap-refinement rounds the research loop may run. Each round re-searches the refined queries and costs up to another per-round worst case (the step-3 term above, itself bounded by the wall-time budget when one is set), so with the deadline disabled the loop's worst case is at most **(1 + `SEARCH_MAX_REFINEMENT_ROUNDS`) × per-round cost** — the initial fan-out plus up to the cap in gap rounds — instead of unbounded; a broken ready-filter or a `refine_plan` that mints a fresh task every round stops after the cap rather than spinning forever (the loop logs `Refinement round limit of N reached…` when the cap is what stopped it).

**Batch runs multiply the per-issue worst case.** The CLI analyzes one issue per invocation — `--batch-size N` does *not* parallelize; it merely correlates `N` separate invocations sharing a `--run-id` and emits a `batch_summary` once that many outcomes are recorded. A sequentially-driven batch therefore takes at most **N × (per-issue worst case)** (the ≈ 4.3 min fan-out above, which already embeds the 8-query `SEARCH_DELAY_SECONDS` stagger and the per-query exhaustion time; with gaps that never close, the research loop multiplies it by 1 + `SEARCH_MAX_REFINEMENT_ROUNDS` ≈ 4 → the ≈ 17 min worst case), plus per-invocation overhead: process startup and the interpretation step (up to `OPENAI_TIMEOUT_SECONDS` per issue when the LLM path is enabled). With defaults, 50 throttled issues, and persistent gaps that consume every refinement round, that is ≈ 50 × 17 min ≈ **14 hours** of worst-case wall time — with healthy providers (or gaps that close after a round or two) it is a few seconds per issue instead. To bound a batch:

- Shrink the per-issue term — `SEARCH_RETRY_ATTEMPTS`, `SEARCH_BACKOFF_MAX_SECONDS`, per-provider `SEARCH_PAGES_PER_QUERY_BY_PROVIDER` / `SEARCH_QUERY_VARIANTS_BY_PROVIDER`, `REQUEST_TIMEOUT_SECONDS`, `SEARCH_DELAY_SECONDS`, and `SEARCH_MAX_REFINEMENT_ROUNDS` (each gap round multiplies the per-issue worst case) all feed into it directly. The cleanest single lever is `--max-wall-time` (or `SEARCH_MAX_WALL_SECONDS`): each issue then returns at that deadline regardless of how the retry loops are misbehaving, turning the batch bound into `N × min(worst case, budget)`.
- Or run invocations concurrently from the orchestrator: `P` parallel CLI processes cut the wall time to roughly `⌈N / P⌉ ×` per-issue time. The batch state file is designed for this — `record()` appends one line per outcome atomically and is safe under concurrent writers — so parallel orchestrators can share a `--run-id` without losing completions.

## Troubleshooting

### Recall looks thin or a provider seems dead

Run `make smoke`, which sends one real query per configured provider and prints what each one returns, including the first few titles and URLs:

```bash
make smoke                       # uses SEARCH_PROVIDERS (default: duckduckgo)
make smoke QUERY="hearing loss" # custom issue
SEARCH_PROVIDERS="duckduckgo,courtlistener,bva" make smoke
```

- A provider that fails prints its error and the command exits non-zero, so failures are hard to miss.
- A provider that returns nothing is often rate-limited — search.usa.gov throttles BVA queries aggressively — so retry later, or pin it down with `SEARCH_PAGES_PER_QUERY_BY_PROVIDER=bva=1` / `SEARCH_QUERY_VARIANTS_BY_PROVIDER=bva=0`.
- Run it after changing provider code (new backends, query expansion, pagination) and before releasing, since CI cannot exercise the live network.

### CourtListener always fails with 401

Its v4 API now requires a token. Set `COURTLISTENER_API_KEY` to a free account token (account → Settings → API).

### CourtListener keeps returning 429 Too Many Requests

The free tier rate-limits the search endpoint aggressively — bursts of a few requests per minute trigger a `retry-after` window, and sustained probing can raise a multi-minute penalty. Unlike DuckDuckGo, CourtListener did not retry at all in earlier versions; it now backs off on its own, so a 429 does not immediately fail the run.

The free tier applies **three rolling windows concurrently** — 5 requests/minute, 50/hour, and 125/day — and the most restrictive one wins. The per-minute pacing (`SEARCH_MAX_RPM_BY_PROVIDER`) can't see the hourly/daily windows, which is why repeated runs within a day can still 429 even at 5 RPM (one issue run costs 8 base queries). By default the app now runs a **pre-flight check** (`COURTLISTENER_USAGE_GUARD=1`) against `/api/rest/v4/api-usage/` before each run: it estimates the run's total CourtListener request cost — **search queries × variants × pages plus deep-read opinion-detail fetches** — and aborts with the daily-window reset time when the budget can't cover it. When `--deep-read` is enabled, the guard also pre-flights the **per-minute window** so deep-read fetches don't grind through 429s with 60-second backoffs mid-run. The check uses the usage endpoint's own throttle, so it never burns the search budget — call it manually with `curl -H "Authorization: Token $COURTLISTENER_API_KEY" https://www.courtlistener.com/api/rest/v4/api-usage/`.

**The CourtListener retry/backoff chain** (inside every `CourtListenerProvider` HTTP call — search, cursor pagination, and the citation-traversal fetches all share one `_get_json` path):

1. Each attempt is paced by the global `SEARCH_MIN_INTERVAL_SECONDS` throttle first, so concurrent workers cannot burst the backend even before the first request.
2. A transient failure — `429`/`500`/`502`/`503`/`504`, a connection error, or a timeout — is retried up to `SEARCH_RETRY_ATTEMPTS` times.
3. Before each retry the provider sleeps `max(exponential-with-jitter, Retry-After)`, capped at `SEARCH_BACKOFF_MAX_SECONDS`. The exponential floor is `SEARCH_BACKOFF_BASE_SECONDS × 2^i × jitter` (jitter 0.75–1.25); the `Retry-After` header is honored in both its integer-seconds and HTTP-date forms. The cap means a server asking for a very long wait can never stall the run past `SEARCH_BACKOFF_MAX_SECONDS`.
4. After the retries are exhausted the call raises a `SearchError` carrying the last underlying error; a token-less 401 raises the "set `COURTLISTENER_API_KEY`" hint instead, and any other non-transient error raises immediately without retrying.

**Recommended settings for a reliable free-tier run** — bound the request count *and* honor the long `retry-after` values the free tier sometimes returns:

```bash
SEARCH_PROVIDERS=courtlistener
SEARCH_QUERY_VARIANTS_BY_PROVIDER=courtlistener=0   # base query only, no expansion
SEARCH_PAGES_PER_QUERY_BY_PROVIDER=courtlistener=1  # one page per query
SEARCH_MAX_WORKERS=1                                 # serialize queries, no overlap
SEARCH_DELAY_SECONDS=3                               # gap between the 8 base queries
SEARCH_MIN_INTERVAL_SECONDS=1                        # floor between raw HTTP requests
SEARCH_MAX_RPM_BY_PROVIDER=courtlistener=5           # hard cap: <=5 CourtListener requests/min
SEARCH_BACKOFF_MAX_SECONDS=60                        # let Retry-After wait out the penalty window
```

`SEARCH_MAX_RPM_BY_PROVIDER` is the strongest of these levers for sustained runs: it spaces that backend's requests at least `60/N` seconds apart regardless of how many workers/variants are running, so a batch cannot burst past the limit the way the delay/stagger knobs (which only shape startup order) can.

If a run still 429s after all retries, wait out the `retry-after` window (the error message and `make smoke` output show it) before retrying — or rerun with `SEARCH_MAX_WALL_SECONDS`/`--max-wall-time` so a throttled issue returns its partial results at the deadline instead of grinding through every retry.

### DuckDuckGo keeps serving challenge pages

DuckDuckGo's `html.duckduckgo.com/html/` endpoint challenge-flags the plain `requests` client — a 202/anomaly page, or a hard 403 after sustained use — keying on **both** the TLS fingerprint and the app's own `USER_AGENT` (which declares itself a bot). The DuckDuckGo provider therefore sends its HTTP through `curl_cffi` with `impersonate="chrome"`, which reproduces a real Chrome handshake and supplies a browser User-Agent. As with BVA, the app's `USER_AGENT` is **not** sent — overriding the impersonation would re-trigger the block.

Two things still cause challenge pages even with impersonation:

1. **Rapid bursts.** DuckDuckGo challenge-flags an IP that fires many requests in quick succession (verified live: back-to-back requests get 202, then a single paced request returns 200 again). Slow the run with `SEARCH_DELAY_SECONDS`, `SEARCH_MIN_INTERVAL_SECONDS`, or `SEARCH_MAX_RPM_BY_PROVIDER=duckduckgo=…`, and space repeated runs out.
2. **A hard 403 after sustained abuse.** If even the browser-impersonated client gets 403 for minutes on end, the IP is flagged longer-term — switch networks/VPN, or drop `duckduckgo` from `SEARCH_PROVIDERS` until it cools.

Failures are graceful: a challenged query counts as a provider failure in the telemetry and the run completes on the other providers.

### BVA returns rate-limit/anomaly challenge pages (202)

`search.usa.gov` (which indexes Board decisions) sits behind **AWS WAF**, which challenges the TLS fingerprint of plain HTTP clients — the `requests` library is almost always blocked regardless of IP or pacing. The BVA provider therefore sends its HTTP through `curl_cffi` with `impersonate="chrome"`, which reproduces a real Chrome TLS handshake and passes the challenge. It reuses one process-wide session so the `AWSALB` cookie jar persists across the whole run. On top of that, the provider **minimizes every query to its issue phrase** before sending it: the broad `site:bva.va.gov "issue" "ClaimType" veterans compensation` recall and the statute-anchored `"1110" "issue" veterans compensation` query are both reduced to just `issue`, dropping the quotes, claim type, statute fragment, and trailing boilerplate. The WAF challenges the *content* of those long recall queries (`service connection for tinnitus veterans compensation decision` returns a 202) while the short issue phrase (`service connection for tinnitus`) sails through, and the Board index ranks decisions on that phrase alone, so nothing is lost. (BVA decision `.txt` files themselves live on `www.va.gov/vetapp…` and are served without a WAF; only the search index is protected.) The app's own `USER_AGENT` is **not** sent to BVA — overriding the impersonation with a bot-identifying UA would re-trigger the block, so it is deliberately omitted.

Things that can still cause 202s even with impersonation, the cookie jar, and query minimization:

1. **Rapid bursts.** WAF challenge-flags an IP that fires many requests in quick succession (verified live: even a plain browser curl gets 202 + empty body during a burst, then 200 again after ~30s). Slow the run with `SEARCH_DELAY_SECONDS` or `SEARCH_MAX_RPM_BY_PROVIDER=bva=…`, and space repeated runs out.
2. **Very long issue phrases.** A genuinely long multi-condition issue can still trip the length heuristic once the issue phrase itself is long; shorten the issue text, or rely on CourtListener to carry the run.
3. **A hard block after sustained abuse.** If even a browser UA gets 202 for minutes on end, the IP is flagged longer-term — switch networks/VPN, or drop `bva` from `SEARCH_PROVIDERS` until it cools.

For reliable Board-level recall from a flagged IP, route BVA through a residential proxy with `SEARCH_HTTP_PROXY` (the provider honors it), or accept that BVA degrades gracefully while CourtListener carries the run.

Failures are graceful: a challenged query counts as a provider failure in the telemetry and the run completes on the other providers.

### A provider name is silently ignored

Typos in `SEARCH_PROVIDERS` are warned about at startup and dropped; `--show-config` prints both `search_providers` (raw) and `effective_search_providers` (what will actually run). Valid names: `duckduckgo`, `courtlistener`, `bva`, `bvasitemap`, `bvalocal`.
