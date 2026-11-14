# DeepSearchAgent — Audit Pass 3: Remaining Core Layer

**Scope:** the Pass 1 files that still contain open findings:
`va_legal_agent/__main__.py`, `config.py`, `models.py`, `llm.py`,
`providers.py`, `search.py`, `fetch.py`, and `deep_read.py`.

**Audited tree:** `3d36464` (`fix: complete Pass 3 audit remediation`) plus the
uncommitted Pass 2/Pass 3 work in this working tree. The operational scope of
`docs/audit-pass3.md` (`webapp.py`, `batch.py`, scripts, deploy, workflows,
Makefile, packaging/config) is already complete; this document extends Pass 3
to the remaining core-layer findings rather than renumbering or reopening the
completed operational set.

**Audit date:** 2026-11-14.

**Document status:** Pass 1 re-audit plus targeted remediation of P1-007,
P1-009, and P1-011 in this tree. The other findings are unchanged and remain
for a later pass. Finding IDs are preserved from `docs/audit-pass1.md`; no new
IDs are introduced.

**Method:** every open finding below was re-executed against the current tree
with `.venv/bin/python`, and current line numbers were read from the file. The
quoted outputs are observed output, not paraphrases. Findings that changed
since Pass 1 are marked **PARTIAL** and distinguish the part that is fixed from
the part that remains. Where a finding turns on a legal premise, the primary
authority is cited rather than a code-style preference.

## Totals

| File | Pass 1 findings | Fixed in current tree | Partial | Open |
|---|---:|---:|---:|---:|
| `va_legal_agent/__init__.py` | 0 | 0 | 0 | 0 |
| `va_legal_agent/__main__.py` | 6 | 4 | 2 | 0 |
| `va_legal_agent/config.py` | 2 | 1 | 0 | 1 |
| `va_legal_agent/models.py` | 2 | 1 | 0 | 1 |
| `va_legal_agent/llm.py` | 2 | 2 | 0 | 0 |
| `va_legal_agent/providers.py` | 6 | 1 | 0 | 5 |
| `va_legal_agent/search.py` | 3 | 0 | 0 | 3 |
| `va_legal_agent/fetch.py` | 9 | 6 | 0 | 3 |
| `va_legal_agent/deep_read.py` | 2 | 0 | 0 | 2 |
| **Total** | **32** | **14** | **2** | **16** |

The 18 still-actionable findings (16 open + 2 partial) comprise **0 High,
11 Medium, and 7 Low**. The High item from the previous pass, P1-007, is fixed.

Current verification state of the tree:

```text
$ .venv/bin/python -m pytest -q
1316 passed in 10.98s
PYTEST_EXIT=0

$ .venv/bin/python -m ruff check va_legal_agent/__main__.py \
    va_legal_agent/config.py va_legal_agent/models.py va_legal_agent/llm.py \
    va_legal_agent/providers.py va_legal_agent/search.py \
    va_legal_agent/fetch.py va_legal_agent/deep_read.py
All checks passed!
RUFF_EXIT=0

$ git diff --check
DIFF_CHECK_EXIT=0
```

## Fixed since Pass 1 (re-verified)

These findings are no longer actionable in the audited tree. The probes were
run on 2026-11-14; the existing regressions named below remain in the suite.

| ID | Current evidence |
|---|---|
| P1-001 | `_mask_proxy_url` handles a scheme-less authority at `__main__.py:218-245`; `_mask_proxy_url("user:pass@proxy.example:8080")` → `***@proxy.example:8080`. |
| P1-002 | `_mask_secret` fully masks values of 16 characters or fewer at `__main__.py:187-196`; `_mask_secret("abcd1234e")` → `***`. |
| P1-003 | A malformed proxy port no longer escapes `--show-config`: `__main__.py:230-236` omits the port after `ValueError`; probe → `http://***@proxy.example`. |
| P1-004 | `batch_dry_run` rejects a non-positive cap at `__main__.py:784-785`; pinned by `test_max_batch_requests_must_be_positive` (`tests/test_cli.py:1824`). |
| P1-007 | `env_float` rejects `nan`/`inf`/`1e309` and caps sleep/backoff/timeout settings at 3600 s (`config.py:31, 94-125`); `SEARCH_MAX_WALL_SECONDS` remains finite-only without the sleep cap. Probe: `nan`, `inf`, and `3600.1` all fall back to the default. Pinned by `test_env_float_rejects_non_finite_values`, `test_env_float_rejects_values_above_the_sleep_cap`, and `test_search_pacing_float_is_capped_at_the_config_call_site`. |
| P1-009 | `precedential` is now tri-state (`None` = unknown) at `models.py:31-38`; empty extraction returns `None` (`fetch.py:594-616`); ranking, interpretation, summary, impact, and the reasoning prompt distinguish unknown from non-precedential. Pinned by `test_case_record_precedential_defaults_to_unknown`, `test_extract_precedential_status_unknown_for_empty_text`, `test_unknown_precedential_is_not_penalised_but_is_flagged`, `test_authority_note_unknown_precedential_is_not_binding`, `test_confidence_unknown_precedential_is_not_binding`, and `test_analysis_summary_flags_unknown_precedential`. |
| P1-011 | Narrative, reasoning, and deep-read reduce prompts now escape and delimit untrusted case text and state the evidence-only rule in the system prompt (`llm.py:22-63, 100-177`; `deep_read.py:135-168`). Pinned by `test_build_messages_delimit_and_escape_untrusted_case_text`, `test_build_reasoning_messages_delimit_and_escape_untrusted_text`, and `test_build_reduce_prompt_delimits_and_escapes_untrusted_text`. |
| P1-013 | `_minimize_bva_query` keeps a non-empty statute-only fallback; pinned by `test_minimize_bva_query_statute_only_anchor` (`tests/test_providers.py:1128`) and the provider-boundary tests. |
| P1-022 | Outcome extraction reads only the disposition block; pinned by `test_extract_outcome_reads_only_the_order_disposition` (`tests/test_fetch.py:1959`). |
| P1-023 | Precedential status is read from the header; pinned by `test_extract_precedential_status_reads_the_header_not_the_body` (`tests/test_fetch.py:2001`). |
| P1-024 | Plural section citations are expanded by `_section_values` at `fetch.py:330-338`; pinned by `test_extract_statutes_matches_plural_section_citations` (`tests/test_fetch.py:2060`). |
| P1-025 | A decision's own citation is preferred over a quoted case; pinned by `test_extract_citation_prefers_own_citation_over_quoted_case` (`tests/test_fetch.py:2136`). |
| P1-026 | Subsection normalization remains in `_normalize_statute_value` at `fetch.py:313-321`; covered by the fetch statutory-citation tests. |
| P1-029 | `_merge_statute_families` now reserves C.F.R. slots at `fetch.py:341-355`. Probe with mixed statutory families: `extract_statutes("38 U.S.C. §§ 1110, 1131, 5103, 5103A, 7104, 7252; 38 C.F.R. §§ 3.303, 3.304")` → `['38 U.S.C. § 1110', '38 U.S.C. § 1131', '38 U.S.C. § 5103', '38 U.S.C. § 5103A', '38 C.F.R. § 3.303', '38 C.F.R. § 3.304']`; pinned by `test_extract_statutes_keeps_cfr_sections_under_the_cap` (`tests/test_fetch.py:2040`). |

---

## Still actionable

### P1-005 — Observability — Medium — **PARTIAL**

**Location:** `va_legal_agent/__main__.py:1113-1115` (catch site) and
`va_legal_agent/__main__.py:130-148` (`_emit_failure_event`).

**Current code:**

```python
                except Exception as exc:  # noqa: BLE001 - per-issue failure is recorded
                    _emit_failure_event(issue, exc, run_id)
                    results[issue] = "error"
```

```python
    _EVENTS_LOGGER.error(
        "analysis failed for %s (run=%s): %s",
        issue,
        run_id,
        error,
        extra={
            "event": "analysis_failed",
            "run_id": run_id,
            "issue": issue,
            "error": str(error),
        },
    )
```

**What changed since Pass 1:** failures are no longer silent. The event logger
now emits `analysis_failed`, so an unattended batch has a terminal signal and
the exception message.

**Executed evidence:**

```python
# analyze_cases_for_claim monkeypatched to raise RuntimeError("boom-in-analysis")
>>> _auto_run(["x"], "Compensation", 1, None)
[{'issue': 'x', 'verdict': 'error'}]
>>> captured_record.getMessage()
'analysis failed for x (run=): boom-in-analysis'
>>> bool(captured_record.exc_info)
False
```

**What remains:** neither the exception type nor a traceback is recorded, and
the per-issue result row is still only `{"issue": ..., "verdict": "error"}`.
A systematic `KeyError`, `TypeError`, or provider-specific exception therefore
still cannot be diagnosed from the batch summary without reproducing it.

**Remediation:** use `_EVENTS_LOGGER.exception(...)` (or include
`type(error).__name__` and `traceback.format_exc()` in `extra`) while keeping
the existing event name and `error` field for log consumers. Add an `error`
field to the per-issue row and summary, then pin it with a test that captures
both a `RuntimeError` event and the summary row.

---

### P1-006 — Batch orchestration — Medium — **PARTIAL**

**Location:** `va_legal_agent/__main__.py:1151-1160` (`_auto_run`).

**Current code:**

```python
        try:
            usage = fetch_courtlistener_usage()
            daily = courtlistener_daily_budget(usage)
        except SearchError as exc:
            logger.error(
                "auto-run stopped: could not determine the next quota window: %s", exc
            )
            break
```

**What changed since Pass 1:** the `SearchError` no longer escapes as a
traceback. The batch stops cleanly and logs why.

**Executed evidence:**

```python
# batch_dry_run returns one abort row; fetch_courtlistener_usage raises SearchError
>>> _auto_run(["x"], "Compensation", 1, None)
[{'issue': 'x', 'verdict': 'abort'}]
stderr: auto-run stopped: could not determine the next quota window: usage down
```

**What remains:** the `break` still abandons every pending issue. The Pass 1
remediation was a bounded fallback sleep followed by re-planning, not merely a
clean exit. A transient 401, network blip, or malformed usage payload therefore
still turns a resumable batch into an incomplete run.

**Remediation:** when the usage lookup fails, record the failure and take one
bounded fallback wait (`min(60, wait_limit)`), then re-plan. Keep the existing
stall and iteration counters so an unavailable endpoint cannot spin forever;
abort only after repeated consecutive lookup failures. Skip the quota wait
entirely when CourtListener is not a configured provider or the backlog is
cap-deferred (the latter already breaks earlier at lines 1132-1148).

---

### P1-008 — Configuration — Low — **OPEN**

**Location:** `va_legal_agent/config.py:58-85` (`env_provider_int_map`).

**Current code:**

```python
        result[name.strip()] = count
    return result
```

**Executed evidence:**

```python
>>> env_provider_int_map("bvaa=0,courtlistener=5")
{'bvaa': 0, 'courtlistener': 5}
```

**Impact:** provider names are never checked against the provider registry.
`bvaa=1` is accepted, displayed by `--show-config`, and silently has no effect;
the real `bva` provider keeps the global setting. The operator debugs recall
against a phantom override.

**Remediation:** validate keys against `_PROVIDERS` in `providers.py` with a
lazy import (to avoid a config/providers cycle), log and skip unknown names,
and add a regression for `bvaa=0`.

---

### P1-010 — Data integrity — Low — **OPEN**

**Location:** `va_legal_agent/models.py:133`.

**Current code:**

```python
    coverage_score: float = 0.0
```

**Executed evidence:**

```python
>>> LegalAnalysis(issue="i", summary="s", how_it_affects_va_claims="h",
...               coverage_score=7.5).coverage_score
7.5
>>> LegalAnalysis(..., coverage_score=-2.0).coverage_score
-2.0
```

**Impact:** there is no bounds validation. The report formats the value as
`{:.2f}` and labels it a score, so a pipeline bug that assigns `7.5` or `-2`
produces “Coverage score: 7.50” or a negative score that reads as valid
output.

**Remediation:** `coverage_score: float = Field(default=0.0, ge=0.0, le=1.0)`
and clamp at the producer as well, so a scoring bug is visible rather than
silently accepted. Add tests for out-of-range construction.

---

### P1-012 — Configuration — Low — **OPEN**

**Location:** `va_legal_agent/llm.py:23-24` (`llm_enabled`).

**Current code:**

```python
def llm_enabled() -> bool:
    return bool(get_settings().openai_api_key)
```

**Executed evidence:**

```python
>>> os.environ["OPENAI_API_KEY"] = "   "; llm_enabled()
True
```

**Impact:** a whitespace-only key is truthy, so every analysis attempts an LLM
call, fails authentication, and logs a warning instead of taking the documented
“no key → template path” route. In batch runs this adds a failed network call
per issue and a misleading “LLM interpretation failed” trail.

**Remediation:** `return bool((get_settings().openai_api_key or "").strip())`,
with a test for whitespace-only and empty values.

---

### P1-014 — Legal accuracy — Medium — **OPEN**

**Location:** `va_legal_agent/providers.py:250-310`
(`_minimize_courtlistener_query`), used with
`va_legal_agent/providers.py:69-79` (`_courtlistener_query`).

**Current code:**

```python
    words = leading.split()
    while len(words) > 1 and words[-1].lower() in _BVA_BOILERPLATE:
        words.pop()
    return " ".join(words)
```

```python
    clause = "court_id:(" + " OR ".join(_CL_COURT_NAMES) + ")"
    return f"({query}) AND {clause}" if query else clause
```

**Executed evidence:**

```python
>>> _minimize_courtlistener_query('""')
''
>>> _courtlistener_query("")
'court_id:(cavc OR cafc OR scotus)'
```

**Impact:** an empty quoted query, or one whose quoted fragments are all
boilerplate, minimizes to `""`. The provider then emits only the court clause.
CourtListener returns its default ordering for those courts — arbitrary recent
opinions, not matches for the issue — and those cases receive full relevance
scores. A zero-term query is presented as a successful search.

**Remediation:** raise `SearchError("empty query after adaptation")` when the
minimized query is empty, rather than issuing a court-only search, or return
the stripped original query. Put the check at the provider boundary as well,
and add a regression for `CourtListenerProvider.search` with `""`.

---

### P1-015 — Search quality — Medium — **OPEN**

**Location:** `va_legal_agent/providers.py:1620-1639`
(`_filter_excluded_terms`).

**Current code:**

```python
    """Remove results whose title or snippet contains any excluded term.

    Matching is case-insensitive and whole-word (\b-bounded). ...
    """
    ...
    pattern = re.compile("|".join(re.escape(t) for t in tokens), re.IGNORECASE)
```

**Executed evidence:**

```python
>>> _filter_excluded_terms(
...     [{"title": "Background on tinnitus", "snippet": ""}], "back"
... )
[]
```

**Impact:** the docstring promises whole-word matching but the pattern has no
word boundaries. `SEARCH_EXCLUDE_TERMS=back` also removes every result
mentioning “background”, “backlog”, or “feedback” — including relevant
decisions whose facts section opens “Background”. Exclusion silently reduces
recall with no way to see why.

**Remediation:** compile
`r"\b(?:" + "|".join(re.escape(t) for t in tokens) + r")\b"` so the
implementation matches the documented contract. Add a test that “background”
survives `back` and that a true “back condition” result is still removed.

---

### P1-016 — Reliability — Medium — **OPEN**

**Location:** `va_legal_agent/providers.py:621-631`
(`courtlistener_minute_budget`); caller at `providers.py:683-701`.

**Current code:**

```python
    try:
        return _extract_rate_row(usage, _MINUTE_RATE_PATTERN, "per-minute")
    except SearchError:
        return {}  # absent minute row is non-fatal; callers check remaining
```

**Executed evidence:**

```python
>>> courtlistener_minute_budget({"current_usage": []})
{}
```

**Impact:** the extractor converts every payload problem into `{}`, including a
completely malformed response with no `current_usage` list. The caller treats
`{}` as “tier has no minute row — skip the guard” (`providers.py:697-699`), so a
changed or broken API shape silently disables the deep-read pre-flight and the
run proceeds into 429 backoffs. The comment's claim that callers “check
remaining” is false: the empty-dict branch returns before any check.

**Remediation:** distinguish “no minute row” from “malformed payload”. Return
`None` for an absent row and raise a `SearchError` for a missing/invalid
`current_usage`; make `check_courtlistener_minute_budget` check `is None`
explicitly. Add tests for both shapes.

---

### P1-017 — Robustness — Low — **OPEN**

**Location:** `va_legal_agent/providers.py:634-659` (`_extract_rate_row`).

**Current code:**

```python
            return {
                "used": int(row.get("used", 0) or 0),
                "limit": int(row.get("limit", 0) or 0),
                "remaining": int(row.get("remaining", 0) or 0),
                "reset_at": row.get("reset_at"),
            }
```

**Executed evidence:**

```python
>>> _extract_rate_row(
...     {"current_usage": [{"scope": "user", "rate": "1/day", "used": "abc"}]},
...     _DAILY_RATE_PATTERN, "daily",
... )
ValueError: invalid literal for int() with base 10: 'abc'
```

**Impact:** a non-numeric field in the external usage payload raises a bare
`ValueError`, not `SearchError`. Callers catch `SearchError`, so the CLI dies
with a traceback instead of the intended “could not read usage” degradation.

**Remediation:** coerce via a helper that catches `TypeError`/`ValueError` and
raises `SearchError(f"malformed {label} usage row: {row!r}")`, so every
caller's existing handling applies. Add a test with a string `used` value.

---

### P1-018 — Robustness — Low — **OPEN**

**Location:** `va_legal_agent/providers.py:1273-1279`
(`_bva_local_needs_rebuild`).

**Current code:**

```python
        try:
            build_time = datetime.fromisoformat(build_time_str)
        except ValueError:
            return True
        age = (datetime.now(timezone.utc) - build_time).total_seconds()
```

**Executed evidence:**

```python
# index.meta.json: {"build_time": "2020-01-01T00:00:00", ...}
>>> _bva_local_needs_rebuild(tmpdir, max_age_hours=1)
TypeError: can't subtract offset-naive and offset-aware datetimes
```

**Impact:** `fromisoformat` happily parses a naive timestamp (hand-written
meta, older build, restored backup). Subtracting it from an aware `now` raises
`TypeError`, which is not caught, so the local-index provider crashes on first
use instead of rebuilding. This is exactly the recovery scenario the freshness
check exists to handle.

**Remediation:** before subtracting, if `build_time.tzinfo is None`, replace it
with `timezone.utc`; treat `TypeError` as “stale, rebuild” as defence in depth.
Add a test with a naive `build_time`.

---

### P1-019 — Search reliability — Medium — **OPEN**

**Location:** `va_legal_agent/search.py:39-41` (`_is_challenge`).

**Current code:**

```python
    return response.status_code == 202 or "anomaly" in response.text.lower()
```

**Executed evidence:**

```python
>>> _is_challenge(SimpleNamespace(
...     status_code=200,
...     text="there is no anomaly in this article",
... ))
True
```

**Impact:** the marker is matched anywhere in the whole page, including result
titles and snippets. A valid results page for a query about anomaly detection
is classified as a challenge, so `DuckDuckGoProvider.search` discards the
parsed results, backs off, retries, and eventually raises. The failure is
query-dependent and looks like throttling, making it hard to diagnose.

**Remediation:** detect the challenge structurally — 202 status, a redirect to
`/anomaly`, or the challenge form/captcha element — rather than a body
substring. If a text marker is kept, require it in the page title or a
challenge-specific container, and add a results-page regression containing the
word “anomaly”.

---

### P1-020 — Security — Medium — **OPEN**

**Location:** `va_legal_agent/search.py:151-167`
(`extract_url_from_duckduckgo`); the URL is later fetched by
`fetch.py:713-732` (`fetch_full_text`) and `fetch.py:759-783`
(`fetch_case_details`).

**Current code:**

```python
    if is_ddg_redirect:
        params = parse_qs(parsed.query)
        if "uddg" in params:
            return params["uddg"][0]
    return link
```

**Executed evidence:**

```python
>>> extract_url_from_duckduckgo(
...     "//duckduckgo.com/l/?uddg=javascript%3Aalert%281%29"
... )
'javascript:alert(1)'
```

**Impact:** the decoded destination is returned with no scheme validation. A
crafted DuckDuckGo redirect (or a scraped page whose link is manipulated) can
yield `javascript:`, `data:`, or `file:` URLs. `_parse_results` rejects only
`duckduckgo.com` hosts; the URL is then stored as a case URL and later passed
to `curl_cffi.get`, creating an unvalidated-scheme fetch path driven by
third-party content.

**Remediation:** after decoding, accept only `http`/`https` with a non-empty
netloc (`urlsplit(...).scheme in {"http", "https"}`); otherwise drop the
result. Apply the same check in `fetch_full_text`/`fetch_case_details` as
defence in depth, and add a regression for the `javascript:` payload.

---

### P1-021 — Search semantics — Low — **OPEN**

**Location:** `va_legal_agent/search.py:203-205` (`_parse_results`).

**Current code:**

```python
    if not results:
        raise SearchError(f"No search results returned for query: {query}")
    return results
```

**Executed evidence:**

```python
>>> _parse_results(SimpleNamespace(text="<html></html>"), "q", 5)
SearchError: No search results returned for query: q
```

**Impact:** a legitimate zero-result page is indistinguishable from a provider
failure. `search_all` counts it in `failures`, `recall_flags` emits “had N
failed query attempt(s); results may be incomplete”, and the run's error list
grows — so a genuinely narrow issue looks like a broken provider, and the last
such error is re-raised when nothing merged. Telemetry cannot answer “did this
query simply have no hits?”.

**Remediation:** return `[]` for a parsed page with no result blocks and let
`search_all` record zero results (it already handles an empty list); reserve
`SearchError` for transport, challenge, and parse failures. Add a zero-result
telemetry test.

---

### P1-027 — Legal accuracy — Medium — **OPEN**

**Location:** `va_legal_agent/fetch.py:240-254` (`extract_legal_standard`).

**Current code:**

```python
    for pattern, label in _STANDARD_REVIEW_PATTERNS:
        if pattern.search(text):
            return label
    return ""
```

**Executed evidence:**

```python
>>> extract_legal_standard(
...     "We review legal questions de novo. "
...     "The Board factual findings require clear error."
... )
'clear error'
```

**Impact:** patterns are tried in tuple order, not document order, and the
clear-error group is first. A decision that opens with de novo review but later
mentions a clear-error argument is recorded as reviewed for **clear error** —
the more deferential standard — even though the court stated de novo. That
inverts the strength of the precedent, which is exactly what `legal_standard`
is used to compare across cases. The distinction matters under
38 U.S.C. § 7261(a)(1) and (a)(4): legal questions are decided de novo, while
factual findings are reviewed for substantial evidence.

**Remediation:** scan for the earliest match position across all patterns
(`min` over `match.start()`), using tuple order only to break ties at the same
offset, and add a test with both standards in document order.

---

### P1-028 — Legal accuracy — Medium — **OPEN**

**Location:** `va_legal_agent/fetch.py:125-137` (`extract_decision_date`);
`_DATE_HINT` at `fetch.py:37`.

**Current code:**

```python
    for match in _DATE_PATTERN.finditer(text):
        context = text[max(0, match.start() - 40):match.start()]
        if _DATE_HINT.search(context):
            iso = _match_to_iso(match)
            if iso:
                return iso
```

**Executed evidence:**

```python
>>> extract_decision_date(
...     "Filed: January 2, 2024\nDecided: January 3, 2023"
... )
'2024-01-02'
```

**Impact:** `_DATE_HINT` treats `filed` as a decision-date hint with equal
weight to `decided`. Court pages routinely show both; because the first hinted
date wins, the **filing** date is stored as the decision date. Recency ranking,
the “decided <date>” line in the LLM prompt, and the report all then misdate
the authority by more than a year.

**Remediation:** rank hints (`decided` > `issued` > `filed` > `handed down`):
first scan for `decided|issued`, then fall back to any hinted date, then to the
first parseable date. Add a test with both dates present in the filing-first
order.

---

### P1-030 — Legal accuracy — Medium — **OPEN**

**Location:** `va_legal_agent/fetch.py:669-675` (`_extract_pdf_text`) and
`fetch.py:790-794` (`fetch_case_details`).

**Current code:**

```python
def _extract_pdf_text(content: bytes, max_pages: int = 4) -> str:
    """Extract text from a PDF, reading up to *max_pages* pages (0 = all)."""
```

```python
    if looks_pdf:
        return extract_case_details(_extract_pdf_text(content))
```

**Executed evidence:**

```python
# pypdf.PdfReader patched to a fake 6-page document
>>> _extract_pdf_text(b"x")
'page1 page2 page3 page4'
>>> _extract_pdf_text(b"x", max_pages=0)
'page1 page2 page3 page4 page5 page6'
```

**Impact:** the enrichment path calls `_extract_pdf_text` with its default of
four pages, while `fetch_full_text` deliberately reads all pages
(`max_pages=0`). A CAVC PDF whose holding or disposition appears on page 5+ is
enriched with empty `holding`, `outcome`, and `statutes` — the case looks like
a header-only document, and those fields feed ranking and the report as if the
decision said nothing.

**Remediation:** pass an explicit page budget from settings (for example
`MAX_FETCH_PDF_PAGES`, default higher than 4) or `max_pages=0` for enrichment,
and record truncation on the case (e.g. `detail_truncated=True`) so a partial
read is never presented as complete. Add a fake-PDF regression covering page 5.

---

### P1-031 — Extraction — Medium — **OPEN**

**Location:** `va_legal_agent/deep_read.py:49-54` (`_FACT_HEADER_RE`) and
`deep_read.py:57-69` (`_extract_chunk_facts`).

**Current code:**

```python
_FACT_HEADER_RE = re.compile(
    ...
    r"(.*?)(?:\n\n|\n(?=(?:[IVXLCDM]+\.(?:[A-Z]\.)?|[A-Z]\.[A-Z]?|\d+\.(?:\d+\.)?)\s+|(?:FINDINGS|CONCLUSIONS|ANALYSIS|DISCUSSION|ARGUMENT|REASONS|DETERMINATION|DECISION|ORDER)))",
    re.IGNORECASE | re.DOTALL,
)
```

**Executed evidence:**

```python
>>> _extract_chunk_facts(
...     "BACKGROUND\nThe veteran served in Vietnam and was diagnosed with tinnitus."
... )
''
>>> _extract_chunk_facts(
...     "BACKGROUND\nThe veteran served in Vietnam and was diagnosed with tinnitus.\n\n"
... )
'The veteran served in Vietnam and was diagnosed with tinnitus.'
```

**Impact:** the body group is terminated only by a blank line or a following
section header. When the facts section is the last block in a chunk and the
text was normalized so it ends without a blank line (common when a chunk
boundary falls inside a section), the regex cannot match at all and the facts —
the veteran's circumstances, the most useful part of a deep-read digest — are
silently dropped.

**Remediation:** make the terminator optional — add `|$` to the non-capturing
group — so end-of-chunk ends the body. Add tests for the end-of-string case and
for a chunk boundary immediately after the facts sentence.

---

### P1-032 — Output quality — Low — **OPEN**

**Location:** `va_legal_agent/deep_read.py:31`, `116-133`
(`_digest_chunk`), and `156-167` (`_synthesize_case`).

**Current code:**

```python
_EMPTY_DIGEST = "(nothing relevant)"
...
    if not parts:
        return _EMPTY_DIGEST
...
    return "\n".join(f"- {digest}" for digest in digests)
```

**Executed evidence:**

```python
>>> _digest_chunk("")
'(nothing relevant)'
>>> _synthesize_case("T", "i", [_digest_chunk("")])
'- (nothing relevant)'
```

**Impact:** `_digest_chunk` returns the sentinel for a chunk with no facts,
holdings, outcome, or statutes, and the no-LLM path joins it like real content.
A case whose chunks are mostly procedural therefore produces a deep summary
that is a list of `- (nothing relevant)` lines, which the reasoning prompt and
report treat as the case's digest.

**Remediation:** filter `_EMPTY_DIGEST` entries before joining and return `""`
when nothing remains, so the caller's snippet-based fallback is taken. Preserve
the sentinel for the chunk-level digest if other consumers need it, but do not
emit it in the final summary. Add a test mixing one empty and one real digest.

---

## Cross-cutting observations

1. **Validation boundaries are inconsistent.** P1-008 (provider map), P1-010
   (`coverage_score`), P1-012 (whitespace key), and P1-017 (rate fields) are
   all cases where external or operator input crosses a boundary without a
   single validated type. P1-007 now validates the float boundary; the same
   treatment should be applied to the remaining adapters.

2. **External payloads still need defence in depth.** P1-011 now delimits
   and escapes case text in the narrative, reasoning, and deep-read reduce
   prompts; P1-020 remains open because a decoded DuckDuckGo destination is
   returned without a scheme check. The URL issue needs validation at the
   producer and again before any fetch.

3. **“Unknown” is now explicit for precedential status.** P1-009 uses `None`
   for an unread body and carries that state through ranking, interpretation,
   summary, impact notes, and prompts. The invariant to preserve is that an
   unknown status is neither binding nor penalized; check any new consumer for
   truthiness that would collapse `None` into `False`.

4. **The U.S.C./C.F.R. fix is the model for the rest of the legal-accuracy
   items.** P1-029 now reserves slots per family instead of letting one family
   consume the cap. P1-027, P1-028, P1-030, and P1-031 are analogous
   “first/truncated signal wins” defects; each needs a test that puts the
   disfavored but correct signal later in the input.

## Verification appendix

All probes were run from the repository root with the project virtualenv:

```bash
.venv/bin/python - <<'PY'
from va_legal_agent.config import env_provider_int_map, get_settings
from va_legal_agent.models import CaseRecord, LegalAnalysis
from va_legal_agent.llm import llm_enabled, _build_messages
from va_legal_agent.search import _is_challenge, extract_url_from_duckduckgo, _parse_results
from va_legal_agent.providers import (
    _minimize_courtlistener_query, _courtlistener_query,
    _filter_excluded_terms, courtlistener_minute_budget, _extract_rate_row,
    _DAILY_RATE_PATTERN, _bva_local_needs_rebuild,
)
from va_legal_agent.fetch import (
    extract_legal_standard, extract_decision_date, extract_statutes,
    _extract_pdf_text,
)
from va_legal_agent.deep_read import _extract_chunk_facts, _digest_chunk, _synthesize_case
PY
```

The full suite, Ruff, and `git diff --check` results are recorded in the
“Totals” section above. The three remediations changed `config.py`,
`models.py`, `fetch.py`, `agent.py`, `ranking.py`, `interpretation.py`,
`impact.py`, and `llm.py`, plus their focused tests; no other finding was
remediated in this pass.

## Bottom line

Pass 3's operational findings remain complete. The remaining core layer is not
clean: **14 of 32 Pass 1 findings are fixed, 2 are partially mitigated, and 16
remain open.** No High item remains. The highest-impact remaining legal item is
P1-027 (out-of-order legal-standard extraction); the highest-impact remaining
security item is P1-020 (unvalidated DuckDuckGo destination schemes). The next
remediation pass should start with the shared input boundaries (P1-008, P1-010,
P1-012, P1-017), then the legal-accuracy ordering/truncation defects (P1-027,
P1-028, P1-030, P1-031), and then the search robustness items (P1-014 through
P1-021).
