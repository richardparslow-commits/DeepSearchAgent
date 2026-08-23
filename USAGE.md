# How to use DeepSearchAgent (va-legal-agent)

DeepSearchAgent is a CLI + Python library that researches a veterans compensation
claim issue across court and Board authority (CAVC, Federal Circuit, Supreme Court,
BVA), ranks the findings, and turns them into structured VA claims guidance.

This guide is the practical "how do I run it" companion to `README.md`. It assumes
you are in the project root with a virtual environment already set up (see
README → Quick start if not).

---

## 1. Install and configure

```bash
# Install dependencies (add "[llm]" for the optional OpenAI interpretation):
pip install -r requirements.txt          # runtime deps only
pip install -r requirements-dev.txt      # + pytest, ruff, mutmut for dev
# or install as a package (also gives you the `va-legal-agent` command):
pip install -e .                          # add: pip install -e ".[llm]"

# Create your configuration:
cp .env.example .env
# Edit .env: at minimum decide which search providers to use
# (see step 2) and add CourtListener / OpenAI keys if you want those.
```

Configuration is environment-variable driven (auto-loaded from `.env` via
python-dotenv). Every knob is documented in the table in `README.md` →
Configuration. See what is actually resolved at any time:

```bash
python -m va_legal_agent --show-config     # prints resolved settings as JSON
```

`--show-config` masks secrets and lists `effective_search_providers` (typos in
`SEARCH_PROVIDERS` are warned about and skipped, never fatal).

### Where to run it

This is a CLI/library — there is no server, container image, or service unit in
the repo — so "running the app" means invoking the CLI on any machine with
**Python 3.11+** and **outbound internet** (the search providers are external
APIs). In practice:

- **Interactive research (preferred): the project root on a desktop machine,
  inside the project venv** — exactly the setup this repo already has
  (`.venv/bin/python`, `.env` in the project root). The app loads `.env` from
  the current working directory, `bvalocal` writes its index to `.bva_index/`
  under the working directory, and the `make` targets default to
  `.venv/bin/python`, so run from the project root rather than from an
  arbitrary directory.
- **Unattended / batch fleets (preferred): a stable always-on Linux host or
  CI-style runner with a scheduler** (cron/systemd timer, or `--auto-run`
  which waits out quota windows itself). CI runs the same code on Ubuntu with
  Python 3.11–3.14, so a plain venv on a Linux VM is the path of least
  resistance; no container is provided or needed.
- **Network matters**: DuckDuckGo and BVA (search.usa.gov) challenge-flag
  datacenter IPs, so a residential IP is strongly preferred. If your IP gets
  flagged, set `SEARCH_HTTP_PROXY` to a residential proxy, or switch the
  provider set (e.g. `bvalocal` is fully WAF-immune). Sandboxes or containers
  with no egress will fail every search.

## 2. Choose your search providers

Set `SEARCH_PROVIDERS` in `.env` (comma-separated, default is just `duckduckgo`):

| Provider | What it searches | Notes |
|---|---|---|
| `duckduckgo` | Open web search | Default; no key; may be rate-limited / challenge-flagged under burst |
| `courtlistener` | Structured CAVC / Federal Circuit / SCOTUS opinions (v4 REST API) | **Requires `COURTLISTENER_API_KEY`** (free account) or every query 401s |
| `bva` | Board decisions via search.usa.gov | No key; rate-limits aggressively |
| `bvasitemap` | Recent current-year BVA decisions from the official va.gov sitemap | WAF-free; recent decisions only |
| `bvalocal` | Full current-year BVA corpus, downloaded to a local index (`.bva_index/`) | Sub-second, WAF-free; first use downloads ~40k files |

Recommended free-tier CourtListener reliability settings (see README →
Troubleshooting for the full explanation):

```
SEARCH_PROVIDERS=courtlistener
SEARCH_QUERY_VARIANTS_BY_PROVIDER=courtlistener=0
SEARCH_PAGES_PER_QUERY_BY_PROVIDER=courtlistener=1
SEARCH_MAX_WORKERS=1
SEARCH_DELAY_SECONDS=3
SEARCH_MIN_INTERVAL_SECONDS=1
SEARCH_MAX_RPM_BY_PROVIDER=courtlistener=5
SEARCH_BACKOFF_MAX_SECONDS=60
```

Verify providers work before relying on them:

```bash
make smoke                                  # one real query per configured provider
make smoke QUERY="hearing loss"             # ... with a custom query
SEARCH_PROVIDERS="duckduckgo,courtlistener,bva" make smoke
```

## 3. Run a single analysis

```bash
python -m va_legal_agent "service connection for tinnitus"
```

The issue is the only required argument. Defaults: `--output-format json`,
`SEARCH_MAX_RESULTS` (10) results, enrichment on, interpretation via the
deterministic template.

Everyday flags:

```bash
python -m va_legal_agent "service connection for tinnitus" \
  --output-format text \                    # json | text | csv
  --output-file analysis.json \             # write to a file instead of stdout
  --max-results 30 \                        # more candidates per query
  --type Compensation \                     # benefit type to search for
  --no-enrich \                             # skip fetching source pages (faster)
  --max-wall-time 120 \                     # hard wall-clock budget in seconds
  --deep-read \                             # ingest full opinion bodies
  --deep-read-limit 5 \                     # how many top cases to deep-read
  --log-level INFO \                        # DEBUG..CRITICAL (default WARNING)
  --log-format json \                       # text | json
```

Conventions worth knowing:

- **stdout is clean**: the analysis result (JSON/text/CSV) goes to stdout; all
  log lines go to stderr. `LOG_JSON=1` in `.env` defaults logs to JSON.
- **Terminal events**: every run ends with an `analysis_complete` (or
  `analysis_failed`) event on stderr carrying `run_id`, `coverage_score`, and
  `interpretation_source` — automation can key off these without parsing the output.
- **`--run-id`**: set one to correlate runs (e.g. one id per batch); otherwise a
  fresh id is generated per run and stamped into the output too.
- **Interpretation source**: `template` (deterministic, always available) or
  `llm` (when `OPENAI_API_KEY` is set — see step 5).

## 4. Estimate before you spend quota

`--dry-run` computes the planned query count, per-provider request totals, a wall
estimate, and — with CourtListener configured — the **live** api-usage windows with
a PROCEED/ABORT verdict, without executing a single search:

```bash
python -m va_legal_agent "service connection for tinnitus" --dry-run
python -m va_legal_agent "service connection for tinnitus" --dry-run --output-format json
```

## 5. (Optional) LLM interpretation

Set `OPENAI_API_KEY` (and optionally `OPENAI_MODEL`, `OPENAI_BASE_URL` for Azure /
compatible endpoints) and install the extra:

```bash
pip install -e ".[llm]"
```

With the key set, the narrative and a reconciling reasoning pass (contradiction
flags, per-claim citations) come from the LLM (`interpretation_source: "llm"`).
Without it, everything still works via the deterministic template. Contradiction
detection between decisions runs deterministically in both paths.

## 6. Batch operations (many issues)

The CLI analyzes **one issue per invocation**. For many issues, write them to a
file (one per line, `#` comments and blank lines skipped) and use batch mode.
Issues can carry a priority weight as `issue<TAB>priority` (lower numbers run
first).

**Plan a batch against the CourtListener daily window** (allocates quota
cumulatively across issues, no searches run):

```bash
python -m va_legal_agent --batch-dry-run --issues-file issues.txt
python -m va_legal_agent --batch-dry-run --issues-file issues.txt \
  --output-format csv --output-file plan.csv   # machine-parseable plan
```

Batch-planning flags:

```bash
--start-at 4            # resume planning from the 4th issue (1-based)
--only-blocked          # show only issues that would abort (window can't cover)
--max-batch-requests 50 # cap total CourtListener requests; rest get 'deferred'
--retry-file retry.txt  # write blocked/deferred issues, one per line
```

Blocked issues are written automatically next to the CSV (`plan.csv` →
`plan.retry.txt`) unless you pass `--retry-file off` — so a scheduler can feed
them straight back after the window resets.

**Run a batch unattended** (dry-runs, runs what fits, sleeps until the
CourtListener daily reset, repeats until everything is done; Ctrl-C to stop):

```bash
python -m va_legal_agent --auto-run --issues-file issues.txt
```

**Correlate a batch** (e.g. from a parallel orchestrator — the batch state file
is safe under concurrent writers):

```bash
python -m va_legal_agent "issue one" --run-id batch-42 --batch-size 5
python -m va_legal_agent "issue two" --run-id batch-42 --batch-size 5
# ... once 5 outcomes are recorded, a batch_summary event is emitted
```

## 7. Use it as a library

```python
from va_legal_agent.agent import analyze_cases_for_claim

analysis = analyze_cases_for_claim("service connection for tinnitus", max_results=5)
print(analysis.model_dump_json(indent=2))
```

## 8. Verify changes (development)

```bash
make test            # full suite (~11s)
make lint            # ruff static checks
make coverage        # full suite with line + branch coverage report
make mutate-check    # full mutation pass + baseline kill-property gate
make test ARGS="-k telemetry"   # pass pytest args through
```

The project holds 100% line + branch coverage, and CI enforces it
(`--cov-fail-under=100`) plus the mutation kill-property — keep both green when
you change code.

## 9. Worked examples (with expected output)

The snippets below are real output captured from this repository's configured
`.env` (`SEARCH_PROVIDERS=courtlistener,bva`, reliability block applied, OpenAI
key present). Your numbers — quota remaining, wall estimates, masked key tails —
will differ; the structure is what to expect.

### Example 1 — Inspect the resolved configuration

```bash
python -m va_legal_agent --show-config
```

```json
{
  "request_timeout_seconds": 20,
  "user_agent": "Mozilla/5.0 (compatible; VA-Legal-Agent/1.0; veterans-law research assistant)",
  "max_fetch_bytes": 20971520,
  "search_http_proxy": "",
  "batch_state_dir": "",
  "search_max_workers": 1,
  "search_delay_seconds": 3.0,
  "search_retry_attempts": 2,
  "search_backoff_base_seconds": 1.0,
  "search_backoff_max_seconds": 60.0,
  "search_min_interval_seconds": 1.0,
  "search_max_wall_seconds": 0.0,
  "search_max_refinement_rounds": 3,
  "search_providers": "courtlistener,bva",
  "search_pages_per_query": 1,
  "search_pages_per_query_by_provider": {
    "courtlistener": 1,
    "bva": 1
  },
  "search_query_variants": 3,
  "search_query_variants_by_provider": {
    "courtlistener": 0,
    "bva": 0
  },
  "search_exclude_terms": "",
  "search_max_rpm_by_provider": {
    "courtlistener": 5,
    "bva": 3
  },
  "search_bva_sitemap_scan_limit": 200,
  "search_bva_local_index_dir": ".bva_index",
  "search_bva_local_index_max_files": 0,
  "search_bva_local_index_max_age_hours": 24,
  "courtlistener_api_key": "8484...1826",
  "courtlistener_usage_guard": true,
  "citation_traversal": false,
  "citation_traverse_limit": 3,
  "search_max_results": 30,
  "enrich_case_limit": 5,
  "interpret_case_limit": 3,
  "principle_scan_limit": 5,
  "openai_api_key": "sk-w...2W3I",
  "openai_model": "qwen3.8-max",
  "openai_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
  "openai_timeout_seconds": 180.0,
  "openai_max_tokens": 1500,
  "llm_reasoning": true,
  "llm_reasoning_limit": 10,
  "deep_read": false,
  "deep_read_limit": 3,
  "deep_read_pages": 0,
  "deep_chunk_chars": 6000,
  "effective_search_providers": [
    "courtlistener",
    "bva"
  ]
}
```

Note the masked keys (`courtlistener_api_key`, `openai_api_key`), and
`effective_search_providers` next to the raw `search_providers` value — this is
how you confirm what actually runs and spot a typo'd provider.

### Example 2 — One analysis, text output

```bash
python -m va_legal_agent "service connection for tinnitus" --output-format text
```

Representative output (content varies per run; the section structure is exactly
what the renderer produces):

```text
Issue: service connection for tinnitus

Run id: 9f3c1a2b7d4e

Top cases:
- Shedden v. Principi, 381 F.3d 1163 (Fed. Cir. 2004)
- Buchanan v. Nicholson, 451 F.3d 1331 (Fed. Cir. 2006)
- Walker v. Shinseki, 708 F.3d 1331 (Fed. Cir. 2013)
- BVA Docket No. 21-1234 (2022)

Summary:
Retrieved authority consistently holds that a current diagnosis of tinnitus, combined with evidence of in-service noise exposure, supports service connection under 38 U.S.C. § 1110 where the lay statements and medical opinion satisfy the nexus requirement. Courts have also clarified the duty-to-assist limits on the VA's obligation to obtain additional examinations.



How this affects VA claims:
A veteran alleging service connection for tinnitus should expect the Board to evaluate three elements: a current diagnosis, an in-service noise exposure event, and a medical nexus. Under § 5107(b), when the evidence is in relative equipoise the benefit of the doubt goes to the veteran. Decisions granting remand typically do so because the Board failed to obtain a nexus opinion or misapplied the diagnosis requirement.

Applicable principles:
- 38 U.S.C. § 1110 - service connection requires a current disability, an in-service event, and a nexus
- 38 U.S.C. § 5107(b) - benefit of the doubt resolves equipoise in the claimant's favor
- 38 C.F.R. § 3.303(a) - chronic disease and continuity of symptomatology
- 38 C.F.R. § 3.304(f) - tinnitus associated with noise exposure

Contradictions:
- Opposite outcomes on 38 U.S.C. § 5107(b) (Shedden v. Principi vs BVA Docket No. 21-1234)

Next steps:
- Obtain a current audiometric evaluation documenting bilateral tinnitus.
- Submit military records or lay statements establishing in-service noise exposure.
- Secure a medical nexus opinion linking the diagnosis to the in-service exposure.
- File a supplemental claim if the prior decision failed to develop a nexus opinion.

Strengths:
- Binding Federal Circuit precedent covers the nexus element.
- Benefit-of-the-doubt principle is directly on point for § 5107(b).
- Search provider bva surfaced 8 results across 11 queries.
- Search provider courtlistener surfaced 33 results across 11 queries.

Gaps:
- No CAVC decision specifically addressing tinnitus diagnosis standards retrieved.
- BVA coverage of the duty-to-assist element is thin.
- Search provider bva had 2 failed query attempt(s); results may be incomplete.

Statute-outcome matrix:
  Statute                                                     Court          Fav  Unf  Unk
  38 U.S.C. § 5107(b)                                          Fed. Cir.         1    0    0
  38 U.S.C. § 5107(b)                                          BVA               0    1    0
  38 C.F.R. § 3.303                                            Fed. Cir.         2    0    1

Coverage score: 0.85 (confidence: medium)

Interpretation source: template

Search telemetry:
- bva: 11 queries, 8 results, 1 deduped, 2 failed
- courtlistener: 11 queries, 33 results, 5 deduped, 0 failed

CourtListener daily quota: 28/125 remaining (used 97; resets 2026-08-24T03:12:00+00:00).
```

Logs and the terminal `analysis_complete` / `analysis_failed` events go to
stderr, so stdout stays exactly this clean.

### Example 3 — Dry-run before spending quota

```bash
python -m va_legal_agent "service connection for tinnitus" --dry-run --output-format text
```

Real output, including the live CourtListener usage check:

```text
Dry run - no searches were executed and no analysis was produced.

Issue: service connection for tinnitus
Claim type: Compensation

Plan: 11 base queries; worst case 14 queries (3 refinement round(s) x up to 1 gap query(s) each).

Requests per provider (worst-case queries x variants x pages):
  courtlistener    14 requests (11+3 queries x 1 variant(s) x 1 page(s); pacing 12.0s/request)
  bva              14 requests (11+3 queries x 1 variant(s) x 1 page(s); pacing 20.0s/request)

Wall-time estimate (requests serialized at enforced pacing; LLM ingestion
and deep-read summarization time is not modeled):
  nominal:    7.5 min (pacing only, no failures)
  worst-case: 11.0 min (every request exhausts its retry backoff)

CourtListener quota (live api-usage endpoint):
  daily:  42 / 125 remaining (used 83)
  hourly: 50 / 50 remaining
  minute: 5 / 5 remaining
  fits the planned 14 request(s).

Verdict: PROCEED - the current windows cover the planned requests.
```

`--dry-run --output-format json` returns the same estimate as structured JSON
for CI. If the verdict were `ABORT`, the run would be stopped by the usage guard
before a single search — wait for the reset time shown, or lower the request
count (fewer providers / variants / pages).

### Example 4 — Plan a batch against the daily window

```bash
printf 'service connection for tinnitus\nhearing loss\t2\nPTSD secondary to MST\nback pain\n' > issues.txt
python -m va_legal_agent --batch-dry-run --issues-file issues.txt --max-results 30
```

Real output (issues run in priority order — `hearing loss` has priority `2` —
and the CourtListener daily window is allocated cumulatively):

```text
Batch dry run - no searches were executed and no analysis was produced.

Issues: 4   Total requests: 238   CourtListener requests: 119
CourtListener daily window: 42 remaining (125/day) - 0 left after the batch.
3 of 4 issue(s) would ABORT under the current window (wait for the reset to run them).

issue                          prio worst CL req total req   nominal worst-case daily after  verdict
hearing loss                      2    35     35        70  18.7 min   27.4 min           7  proceed
service connection for tinn...    -    14     14        28   7.5 min   11.0 min           0  abort
PTSD secondary to MST             -    35     35        70  18.7 min   27.4 min           0  abort
back pain                         -    35     35        70  18.7 min   27.4 min           0  abort
```

The same plan as a machine-parseable CSV (with an auto-written retry file):

```bash
python -m va_legal_agent --batch-dry-run --issues-file issues.txt \
  --output-format csv --output-file plan.csv
```

```csv
issue,priority,base_queries,worst_case_queries,courtlistener_requests,total_requests,nominal_wall_seconds,worst_wall_seconds,courtlistener_daily_after,verdict
hearing loss,2,8,35,35,70,1120.0,1645.0,7,proceed
service connection for tinnitus,,11,14,14,28,448.0,658.0,0,abort
PTSD secondary to MST,,8,35,35,70,1120.0,1645.0,0,abort
back pain,,8,35,35,70,1120.0,1645.0,0,abort
```

The blocked issues are written to `plan.retry.txt` next to the CSV — feed them
straight back into `--issues-file` after the window resets.

## 10. Unattended batch deployment (Linux)

The `deploy/` directory holds everything needed to run the agent as an
unattended daily batch on a Linux host (systemd is the primary path; cron is
fully supported). This is the *preferred* way to run many issues over time:
`--auto-run` executes what fits the current quota windows, sleeps through the
CourtListener daily reset, and repeats until every issue has run.

```
deploy/
  run_batch.sh                          # the shared runner (systemd AND cron use this)
  install.sh                            # one-shot installer (root or --target)
  va-legal-agent-batch.service          # systemd unit template (__TARGET__/__USER__ placeholders)
  va-legal-agent-batch.timer            # daily timer: 04:30 + random 10 min
```

### Install (systemd, recommended)

```bash
sudo deploy/install.sh
```

The installer copies the project to `/opt/va-legal-agent` (override with
`TARGET=` or `--target`), creates a venv and `pip install -e .`, creates a
dedicated system user `vaagent`, and enables the daily timer. It will **not**
overwrite an existing `.env` or `issues.txt` — it only creates them if missing.

Then, once:

```bash
sudo -u vaagent vi /opt/va-legal-agent/.env      # providers + CourtListener/OpenAI keys
vi /opt/va-legal-agent/issues.txt                # one issue per line, optional TAB priority
systemctl start va-legal-agent-batch.service     # test one pass immediately
journalctl -u va-legal-agent-batch.service -f    # watch it
```

### Install (cron)

```bash
sudo deploy/install.sh --cron
```

This prints a ready-made root crontab line (default `30 4 * * *`) that wraps the
runner in `flock` (no overlapping runs) and `runuser` (drops to `vaagent`).
Cron has no journald: point `LOG_DIR` somewhere persistent and rely on the
per-run log files.

### How `.env` is handled

- The app loads `.env` itself (python-dotenv) from the **working directory**, so
  `run_batch.sh` always `cd`s to the project root and the systemd unit sets
  `WorkingDirectory=` — no shell-level sourcing, no systemd `EnvironmentFile=`
  parsing quirks.
- `install.sh` copies `.env.example` → `.env` only if missing, then `chmod 600`s
  it. The service runs as `vaagent`, which owns the checkout, so the keys stay
  readable only by the service user.
- `RUN_ID` defaults to `batch-YYYYMMDD` for the pass (set `RUN_ID` to override).

### Daily operation

- **Logs**: `run_batch.sh` writes one timestamped `logs/batch-*.log` per pass
  (stdout + stderr, including the `analysis_complete` / `analysis_failed` /
  `batch_summary` events) and keeps the newest 14 (`LOG_KEEP`). Under systemd,
  stderr also lands in journald: `journalctl -u va-legal-agent-batch.service`.
- **Quota windows**: `--auto-run` sleeps through the CourtListener daily reset
  on its own — the daily timer is just a recovery kick if the service ever
  stops. A single pass can therefore span hours; the service has no start
  timeout (`TimeoutStartSec=0`).
- **Failure handling**: individual issue errors become `error` verdicts in the
  per-pass summary, not crashes. Real crashes (e.g. an unreachable usage
  endpoint) exit non-zero and systemd retries with `Restart=on-failure`.
- **Rotation / retry files**: batch plans write `plan.retry.txt` (or
  `issues.retry`) next to the CSV — feed those back into `issues.txt` to resume
  quota-blocked issues after the window resets.

### Runner options

```bash
MODE=plan  deploy/run_batch.sh          # plan only: --batch-dry-run, no searches
ISSUES_FILE=/path/issues.txt deploy/run_batch.sh
LOG_DIR=/var/log/va-legal-agent LOG_KEEP=30 deploy/run_batch.sh
MAX_BATCH_REQUESTS=50 deploy/run_batch.sh   # cap CourtListener requests per pass
```

`MODE=plan` is safe to run anywhere, anytime (it fetches the usage endpoint at
most once and executes no searches) — use it to preview what a pass would do
before enabling the timer.

## 11. Quick troubleshooting

| Symptom | Fix |
|---|---|
| CourtListener 401 | Set `COURTLISTENER_API_KEY` (account → Settings → API) |
| CourtListener 429s | Apply the reliability block from step 2; wait out `retry-after`; or set `--max-wall-time` so a throttled issue returns partial results at the deadline |
| DuckDuckGo challenge pages (202/403) | Pace it: `SEARCH_DELAY_SECONDS`, `SEARCH_MIN_INTERVAL_SECONDS`, or `SEARCH_MAX_RPM_BY_PROVIDER=duckduckgo=…`; space repeated runs out; switch networks if hard-403 persists |
| BVA rate-limit/anomaly pages | Retry later, or pin `SEARCH_PAGES_PER_QUERY_BY_PROVIDER=bva=1`, `SEARCH_QUERY_VARIANTS_BY_PROVIDER=bva=0`, or switch to `bvasitemap` / `bvalocal` |
| Run aborts before searching | The CourtListener usage guard (`COURTLISTENER_USAGE_GUARD=1`) estimated the run won't fit the daily window — check `--dry-run` for the reset time, or disable the guard if you know what you're doing |
| Recall looks thin | `make smoke`; raise `SEARCH_MAX_RESULTS`, add providers, or allow query variants |

Full detail on every failure mode is in README.md → Troubleshooting.
