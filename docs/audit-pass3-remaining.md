# DeepSearchAgent — Audit Pass 3: Remaining Core Layer

**Scope:** the Pass 1 files covered by this remaining-core audit:
`va_legal_agent/__main__.py`, `config.py`, `models.py`, `llm.py`,
`providers.py`, `search.py`, `fetch.py`, and `deep_read.py`.

**Audited tree:** `4d89774` (`fix: finish audit remediation and reconcile audit docs`)
plus the current P1 remediation changes in this working tree. The operational scope of
`docs/audit-pass3.md` (`webapp.py`, `batch.py`, scripts, deploy, workflows,
Makefile, packaging/config) is already complete; this document extends Pass 3
to the remaining core-layer findings rather than renumbering or reopening the
completed operational set.

**Audit date:** 2026-11-14.

**Document status:** Pass 1 re-audit plus targeted remediation of all 32 Pass 1
findings in this tree, including the final seven Low items P1-008, P1-010,
P1-012, P1-017, P1-018, P1-021, and P1-032. No finding remains actionable.
Finding IDs are preserved from `docs/audit-pass1.md`; no new IDs are introduced.

**Method:** every finding in this scope was re-executed against the current tree
with `.venv/bin/python`, and current line numbers were read from the file. The
quoted outputs are observed output, not paraphrases. Findings fixed since
Pass 1 are recorded in the evidence table; no partial findings remain in this
pass. Where a finding turns on a legal premise, the primary
authority is cited rather than a code-style preference.

## Totals

| File | Pass 1 findings | Fixed in current tree | Partial | Open |
|---|---:|---:|---:|---:|
| `va_legal_agent/__init__.py` | 0 | 0 | 0 | 0 |
| `va_legal_agent/__main__.py` | 6 | 6 | 0 | 0 |
| `va_legal_agent/config.py` | 2 | 2 | 0 | 0 |
| `va_legal_agent/models.py` | 2 | 2 | 0 | 0 |
| `va_legal_agent/llm.py` | 2 | 2 | 0 | 0 |
| `va_legal_agent/providers.py` | 6 | 6 | 0 | 0 |
| `va_legal_agent/search.py` | 3 | 3 | 0 | 0 |
| `va_legal_agent/fetch.py` | 9 | 9 | 0 | 0 |
| `va_legal_agent/deep_read.py` | 2 | 2 | 0 | 0 |
| **Total** | **32** | **32** | **0** | **0** |

All 32 findings are fixed; **0 High, 0 Medium, and 0 Low remain open**, and no
finding is partially mitigated. The previous pass's High item, P1-007, is
fixed.

Current verification state of the tree:

```text
$ .venv/bin/python -m pytest -q
1393 passed in 11.24s
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
| P1-005 | Analysis failures return a per-issue row carrying `error`, `error_type`, and the full `traceback` (`__main__.py:1126-1134`); the `analysis_failed` event carries the same fields plus `exc_info` (`:138-160`), and `_auto_run_summary` exposes an `errors` list (`:1232-1255`). Pinned by `test_auto_run_analysis_failure_retains_type_and_traceback` and `test_auto_run_analyze_failure_records_error`. |
| P1-006 | A transient `SearchError` from `fetch_courtlistener_usage` is logged, followed by one bounded fallback wait (`min(60, wait_limit)`) and a re-plan; a second consecutive failure stops with the pending rows (`__main__.py:1181-1208`). Pending batches without CourtListener skip the quota wait (`:1171-1180`). Pinned by `test_auto_run_retries_transient_usage_failure_after_bounded_wait`, `test_auto_run_usage_failure_retries_then_stops_with_pending`, and `test_auto_run_without_courtlistener_skips_usage_lookup`. |
| P1-007 | `env_float` rejects `nan`/`inf`/`1e309` and caps sleep/backoff/timeout settings at 3600 s (`config.py:31, 94-125`); `SEARCH_MAX_WALL_SECONDS` remains finite-only without the sleep cap. Probe: `nan`, `inf`, and `3600.1` all fall back to the default. Pinned by `test_env_float_rejects_non_finite_values`, `test_env_float_rejects_values_above_the_sleep_cap`, and `test_search_pacing_float_is_capped_at_the_config_call_site`. |
| P1-008 | `env_provider_int_map` lazily imports the provider registry and skips keys not in `_PROVIDERS` (`config.py:64-103`). Probe: `env_provider_int_map("bvaa=0,courtlistener=5")` → `{'courtlistener': 5}`. Pinned by `test_env_provider_int_map_skips_malformed_entries`. |
| P1-009 | `precedential` is now tri-state (`None` = unknown) at `models.py:31-38`; empty extraction returns `None` (`fetch.py:594-616`); ranking, interpretation, summary, impact, and the reasoning prompt distinguish unknown from non-precedential. Pinned by `test_case_record_precedential_defaults_to_unknown`, `test_extract_precedential_status_unknown_for_empty_text`, `test_unknown_precedential_is_not_penalised_but_is_flagged`, `test_authority_note_unknown_precedential_is_not_binding`, `test_confidence_unknown_precedential_is_not_binding`, and `test_analysis_summary_flags_unknown_precedential`. |
| P1-010 | `LegalAnalysis.coverage_score` is bounded to 0–1 (`models.py:134`), and the producer clamps the weighted ratio before constructing it (`interpretation.py:554-562`). Probes: `coverage_score=7.5` and `coverage_score=-2.0` both raise `ValueError`. Pinned by `test_legal_analysis_rejects_out_of_range_coverage_scores`. |
| P1-011 | Narrative, reasoning, and deep-read reduce prompts now escape and delimit untrusted case text and state the evidence-only rule in the system prompt (`llm.py:22-63, 100-177`; `deep_read.py:135-168`). Pinned by `test_build_messages_delimit_and_escape_untrusted_case_text`, `test_build_reasoning_messages_delimit_and_escape_untrusted_text`, and `test_build_reduce_prompt_delimits_and_escapes_untrusted_text`. |
| P1-012 | `llm_enabled` strips the configured key before testing it (`llm.py:48-49`), so whitespace-only and empty values take the template path; `call_openai` now passes that same stripped key to the client (`llm.py:74`). Pinned by `test_llm_enabled_requires_a_non_whitespace_key` and the padded-key assertion in `test_call_openai_success_with_real_client_body`. |
| P1-013 | `_minimize_bva_query` keeps a non-empty statute-only fallback; pinned by `test_minimize_bva_query_statute_only_anchor` (`tests/test_providers.py:1128`) and the provider-boundary tests. |
| P1-014 | `CourtListenerProvider.search` raises `SearchError("empty query after CourtListener adaptation")` before building params or making a request when minimization empties the query (`providers.py:407-409`). Pinned by `test_courtlistener_search_rejects_empty_adapted_query`. |
| P1-015 | `_filter_excluded_terms` compiles a whole-word regex, so `back` removes a true `Back condition` result but preserves `Background` and `Feedback` (`providers.py:1638-1642`). Pinned by `test_filter_excluded_terms_uses_whole_word_boundaries`. |
| P1-016 | `courtlistener_minute_budget` returns `None` for a well-formed payload with no minute row and propagates `SearchError` for a missing/non-list `current_usage`; the caller checks `is None` (`providers.py:628-665`). Pinned by `test_courtlistener_minute_budget_returns_none_when_absent` and `test_courtlistener_minute_budget_raises_on_malformed_payload`. |
| P1-017 | `_usage_counter` coerces `used`/`limit`/`remaining` and raises `SearchError("malformed daily usage row: ...")` for non-numeric values (`providers.py:640-675`). Pinned by `test_courtlistener_daily_budget_rejects_non_numeric_counter`. |
| P1-018 | A naive `build_time` is interpreted as UTC, and an un-ageable timestamp is treated as stale (`providers.py:1289-1305`). Pinned by `test_naive_build_time_is_treated_as_utc`. |
| P1-019 | `_is_challenge` now keys on 202, an `/anomaly` redirect, a DuckDuckGo anomaly title, and challenge/captcha containers rather than any body substring (`search.py:39-70`). Pinned by `test_is_challenge_ignores_anomaly_word_in_results_text` and the existing 202/retry tests. |
| P1-020 | Decoded DuckDuckGo destinations are restricted to absolute HTTP(S) URLs with a host (`search.py:180-206`), and `_require_http_url` rejects non-HTTP(S) URLs in both fetch entry points (`fetch.py:31-35, 731, 776`). Pinned by `test_extract_url_drops_non_http_redirect_destination` and `test_fetch_rejects_non_http_url_before_network`. |
| P1-021 | A parsed DuckDuckGo page with no result blocks returns `[]` (`search.py:242-245`); `search_all` records it as a successful query with zero results rather than a failure. Pinned by `test_search_web_returns_empty_when_no_results` and the telemetry assertion in `test_search_all_returns_empty_when_no_results_no_errors`. |
| P1-022 | Outcome extraction reads only the disposition block; pinned by `test_extract_outcome_reads_only_the_order_disposition` (`tests/test_fetch.py:1959`). |
| P1-023 | Precedential status is read from the header; pinned by `test_extract_precedential_status_reads_the_header_not_the_body` (`tests/test_fetch.py:2001`). |
| P1-024 | Plural section citations are expanded by `_section_values` at `fetch.py:330-338`; pinned by `test_extract_statutes_matches_plural_section_citations` (`tests/test_fetch.py:2060`). |
| P1-025 | A decision's own citation is preferred over a quoted case; pinned by `test_extract_citation_prefers_own_citation_over_quoted_case` (`tests/test_fetch.py:2136`). |
| P1-026 | Subsection normalization remains in `_normalize_statute_value` at `fetch.py:313-321`; covered by the fetch statutory-citation tests. |
| P1-027 | `extract_legal_standard` now selects the earliest match across all review patterns, with tuple order only breaking ties; a single `de novo` pattern covers `de novo review`, `review ... de novo`, and intervening subjects (`fetch.py:228-230, 264-285`). Pinned by `test_extract_legal_standard_uses_earliest_document_match`. |
| P1-028 | Decision-date hints are ranked `decided` > `issued` > `filed` > `handed down`, so a filing date shown first no longer displaces the later decision date (`fetch.py:49-54, 141-161`). Pinned by `test_extract_decision_date_prefers_decided_over_filing_date`. |
| P1-029 | `_merge_statute_families` now reserves C.F.R. slots at `fetch.py:341-355`. Probe with mixed statutory families: `extract_statutes("38 U.S.C. §§ 1110, 1131, 5103, 5103A, 7104, 7252; 38 C.F.R. §§ 3.303, 3.304")` → `['38 U.S.C. § 1110', '38 U.S.C. § 1131', '38 U.S.C. § 5103', '38 U.S.C. § 5103A', '38 C.F.R. § 3.303', '38 C.F.R. § 3.304']`; pinned by `test_extract_statutes_keeps_cfr_sections_under_the_cap` (`tests/test_fetch.py:2040`). |
| P1-030 | Enrichment now reads every PDF page via `_extract_pdf_text(content, max_pages=0)` instead of stopping at four pages (`fetch.py:831`). Pinned by `test_fetch_case_details_pdf_reads_all_pages`. |
| P1-031 | Fact-section extraction treats the chunk boundary as a terminator (`deep_read.py:50-55`), while the header still consumes only its own line so an empty facts section remains empty. Pinned by `test_extract_chunk_facts_body_at_end_of_chunk`. |
| P1-032 | `_synthesize_case` filters `_EMPTY_DIGEST` from both the reduce prompt and the fallback join, returning `""` when no usable digest remains (`deep_read.py:167-183`). Pinned by `test_synthesize_case_omits_empty_digest_sentinels`. |

---

## Remaining actionable findings

None. All 32 Pass 1 findings in this audit scope are fixed and covered by the
regressions named above.

---

## Cross-cutting observations

1. **Validation boundaries are now guarded at the edges.** P1-007 validates
   numeric settings, P1-008 rejects unknown provider overrides, P1-010 bounds
   the coverage score, P1-012 rejects a blank key, P1-017 wraps malformed usage
   counters, and P1-018 normalizes local-index timestamps. Future adapters
   should validate external input the same way rather than trusting callers.

2. **External payloads still need defence in depth.** P1-011 now delimits
   and escapes case text in the narrative, reasoning, and deep-read reduce
   prompts; P1-020 now validates decoded DuckDuckGo destinations at the producer
   and again before any fetch. Future network adapters should reuse the same
   boundary rather than assuming their callers validated the URL.

3. **“Unknown” is now explicit for precedential status.** P1-009 uses `None`
   for an unread body and carries that state through ranking, interpretation,
   summary, impact notes, and prompts. The invariant to preserve is that an
   unknown status is neither binding nor penalized; check any new consumer for
   truthiness that would collapse `None` into `False`.

4. **Legal-accuracy extraction now distinguishes the right signal from the
   first one.** P1-029 reserves slots per statutory family instead of letting
   one family consume the cap; P1-027 uses document order for the review
   standard, P1-028 ranks decision-date hints, P1-030 reads every PDF page for
   enrichment, and P1-031 terminates facts at a chunk boundary. Future
   extractors should test the disfavored signal when it appears later or at the
   edge of the input.

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
“Totals” section above. The 32 remediations changed the core-layer files above
plus `agent.py`, `ranking.py`, `impact.py`, and `__main__.py`, along with their
focused tests; no finding outside Pass 1 was remediated in this pass.

## Bottom line

Pass 3's operational findings remain complete. The remaining core layer has no
open findings: **32 of 32 Pass 1 findings are fixed, 0 are partially mitigated,
and 0 remain open.**
