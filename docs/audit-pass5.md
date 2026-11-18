# Audit Pass 5 — DeepSearchAgent (`va_legal_agent`)

**Defect prefix:** `AUD5-` · **Mode:** read-only, deterministic, line-by-line · **Scope:** all 144 tracked files (~85,409 lines)
**Tree audited:** branch `agents/will-will-be-working-in-github-deepsearchagent-h`, HEAD `95352af`
**Constraint honored during the audit:** no tracked source file was modified while auditing. Every fix below was originally a *proposed* unified diff.

> **Implementation status (2026-11-18, post-approval):** all proposed code patches, test-gap guards (TG-1, TG2–TG14, row-4), and documentation patches (DOC1–DOC9) have been **applied** with the user's approval. Four items were implemented with refined semantics, annotated inline at **C9**, **D11**, **E6**, and **P2**. Line numbers in this report refer to the pre-patch tree (`95352af`); patched files have grown. Validation after implementation: `tests/test_audit_pass5.py` **73 passed**; full suite **2431 passed, 3 deselected, 0 failed**; `ruff` clean.

## Executive summary

| Severity | Count | Notes |
|---|---|---|
| High | 13 | Includes 2 documentation/test-gap items (DOC1, TG-1) |
| Medium | ~40 | Includes 6 documentation items (DOC2–DOC6) and 5 test-gap guards (TG2–TG6) |
| Low | ~45 | Includes 3 documentation items (DOC7–DOC9), 8 test-gap guards (TG7–TG14), 3 cross-cutting items (X1–X3) |

**Already fixed in the restored tree (verified, not re-reported as open):**
- **D7** — `__main__.py:2817` now guards the interactive prompt with `and not (args.batch_dry_run or args.auto_run)`. Regression test `test_aud5_d7_start_at_with_auto_run` **passes**.
- **C7 (partial)** — `F.4th` is now present in `_CITATION_RE`. The remaining gaps (4-digit volume, `F.Supp.3d`, curly `F. App’x`) are still open and tested.
- **P8 (partial)** — the BVA results loop now guards non-dict rows (`providers.py:1788-1790`). The `"anomaly" in response.text` substring false-positive at `providers.py:1780` remains open and tested.

**Validation (Part 4):** `tests/test_audit_pass5.py` → **69 failed, 4 passed** (2.62 s). Every failure is a defect proof; the 4 passes are the already-fixed D7 guard plus TG2/TG3/TG4 behavior pins. Full suite: **69 failed, 2357 passed, 3 deselected** (23.09 s) — zero non-audit failures. Baseline excluding the audit file: **2353 passed**.

---

## Part 1 — File-by-file inventory and defect log

Line counts are exact (`wc -l`). Files with no findings are listed as **CLEAN**. Defect IDs are stable across Parts 1–4 and the regression module.

### 1.1 Repository root, config, CI, deploy

| File | Lines | Findings |
|---|---|---|
| `.env.example` | 392 | CLEAN |
| `.gitattributes` | 2 | CLEAN |
| `.github/workflows/ci.yml` | 53 | CLEAN |
| `.github/workflows/mutation.yml` | 151 | CLEAN |
| `.gitignore` | 44 | **DOC9** (Low): duplicate ignore entries; missing final newline on some tracked text files |
| `.mutation-baseline.json` | 671 | CLEAN (data) |
| `.vscode/launch.json` | 45 | CLEAN |
| `.vscode/tasks.json` | 32 | CLEAN |
| `GEMINI-GEM-KNOWLEDGE.md` | 417 | CLEAN |
| `GEMINI-GEM.md` | 62 | CLEAN |
| `Makefile` | 108 | **DOC5** (Medium): README claims mutation testing covers "every module"; Makefile L75–90 targets only 15 of 32 package modules |
| `PROGRESSIVE-GEM.md` | 215 | CLEAN |
| `PROMPT-CHATBOT.md` | 88 | **DOC1** (High): stale module map / test counts (L20–45) |
| `PROMPT.md` | 83 | **DOC1** (High): stale module map / test counts (L23–45, L62) |
| `README.md` | 683 | **DOC2** (Medium): test counts 864/2267 vs actual 2356 (L38, L23, L206); date "August" should be November 2026. **DOC5** cross-ref |
| `USAGE.md` | 574 | **DOC3** (Medium): L41–42 "no server" is false — Flask+Waitress webapp on PORT 8080. **DOC4** (Medium): `--show-config` section L232–303 omits ~23 of 74 settings keys |
| `pyproject.toml` | 70 | CLEAN (hypothesis declared in dev extras, L27 — earlier lead resolved as non-defect) |
| `requirements-dev.txt` | 3 | CLEAN (`-e .[dev,llm,web]`) |
| `requirements.txt` | 3 | CLEAN (`-e .`) |
| `webapp.py` | 728 | **D1, D2, D3, D4** (Low) — see §1.6 |
| `deploy/install.sh` | 177 | **stage5-B / AUD5-057** (Low) — see §1.7 |
| `deploy/run_batch.sh` | 100 | CLEAN |
| `deploy/va-legal-agent-batch.service` | 29 | CLEAN |
| `deploy/va-legal-agent-batch.timer` | 11 | CLEAN |

### 1.2 Benchmarks (data — all CLEAN)

| File | Lines |
|---|---|
| `benchmarks/citator/README.md` | 121 |
| `benchmarks/citator/baseline.json` | 5 |
| `benchmarks/citator/cases.json` | 2119 |
| `benchmarks/citator_real/README.md` | 190 |
| `benchmarks/citator_real/baseline.json` | 11 |
| `benchmarks/citator_real/cases.json` | 231 |
| `benchmarks/citator_real/fixtures/*` (9 files) | 80–84 each |
| `benchmarks/extraction/README.md` | 80 |
| `benchmarks/extraction/baseline.json` | 10 |
| `benchmarks/extraction/cases.json` | 70 |
| `benchmarks/extraction/fixtures/*` | 70 / 72 / 827 / 380 |

Benchmark fixtures and baselines were checked for internal consistency (citation strings, expected statuses, score ranges): **CLEAN**. The only benchmark-adjacent defect is in the *runner* (`scripts/run_citator_real_benchmark.py`, stage5-A below), not the data.

### 1.3 Scripts

| File | Lines | Findings |
|---|---|---|
| `scripts/_apply_mutant.py` | 112 | CLEAN |
| `scripts/adjudicate_citator_queue.py` | 151 | CLEAN |
| `scripts/check_mutation_baseline.py` | 257 | CLEAN |
| `scripts/mutmut_pass.py` | 267 | CLEAN |
| `scripts/run_citator_benchmark.py` | 472 | CLEAN |
| `scripts/run_citator_real_benchmark.py` | 659 | **stage5-A / AUD5-014** (Medium) — see §1.7 |
| `scripts/run_extraction_benchmark.py` | 217 | CLEAN |
| `scripts/run_source_allocation_benchmark.py` | 154 | CLEAN |
| `scripts/smoke_search.py` | 62 | CLEAN |

### 1.4 Docs

| File | Lines | Findings |
|---|---|---|
| `docs/appellate-history-layer.md` | 206 | CLEAN |
| `docs/aud4-001.md` | 165 | CLEAN |
| `docs/aud4-002-003-004.md` | 109 | CLEAN |
| `docs/aud4-005-008.md` | 90 | CLEAN |
| `docs/audit-pass1.md` | 1307 | **DOC8** (Low): L25 line-count mismatch vs current tree |
| `docs/audit-pass2.md` | 539 | CLEAN |
| `docs/audit-pass3-remaining.md` | 174 | CLEAN |
| `docs/audit-pass3.md` | 1042 | CLEAN |
| `docs/defect-register.md` | 192 | **DOC6** (Medium): ≥7 stale line refs (P1-001→:275, P1-002→:263, P1-003→:275, P1-007→:174, P1-013→:1660, P1-022→:577, L-1→:265). **DOC7** (Low): L170 "through AUD4-016" understates current coverage |

### 1.5 Tests (existing suite)

All existing test modules are CLEAN as code. Two carry *coverage gaps* (reported as TG items, not test bugs) and one carries stale-default assertions that interact with proposed fix M7:

| File | Lines | Notes |
|---|---|---|
| `tests/conftest.py` | 90 | CLEAN |
| `tests/audit_invariants.py` | 382 | CLEAN |
| `tests/test_adjudication.py` | 454 | CLEAN |
| `tests/test_appellate_history.py` | 330 | CLEAN |
| `tests/test_apply_mutant.py` | 112 | CLEAN |
| `tests/test_audit_invariants.py` | 134 | CLEAN |
| `tests/test_authorities.py` | 840 | CLEAN |
| `tests/test_authority_queue.py` | 662 | CLEAN |
| `tests/test_batch.py` | 389 | CLEAN |
| `tests/test_check_mutation_baseline.py` | 233 | CLEAN |
| `tests/test_citator.py` | 2553 | CLEAN |
| `tests/test_citator_provider.py` | 344 | CLEAN |
| `tests/test_cli.py` | 4716 | CLEAN |
| `tests/test_cli_citator.py` | 1135 | **M7 caveat**: L649/659/667/700/711 assert current `review_status` default |
| `tests/test_config.py` | 607 | CLEAN |
| `tests/test_congress.py` | 720 | CLEAN |
| `tests/test_courtlistener_cache.py` | 553 | CLEAN |
| `tests/test_courtlistener_metadata.py` | 401 | CLEAN |
| `tests/test_deep_read.py` | 738 | CLEAN |
| `tests/test_dependency_declarations.py` | 22 | CLEAN |
| `tests/test_extraction.py` | 284 | CLEAN |
| `tests/test_fetch.py` | 2697 | **P18 caveat**: `extract_holding_sentence` callers L754/960/966/972 |
| `tests/test_fetch_error_kind.py` | 606 | CLEAN |
| `tests/test_govinfo_legislation.py` | 420 | CLEAN |
| `tests/test_impact.py` | 433 | CLEAN |
| `tests/test_install_sh.py` | 128 | CLEAN |
| `tests/test_interpretation.py` | 1716 | **TG-1** (High): medium-cap confidence logic L699–707 untested |
| `tests/test_legal_agent.py` | 4926 | **P18 caveat**: `extract_holding_sentence` callers L1038/1042 |
| `tests/test_legal_agent_citator.py` | 311 | CLEAN |
| `tests/test_llm.py` | 824 | CLEAN |
| `tests/test_mutation_gate_integrity.py` | 148 | CLEAN |
| `tests/test_mutmut_pass.py` | 212 | CLEAN |
| `tests/test_planning.py` | 193 | CLEAN |
| `tests/test_prompt_docs.py` | 192 | CLEAN |
| `tests/test_provenance_compare.py` | 743 | CLEAN |
| `tests/test_providers.py` | 6033 | **P18 caveat**: `_bva_local_write_meta` L5141/5192/5241/5417/5497/5592; `_bva_local_load` L4663/4684 |
| `tests/test_ranking.py` | 346 | CLEAN |
| `tests/test_redaction.py` | 112 | **row-4** (Low): mutual-cycle redaction L101–111 untested |
| `tests/test_regulations.py` | 782 | CLEAN |
| `tests/test_reliability.py` | 134 | CLEAN |
| `tests/test_research.py` | 313 | CLEAN (imports from agent/interpretation/models/topics; no `research.py` module exists) |
| `tests/test_run_batch.py` | 111 | CLEAN |
| `tests/test_search.py` | 1111 | CLEAN |
| `tests/test_snapshots.py` | 348 | CLEAN |
| `tests/test_source_audit_properties.py` | 1065 | CLEAN |
| `tests/test_source_validity.py` | 3336 | **M7 caveat**: L598 asserts current `review_status` default |
| `tests/test_supersession.py` | 333 | CLEAN |
| `tests/test_topics.py` | 132 | CLEAN |
| `tests/test_webapp.py` | 564 | CLEAN |

### 1.6 Package `va_legal_agent/`

| File | Lines | Findings (IDs) |
|---|---|---|
| `__init__.py` | 7 | CLEAN |
| `__main__.py` | 3078 | D5, D6, D7(fixed), D8, D9, D12, D13 |
| `adjudication.py` | 504 | E11 |
| `agent.py` | 1540 | D9, D10, D11 |
| `appellate_history.py` | 341 | CLEAN |
| `authorities.py` | 1301 | E14, E15, E16 |
| `authority_queue.py` | 405 | E19 |
| `batch.py` | 159 | E22 |
| `citator.py` | 3790 | C1–C11, M7(caller) |
| `config.py` | 750 | M11, M12 |
| `congress.py` | 584 | E20, E23 |
| `courtlistener_cache.py` | 320 | CLEAN (already catches `(OSError, ValueError)`; E23 is consumer-side) |
| `deep_read.py` | 304 | E2(producer; consumers gated elsewhere) |
| `extraction.py` | 328 | X1 |
| `fetch.py` | 1041 | P1, P5, P16, P17, P18 |
| `impact.py` | 322 | E2 |
| `interpretation.py` | 740 | E1, TG-1 |
| `llm.py` | 528 | E6, E7, E18 |
| `models.py` | 1960 | M5, M6, M7, M8 |
| `planning.py` | 255 | CLEAN |
| `provenance_compare.py` | 450 | CLEAN |
| `providers.py` | 2746 | P2, P6, P7, P8, P9, P10, P11, P12, P13, P14, P15, P18 |
| `queries.py` | 105 | CLEAN |
| `ranking.py` | 190 | CLEAN (TG2 pin) |
| `redaction.py` | 321 | P3, P4 |
| `regulations.py` | 611 | E21, E23 |
| `reliability.py` | 172 | X2, X3 |
| `search.py` | 496 | E24 |
| `snapshots.py` | 398 | E12, E13, E16 |
| `topics.py` | 262 | CLEAN (TG3/TG4 pins) |
| `va_domain.py` | 1587 | M1, M2, M3, M4, M9, M10, M13, E4, E5, E8, E9, E10, E17 |

### 1.7 High-severity defects (13)

#### AUD5-001 · M1 — Provision-citation prefix collision (`va_domain.py:965-984`)
`extract_provision_impacts` builds `pattern = re.compile(re.escape(spec.citation), re.IGNORECASE)` with **no right boundary**, so `§ 5107` matches inside `§ 5107(a)` text and `§ 3.105` matches `§ 3.1050`. Combined with E4 (subsection never compared) in the same loop.
```diff
--- a/va_legal_agent/va_domain.py
+++ b/va_legal_agent/va_domain.py
@@ extract_provision_impacts (canonical pattern, ~L967)
-        pattern = re.compile(re.escape(spec.citation), re.IGNORECASE)
+        pattern = re.compile(re.escape(spec.citation) + r"(?![0-9A-Za-z])", re.IGNORECASE)
```

#### AUD5-002 · E1 — `interpret_cases` narrative never provenance-validated (`interpretation.py:648-678`)
`provenance_gate_applied = raw_reasoning is not None` (L678) gates only the *reasoning* pass. The lighter `llm_text = interpret_cases(...)` path is never run through `validate_text_provenance`, yet `llm_provenance_validated` is reported `True` whenever reasoning ran. Fabricated narrative survives.
```diff
--- a/va_legal_agent/interpretation.py
+++ b/va_legal_agent/interpretation.py
@@ from .llm import (...)
-    interpret_cases, reason_cases, validate_llm_synthesis_provenance,
+    interpret_cases, reason_cases, validate_llm_synthesis_provenance, validate_text_provenance,
@@ build_interpretive_analysis (~L678)
-    provenance_gate_applied = raw_reasoning is not None
+    if llm_text:
+        llm_failures = validate_text_provenance(llm_text, cases[: settings.llm_reasoning_limit])
+        if llm_failures:
+            provenance_warnings.extend(llm_failures)
+            llm_text = ""
+    provenance_gate_applied = raw_reasoning is not None and not provenance_warnings
```

#### AUD5-003 · E2 — Unverified `deep_summary` feeds impact + domain text (`impact.py:128-131`, `va_domain.py:865-875`)
`deep_read.py` sets `deep_summary_provenance_verified=False` when no extractive fallback exists, but both consumers concatenate `case.deep_summary` unconditionally.
```diff
--- a/va_legal_agent/impact.py
+++ b/va_legal_agent/impact.py
@@ _impact_text (~L128)
-    return f"{case.title} {case.snippet} {case.holding} {case.deep_summary} {abstract_text}".lower()
+    summary = case.deep_summary if case.deep_summary_provenance_verified else ""
+    return f"{case.title} {case.snippet} {case.holding} {summary} {abstract_text}".lower()
--- a/va_legal_agent/va_domain.py
+++ b/va_legal_agent/va_domain.py
@@ _case_text (~L865)
-        case.deep_summary,
+        case.deep_summary if case.deep_summary_provenance_verified else "",
```

#### AUD5-004 · P1 — HTTP response leak on body-cap abort (`fetch.py:930-936, 960-966`)
`_read_response_body` raises `FetchError` on the Content-Length / streaming cap, but `fetch_full_text` / `fetch_case_details_and_text` only catch `(requests.RequestException, CffiRequestException)`. The streamed `response` is never closed on that path.
```diff
--- a/va_legal_agent/fetch.py
+++ b/va_legal_agent/fetch.py
@@ fetch_full_text / fetch_case_details_and_text (both sites)
     try:
         response, final_url = _fetch_with_redirects(url, timeout)
-        content = _read_response_body(response, settings.max_fetch_bytes)
+        try:
+            content = _read_response_body(response, settings.max_fetch_bytes)
+        except BaseException:
+            getattr(response, "close", lambda: None)()
+            raise
     except (requests.RequestException, CffiRequestException) as exc:
         raise FetchError(f"Failed to fetch {url}: {exc}") from exc
```

#### P2 — Non-atomic BVA local-index commit + unlocked reads (`providers.py:2277-2279, 2464-2471`)
`_bva_local_build` swaps three files with sequential `os.replace` (corpus, manifest, meta) — a reader between swaps sees a mismatched set. `BvaLocalIndexProvider.search` then reads corpus+manifest **outside** `_bva_local_lock`, contradicting its own comment. Fix: write to a generation directory and atomically swap a pointer; wrap the shard reads in `with _bva_local_lock:`. (Structural; diff in Part 3.)

#### AUD5-006 · C1 — Negation window too short (`citator.py:1509-1518`)
`_is_negated` scans only 40 chars before the verb with ≤3 intervening words; "did not, after careful and extended consideration of the arguments, overrule" is missed.
```diff
--- a/va_legal_agent/citator.py
+++ b/va_legal_agent/citator.py
@@ _is_negated (~L1510)
-    prefix = sentence[max(0, term_start - 40) : term_start].lower()
-    return bool(re.search(r"\b(?:not|never|without|no\s+longer|declined\s+to|refused\s+to|failed\s+to)\b(?:\W+\w+){0,3}\W*$", prefix))
+    prefix = sentence[max(0, term_start - 80) : term_start].lower()
+    return bool(re.search(r"\b(?:not|never|without|no\s+longer|declin(?:e|ed|es|ing)\s+to|refus(?:e|ed|es|ing)\s+to|fail(?:s|ed|ing)?\s+to)\b(?:\W+\w+){0,12}\W*$", prefix))
```

#### AUD5-007 · C3 — Noun-object false treatments (`citator.py:1521-1534`)
`_is_non_verb_negative` returns `True` for "question"/"questions" regardless of object ("We question the doctrine" → falsely *questioned* treatment). Require a noun-object context. **Apply C3 before C4** (overlapping hunks).
```diff
--- a/va_legal_agent/citator.py
+++ b/va_legal_agent/citator.py
@@ _is_non_verb_negative (~L1523)
-    if treatment == "questioned" and lowered in {"question", "questions"}:
-        return True
+    if treatment == "questioned" and lowered in {"question", "questions"}:
+        return bool(re.match(r"\s+(?:the|a|an|this|that|whether)\b", sentence[verb_end:], re.I))
```

#### AUD5-008 · C6 — Abbreviation set gaps (`citator.py:300-329`)
`_ABBREVIATIONS` lacks `supp`, `ed`, `fed`, `cl`, `app'x`, `app’x`, so sentence splitting breaks inside reporter citations.
```diff
--- a/va_legal_agent/citator.py
+++ b/va_legal_agent/citator.py
@@ _ABBREVIATIONS (~L300)
     "app",
+    "app'x",
+    "app’x",
     "cir",
+    "cl",
     "co",
@@
+    "ed",
+    "fed",
@@
+    "supp",
```

#### AUD5-009 · C7 — Citation regex gaps (`citator.py:158-164`)
`F.4th` is now present (tree-evolved). Still missing: 4-digit volumes (`1234 Vet. App. 567`), `F.Supp.3d`, curly `F. App’x`.
```diff
--- a/va_legal_agent/citator.py
+++ b/va_legal_agent/citator.py
@@ _CITATION_RE (~L159)
-    r"|\b\d{1,3}\s+(?:Vet\.?\s*App\.?|F\.?\s*(?:2d|3d|4th|App'x|Supp\.?\s*2d)?"
+    r"|\b\d{1,4}\s+(?:Vet\.?\s*App\.?|F\.?\s*(?:2d|3d|4th|App['’]x|Supp\.?\s*(?:2d|3d))?"
```

#### AUD5-011 · C9 — `good_law` wrongly downgraded to `unknown` (`citator.py:3280-3293`)
`_apply_source_validity_enforcement` downgrades when `check.status in {"negative", "good_law"}` and no negative treatments remain — but a `good_law` check with a *withheld/invalid source signal* should keep `good_law`, not collapse to `unknown`.
```diff
--- a/va_legal_agent/citator.py
+++ b/va_legal_agent/citator.py
@@ _apply_source_validity_enforcement (~L3283)
-    if check.status in {"negative", "good_law"} and not remaining:
+    if check.status == "negative" and check.negative_treatments and not remaining:
         check.status = "unknown"
         check.good_law_as_of = ""
```
> **Implemented (refined):** the applied guard also downgrades a `good_law` check whose `good_law_as_of` date was carried over from the now-blocked treatments — `if check.negative_treatments and not remaining and (check.status == "negative" or (check.status == "good_law" and check.good_law_as_of))`. A clean `good_law` check with no negative treatments is never demoted; a stale as-of date cannot survive enforcement. See `_apply_source_validity_enforcement` in `citator.py`.

#### AUD5-012 · C10 — Passive separate-speaker not matched (`citator.py:263-265`)
`_PASSIVE_PARTY_RE` matches "by the court/panel" but not "by the dissent/concurrence", so separate-opinion treatments are attributed to the majority.
```diff
--- a/va_legal_agent/citator.py
+++ b/va_legal_agent/citator.py
@@ _PASSIVE_PARTY_RE (~L263)
-_PASSIVE_PARTY_RE = re.compile(rf"^\s*by\s+(?:the\s+)?{_PARTY_SUBJECT}\b", re.I)
+_PASSIVE_PARTY_RE = re.compile(
+    rf"^\s*by\s+(?:the\s+)?(?:{_PARTY_SUBJECT}|{_SEPARATE_SPEAKER})\b", re.I
+)
```

#### DOC1 — Stale prompt module maps / test counts (High)
`PROMPT.md` L23–45, L62 and `PROMPT-CHATBOT.md` L20–45 describe a module layout and test totals that no longer match the tree. Update both to the current 32-module package and 2356-test baseline. (Documentation diff; no code.)

#### TG-1 — Medium-cap confidence logic untested (High, test gap)
`interpretation.py:699-720` caps coverage confidence at "medium" under a specific condition; `tests/test_interpretation.py` never exercises it. Add a focused test pinning the cap. (Test-only; no source change.)

### 1.8 Medium-severity defects

#### AUD5-014 · stage5-A — Benchmark ignores `expected_status` (`scripts/run_citator_real_benchmark.py:384-390`)
`case_pass` checks fp/fn/tp and `status_safe` but never compares `check.status` to the case's `expected_status`, so a wrong-status result can still "pass".
```diff
--- a/scripts/run_citator_real_benchmark.py
+++ b/scripts/run_citator_real_benchmark.py
@@ (~L388)
-    case_pass = (fp == 0 and fn == 0 and strict_tp == tp and status_safe)
+    case_pass = (
+        fp == 0 and fn == 0 and strict_tp == tp and status_safe
+        and check.status == case["expected_status"]
+    )
```

#### AUD5-015 · D5 — Packing cap applied to non-proceed rows (`__main__.py:2090-2102`)
The packing-cap branch runs for every row regardless of verdict, and `remaining` is decremented even for deferred/aborted rows. Gate on `verdict == "proceed"`.
```diff
--- a/va_legal_agent/__main__.py
+++ b/va_legal_agent/__main__.py
@@ (~L2090)
-    if packing_cap is not None:
+    if packing_cap is not None and row["verdict"] == "proceed":
         if packed_total + cl_requests > packing_cap:
             row["verdict"] = "deferred"
         else:
             packed_total += cl_requests
@@ (~L2100)
-    if remaining is not None:
+    if remaining is not None and row["verdict"] == "proceed":
         remaining -= min(cl_requests, remaining)
         row["courtlistener_after"] = remaining
```

#### AUD5-016 · D6 — `_auto_run` drops deep-read/enrich flags (`__main__.py:2292-2302, 2349-2373, 2838-2846`)
`_auto_run`'s signature lacks `deep_read`, `deep_read_limit`, `enrich`; the batch-dry-run and analyze calls and the call site never forward them, so auto-run silently ignores user deep-read settings. Add the three parameters and forward at all three sites. (Signature + call-site diff in Part 3.)

#### AUD5-017 · D10 — Raw exception text in extraction flags/logs (`agent.py:1044-1049`)
`enrich_top_cases` writes `f"LLM extraction failed: {exc}"` into `case.extraction_flags` and the warning log without redaction; a URL-embedded secret leaks into persisted output.
```diff
--- a/va_legal_agent/agent.py
+++ b/va_legal_agent/agent.py
@@ from .ranking import ... (~L49)
+from .redaction import redact_secrets
@@ enrich_top_cases (~L1046)
-        logger.warning("Quote-first extraction failed for %s: %s", case.url, exc)
-        case.extraction_flags = [f"LLM extraction failed: {exc}"]
+        logger.warning("Quote-first extraction failed for %s: %s", case.url, redact_secrets(str(exc)))
+        case.extraction_flags = [f"LLM extraction failed: {redact_secrets(str(exc))}"]
```

#### AUD5-017b · D11 — `verified_holding` emitted for unverified summaries (`agent.py:1343-1356`)
The deep-summary export sets `"verified_holding": case.holding or case.snippet` whenever `deep_summary_source` is set, ignoring `deep_summary_provenance_verified`.
```diff
--- a/va_legal_agent/agent.py
+++ b/va_legal_agent/agent.py
@@ deep_summaries export (~L1349)
-            "verified_holding": case.holding or case.snippet,
+            **(
+                {"verified_holding": case.holding or case.snippet}
+                if case.deep_summary_provenance_verified
+                else {"unverified_snippet": case.snippet}
+            ),
```
> **Implemented (refined):** three-way export in `agent.py` — a provenance-verified summary exports `verified_holding = case.holding or case.snippet`; an unverified summary with a real source holding still exports that holding as `verified_holding` (so readers can compare it against the unverified model text); only an unverified summary with no holding falls back to `unverified_snippet = case.snippet`. Model text is never promoted to verified.

#### AUD5-018 · M2 — `_history_status` substring + missing statuses (`va_domain.py:1307-1320`)
`if status in text` matches substrings and the tuple omits `abrogated`, `superseded`, `questioned`. Use word boundaries and extend the tuple.
```diff
--- a/va_legal_agent/va_domain.py
+++ b/va_legal_agent/va_domain.py
@@ _history_status (~L1308)
-    for status in ("superseded_by_statute", "overruled", "reversed", "vacated", "affirmed", "good_law"):
-        if status in text:
+    for status in ("superseded_by_statute", "abrogated", "superseded", "overruled",
+                   "reversed", "vacated", "questioned", "affirmed", "good_law"):
+        if re.search(rf"\b{re.escape(status)}\b", text):
```

#### AUD5-019 · M3 — `_sentence_for` truncates at decimal points (`va_domain.py:878-884`)
`text.find(".", end)` stops at the "." in "§ 4.16", cutting the quote mid-citation. Use a terminal-punctuation regex that requires following whitespace+capital or end-of-string.
```diff
--- a/va_legal_agent/va_domain.py
+++ b/va_legal_agent/va_domain.py
@@ _sentence_for (~L880)
-    right_candidates = [pos for pos in (text.find(".", end), text.find(";", end)) if pos >= 0]
-    right = min(right_candidates) if right_candidates else len(text)
+    term = re.search(r"[.;](?=\s+[A-Z(]|\s*$)", text[end:])
+    right = end + term.start() if term else len(text)
```

#### AUD5-020 · M4 — `_relevant_cases` substring term match (`va_domain.py:1328-1336`)
`term in haystack` matches "cue" inside "rescue". Use word-boundary search.
```diff
--- a/va_legal_agent/va_domain.py
+++ b/va_legal_agent/va_domain.py
@@ _relevant_cases (~L1333)
-        if any(term in haystack for term in lowered):
+        if any(re.search(rf"\b{re.escape(term)}\b", haystack) for term in lowered):
```

#### AUD5-021 · M5 — Ledger `model_dump` crashes on empty/dup opinion IDs (`models.py:1086-1113`)
`opinion_audit_bottlenecks` raises `ValueError` on empty or duplicate `opinion_id`, so `SourceAuditLedger(...).model_dump()` throws. Skip instead of raise; update the docstring "Raises:" section.
```diff
--- a/va_legal_agent/models.py
+++ b/va_legal_agent/models.py
@@ opinion_audit_bottlenecks (~L1095)
-            if not row.opinion_id:
-                raise ValueError(...)
-            if row.opinion_id in seen_opinion_ids:
-                raise ValueError(...)
+            if not row.opinion_id or row.opinion_id in seen_opinion_ids:
+                continue
```

#### AUD5-022 · M6 — Secondary summary keeps official flag (`models.py:1786-1800`)
`SurfacedCaseRecord._infer_authority_tier` only reclassifies when tier is `unverified`; an explicit `secondary_summary` with `is_official_authority=True` survives.
```diff
--- a/va_legal_agent/models.py
+++ b/va_legal_agent/models.py
@@ _infer_authority_tier (~L1798, after the unverified branch)
+        if self.authority_tier in {"secondary", "secondary_summary"}:
+            self.is_official_authority = False
         return self
```

#### AUD5-045 · P3 — Userinfo redaction stops at first `@` (`redaction.py:78-80`)
`_USERINFO_RE` uses `[^/\s@]+@`, so `https://user:pa@ss@host/` leaves `ss@host` exposed. Match greedily to the last `@`.
```diff
--- a/va_legal_agent/redaction.py
+++ b/va_legal_agent/redaction.py
@@ _USERINFO_RE (~L78)
-_USERINFO_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)(?!\[REDACTED\]@)([^/\s@]+)@")
+_USERINFO_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)(?!\[REDACTED\]@)([^/\s]+)@")
```

#### AUD5-046 · P4 — Query redaction misses fragment params (`redaction.py:73-75`)
`_QUERY_PAIR_RE` anchors on `[?&]` only; `#api_key=SECRET` in a fragment is not redacted.
```diff
--- a/va_legal_agent/redaction.py
+++ b/va_legal_agent/redaction.py
@@ _QUERY_PAIR_RE (~L73)
-_QUERY_PAIR_RE = re.compile(r"(?i)([?&])([^=&\s'\"#]+)(=|%3D)([^&\s'\"#]*)")
+_QUERY_PAIR_RE = re.compile(r"(?i)([?&#])([^=&\s'\"#]+)(=|%3D)([^&\s'\"#]*)")
```

#### P5 — `FetchError` messages carry raw URLs; webapp has no redacting log handler (`fetch.py`, `webapp.py`)
`_require_http_url` (L34–38) and the redirect-exhaustion / body-cap messages embed `{url!r}` / `{response.url}` verbatim. Add `from .redaction import redact_url` and wrap each. Separately, `webapp.py` never calls `logging.basicConfig`, so the package's redacting formatter is bypassed (lastResort stderr). (Multi-site diff in Part 3.)

#### AUD5-048 · P6 — `_get_json` returns non-dict payloads (`providers.py:740, 809`)
Both the cache-hit `return entry.response` and final `return data` lack an `isinstance(..., dict)` guard; a list payload propagates and crashes callers with `AttributeError`. Add a central guard raising `SearchError(kind="malformed_response", retryable=False)`. (Diff in Part 3.)

#### AUD5-049 · P7 — String citation indexed as characters (`providers.py:332-365`)
`_map_courtlistener_opinion` does `citations[0]` on a bare string `"12 Vet. App. 34"`, yielding `"1"`.
```diff
--- a/va_legal_agent/providers.py
+++ b/va_legal_agent/providers.py
@@ _map_courtlistener_opinion (~L335)
     citations = item.get("citation") or []
+    if isinstance(citations, str):
+        citations = [citations]
     citation = ""
```

#### AUD5-050 · P8 — BVA "anomaly" substring false-positive (`providers.py:1780`)
`"anomaly" in response.text.lower()` treats a legitimate decision mentioning "congenital anomaly" as a bot challenge. Require challenge context. (Results-loop dict guard already fixed at L1788–1790.)
```diff
--- a/va_legal_agent/providers.py
+++ b/va_legal_agent/providers.py
@@ BVAProvider.search (~L1780)
-        if response.status_code == 202 or "anomaly" in response.text.lower():
+        if response.status_code == 202 or (
+            "anomaly" in response.text.lower() and '"resultsData"' not in response.text
+        ):
```

#### AUD5-026 · E4 — Alias subsection captured but never compared (`va_domain.py:971-983`)
The alias regex captures group(1) (the subsection) but the loop never checks it against the spec suffix, so `§ 5107(a)` text emits a `§ 5107(b)` impact. Same loop as M1 — combine.
```diff
--- a/va_legal_agent/va_domain.py
+++ b/va_legal_agent/va_domain.py
@@ alias loop (~L978)
         for match in alias_pattern.finditer(text):
+            subsection = (match.group(1) or "").strip()
+            if subsection and not spec.citation.endswith(f"({subsection})"):
+                continue
```

#### E5 — `provenance_verified` is host-only (`va_domain.py:905-940`)
`_impact` sets `verified` from hostname membership alone; the anchor/citation is never confirmed against the source. AND with an anchor-confirmation check. **No auto-test** (requires live source semantics); diff in Part 3.

#### AUD5-027 · E6 — Contradiction items not schema-checked (`llm.py:216-236`)
`_reasoning_schema_error` validates only top-level shapes; a dict-valued `case_a` is coerced by `str(...)` in `_parse_reasoning_response` into a fabricated `Contradiction`. Require each contradiction item to be a dict with string `statement`/`case_a`/`case_b`; otherwise return the whole parse as `None`. (Diff in Part 3.)

#### AUD5-028 · E7 — Provenance overlap is binary, not ratio (`llm.py:377-409`)
`_provenance_failures` fails only when there is *zero* lexical overlap; a single shared word passes a wholly fabricated narrative. Add a ratio threshold (< 0.3 fails).
```diff
--- a/va_legal_agent/llm.py
+++ b/va_legal_agent/llm.py
@@ _provenance_failures (~L405)
-    if words and not (words & corpus_words):
-        failures.append("no lexical overlap with the retrieved authorities")
+    if words:
+        overlap = len(words & corpus_words) / len(words)
+        if overlap < 0.3:
+            failures.append("insufficient lexical overlap with the retrieved authorities")
```

#### AUD5-029 · E8 — Abstract self-feeds impact/deep_summary (`va_domain.py:798-807`)
`build_case_abstract`'s metadata fallback joins `case.impact` and `case.deep_summary` into the abstract source text, circularly re-ingesting derived fields. Drop `impact`; gate `deep_summary` on provenance. (Diff in Part 3.)

#### AUD5-030 · E9 — Unconditional George cite + placeholder authorities (`va_domain.py:1527-1530, 1479, 1586`)
`build_senior_rater_guidance` injects "George v. McDonough" into `cue_steps` unconditionally (gate on `_has(cases, "george")`), and both builders return `source_authorities=["the retrieved authorities"]` when no refs exist. Replace the placeholder with an explicit "(no retrieved authorities — procedural guidance only)". (Diff in Part 3.)

#### AUD5-031 · E10 — Missing registry provisions (`va_domain.py:206, 515`)
`38 U.S.C. § 5109A` (CUE revision) and `38 C.F.R. § 20.1403(e)` (Board CUE) are absent from `PROVISION_REGISTRY`, so their impacts are never extracted. Insert both specs. (Diff in Part 3.)

#### E11 — `record_decision` lacks file lock (`adjudication.py:375-409`)
Load→mutate→save with no `flock`; concurrent adjudications lose updates. Reuse the `_exclusive_lock` contextmanager pattern from `batch.py:56-69`. **No auto-test** (concurrency); diff in Part 3.

#### AUD5-032 · E12 — Manifest not self-healed on re-record (`snapshots.py:199-200`)
`record()` returns early when the snapshot file exists, skipping `_append_manifest`; a deleted manifest row is never restored. Add a `_manifest_has_id` check and re-append when missing. (Diff in Part 3.)

#### AUD5-033 · E13 — `_capture` swallows only `OSError` (`snapshots.py:283-298`)
A store raising `TypeError`/`ValueError` escapes `_capture` and aborts the provider call.
```diff
--- a/va_legal_agent/snapshots.py
+++ b/va_legal_agent/snapshots.py
@@ SnapshottingCourtListenerProvider._capture (~L296)
-        except OSError as exc:
+        except (OSError, TypeError, ValueError) as exc:
             logger.warning("Could not persist %s snapshot: %s", kind, exc)
```

#### AUD5-034 · E14 — eCFR list payload → `AttributeError` (`authorities.py:321-345`)
`_ecfr_current_date` calls `payload.get("titles", [])` on a list payload. Guard with `isinstance(payload, dict)` → `SearchError`. (Diff in Part 3.)

#### AUD5-035 · E15 — Federal Register non-dict rows fabricate "verified" (`authorities.py:454-461`)
`results = payload.get("results") or []` then `latest = results[0] if isinstance(results[0], dict) else {}` yields `title="Untitled rule"` and `status="verified"` for `["x"]`. Filter non-dict rows first → `no_recent_documents`. (Diff in Part 3.)

#### AUD5-036 · E16 — Raw exception text in authority/snapshot details (`authorities.py`, `snapshots.py`)
`detail=str(exc)` / `{"error": str(exc)}` at authorities L161, 193, 374, 385, 459, 573, 949, 971, 1063, 1079, 1146, 1203 and snapshots L305, 326, 341, 353, 380, 394 leak URL-embedded secrets. Wrap with `redact_secrets`/`sanitize_exception` (authorities already imports both at L38; snapshots needs the import). L873 already redacts. (Multi-site diff in Part 3.)

#### AUD5-023 · C2 — "decline" base form missing (`citator.py:146-154`)
`_REAFFIRMANCE_PATTERNS` has `declin(?:ed|es|ing)` but not base `decline`; "We decline to overrule Smith" is missed.
```diff
--- a/va_legal_agent/citator.py
+++ b/va_legal_agent/citator.py
@@ _REAFFIRMANCE_PATTERNS (~L148)
-    re.compile(r"\bdeclin(?:ed|es|ing)\s+to\s+overrule\b", re.I),
+    re.compile(r"\bdeclin(?:e|ed|es|ing)\s+to\s+overrule\b", re.I),
```

#### AUD5-024 · C4 — "limited to" scope too broad (`citator.py:1521-1534`)
`_is_non_verb_negative` treats "limited to the question presented" as a *limited* treatment. Narrow to `(?:question|issue)s?\s+(?:presented|before|of)`. **Apply after C3.**
```diff
--- a/va_legal_agent/citator.py
+++ b/va_legal_agent/citator.py
@@ _is_non_verb_negative (~L1527)
-    if treatment == "limited" and re.match(r"\s+to\s+(?:the\s+)?(?:question|issue|extent|scope|whether)\b", sentence[verb_end:], re.I):
+    if treatment == "limited" and re.match(r"\s+to\s+(?:the\s+)?(?:question|issue)s?\s+(?:presented|before|of)\b", sentence[verb_end:], re.I):
```

#### AUD5-025 · C5 — "to the extent" not scoped as part (`citator.py:1537-1550`)
`_scope_for` only recognizes "in part"; "Overruled to the extent it conflicts" should be `part`.
```diff
--- a/va_legal_agent/citator.py
+++ b/va_legal_agent/citator.py
@@ _scope_for (~L1538)
-    if re.search(r"\bin part\b", sentence, re.I):
+    if re.search(r"\bin part\b|\bto the extent\b|\bto that extent\b|\binsofar\b", sentence, re.I):
         return "part"
```

#### AUD5-010 · C8 — Single-row lookup shortcut (`citator.py:679-694`)
`_select_lookup_row` returns `results[0]` when exactly one row exists, without checking it matches the wanted citation. Remove the shortcut; always normalize-match.
```diff
--- a/va_legal_agent/citator.py
+++ b/va_legal_agent/citator.py
@@ _select_lookup_row (~L682)
-    if len(results) == 1:
-        return results[0]
     wanted = _normalize_citation(citation)
```

#### DOC2–DOC6 (Medium, documentation)
- **DOC2** `README.md`: test counts 864/2267 → 2356; "August" → November 2026.
- **DOC3** `USAGE.md:41-42`: "no server" is false (Flask+Waitress, PORT 8080).
- **DOC4** `USAGE.md:232-303`: `--show-config` omits ~23 of 74 keys.
- **DOC5** `README.md` vs `Makefile:75-90`: mutation "every module" → 15 of 32.
- **DOC6** `docs/defect-register.md`: ≥7 stale line refs (see §1.4).

#### TG2–TG6 (Medium, test-gap guards)
Behavior pins the patch set must preserve: TG2 official tie-break in `rank_cases`; TG3 `38 U.S.C. § 5103` statute impact; TG4 `38 C.F.R. § 3.2601` STAGE_APPELLATE; TG5/TG6 additional ranking/impact invariants. TG2/3/4 are implemented and **pass** in Part 4.

### 1.9 Low-severity defects

#### AUD5-057 · stage5-B — `install.sh` sed injection (`deploy/install.sh:138-141`)
`sed "s|__TARGET__|$TARGET|g; s|__USER__|$USER_NAME|g"` breaks if `$TARGET`/`$USER_NAME` contain `|`, `&`, or `\`. Escape those characters before substitution. (Diff in Part 3.)

#### D1 — `esc()` does not escape quotes (`webapp.py:653-657`)
`esc()` uses `textContent`→`innerHTML`, which escapes `<>&` but not `"`/`'`; it is interpolated into `title="..."` (L679) and `data-id="..."` (L703) attributes.
```diff
--- a/webapp.py
+++ b/webapp.py
@@ esc() (~L655)
-    return d.innerHTML;
+    return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
```

#### D2 — `job_id` not validated before path join (`webapp.py:114-115`)
`_job_path` joins `STATE_DIR / f"{job_id}.json"` with no format check. Add `import re` and `re.fullmatch(r"[0-9a-f]{32}", job_id)` validation (reject otherwise). (Diff in Part 3.)

#### D3 — `_save_job` `OSError` aborts job completion (`webapp.py:290, 310`)
Both `_run_job` completion sites call `_save_job(job)` unguarded; a disk error raises out of the worker. Wrap each in `try/except OSError: pass`. (Diff in Part 3.)

#### D4 — Unguarded `res.json()` in submit/poll (`webapp.py:609, 625-626`)
`const data = await res.json();` throws on a non-JSON error response. Wrap in try/catch with retry (loadRuns already does). (Diff in Part 3.)

#### AUD5-060 · D7 — **ALREADY FIXED** (`__main__.py:2817`)
Interactive prompt now guarded by `and not (args.batch_dry_run or args.auto_run)`. Regression test passes; no change proposed.

#### AUD5-061 · D8 — `--batch-size` accepts non-positive ints (`__main__.py:2683-2689`)
`type=int` allows 0/negative. Use the existing `_positive_int` (L236–244).
```diff
--- a/va_legal_agent/__main__.py
+++ b/va_legal_agent/__main__.py
@@ --batch-size (~L2686)
-        type=int,
+        type=_positive_int,
```

#### AUD5-062 · D9 — `or` swallows explicit zero limit (`agent.py:707, 743, 941`)
`deep_read_limit or _settings.deep_read_limit` treats an explicit `0` as unset. Use `deep_read_limit if deep_read_limit is not None else ...` at all three sites. (Diff in Part 3.)

#### AUD5-072 · D12 — Text report omits element/finding/provenance sections (`__main__.py:1338`)
`_analysis_to_text` emits "Applicable principles" but not "Detected claim elements", "Principle findings", or "LLM provenance validated". Insert bullets using existing `_element_label` (L1457) / `_finding_label` (L1462). (Diff in Part 3.)

#### AUD5-073 · D13 — Guidance gates test raw fields, not rendered lines (`__main__.py:1265-1345`)
Five `if analysis...checklist`/`.actions`/`.error_calls`/`.focus` gates fire on empty-but-present structures. Gate on the `_rvsr_workflow_lines`/`_senior_workflow_lines`/`_qrs_workflow_lines`/`_rvsr_lines`/`_senior_lines` results instead. (Diff in Part 3.)

#### AUD5-065 · M7 — `review_status` defaults to "reviewed" (`models.py:738`)
`SourceInvalidatorCheck.review_status` defaults to `"reviewed"`, marking unreviewed checks as reviewed. Default to `"unreviewed"` + add a vocabulary validator against `SOURCE_INVALIDATOR_REVIEW_STATUSES`. **Caller caveat:** add explicit `review_status="reviewed"` at `citator.py:2429` only (L2400 already explicit). **Test caveat:** `test_source_validity.py:598` and `test_cli_citator.py:649/659/667/700/711` assert the current default and must be updated alongside. (Diff in Part 3.)

#### AUD5-066 · M8 — Negative counters accepted (`models.py:754-815`)
Nine plain-int counters on `SourceOpinionAudit` accept negatives. Apply `Field(default=0, ge=0)` to each (pattern: existing `_known_limit_reason` validator). (Diff in Part 3.)

#### AUD5-067 · M9 — M21 regex lacks dot continuation; "part x" substring (`va_domain.py:544-548, 1057-1073`)
`_M21_RE` stops before `.a`-style section dots; the stage classifier uses `"part x" in lower`, matching "Part XI". Add `(?:\.[A-Za-z0-9\-]+)*` to the regex and use `\bpart x\b`. (Diff in Part 3.)

#### AUD5-068 · M10 — Diagnostic Code accepts 0000 (`va_domain.py:540-543`)
`_DC_RE` uses `(\d{4})`, accepting invalid `0000`. DCs are 5000–9999.
```diff
--- a/va_legal_agent/va_domain.py
+++ b/va_legal_agent/va_domain.py
@@ _DC_RE (~L541)
-    r"\b(?:DC|Diagnostic\s+Code)\s*(\d{4})(?:\s*-\s*(\d{4}))?\b"
+    r"\b(?:DC|Diagnostic\s+Code)\s*([5-9]\d{3})(?:\s*-\s*([5-9]\d{3}))?\b"
```

#### AUD5-069 · M11 — `env_bool` silently ignores invalid values (`config.py:227-238`)
An unrecognized value falls through to `default` with no warning (unlike `env_choice`). Add `if raw: logger.warning(...)` before the final return. (Diff in Part 3.)

#### AUD5-070 · M12 — `USER_AGENT` whitespace not stripped (`config.py:519`)
`user_agent=os.getenv("USER_AGENT", d.user_agent)` keeps leading/trailing whitespace and accepts empty string.
```diff
--- a/va_legal_agent/config.py
+++ b/va_legal_agent/config.py
@@ from_env (~L519)
-        user_agent=os.getenv("USER_AGENT", d.user_agent),
+        user_agent=(os.getenv("USER_AGENT") or "").strip() or d.user_agent,
```

#### AUD5-071 · M13 — Dead regexes (`va_domain.py:530-539`)
`_USC_RE` and `_CFR_RE` are defined but never referenced. Delete both. (Diff in Part 3.)

#### P9 — Wall-time raise drops telemetry (`providers.py:2708-2709`)
`search_all` raises `SearchError` on deadline without appending the accumulated `stats` record (append happens only at L2739–2743). Append before raising. (Diff in Part 3.)

#### P10 — Excluded terms counted as dedup (`providers.py:2722-2727, 2538`)
`stats["deduped"] += excluded_count` conflates term-exclusion with URL-dedup. Add a separate `"excluded"` counter and extend `_TELEMETRY_KEYS`. (Diff in Part 3.)

#### P11 — Docstring claims canonical-URL dedup (`providers.py:2643-2652`)
`search_all` dedups by exact string (L2731–2737), not canonical URL. Correct the docstring. (Doc-only.)

#### P12 — `_get_bva_session` race (`providers.py:1594-1608`)
Lazy global session init has no lock; concurrent first calls create multiple sessions. Add a `threading.Lock` double-check. (Diff in Part 3.)

#### P13 — Unbounded BVA text cache (`providers.py:1909-1930`)
`_bva_sitemap_cache[f"text:{url}"]` grows without bound. Cap at 512 entries with eviction. (Diff in Part 3.)

#### AUD5-054 · P14 — Negative page slices sitemap (`providers.py:2049-2056`)
`window = leaf[(page - 1) * scan_limit : page * scan_limit]` with `page=0` yields a negative slice. Clamp `page = max(page, 1)`.
```diff
--- a/va_legal_agent/providers.py
+++ b/va_legal_agent/providers.py
@@ BvaSitemapProvider.search (~L2050)
+        page = max(page, 1)
         window = leaf[(page - 1) * scan_limit : page * scan_limit]
```

#### P15 — **RECLASSIFIED by-design** (`fetch.py:509-523`)
`_merge_statute_families` CFR-slot allocation is intentional; only the docstring is misleading. Docstring clarification only; **no test, no behavior change**.

#### AUD5-055 · P16 — Treatment window off-by-one (`fetch.py:669+`)
`window = text[start:end + 1]` includes one extra character past the computed `end`.
```diff
--- a/va_legal_agent/fetch.py
+++ b/va_legal_agent/fetch.py
@@ extract_citation_treatments (~L675)
-        window = text[start:end + 1]
+        window = text[start:end]
```

#### P17 — "byte-offset" actually char-offset (`fetch.py`)
Treatment offsets are described as byte offsets but are character indices; also open files with `newline=""` for stable offsets. Docstring + `newline=""` only. (Diff in Part 3.)

#### P18 — Test-only callers: **no deletion** (`fetch.py`, `providers.py`)
`extract_holding_sentence`, `_bva_local_write_meta`, `_bva_local_load` are referenced only by tests (callers listed in §1.5). Do **not** delete; add docstring/deprecation notes only. **No test.**

#### AUD5-037 · E17 — Bare modal triggers invalidation (`va_domain.py:886-902`)
`_effect_for` maps "cannot"/"may not"/"does not permit" to `invalidating_m21_1_rule` without requiring an M21/manual/provision/rule context. "The court cannot say" is misclassified. Require context for the bare modals. (Diff in Part 3.)

#### AUD5-038 · E18 — LLM failure logs unredacted (`llm.py:506, 526`)
`reason_cases`/`interpret_cases` log `exc` verbatim. Add `from .redaction import redact_secrets` and wrap both. (Diff in Part 3.)

#### AUD5-039 · E19 — Comma-joined opinion IDs not split (`authority_queue.py:51-67`)
`_identity` returns `("opinion", opinion_id)` for the full string; `"101,102"` and `"101"` become distinct queue entries (while `_check_matches_case` already comma-splits). Use `opinion_id.split(",")[0]`. (Diff in Part 3.)

#### AUD5-040 · E20 — CRS regex matches any R-number (`congress.py:64`)
`_CRS_REPORT_RE = r"\b(R\d{4,5})\b"` matches "Docket R12345". Require CRS context.
```diff
--- a/va_legal_agent/congress.py
+++ b/va_legal_agent/congress.py
@@ _CRS_REPORT_RE (~L64)
-_CRS_REPORT_RE = re.compile(r"\b(R\d{4,5})\b")
+_CRS_REPORT_RE = re.compile(r"\bCRS\s+(?:Report\s+)?(R\d{4,5})\b", re.I)
```

#### AUD5-041 · E21 — Comment limit checked after append (`regulations.py:424-480`)
`comments.append(...)` then `if len(comments) >= self.comment_sample_limit: break` yields one comment when limit=0. Check before appending. (Diff in Part 3.)

#### AUD5-042 · E22 — NaN coverage score accepted (`batch.py:108`)
`scores.append(float(raw))` admits NaN, poisoning the mean. Add `import math` and `math.isfinite` + bool guard. (Diff in Part 3.)

#### AUD5-043 · E23 — Cache consumers catch only `OSError` (`congress.py:203-224`, `regulations.py:198-214`)
`_cache_get`/`_cache_set` catch `OSError` only; a `UnicodeEncodeError` (a `ValueError`) from the cache escapes. `courtlistener_cache._read` already catches `(OSError, ValueError)` — the defect is consumer-side. Widen both consumers to `(OSError, ValueError)`. (Diff in Part 3.)

#### E24 — Throttle sleeps inside lock (`search.py:286-322`)
`_throttle` sleeps while holding `_interval_lock`, serializing all providers. Reserve timestamps under the lock, sleep outside it. **No auto-test** (timing); diff in Part 3.

#### AUD5-076 · C11 — `count_malformed` penalty missing (`citator.py:104-120`)
`_CONFIDENCE_GAP_PENALTIES` (15 entries) lacks `count_malformed`. Add `"count_malformed": 0.25`. (Diff in Part 3.)

#### DOC7–DOC9 (Low, documentation)
- **DOC7** `docs/defect-register.md:170`: "through AUD4-016" understates coverage.
- **DOC8** `docs/audit-pass1.md:25`: line-count mismatch.
- **DOC9** `.gitignore`: duplicate entries; missing final newlines.

#### TG7–TG14 (Low, test-gap guards)
Additional behavior pins (ranking, impact, reliability, redaction edge cases) the patch set must preserve. Test-only.

#### AUD5-084 · X1 — Extraction flags/logs unredacted (`extraction.py:304-314`)
`result.flags.append(f"LLM extraction failed: {exc}")` and the warning log leak secrets. Add `from .redaction import redact_secrets` and wrap both. (Diff in Part 3.)

#### AUD5-085 · X2 — Court-data host mis-tiered as agency guidance (`reliability.py:93-131`)
`classify_authority_tier` falls through to `AUTHORITY_AGENCY_GUIDANCE` for `courtlistener.com` with empty `court`. Insert a court-data host check before the fallthrough → `AUTHORITY_PERSUASIVE, True`. (Diff in Part 3.)

#### AUD5-086 · X3 — Dead path token in host set (`reliability.py:54`)
`_AGENCY_GUIDANCE_HOSTS` contains `"va.gov/vetapp"`, a path fragment that can never match a hostname. Remove it.
```diff
--- a/va_legal_agent/reliability.py
+++ b/va_legal_agent/reliability.py
@@ _AGENCY_GUIDANCE_HOSTS (~L54)
-_AGENCY_GUIDANCE_HOSTS = ("va.gov", "benefits.va.gov", "va.gov/vetapp")
+_AGENCY_GUIDANCE_HOSTS = ("va.gov", "benefits.va.gov")
```

#### row-4 — Mutual-cycle redaction untested (`tests/test_redaction.py:101-111`)
The mutual-cycle case in `redact_secrets` has no assertion. Test-only addition.

---

## Part 2 — D1–D5 VA-compensation adjudication compliance matrix

**Emission status:** all five deliverables are *emitted* by every renderer — text (`__main__.py:1096-1145`), CSV (`__main__.py:1485-1605`), JSON (full model dump), and webapp (`webapp.py:287-288`). The gaps below are **substantive correctness**, not missing output.

| Deliverable | Definition (abridged) | Emission | Substantive gaps (defect IDs) |
|---|---|---|---|
| **D1** | Full caption/court/docket/reporter citation/date/precedential-panel status/appellate history + verified official transcript URL + official-vs-secondary label | ✅ all renderers | M2 (history status), M6 (secondary keeps official flag), M7 (review_status default), X2 (court-data mis-tier), C9 (good_law downgrade) |
| **D2** | Untruncated structured 6-part abstract | ✅ all renderers | M3 (sentence truncation), E8 (abstract self-feed), E2 (unverified deep_summary) |
| **D3** | Provision-level impacts: M21-1 Part/Subpart/Chapter/Section + invalidate/narrow/update; 38 CFR 3/4 (§4.14/§4.26/§4.16/§3.321(b)(1)/medication); 38 USC §5103/§5103A/§5107(b)/§5110/§3.400; OGC Precedent Ops/Federal Register | ✅ all renderers | M1 (prefix collision), E4 (subsection), E5 (host-only provenance), E10 (missing §5109A/§20.1403(e)), M9 (M21 dots/part-x), M10 (DC range), E17 (bare-modal invalidation) |
| **D4** | RVSR step-by-step: STRs/lay evidence; §4.2 + M21-1 IV.i.3.C deferral with exact examiner questions; DC/staged/combined/effective dates; §5104(b) reasons-for-decision | ✅ all renderers | E9 (placeholder authorities), D12 (missing report sections), D13 (empty guidance gates) |
| **D5** | Senior Rater/DRO/QRS: HLR §3.2601 de novo/informal conference; pre-decisional DTA; CUE §3.105(a)/§5109A/George v. McDonough 142 S. Ct. 1953 (2022)/§20.1403(e); precedent-vs-unrevised-M21 conflicts; second-signature/NQR-QRT | ✅ all renderers | E9 (unconditional George + placeholders), E10 (missing CUE provisions), E11 (unlocked queue) |

**Cross-cutting provenance gates:** E1 (interpret narrative), E2 (deep_summary consumers), D11 (verified_holding export), E6 (contradiction schema), E7 (overlap ratio).
**Citator accuracy (feeds D1):** C1–C10.

### AUD4 fix verification

| AUD4 item | Verdict | Evidence |
|---|---|---|
| AUD4-009 | ✅ verified | fix present and exercised |
| AUD4-014 | ✅ verified | fix present and exercised |
| AUD4-001 | ⚠️ gaps | redaction wired but E16 raw-exc sites remain |
| AUD4-005 | ❌ partial | E9 placeholder/unconditional-cite gaps remain |
| others (002/003/004/006/007/008/010–013/015/016) | ✅ wired | present in tree |

---

## Part 3 — Prioritized patch set

**Apply order constraints:**
1. **C3 before C4** — overlapping hunks in `_is_non_verb_negative`.
2. **M1 + E4 combined** — same `extract_provision_impacts` alias loop; apply the boundary fix and subsection check together.
3. **M7** — change the model default *and* add explicit `review_status="reviewed"` at `citator.py:2429` only (L2400 already explicit); update the six stale-default test assertions (`test_source_validity.py:598`, `test_cli_citator.py:649/659/667/700/711`).
4. **P18** — no deletion; docstring/deprecation only (test callers exist).
5. **E5, E11, E24, P15** — no auto-test (live-source / concurrency / timing / by-design respectively).

Priority tiers: **P0** = High correctness (M1/E1/E2/P1/P2/C1/C3/C6/C7/C9/C10); **P1** = Medium correctness; **P2** = Low + documentation + test gaps. Inline diffs for the surgical fixes appear in Part 1; the structural fixes follow.

### P2 — Atomic BVA local-index commit + locked reads (`providers.py`)
```diff
--- a/va_legal_agent/providers.py
+++ b/va_legal_agent/providers.py
@@ _bva_local_build commit (~L2277)
-    os.replace(corpus_tmp, corpus_path)
-    os.replace(manifest_tmp, manifest_path)
-    os.replace(meta_tmp, meta_path)
+    # Write all three into a fresh generation dir, then swap one pointer so a
+    # reader never observes a half-committed corpus/manifest/meta set.
+    gen_dir = corpus_path.parent / f".gen-{os.getpid()}-{time.monotonic_ns()}"
+    gen_dir.mkdir()
+    os.replace(corpus_tmp, gen_dir / corpus_path.name)
+    os.replace(manifest_tmp, gen_dir / manifest_path.name)
+    os.replace(meta_tmp, gen_dir / meta_path.name)
+    os.replace(gen_dir, corpus_path.parent / "current")
@@ BvaLocalIndexProvider.search shard reads (~L2464)
-        for _year, corpus_path, manifest_path in shards:
-            with open(corpus_path, encoding="utf-8") as fh:
-                corpus_text = fh.read()
-            with open(manifest_path, encoding="utf-8") as fh:
-                manifest = json.load(fh)
+        with _bva_local_lock:
+            for _year, corpus_path, manifest_path in shards:
+                with open(corpus_path, encoding="utf-8") as fh:
+                    corpus_text = fh.read()
+                with open(manifest_path, encoding="utf-8") as fh:
+                    manifest = json.load(fh)
```
*(Generation-dir layout requires the load path to resolve `current/`; adapt `_bva_local_load` accordingly. The locked-read hunk is independently applicable and is the minimum fix for the race.)*

> **Implemented (minimal):** the locked-read hunk was applied — `BvaLocalIndexProvider.search` now holds `_bva_local_lock` across the corpus/manifest shard reads, and the build path documents that writers hold the same lock across the commit, so a reader never observes a half-committed set within the process. The generation-directory pointer swap was **not** applied: it changes the on-disk layout and load path, and the lock already closes the intra-process race. Cross-process atomicity remains a possible follow-up.

### D6 — Forward deep-read/enrich through `_auto_run` (`__main__.py`)
```diff
--- a/va_legal_agent/__main__.py
+++ b/va_legal_agent/__main__.py
@@ _auto_run signature (~L2292)
 def _auto_run(issues, claim_type, max_results, max_wall_seconds,
               max_batch_requests=None, run_id="", max_iterations=None,
-              max_wait_seconds=None, snapshot_dir=None):
+              max_wait_seconds=None, snapshot_dir=None,
+              deep_read=False, deep_read_limit=None, enrich=False):
@@ batch_dry_run call (~L2349) and analyze_cases_for_claim call (~L2366)
+        deep_read=deep_read, deep_read_limit=deep_read_limit, enrich=enrich,
@@ call site (~L2838)
         snapshot_dir=args.snapshot_dir,
+        deep_read=args.deep_read, deep_read_limit=args.deep_read_limit, enrich=args.enrich,
```

### P5 — Redact URLs in FetchError + webapp logging (`fetch.py`, `webapp.py`)
```diff
--- a/va_legal_agent/fetch.py
+++ b/va_legal_agent/fetch.py
@@ imports (~L27)
+from .redaction import redact_url
@@ _require_http_url (~L37)
-    raise FetchError(f"Refusing to fetch non-HTTP(S) URL: {url!r}")
+    raise FetchError(f"Refusing to fetch non-HTTP(S) URL: {redact_url(url)!r}")
@@ redirect exhaustion (~L178) and body-cap messages (~L900, L912)
-    ... {url} ... / ... {response.url} ...
+    ... {redact_url(url)} ... / ... {redact_url(response.url)} ...
--- a/webapp.py
+++ b/webapp.py
@@ module init
+import logging
+logging.basicConfig()  # install the package's redacting formatter instead of lastResort stderr
```

### P6 — Central dict guard in `_get_json` (`providers.py`)
```diff
--- a/va_legal_agent/providers.py
+++ b/va_legal_agent/providers.py
@@ _get_json cache-hit (~L740) and final return (~L809)
-        return entry.response
+        if not isinstance(entry.response, dict):
+            raise SearchError("Malformed CourtListener response", kind="malformed_response", retryable=False)
+        return entry.response
@@
-    return data
+    if not isinstance(data, dict):
+        raise SearchError("Malformed CourtListener response", kind="malformed_response", retryable=False)
+    return data
```

### E6 — Contradiction item schema (`llm.py`)
```diff
--- a/va_legal_agent/llm.py
+++ b/va_legal_agent/llm.py
@@ _reasoning_schema_error (~L230, after top-level checks)
+    for item in data.get("contradictions", []):
+        if not isinstance(item, dict):
+            return "contradiction item must be an object"
+        for key in ("statement", "case_a", "case_b"):
+            if not isinstance(item.get(key), str):
+                return f"contradiction.{key} must be a string"
```
> **Implemented (refined):** `_reasoning_schema_error` rejects a non-list `contradictions` value and any dict item whose present `statement`/`case_a`/`case_b` is not a string; a **non-dict item is skipped** (`continue`) rather than failing the whole parse, so one malformed entry cannot discard the well-formed contradictions that follow it (the parser already skips non-dict items). Optional keys remain tolerated for backward compatibility.

### E8 — Drop impact / gate deep_summary in abstract fallback (`va_domain.py`)
```diff
--- a/va_legal_agent/va_domain.py
+++ b/va_legal_agent/va_domain.py
@@ build_case_abstract metadata fallback (~L798)
-        case.impact,
-        case.deep_summary,
+        case.deep_summary if case.deep_summary_provenance_verified else "",
```

### E9 — Gate George cite + replace placeholders (`va_domain.py`)
```diff
--- a/va_legal_agent/va_domain.py
+++ b/va_legal_agent/va_domain.py
@@ build_senior_rater_guidance cue_steps (~L1527)
-    cue_steps.append("George v. McDonough, 142 S. Ct. 1953 (2022) ...")
+    if _has(cases, "george"):
+        cue_steps.append("George v. McDonough, 142 S. Ct. 1953 (2022) ...")
@@ both builders' return (~L1479, ~L1586)
-    source_authorities=refs or ["the retrieved authorities"],
+    source_authorities=refs or ["(no retrieved authorities — procedural guidance only)"],
```

### E10 — Add CUE provisions to registry (`va_domain.py`)
```diff
--- a/va_legal_agent/va_domain.py
+++ b/va_legal_agent/va_domain.py
@@ after §7261 spec (~L206)
+    ProvisionSpec(
+        "38 U.S.C. § 5109A", "statute", STAGE_APPELLATE, "clarifying",
+        aliases=("5109A",), m21_1=(M21_HLR_CUE,), source_url=_usc("5109A"),
+    ),
@@ before PROVISION_REGISTRY close (~L515)
+    ProvisionSpec(
+        "38 C.F.R. § 20.1403(e)", "regulation_part20", STAGE_APPELLATE, "clarifying",
+        aliases=("20.1403",), m21_1=(M21_HLR_CUE,), source_url=_cfr("20", "20.1403"),
+    ),
```

### E11 — Lock `record_decision` (`adjudication.py`)
```diff
--- a/va_legal_agent/adjudication.py
+++ b/va_legal_agent/adjudication.py
@@ imports (~L20)
+import fcntl
+from contextlib import contextmanager
@@ add _exclusive_lock (pattern from batch.py:56-69), then record_decision (~L375)
-    queue = load_queue(path)
-    item = next(...)
-    ...
-    save_queue(queue, path)
-    return item
+    with _exclusive_lock(path):
+        queue = load_queue(path)
+        item = next(...)
+        ...
+        save_queue(queue, path)
+        return item
```

### E12 — Self-heal manifest (`snapshots.py`)
```diff
--- a/va_legal_agent/snapshots.py
+++ b/va_legal_agent/snapshots.py
@@ record() (~L199)
-        if path.exists():
-            return record
+        if path.exists():
+            if not _manifest_has_id(self.manifest_path, record.id):
+                _append_manifest(self.manifest_path, record)
+            return record
```

### E14 / E15 — Guard eCFR / Federal Register payloads (`authorities.py`)
```diff
--- a/va_legal_agent/authorities.py
+++ b/va_legal_agent/authorities.py
@@ _ecfr_current_date (~L330)
     payload = response.json()
+    if not isinstance(payload, dict):
+        raise SearchError("Malformed eCFR response")
     for title in payload.get("titles", []):
@@ verify_federal_register_part (~L455)
-    results = payload.get("results") or []
+    results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
```

### E16 — Redact authority/snapshot exception details (`authorities.py`, `snapshots.py`)
At each listed site replace `str(exc)` with `sanitize_exception(exc)` (authorities, already imported) or `redact_secrets(str(exc))` (snapshots; add `from .redaction import redact_secrets`). Sites: authorities L161, 193, 374, 385, 459, 573, 949, 971, 1063, 1079, 1146, 1203; snapshots L305, 326, 341, 353, 380, 394. (L873 already redacts; leave unchanged.)

### D2 / D3 / D4 — webapp hardening (`webapp.py`)
```diff
--- a/webapp.py
+++ b/webapp.py
@@ imports (~L41)
+import re
@@ _job_path (~L114)
 def _job_path(job_id):
+    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
+        raise ValueError("invalid job id")
     return STATE_DIR / f"{job_id}.json"
@@ _run_job done/error sites (~L290, ~L310)
-        _save_job(job)
+        try:
+            _save_job(job)
+        except OSError:
+            pass
@@ submit (~L609) and poll (~L625)
-    const data = await res.json();
+    let data; try { data = await res.json(); } catch (e) { /* retry / surface error */ }
```

### D9 — Zero-limit ternary (`agent.py`, ×3)
```diff
--- a/va_legal_agent/agent.py
+++ b/va_legal_agent/agent.py
@@ L707, L743
-        deep_read_limit=deep_read_limit or _settings.deep_read_limit,
+        deep_read_limit=deep_read_limit if deep_read_limit is not None else _settings.deep_read_limit,
@@ L941
-        deep_read_limit=deep_read_limit or get_settings().deep_read_limit,
+        deep_read_limit=deep_read_limit if deep_read_limit is not None else get_settings().deep_read_limit,
```

### D12 / D13 — Text report sections + guidance gates (`__main__.py`)
```diff
--- a/va_legal_agent/__main__.py
+++ b/va_legal_agent/__main__.py
@@ _analysis_to_text after "Applicable principles" (~L1338)
+    lines += _bullets("Detected claim elements", [_element_label(e) for e in analysis.claim_elements])
+    lines += _bullets("Principle findings", [_finding_label(f) for f in analysis.principle_findings])
+    lines += _bullets("LLM provenance validated", [str(analysis.llm_provenance_validated)])
@@ five guidance gates (~L1265-1345): replace raw-field truthiness with rendered-line truthiness
-    if analysis.adjudication_guidance.rvsr.evidence_review_checklist:
+    if _rvsr_workflow_lines(analysis):
   (and analogously for senior/qrs/rvsr/senior line helpers)
```

### M7 / M8 — Model defaults + counter bounds (`models.py`)
```diff
--- a/va_legal_agent/models.py
+++ b/va_legal_agent/models.py
@@ SourceInvalidatorCheck (~L738)
-    review_status: str = "reviewed"
+    review_status: str = "unreviewed"
+
+    @field_validator("review_status")
+    @classmethod
+    def _known_review_status(cls, v):
+        if v not in SOURCE_INVALIDATOR_REVIEW_STATUSES:
+            raise ValueError(f"unknown review_status: {v}")
+        return v
@@ SourceOpinionAudit: nine counters (~L754-815)
-    opinion_index: int = 0
+    opinion_index: int = Field(default=0, ge=0)
   (repeat for candidate_allocation, forward_citations_reported, citing_ids_collected,
    citing_ids_new_to_cluster, citing_ids_duplicate_across_cluster, duplicate_rows_ignored,
    invalid_rows_ignored, pages_fetched)
```
**Companion changes:** `citator.py:2429` add `review_status="reviewed"`; update `test_source_validity.py:598` and `test_cli_citator.py:649/659/667/700/711`.

### M9 / M11 — M21 regex + env_bool warning (`va_domain.py`, `config.py`)
```diff
--- a/va_legal_agent/va_domain.py
+++ b/va_legal_agent/va_domain.py
@@ _M21_RE (~L544)
-    r"M21-1(?:,\s*Part\s+[IVXL]+)?..."
+    r"M21-1(?:,\s*Part\s+[IVXL]+)?(?:\.[A-Za-z0-9\-]+)*..."
@@ stage classifier (~L1063)
-    elif "part x" in lower or "cue" in lower or "higher-level" in lower:
+    elif re.search(r"\bpart x\b", lower) or "cue" in lower or "higher-level" in lower:
--- a/va_legal_agent/config.py
+++ b/va_legal_agent/config.py
@@ env_bool (~L236)
+    if raw:
+        logger.warning("Invalid boolean for %s: %r; using default %r", name, raw, default)
     return default
```

### P9 / P10 / P12 / P13 — providers telemetry + session + cache (`providers.py`)
```diff
--- a/va_legal_agent/providers.py
+++ b/va_legal_agent/providers.py
@@ search_all deadline raise (~L2708)
     if deadline is not None and time.monotonic() >= deadline:
+        telemetry.append(stats)
         raise SearchError(f"Search wall-time budget exhausted for query {query!r}")
@@ excluded terms (~L2722)
-        stats["deduped"] = int(stats["deduped"]) + excluded_count
+        stats["excluded"] = int(stats.get("excluded", 0)) + excluded_count
@@ _TELEMETRY_KEYS (~L2538)
-_TELEMETRY_KEYS = ("queries_issued", "results", "deduped", "failures")
+_TELEMETRY_KEYS = ("queries_issued", "results", "deduped", "excluded", "failures")
@@ _get_bva_session (~L1594): add module _bva_session_lock = threading.Lock(); double-check under lock
@@ _fetch_bva_decision_text (~L1925): evict oldest text: entries when len > 512
```

### E17 / E18 / E19 / E21 / E22 / E23 / X1 / X2 — remaining surgical fixes
```diff
--- a/va_legal_agent/va_domain.py  (E17, _effect_for ~L888)
-    if re.search(r"\b(...|cannot|may not|does not permit)\b", quote, re.I):
+    if re.search(r"\b(impermissible|invalid|unlawful|unconstitutional|not persuasive|contrary to|reject(?:s|ed)?)\b", quote, re.I) or (
+        re.search(r"\b(cannot|may not|does not permit)\b", quote, re.I)
+        and re.search(r"\b(m21|manual|provision|rule)\b", quote, re.I)):
--- a/va_legal_agent/llm.py  (E18)
+from .redaction import redact_secrets
@@ L506, L526: wrap exc with redact_secrets(str(exc))
--- a/va_legal_agent/authority_queue.py  (E19, _identity ~L60)
-    return ("opinion", opinion_id)
+    return ("opinion", opinion_id.split(",")[0])
--- a/va_legal_agent/regulations.py  (E21, _comments ~L470)
+        if len(comments) >= self.comment_sample_limit:
+            break
         comments.append(RegulationsPublicComment(...))
-        if len(comments) >= self.comment_sample_limit:
-            break
--- a/va_legal_agent/batch.py  (E22)
+import math
@@ summary (~L108)
-        scores.append(float(raw))
+        value = float(raw)
+        if isinstance(raw, bool) or not math.isfinite(value):
+            continue
+        scores.append(value)
--- a/va_legal_agent/congress.py & regulations.py  (E23, _cache_get/_cache_set)
-        except OSError:
+        except (OSError, ValueError):
--- a/va_legal_agent/extraction.py  (X1)
+from .redaction import redact_secrets
@@ L308-310: wrap exc with redact_secrets(str(exc)) in log and flags
--- a/va_legal_agent/reliability.py  (X2, classify_authority_tier before agency fallthrough)
+    if host == "courtlistener.com" or host.endswith(".courtlistener.com"):
+        return AUTHORITY_PERSUASIVE, True
```

### E24 / E5 / P17 / stage5-B — no-auto-test / doc / script fixes
- **E24** `search.py:_throttle` — reserve next-slot timestamps under `_interval_lock`, release, then sleep outside the lock.
- **E5** `va_domain.py:_impact` — AND host-membership with an anchor/citation confirmation against the fetched source.
- **P17** `fetch.py` — correct "byte-offset" docstrings to character offsets; open text files with `newline=""`.
- **stage5-B** `deploy/install.sh:138-141` — escape `|`, `&`, `\` in `$TARGET`/`$USER_NAME` before the `sed` substitution.

### Documentation patches (DOC1–DOC9)
Update `PROMPT.md`/`PROMPT-CHATBOT.md` module maps + test counts (DOC1); `README.md` counts/date + mutation scope (DOC2/DOC5); `USAGE.md` server statement + `--show-config` key list (DOC3/DOC4); `docs/defect-register.md` line refs + coverage sentence (DOC6/DOC7); `docs/audit-pass1.md:25` line count (DOC8); `.gitignore` duplicates + final newlines (DOC9). No code behavior changes.

---

## Part 4 — Regression tests and validation evidence

**Module:** `tests/test_audit_pass5.py` (untracked, ~890 lines, 73 tests). Every `test_aud5_*` defect-proof test **fails on the current tree**; each failure is the evidence for its named defect. The `test_aud5_tg*` guards **pass** and pin behavior the patches must preserve. No tracked source file is modified by the module.

**Run command:**
```bash
.venv/bin/python -m pytest tests/test_audit_pass5.py -q -p no:cacheprovider
```
Environment: Python 3.14.10 (`/opt/homebrew/bin/python3.14`), editable install `-e ".[dev,llm,web]"`. System `python3` is 3.9.6 (too old). Settings are a frozen dataclass rebuilt from env each call — tests override via `monkeypatch.setenv` (`LLM_EXTRACTION`/`LLM_REASONING="true"`, `SEARCH_MIN_INTERVAL_SECONDS="0"`); `llm_enabled()` is patched per importing module.

### Result: 69 failed, 4 passed (2.62 s)

**Passed (4):**
| Test | Meaning |
|---|---|
| `test_aud5_d7_start_at_with_auto_run` | D7 already fixed in tree |
| `test_aud5_tg2_official_tiebreak_guard` | TG2 behavior pin |
| `test_aud5_tg3_usc_impact_guard` | TG3 behavior pin |
| `test_aud5_tg4_stage_effect_guard` | TG4 behavior pin |

**Failed (69) — defect proofs, by ID:**

| AUD5 ID | Defect | Test line | Failure signature |
|---|---|---|---|
| 014 | stage5-A | 150 | `case_pass` regex lacks `expected_status` |
| 057 | stage5-B | 161 | raw `s|__TARGET__|$TARGET|g` present |
| 001 | M1 (×2) | 174/183 | prefix collision emits wrong provision |
| 018 | M2 | 192 | substring/missing history status |
| 019 | M3 | 197 | quote "truncated to § 4." |
| 020 | M4 | 206 | "rescue" matches "cue" |
| 021 | M5 | 212 | `model_dump()` raises ValueError |
| 022 | M6 | 219 | secondary keeps official flag |
| 065 | M7 | 231 | default `review_status == "reviewed"` |
| 066 | M8 | 237 | negative `pages_fetched` accepted |
| 067 | M9 (×2) | 243/252 | "Part XI" → STAGE_APPELLATE; dot continuation lost |
| 068 | M10 | 261 | DC 0000 accepted |
| 069 | M11 | 267 | invalid env_bool silent |
| 070 | M12 | 275 | whitespace USER_AGENT kept |
| 071 | M13 | 282 | dead regexes present |
| 002 | E1 | 293 | `llm_provenance_validated` True for fabricated narrative |
| 003 | E2 | 312 | "fabricated" leaks into impact/domain text |
| 026 | E4 | 321 | § 5107(a) emits § 5107(b) |
| 027 | E6 | 329 | dict `case_a` not rejected |
| 028 | E7 | 341 | zero-overlap-only check passes fabrication |
| 029 | E8 | 350 | abstract re-ingests impact |
| 030 | E9 (×2) | 367/373 | unconditional George; placeholder authorities |
| 031 | E10 (×2) | 379/387 | §5109A / §20.1403(e) absent |
| 032 | E12 | 395 | manifest not re-appended |
| 033 | E13 | 405 | `_capture` raises TypeError |
| 034 | E14 | 417 | list payload → AttributeError (not SearchError) |
| 035 | E15 | 427 | non-dict row → status "verified" |
| 036 | E16 | 439 | "SECRET" in check.detail |
| 037 | E17 | 453 | "cannot say" → invalidating_m21_1_rule |
| 038 | E18 | 460 | "SECRET" in reasoning failure log |
| 039 | E19 | 473 | queue len 2 for comma IDs |
| 040 | E20 | 481 | "Docket R12345" matched |
| 041 | E21 | 486 | limit=0 yields 1 comment |
| 042 | E22 | 501 | NaN coverage mean |
| 043 | E23 | 511 | UnicodeEncodeError escapes `_cache_set` |
| 006 | C1 | 531 | long negation missed |
| 023 | C2 | 537 | "decline to overrule" missed |
| 007 | C3 | 543 | noun "question" false treatment |
| 024 | C4 | 549 | "limited to the question presented" false |
| 025 | C5 | 555 | "to the extent" not part |
| 008 | C6 | 561 | Supp/Ed/Fed/Cl/App'x not abbreviations |
| 009 | C7 | 567 | 4-digit/Supp.3d/curly App’x unmatched |
| 010 | C8 | 573 | single wrong row returned |
| 011 | C9 | 579 | good_law downgraded to unknown |
| 012 | C10 | 585 | "by the dissent" unmatched |
| 076 | C11 | 606 | `count_malformed` absent |
| 015 | D5 | 616 | packing regex lacks proceed gate |
| 016 | D6 | 626 | `_auto_run` signature lacks deep_read/enrich |
| 060 | D7 | 636 | **passes** (already fixed) |
| 061 | D8 | 644 | `--batch-size` not `_positive_int` |
| 062 | D9 | 652 | `deep_read_limit or ` present |
| 017 | D10 | 662 | "SECRET" in extraction_flags |
| 017b | D11 | 680 | unverified `verified_holding` emitted |
| 072 | D12 | 690 | report sections absent |
| 073 | D13 | 701 | raw-field gates present |
| 004 | P1 | 714 | response not closed on cap abort |
| 045 | P3 | 740 | "ss@host"/"pa@ss" survives |
| 046 | P4 | 746 | fragment "SECRET" survives |
| 048 | P6 | 752 | list payload → AttributeError |
| 049 | P7 | 765 | "citation parsed as 1" |
| 050 | P8 | 776 | "congenital anomaly" → SearchError |
| 054 | P14 | 791 | page=0 ≠ page=1 |
| 055 | P16 | 800 | `text[start:end + 1]` present |
| 084 | X1 | 813 | "SECRET" in extraction flags |
| 085 | X2 | 822 | courtlistener tier == agency guidance |
| 086 | X3 | 830 | "va.gov/vetapp" in host set |

*(TG2 L840, TG3 L854, TG4 L863 pass.)*

**Deliberately no auto-test:** E5 (live-source semantics), E11 (concurrency), E24 (timing), P15 (by-design), P18 (test-only callers — no deletion). These are covered by the Part-3 diffs and manual review only.

### Full-suite regression check

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  -k "not impersonation_never_sends_bot_user_agent" \
  --deselect tests/test_fetch.py::<socket-impersonation-2> \
  --deselect tests/test_fetch.py::<socket-impersonation-3>
```
**Result: 69 failed, 2357 passed, 3 deselected (23.09 s).** All 69 failures are the audit defect proofs; **zero non-audit failures**. The 3 deselected tests require raw socket impersonation blocked by the sandbox. Baseline excluding the audit file: **2353 passed**.

### Notes on tree evolution and test construction
- **D7** was already fixed in the restored tree; its test passes and is retained as a guard.
- **C7** `F.4th` is already present; the test keeps the still-open 4-digit / `Supp.3d` / curly-`App’x` gaps.
- **P8** results-loop dict guard is already fixed (L1788–1790); the test targets the remaining `"anomaly"` substring at L1780.
- **E23** test constructs `UnicodeEncodeError("utf-8", "", 0, 1, "boom")` (str args, not bytes) — the correct constructor signature; the error escapes `congress.py:217` / `regulations.py` cache handlers that catch only `OSError`.
- Provider class is **`BvaLocalIndexProvider`** (`providers.py:2365`, registered as `"bvalocal"`), not `BvaLocalProvider`.

---

*End of Audit Pass 5. No tracked source files were modified; all fixes are proposed diffs pending approval.*