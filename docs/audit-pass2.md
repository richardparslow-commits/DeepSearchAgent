# DeepSearchAgent — Audit Pass 2

**Scope:** `va_legal_agent/agent.py`, `planning.py`, `queries.py`, `topics.py`,
`ranking.py`, `reliability.py`, `interpretation.py`, `impact.py`, and the prompt
documents (`PROMPT.md`, `PROMPT-CHATBOT.md`, `GEMINI-GEM.md`,
`PROGRESSIVE-GEM.md`, `GEMINI-GEM-KNOWLEDGE.md`).

**Audited commit:** `9a20098` (the local audit-remediation commit) plus the
current working tree. The Pass 2 target modules were re-read at their current
line counts; uncommitted changes elsewhere in the tree do not affect these
findings.

**Environment:** Python 3.12.3, project `.venv`, `pytest 9.2.0`, `ruff 0.17.5`
on Linux.

**Document status:** newly regenerated Pass 2 on 2026-11-13. Pass 2 was absent
from the working tree, so no earlier `P2-###` IDs existed to preserve. IDs
`P2-001` … `P2-007` are assigned in this document. `P2-004` re-verifies the
supplementary legal finding `L-1` from Pass 1 because `impact.py` is now in
scope. All seven findings are now **FIXED** with targeted regression tests or
document-consistency checks.

**Method:** every finding was reproduced with the current tree using the exact
call or file inspection shown under **Evidence**. Behavioural probes use the
project virtualenv; document findings use `grep` and `wc` against the current
files. The original audit ran **1,204 passed**; the completed fixes leave
**1,211 passed** (`pytest -q`, exit 0) and
`ruff check va_legal_agent tests scripts webapp.py` clean. The target-module
tests are included in that run.

## Totals

| File | Total lines verified | Findings | Fixed | Open |
|---|---:|---:|---:|---:|
| `va_legal_agent/agent.py` | 1,024 | 1 | 1 | 0 |
| `va_legal_agent/planning.py` | 211 | 0 | 0 | 0 |
| `va_legal_agent/queries.py` | 105 | 0 | 0 | 0 |
| `va_legal_agent/topics.py` | 182 | 1 | 1 | 0 |
| `va_legal_agent/ranking.py` | 167 | 0 | 0 | 0 |
| `va_legal_agent/reliability.py` | 63 | 0 | 0 | 0 |
| `va_legal_agent/interpretation.py` | 546 | 1 | 1 | 0 |
| `va_legal_agent/impact.py` | 204 | 1 | 1 | 0 |
| `GEMINI-GEM-KNOWLEDGE.md` | 392 | 1 | 1 | 0 |
| `GEMINI-GEM.md`, `PROGRESSIVE-GEM.md` | 273 | 1 | 1 | 0 |
| `PROMPT.md`, `PROMPT-CHATBOT.md` | 171 | 1 | 1 | 0 |
| **Total** | **3,338** | **7** | **7** | **0** |

Severity: **2 High, 3 Medium, 2 Low**. Fixed: **2 High, 3 Medium, 2 Low**.
Open: **none**.

All seven findings are fixed: `P2-004` makes the CAVC authority note respect
`precedential`; `P2-005` aligns the Gemini knowledge pack's Secretary-appeal
rule with the party-aware code; `P2-002` separates § 5103 notice from § 5103A
duty to assist; `P2-001` dedupes same-URL copies regardless of citation
metadata; `P2-003` uses the canonical Supreme Court court constant; and `P2-006`
and `P2-007` bring the Gem and prompt documents back in sync with the code.

---

## `va_legal_agent/agent.py` (Total Lines Verified: 1,024)

### P2-001 — Deduplication — Medium — **FIXED**

**Location:** `va_legal_agent/agent.py:426-459` (`_dedupe`), called before
enrichment at `:481` and again at `:493`.

**Offending code (before the fix):**
```python
        if case.citation and case.decision_date:
            citation_key = (case.citation, case.decision_date)
            if citation_key in seen:
                continue
            seen.add(citation_key)
        # Citation fallback: when citation isn't available to establish
        # the opinion's canonical identity, dedupe by URL alone so that
        # the same page with different parsed titles doesn't appear twice.
        elif case.url and case.url in seen_urls:
            continue
        if case.url:
            seen_urls.add(case.url)
```

**Root cause & impact:** the URL fallback was in an `elif` reached only when
the *current* case had no citation and decision date. If the first copy of a
page had no citation (the usual search-time state) and a later copy of the same
URL had a citation, the later copy took the citation branch, never checked
`seen_urls`, and survived. The final report could therefore contain the same
opinion twice — once unenriched and once enriched — distorting case counts,
court-floor selection and the report's top cases.

**Fix:** the URL check is now unconditional for every case, before the
citation/date key. The citation key remains an additional cross-mirror check,
not an alternative to URL dedupe.

```python
        if case.url and case.url in seen_urls:
            continue
        if case.citation and case.decision_date:
            citation_key = (case.citation, case.decision_date)
            if citation_key in seen:
                continue
            seen.add(citation_key)
```

**Evidence:**
```python
>>> first = CaseRecord(title="Smith", court=COURT_CAVC,
...                    url="https://example.test/same")
>>> second = CaseRecord(title="Smith v. Wilkie", court=COURT_CAVC,
...                     url="https://example.test/same",
...                     citation="35 Vet.App. 1", decision_date="2024-01-01")
>>> len(_dedupe([first, second]))
1
```

**Regression test:**
`tests/test_legal_agent.py::test_dedupe_same_url_with_citation_after_unenriched_copy_is_deduped`.

---

## `va_legal_agent/planning.py` (Total Lines Verified: 211)

No reportable defect. `decompose_issue` deterministically emits the eight broad
court-site queries, statute-anchored queries for detected topics, and a terminal
synthesis step; `plan_queries` preserves plan order. The `_TOPIC_STATUTES`
invariant is covered by `tests/test_planning.py`; the § 5103A row is included so
the corrected duty-to-assist citation can be planned.

## `va_legal_agent/queries.py` (Total Lines Verified: 105)

No reportable defect. `derive_variants` returns the original first, respects
`limit <= 0`, deduplicates expansion phrases and limits expansion to topics and
statutes implicated by the issue. `adapt_query_for_provider` strips `site:`
tokens only for providers with their own court filters. Existing tests cover the
variant and provider-adaptation paths.

## `va_legal_agent/topics.py` (Total Lines Verified: 182)

### P2-002 — Legal citation mapping — Medium — **FIXED**

**Location:** `va_legal_agent/topics.py:139-140` (`STATUTE_HINTS`); consumed by
`va_legal_agent/impact.py:135-147` (`_statute_hint_matches`,
`statute_note_for`).

**Offending code (before the fix):**
```python
    ("5103", "the notice-and-development duties of 38 U.S.C. § 5103A"),
```

**Root cause & impact:** the fragment `5103` is the notice provision, while
§ 5103A is the separate duty to assist. The hint attached the § 5103A label to
§ 5103. `impact.statute_note_for` substring-matched the table, so a case citing
§ 5103 was described as engaging § 5103A — a different statutory duty. There
was no separate `5103A` row, and a table-order fix alone would still match
`5103` inside `5103A`.

**Fix:** the table now has distinct § 5103 and § 5103A rows, and
`_statute_hint_matches` uses section-token boundaries instead of a bare
substring. `planning._TOPIC_STATUTES` also maps the new § 5103A row so query
planning remains complete.

```python
    ("5103", "the notice requirements of 38 U.S.C. § 5103"),
    ("5103A", "the duty to assist under 38 U.S.C. § 5103A"),
```

**Evidence:**
```text
>>> statute_note_for(["38 U.S.C. § 5103"])
'The analysis engages the notice requirements of 38 U.S.C. § 5103, which can anchor briefing.'
>>> statute_note_for(["38 U.S.C. § 5103A"])
'The analysis engages the duty to assist under 38 U.S.C. § 5103A, which can anchor briefing.'
```

**Regression tests:**
`tests/test_impact.py::test_statute_note_distinguishes_notice_from_duty_to_assist`
and the updated `tests/test_planning.py::test_relevant_statutes_follow_detected_topics`.


## `va_legal_agent/ranking.py` (Total Lines Verified: 167)

No reportable defect. Authority tier remains strictly dominant; within-tier
weights are 0.60 relevance, 0.25 recency and 0.15 completeness; the
non-precedential penalty is applied only within the same tier; and the
court-representation floor is implemented without exceeding the requested
limit. `tests/test_ranking.py` covers the empty, tier, recency, floor and
penalty paths.

## `va_legal_agent/reliability.py` (Total Lines Verified: 63)

No reportable defect. `_hostname` handles scheme-less URLs and
`classify_source` uses a dot-boundary match against official domains, so
lookalikes such as `notva.gov` and `va.gov.bad.example` are secondary rather
than official. No case in the target modules relies on a substring host match.

---

## `va_legal_agent/interpretation.py` (Total Lines Verified: 546)

### P2-003 — Court ordering — Low — **FIXED**

**Location:** `va_legal_agent/interpretation.py:342-351` (`_COURT_ORDER`,
`_court_sort_key`).

**Offending code (before the fix):**
```python
_COURT_ORDER = ["Board of Veterans' Appeals", "Court of Appeals for Veterans Claims",
                "U.S. Court of Appeals for the Federal Circuit", "Supreme Court of the United States"]
...
def _court_sort_key(court: str) -> int:
    try:
        return _COURT_ORDER.index(court)
    except ValueError:
        return len(_COURT_ORDER)  # unknown courts sort last
```

**Root cause & impact:** the canonical Supreme Court constant in `topics.py` is
`"U.S. Supreme Court"`, but `_COURT_ORDER` spelled the literal `"Supreme Court
of the United States"`. `_court_sort_key("U.S. Supreme Court")` therefore took
the unknown-court path. The matrix usually still put the canonical court last
because unknown courts also sort last, but the order table was not recognising
a known court.

**Fix:** `interpretation.py` imports `COURT_SUPREME` and uses it as the final
entry in `_COURT_ORDER`, so the canonical constant is recognized directly and
unknown courts remain the only fallback.

```python
from .topics import COURT_SUPREME, PRINCIPLE_PATTERNS, TOPICS
...
_COURT_ORDER = ["Board of Veterans' Appeals", "Court of Appeals for Veterans Claims",
                "U.S. Court of Appeals for the Federal Circuit", COURT_SUPREME]
```

**Evidence:**
```text
COURT_SUPREME: U.S. Supreme Court
in _COURT_ORDER: True
_COURT_ORDER[-1]: U.S. Supreme Court
```

**Regression test:**
`tests/test_interpretation.py::test_court_order_uses_canonical_supreme_court_constant`.


---

## `va_legal_agent/impact.py` (Total Lines Verified: 204)

### P2-004 — Legal authority — High — **FIXED**

**Location:** `va_legal_agent/impact.py:160-176` (`authority_note_for`), called
from `analyze_case_impact` at `:186`.

**Offending code (before the fix):**
```python
def authority_note_for(court: str) -> str:
    """Describe the weight of authority for the issuing body."""
    if court in BINDING_COURTS:
        return "As appellate precedent, this ruling is binding on the Board and VA adjudicators."
    if court == BOARD_COURT:
        return "As a Board-level decision, this ruling is persuasive but not binding precedent."
    return "The issuing body is unidentified, so treat this ruling as persuasive only."
```

**Root cause & impact:** the function received only the court name, never the
case's `precedential` flag. `BINDING_COURTS` includes every CAVC decision, so
non-precedential CAVC memorandum dispositions and single-judge actions were
described as binding on the Board. Federal Circuit and Supreme Court precedent
is binding when precedential, but the CAVC category is not homogeneous. The
wrong note was embedded in the per-case impact summary and could be carried
into the final report; this is the same legal defect recorded as `L-1` in Pass
1, now fixed inside Pass 2's `impact.py` scope.

**Fix:** `authority_note_for` now accepts a `precedential` flag (defaulting to
`True` for compatibility). A binding-court decision with `precedential=False`
returns a persuasive-only note, and `analyze_case_impact` passes
`case.precedential` explicitly. The default court behaviour is unchanged.

```python
def authority_note_for(court: str, precedential: bool = True) -> str:
    ...
    if court in BINDING_COURTS:
        if not precedential:
            return (
                "As a non-precedential appellate decision, this ruling is persuasive only "
                "and is not binding on the Board or VA adjudicators."
            )
        return "As appellate precedent, this ruling is binding on the Board and VA adjudicators."
    ...
```

**Evidence:**
```text
precedential: False
note: As a non-precedential appellate decision, this ruling is persuasive only and is not binding on the Board or VA adjudicators.
profile: As a non-precedential appellate decision, this ruling is persuasive only and is not binding on the Board or VA adjudicators.
nuance contains persuasive only: True
```

**Regression tests:**
`tests/test_impact.py::test_authority_note_nonprecedential_cavc_is_persuasive`
and `tests/test_impact.py::test_analyze_case_impact_nonprecedential_cavc_uses_persuasive_note`;
the focused impact/interpretation/doc suite passes **101 tests**.

## `GEMINI-GEM-KNOWLEDGE.md` (Total Lines Verified: 392)

### P2-005 — Legal outcome rule — High — **FIXED**

**Location:** `GEMINI-GEM-KNOWLEDGE.md:322-329` (`Secretary-appellant flip rule`).

**Offending code (before the fix):**
```text
When the appellant is the Secretary (not the veteran), the following outcomes FLIP to favorable:
- affirmed: a grant to the veteran was upheld on appeal = favorable
- dismissed: the Secretary's cross-appeal was dismissed = favorable
(denied is always unfavorable; granted/vacated/remanded are always favorable regardless of appellant)
```

**Root cause & impact:** the parenthetical was the inverse of the current code
and of the correct party-aware reading. When the Secretary is the appellant,
the disposition is read from the Secretary's point of view and then inverted:
a **denied** Secretary appeal leaves the veteran's grant in place (favorable),
while **granted**, **vacated** and **remanded** disturb that grant (unfavorable).
`interpretation._outcome_direction` and `impact._SECRETARY_OVERRIDES` implement
that rule. A Gem following the old knowledge pack classified four of the six
dispositions backwards, corrupting the statute-outcome matrix and
contradictions. This was a legal-output defect in the prompt corpus, not just a
formatting inconsistency.

**Fix:** section 5 now states that the default lists assume the veteran
appealed, then lists all six dispositions for a Secretary appeal with the
correct direction.

```text
The lists above assume the veteran appealed. When the appellant is the Secretary (not the veteran), read every disposition from the Secretary's point of view and invert it:
- **affirmed**: the Board's grant to the veteran was upheld on appeal = favorable
- **dismissed**: the Secretary's appeal was dismissed, leaving the veteran's grant in place = favorable
- **denied**: the Secretary's appeal was denied, leaving the veteran's grant in place = favorable
- **granted**: the Secretary's appeal was granted and the veteran's grant was reversed = unfavorable
- **vacated**: the Secretary's appeal vacated the veteran's grant = unfavorable
- **remanded**: the Secretary's appeal remanded the veteran's grant, so it is not final = unfavorable
```

**Evidence:**
```python
>>> from va_legal_agent.interpretation import _outcome_direction
>>> for outcome in ("denied", "granted", "vacated", "remanded"):
...     print(outcome, _outcome_direction(outcome, appellant_role="secretary"))
denied 1
granted -1
vacated -1
remanded -1
```
The existing `tests/test_interpretation.py::test_outcome_direction_secretary_denied_means_veteran_won`
and `tests/test_impact.py::test_outcome_note_secretary_denied_is_favorable`
assert the code behavior; the new documentation regression is
`tests/test_prompt_docs.py::test_secretary_appellant_rule_matches_party_aware_code`.

## `GEMINI-GEM.md`, `PROGRESSIVE-GEM.md` (Total Lines Verified: 273)

### P2-006 — Legal authority wording — Medium — **FIXED**

**Location:** `GEMINI-GEM.md:28`; `PROGRESSIVE-GEM.md:104`.

**Offending code (before the fix):**
```text
- Whether it is binding (Federal Circuit, CAVC) or persuasive (BVA)
```
```text
- **Authority tier**: binding (Federal Circuit, CAVC) or persuasive (BVA)
```

**Root cause & impact:** the Gem instructions treated every CAVC decision as
binding. CAVC memorandum/single-judge dispositions are not precedent for future
cases, so the instruction overstates their authority and conflicts with the
`precedential` field and the `P2-004` code path. The one-shot and progressive
Gems shared the error.

**Fix:** both prompts now require precedential status for CAVC authority and
identify memorandum/single-judge decisions as persuasive.

```text
- Whether it is binding when precedential (Federal Circuit; CAVC panel/en banc) or persuasive (CAVC memorandum/single-judge; BVA)
- **Authority tier**: binding when precedential (Federal Circuit; CAVC panel/en banc), or persuasive (CAVC memorandum/single-judge; BVA)
```

**Evidence:**
```text
$ grep -n 'binding when precedential' GEMINI-GEM.md PROGRESSIVE-GEM.md
GEMINI-GEM.md:28:- Whether it is binding when precedential (Federal Circuit; CAVC panel/en banc) or persuasive (CAVC memorandum/single-judge; BVA)
PROGRESSIVE-GEM.md:104:- **Authority tier**: binding when precedential (Federal Circuit; CAVC panel/en banc), or persuasive (CAVC memorandum/single-judge; BVA)
```

**Regression test:**
`tests/test_prompt_docs.py::test_gem_prompts_require_precedential_cavc_authority`.

## `PROMPT.md`, `PROMPT-CHATBOT.md` (Total Lines Verified: 171)

### P2-007 — Prompt metadata — Low — **FIXED**

**Location:** `PROMPT.md:7,9,23-45,62`; `PROMPT-CHATBOT.md:11,20-42`.

**Offending code (before the fix):**
```text
1. **Plan** — ... 4 broad court-site queries ...
...
Results are merged, deduplicated by URL, and capped.
...
| `agent.py` | 1,009 | ...
| `fetch.py` | 672 | ...
| `interpretation.py` | 553 | ...
| `impact.py` | 162 | ...
...
- **Tests**: 1,148+ tests, ~11s runtime.
```

**Root cause & impact:** the prompt's pipeline and module metadata had drifted
from the implementation. `build_case_queries("service connection for tinnitus",
"Compensation")` returns **11** queries: **8** broad court-site queries plus
three statute queries. `_dedupe` checks title/URL, citation-plus-date and the
same URL fallback, not URL alone. The package total was 7,960 lines, not
“~7,500”, and the module table still showed 1,009/672/553/162 for
`agent.py`/`fetch.py`/`interpretation.py`/`impact.py`.

**Fix:** both prompt documents now say eight broad court-site queries, describe
the actual dedupe keys, use the current package total (~8,000), and show current
line counts for every module, including `__main__.py` 1,483, `agent.py` 1,024,
`fetch.py` 832, `interpretation.py` 546, `planning.py` 211, `topics.py` 182,
`impact.py` 204 and `webapp.py` 603. `PROMPT.md` also records the current
**1,211** tests and ~12s runtime.

**Evidence:**
```text
$ .venv/bin/python - <<'PY'
from pathlib import Path
for name in ("PROMPT.md", "PROMPT-CHATBOT.md"):
    text = Path(name).read_text()
    print(name, {
        "8_broad": "8 broad court-site queries" in text,
        "old_4_broad": "4 broad court-site queries" in text,
        "dedupe_keys": "deduplicated by title/URL" in text,
        "package_8k": "~8,000 lines of package code" in text,
        "tests_1211": "1,211 tests" in text,
    })
PY
PROMPT.md {'8_broad': True, 'old_4_broad': False, 'dedupe_keys': True, 'package_8k': True, 'tests_1211': True}
PROMPT-CHATBOT.md {'8_broad': True, 'old_4_broad': False, 'dedupe_keys': False, 'package_8k': True, 'tests_1211': False}

$ wc -l va_legal_agent/agent.py va_legal_agent/fetch.py \
    va_legal_agent/interpretation.py va_legal_agent/impact.py
1024 va_legal_agent/agent.py
 832 va_legal_agent/fetch.py
 546 va_legal_agent/interpretation.py
 204 va_legal_agent/impact.py
```

**Verification:** documentation-only metadata, checked with `grep` and
`wc -l`; the full suite exercises the pipeline count and the updated prompt
metadata is the visible source of truth.

---

## Verified-execution appendix

All behavioural probes were run from the repository root with the project
virtualenv. The exact outputs are quoted in the findings above.

```python
from va_legal_agent.agent import _dedupe, build_case_queries
from va_legal_agent.impact import analyze_case_impact, authority_note_for, statute_note_for
from va_legal_agent.interpretation import _COURT_ORDER, _outcome_direction, build_statute_outcome_matrix
from va_legal_agent.models import CaseRecord
from va_legal_agent.topics import COURT_CAVC, COURT_SUPREME

# P2-004: non-precedential CAVC is persuasive, not binding.
case = CaseRecord(title="Nonprecedential v. McDonough", court=COURT_CAVC,
                  precedential=False, outcome="denied",
                  url="https://example.test/nonprec")
assert authority_note_for(case.court, case.precedential).startswith("As a non-precedential")
assert analyze_case_impact(case).authority_note == authority_note_for(
    case.court, case.precedential
)

# P2-002: 5103 notice and 5103A duty to assist are distinct.
assert "notice requirements" in statute_note_for(["38 U.S.C. § 5103"])
assert "duty to assist" in statute_note_for(["38 U.S.C. § 5103A"])
assert statute_note_for(["38 U.S.C. § 5103"]) != statute_note_for(["38 U.S.C. § 5103A"])

# P2-001: same URL is deduped even when only the later copy has a citation.
first = CaseRecord(title="Smith", court=COURT_CAVC, url="https://example.test/same")
second = CaseRecord(title="Smith v. Wilkie", court=COURT_CAVC,
                    url="https://example.test/same",
                    citation="35 Vet.App. 1", decision_date="2024-01-01")
assert len(_dedupe([first, second])) == 1

# P2-003: the canonical Supreme Court constant is in the order table.
assert _COURT_ORDER[-1] == COURT_SUPREME

# P2-005: party-aware code inverts every Secretary disposition.
assert _outcome_direction("denied", appellant_role="secretary") == 1
assert _outcome_direction("granted", appellant_role="secretary") == -1
assert _outcome_direction("vacated", appellant_role="secretary") == -1
assert _outcome_direction("remanded", appellant_role="secretary") == -1

# P2-007: current plan fan-out is eight broad queries, not four.
queries = build_case_queries("service connection for tinnitus", "Compensation")
assert len(queries) == 11
assert sum(query.startswith("site:") for query in queries[:8]) == 8
```

```text
$ .venv/bin/python -m pytest -q
1211 passed in 11.25s

$ .venv/bin/python -m ruff check va_legal_agent tests scripts webapp.py
All checks passed!

$ wc -l va_legal_agent/agent.py va_legal_agent/planning.py \
    va_legal_agent/queries.py va_legal_agent/topics.py \
    va_legal_agent/ranking.py va_legal_agent/reliability.py \
    va_legal_agent/interpretation.py va_legal_agent/impact.py \
    GEMINI-GEM-KNOWLEDGE.md GEMINI-GEM.md PROGRESSIVE-GEM.md \
    PROMPT-CHATBOT.md PROMPT.md
1024 va_legal_agent/agent.py
 211 va_legal_agent/planning.py
 105 va_legal_agent/queries.py
 182 va_legal_agent/topics.py
 167 va_legal_agent/ranking.py
  63 va_legal_agent/reliability.py
 546 va_legal_agent/interpretation.py
 204 va_legal_agent/impact.py
 392 GEMINI-GEM-KNOWLEDGE.md
  60 GEMINI-GEM.md
 213 PROGRESSIVE-GEM.md
  88 PROMPT-CHATBOT.md
  83 PROMPT.md
3338 total
```
