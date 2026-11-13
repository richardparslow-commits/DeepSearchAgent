# DeepSearchAgent — Consolidated Defect Register

**Status date:** 2026-11-13

**Registered:** Audit Pass 1 (32 findings) and the supplementary legal finding `L-1`.
**Pending:** Audit Pass 2 (source document is not present in this working tree).
**Reserved:** Passes 3 and 4, append-only.

**Sources:** [Audit Pass 1](audit-pass1.md) · [Audit Pass 3](audit-pass3.md)

> **Pass 2 is missing.** Only `docs/audit-pass1.md` and `docs/audit-pass3.md` exist in this
> working tree. No `P2-###` rows are registered, because inferring them from the code would
> not be a consolidation of Pass 2 — it would be a new audit. The [Pass 2 intake](#pass-2-intake)
> section defines how to append them when the source is supplied.

IDs preserve their source: `P1-###` = Pass 1, `P2-###` = Pass 2, `P3-###` / `P4-###` =
later passes, `L-1` = supplementary legal-premise check. Do not renumber existing IDs.

## Source status

| Pass | Source document | Registered | Notes |
|---|---|---:|---|
| 1 | `docs/audit-pass1.md` | 32 | 8 High, 16 Medium, 8 Low; 4 fixed |
| 2 | — | 0 | **Source missing**; no rows fabricated |
| 3 | `docs/audit-pass3.md` | 0 | 22 findings reserved for a later append |
| 4 | — | 0 | Reserved |
| Legal check | `docs/audit-pass1.md` § Legal-premise check | 1 | `L-1`, High |

## Summary

| Severity | Total | Open | Closed |
|---|---:|---:|---:|
| High | 9 | 6 | 3 |
| Medium | 16 | 15 | 1 |
| Low | 8 | 8 | 0 |
| **Total** | **33** | **29** | **4** |

Pass 1 alone contributes 32 findings (8 High, 16 Medium, 8 Low; 28 open, 4 closed). The ninth
High is the supplementary legal finding `L-1`: it is outside Pass 1's file scope but is a
known release-blocking defect recorded in the same audit document, so it is included here
rather than left out of the consolidated view.

## Severity and disposition

**Severity** is the triage recorded by the source pass; this register does not silently
re-triage it.

| Severity | Meaning |
|---|---|
| **High** | Release-blocking: credential exposure, legal misstatement, data loss, or an unbounded/looping run. |
| **Medium** | Material correctness, security, or robustness defect with bounded impact or a practical workaround. |
| **Low** | Minor robustness, observability, or configuration defect; not release-blocking. |

**Status** is `Open` or `Closed`. `Closed` means the fix and its regression test are already
in the tree; it does not mean this register independently re-verified the fix.

**Suggested disposition** is a release recommendation, not a status:

| Disposition | Meaning |
|---|---|
| `Fix now` | Release blocker; do not ship with this open. |
| `Fix next` | Schedule in the next remediation pass. |
| `Backlog` | Accept for now; revisit after High and Medium items. |
| `Closed — verify` | Fix is in the tree; keep and run the regression test. |

## Register

Sorted by severity; within a severity band, open items precede closed ones, then numeric ID.

| Severity | ID | File and line range | One-line defect | Status | Suggested disposition |
|---|---|---|---|---|---|
| High | P1-001 | `va_legal_agent/__main__.py:183-210` | Scheme-less `SEARCH_HTTP_PROXY` bypasses `_mask_proxy_url`, printing credentials in `--show-config`. | Open | Fix now |
| High | P1-004 | `va_legal_agent/__main__.py:726-818, 769` | `--max-batch-requests <= 0` defers every row, and `--auto-run` then loops indefinitely. | Open | Fix now |
| High | P1-007 | `va_legal_agent/config.py:88-105` | `env_float` accepts `nan`/`inf`, disabling throttling or crashing `time.sleep`. | Open | Fix now |
| High | P1-013 | `va_legal_agent/providers.py:817-880` | Statute-only BVA queries minimize to `""` and return unfiltered recent decisions. | Open | Fix now |
| High | P1-026 | `va_legal_agent/fetch.py:300-303` | `_normalize_statute_value` strips real two-letter subsections (`§ 3.1(aa)` becomes `§ 3.1`). | Open | Fix now |
| High | L-1 | `va_legal_agent/impact.py:144-150` | Every CAVC case, including non-precedential memoranda, is described as binding on the Board. | Open | Fix now |
| High | P1-022 | `va_legal_agent/fetch.py:346-400` | Outcome scan read recitals as dispositions; fixed by scoping to the ORDER/CONCLUSION block. | Closed | Closed — verify |
| High | P1-023 | `va_legal_agent/fetch.py:552-578` | Body-wide non-precedential markers demoted published opinions; fixed by reading the header only. | Closed | Closed — verify |
| High | P1-025 | `va_legal_agent/fetch.py:60-100` | Quoted reporter citations overrode the decision's own BVA citation; fixed to prefer the own citation. | Closed | Closed — verify |
| Medium | P1-002 | `va_legal_agent/__main__.py:176-180` | `_mask_secret` reveals nearly all of an 8–9 character secret. | Open | Fix next |
| Medium | P1-003 | `va_legal_agent/__main__.py:183-210` | A non-numeric proxy port raises `ValueError` and crashes `--show-config`. | Open | Fix next |
| Medium | P1-005 | `va_legal_agent/__main__.py:1024-1032` | `_auto_run` discards exception type and traceback, leaving batch failures undebuggable. | Open | Fix next |
| Medium | P1-006 | `va_legal_agent/__main__.py:1033-1042` | Auto-run assumes every remainder is quota-blocked; a usage lookup failure aborts the batch. | Open | Fix next |
| Medium | P1-009 | `va_legal_agent/models.py:37` | Unknown precedential status defaults to `True`, overstating authority and skipping the penalty. | Open | Fix next |
| Medium | P1-011 | `va_legal_agent/llm.py:27-51` | Untrusted case text enters the prompt without delimiters or an instruction hierarchy. | Open | Fix next |
| Medium | P1-014 | `va_legal_agent/providers.py:250-320` | Empty or boilerplate CourtListener queries run a court-only search. | Open | Fix next |
| Medium | P1-015 | `va_legal_agent/providers.py:1619-1640` | Exclusion terms lack word boundaries, so `back` also removes `background`. | Open | Fix next |
| Medium | P1-016 | `va_legal_agent/providers.py:621-631` | Malformed minute payloads return `{}` and silently disable the budget guard. | Open | Fix next |
| Medium | P1-019 | `va_legal_agent/search.py:39-42` | `_is_challenge` matches `anomaly` anywhere, discarding valid DuckDuckGo results. | Open | Fix next |
| Medium | P1-020 | `va_legal_agent/search.py:151-163` | DuckDuckGo redirect URLs are accepted without scheme validation (`javascript:`, `data:`, `file:`). | Open | Fix next |
| Medium | P1-027 | `va_legal_agent/fetch.py:228-245` | Legal-standard extraction uses tuple order, so a later `clear error` mention beats an earlier `de novo`. | Open | Fix next |
| Medium | P1-028 | `va_legal_agent/fetch.py:114-130` | Filing date outranks decision date, misdating authority by months or years. | Open | Fix next |
| Medium | P1-030 | `va_legal_agent/fetch.py:718-745, 628-645` | Enrichment reads only four PDF pages, so holdings on page 5+ are recorded as empty. | Open | Fix next |
| Medium | P1-031 | `va_legal_agent/deep_read.py:49-55, 57-80` | Facts extraction fails when a facts block ends at a chunk boundary without a blank line. | Open | Fix next |
| Medium | P1-024 | `va_legal_agent/fetch.py:138-160, 328-345` | Single-`§` pattern dropped plural `§§` citations; fixed to accept plural cites. | Closed | Closed — verify |
| Low | P1-008 | `va_legal_agent/config.py:58-85` | Unknown provider keys in the page-cap map are silently accepted. | Open | Backlog |
| Low | P1-010 | `va_legal_agent/models.py:133` | `coverage_score` lacks 0–1 bounds, so invalid scores print as valid. | Open | Backlog |
| Low | P1-012 | `va_legal_agent/llm.py:23-24` | Whitespace-only `OPENAI_API_KEY` is treated as enabled, causing a failed call per issue. | Open | Backlog |
| Low | P1-017 | `va_legal_agent/providers.py:634-659` | Non-numeric usage rows raise bare `ValueError`, bypassing `SearchError` handling. | Open | Backlog |
| Low | P1-018 | `va_legal_agent/providers.py:1241-1290` | Naive local-index timestamps raise `TypeError` instead of triggering a rebuild. | Open | Backlog |
| Low | P1-021 | `va_legal_agent/search.py:170-205` | Zero-result pages are classified as provider failures. | Open | Backlog |
| Low | P1-029 | `va_legal_agent/fetch.py:328-345` | Six U.S.C. citations exhaust the cap, dropping all C.F.R. regulations. | Open | Backlog |
| Low | P1-032 | `va_legal_agent/deep_read.py:156-170` | Deep-read synthesis emits `- (nothing relevant)` sentinel lines as content. | Open | Backlog |

## Pass 2 intake

Pass 2 is not present in this working tree as of 2026-11-13; the only audit documents are
`docs/audit-pass1.md` and `docs/audit-pass3.md`. The register therefore contains no `P2-###`
rows. This is deliberate: inferring Pass 2 findings from the code or from Pass 1 would
create a new audit and corrupt the register's provenance.

When the Pass 2 document is supplied:

1. Retain the source's `P2-###` IDs exactly; do not renumber.
2. Add its rows to the single [Register](#register) table with the same six columns.
3. Use `Open` unless the source marks the finding fixed; use `Closed — verify` for fixed.
4. Re-sort: High → Medium → Low; within a band, Open before Closed, then numeric ID.
5. Update [Source status](#source-status) and [Summary](#summary).

## Appending Pass 3 / Pass 4

Pass 3 already exists (`docs/audit-pass3.md`, 22 findings) but is deliberately not copied
here yet. Pass 4 is reserved. The register is append-only:

- IDs: `P3-###`, `P4-###`; never reuse or renumber an existing ID.
- Columns: `Severity | ID | File and line range | One-line defect | Status | Suggested disposition`.
- Sorting: High → Medium → Low; within a band, Open before Closed, then numeric ID.
- Supersession: keep the original row; change its disposition to `Superseded by P3-###`
  (or add a note) rather than deleting it.
- Recompute [Summary](#summary) and [Source status](#source-status) after each append.

Template row:

```markdown
| High | P3-001 | `path/to/file.py:12-34` | One-line defect. | Open | Fix now |
```

## Notes

- Line ranges are the source pass's audited locations. A later fix may have shifted the
  current line numbers; use the source finding to relocate before editing.
- Severity is not re-triaged here. If a later pass disagrees, keep the source severity and
  record the disagreement in the disposition or a note rather than silently changing rank.
- `Closed — verify` means the fix and its regression test are in the tree. It does not mean
  this register independently re-ran that test.
- The source findings remain the audit trail for evidence and remediation detail; the ID in
  this register points back to them.
- Unregistered legal-premise observations (the § 5103/§ 5103A hint conflation and the
  "citable as binding authority" comments) remain in `docs/audit-pass1.md`; they have no
  finding ID or severity and are not added here as new defects.
