# DeepSearchAgent — Consolidated Defect Register

**Status date:** 2026-11-14

**Registered here:** Audit Pass 1 (32 findings) and the supplementary legal finding `L-1`.
**Source-complete:** Audit Pass 2 (7 findings) and Audit Pass 3 (22 findings) are fully
closed in their source documents. They are summarized below but not row-expanded here,
because this maintenance update reconciles the remaining-core audit report; their source
documents remain authoritative.
**Reserved:** Pass 4.

**Sources:** [Audit Pass 1](audit-pass1.md) · [Audit Pass 2](audit-pass2.md) ·
[Audit Pass 3](audit-pass3.md) · [Remaining core layer](audit-pass3-remaining.md)

IDs preserve their source: `P1-###` = Pass 1, `P2-###` = Pass 2, `P3-###` / `P4-###` =
later passes, `L-1` = supplementary legal-premise check. Do not renumber existing IDs.

## Source status

| Pass | Source document | Findings | Rows here | Current state |
|---|---|---:|---:|---|
| 1 | `docs/audit-pass1.md` | 32 | 32 | 32 Closed, 0 Partial, 0 Open |
| 2 | `docs/audit-pass2.md` | 7 | 0 | 7 Closed; source document only |
| 3 | `docs/audit-pass3.md` | 22 | 0 | 22 Closed; source document only |
| 4 | — | 0 | 0 | Reserved |
| Legal check | `docs/audit-pass1.md` § Legal-premise check | 1 | 1 | `L-1`, High; Closed |

## Summary

| Severity | Total | Open | Partial | Closed |
|---|---:|---:|---:|---:|
| High | 9 | 0 | 0 | 9 |
| Medium | 16 | 0 | 0 | 16 |
| Low | 8 | 0 | 0 | 8 |
| **Total** | **33** | **0** | **0** | **33** |

Pass 1 alone contributes 32 findings: **32 Closed, 0 Partial, 0 Open**, matching the
remaining-core audit report. The ninth High is the supplementary legal finding `L-1`: it is
outside Pass 1's file scope but is a fixed release-blocking defect recorded in the same
audit document, so it is included here rather than left out of the consolidated view.

Pass 2 has **2 High, 3 Medium, 2 Low**, all Closed. Pass 3 has **3 High, 10 Medium,
9 Low**, all Closed. They are not part of the 33-row table below; see
[Pass 2 / Pass 3 status](#pass-2--pass-3-status).

## Severity and disposition

**Severity** is the triage recorded by the source pass; this register does not silently
re-triage it.

| Severity | Meaning |
|---|---|
| **High** | Release-blocking: credential exposure, legal misstatement, data loss, or an unbounded/looping run. |
| **Medium** | Material correctness, security, or robustness defect with bounded impact or a practical workaround. |
| **Low** | Minor robustness, observability, or configuration defect; not release-blocking. |

**Status** is `Open`, `Partial`, or `Closed`. `Partial` means a mitigation is in the tree
but the source finding remains actionable; `Closed` means the fix and its regression test
are in the tree, not that this register independently re-verified every fix.

**Suggested disposition** is a release recommendation, not a status:

| Disposition | Meaning |
|---|---|
| `Fix now` | Release blocker; do not ship with this open. |
| `Fix next` | Schedule in the next remediation pass. |
| `Backlog` | Accept for now; revisit after High and Medium items. |
| `Closed — verify` | Fix is in the tree; keep and run the regression test. |

## Register

Sorted by severity; within a severity band, Open before Partial before Closed, then numeric
ID. Line ranges are current for the remediated P1 rows in this tree.

| Severity | ID | File and line range | One-line defect | Status | Suggested disposition |
|---|---|---|---|---|---|
| High | P3-001 | `path/to/file.py:12-34` | One-line defect. | Open | Fix now |
| High | P1-001 | `va_legal_agent/__main__.py:218-245` | Scheme-less `SEARCH_HTTP_PROXY` bypassed `_mask_proxy_url`; fixed by prepending `//` for parsing and redacting userinfo. | Closed | Closed — verify |
| High | P1-004 | `va_legal_agent/__main__.py:784-785, 1019-1179, 1431-1434` | Non-positive `--max-batch-requests` deferred every row and `--auto-run` looped indefinitely; fixed with positive validation and bounded, stall-aware retries. | Closed | Closed — verify |
| High | P1-007 | `va_legal_agent/config.py:31, 94-125` | `env_float` accepted `nan`/`inf`; fixed to reject non-finite values and cap sleep/backoff/timeout settings at 3600 s (wall-clock deadlines remain unbounded but finite). | Closed | Closed — verify |
| High | P1-013 | `va_legal_agent/providers.py:817-858` | Statute-only BVA queries minimized to `""`; the shared minimizer now keeps the site-stripped original. | Closed | Closed — verify |
| High | P1-022 | `va_legal_agent/fetch.py:346-400` | Outcome scan read recitals as dispositions; fixed by scoping to the ORDER/CONCLUSION block. | Closed | Closed — verify |
| High | P1-023 | `va_legal_agent/fetch.py:552-578` | Body-wide non-precedential markers demoted published opinions; fixed by reading the header only. | Closed | Closed — verify |
| High | P1-025 | `va_legal_agent/fetch.py:60-100` | Quoted reporter citations overrode the decision's own BVA citation; fixed to prefer the own citation. | Closed | Closed — verify |
| High | P1-026 | `va_legal_agent/fetch.py:312-321` | `_normalize_statute_value` stripped real attached subsections (`§ 3.1(aa)` became `§ 3.1`, nested `(iv)` was lost); fixed to preserve attached subsections. | Closed | Closed — verify |
| High | L-1 | `va_legal_agent/impact.py:226-258` | Every CAVC case, including non-precedential memoranda, was described as binding; fixed to pass the case's three-state `precedential` value into the authority note. | Closed | Closed — verify |
| Medium | P1-002 | `va_legal_agent/__main__.py:187-196` | `_mask_secret` revealed nearly all of an 8–9 character secret; fixed to mask values of 16 characters or fewer completely. | Closed | Closed — verify |
| Medium | P1-003 | `va_legal_agent/__main__.py:230-236` | A non-numeric proxy port raised `ValueError` and crashed `--show-config`; fixed to omit the bad port while keeping the host visible. | Closed | Closed — verify |
| Medium | P1-005 | `va_legal_agent/__main__.py:138-160, 1126-1134, 1232-1255` | Analysis failures now retain the exception type and full traceback in the failure event and per-issue error row, and the summary exposes an errors list. | Closed | Closed — verify |
| Medium | P1-006 | `va_legal_agent/__main__.py:1181-1208` | Auto-run retries one transient usage-lookup `SearchError` after a bounded wait and stops after repeated failures; no-CourtListener pending batches skip the wait. | Closed | Closed — verify |
| Medium | P1-009 | `va_legal_agent/models.py:31-38`; `fetch.py:594-616` | Unknown precedential status defaulted to `True`, overstating authority; fixed to a tri-state value where `None` means unknown, no penalty, and a verification flag. | Closed | Closed — verify |
| Medium | P1-011 | `va_legal_agent/llm.py:22-63, 100-177`; `deep_read.py:135-168` | Untrusted case text entered LLM prompts without delimiters or an instruction hierarchy; fixed by escaping and delimiting narrative, reasoning, and reduce prompts. | Closed | Closed — verify |
| Medium | P1-014 | `va_legal_agent/providers.py:407-409` | Empty or boilerplate CourtListener queries are rejected before a court-only search. | Closed | Closed — verify |
| Medium | P1-015 | `va_legal_agent/providers.py:1638-1642` | Exclusion terms are matched as whole words, so `back` no longer removes `background` or `feedback`. | Closed | Closed — verify |
| Medium | P1-016 | `va_legal_agent/providers.py:628-665` | Malformed minute payloads raise while a well-formed payload with no minute row returns `None`; the guard checks explicitly. | Closed | Closed — verify |
| Medium | P1-019 | `va_legal_agent/search.py:39-70` | Challenge detection is structural (202, `/anomaly`, title, challenge/captcha containers), not an arbitrary body substring. | Closed | Closed — verify |
| Medium | P1-020 | `va_legal_agent/search.py:180-206`; `fetch.py:31-35, 731, 776` | Decoded DuckDuckGo destinations and both fetch entry points accept only absolute HTTP(S) URLs, dropping `javascript:`, `data:`, and `file:`. | Closed | Closed — verify |
| Medium | P1-024 | `va_legal_agent/fetch.py:330-338` | Single-`§` pattern dropped plural `§§` citations; fixed to accept plural cites. | Closed | Closed — verify |
| Medium | P1-027 | `va_legal_agent/fetch.py:237-243, 277-298` | Legal-standard extraction now takes the earliest document-order match, so an opening de novo standard survives a later clear-error reference; `review legal questions de novo` is recognized. | Closed | Closed — verify |
| Medium | P1-028 | `va_legal_agent/fetch.py:49-54, 141-167` | Decision-date hints are ranked decided > issued > filed > handed down, so a filing date no longer displaces the decision date. | Closed | Closed — verify |
| Medium | P1-030 | `va_legal_agent/fetch.py:840-844` | Enrichment now reads every PDF page, so holdings on page 5+ are no longer recorded as empty. | Closed | Closed — verify |
| Medium | P1-031 | `va_legal_agent/deep_read.py:50-55, 58-72` | Facts extraction now treats the chunk boundary as a section terminator while keeping an empty facts section empty. | Closed | Closed — verify |
| Low | P1-008 | `va_legal_agent/config.py:64-103` | Unknown provider keys in provider override maps were silently accepted; fixed to validate against `_PROVIDERS` and skip them with a warning. | Closed | Closed — verify |
| Low | P1-010 | `va_legal_agent/models.py:134`; `interpretation.py:554-562` | `coverage_score` lacked 0–1 bounds; fixed with bounded model validation and a producer clamp. | Closed | Closed — verify |
| Low | P1-012 | `va_legal_agent/llm.py:48-49, 74` | Whitespace-only `OPENAI_API_KEY` was treated as enabled; fixed to strip the key before testing it and before constructing the client. | Closed | Closed — verify |
| Low | P1-017 | `va_legal_agent/providers.py:640-675` | Non-numeric usage rows raised bare `ValueError`; fixed to coerce counters and raise `SearchError` for malformed payloads. | Closed | Closed — verify |
| Low | P1-018 | `va_legal_agent/providers.py:1289-1305` | Naive local-index timestamps raised `TypeError`; fixed to treat them as UTC and treat un-ageable timestamps as stale. | Closed | Closed — verify |
| Low | P1-021 | `va_legal_agent/search.py:242-245` | Zero-result pages were classified as provider failures; fixed to return an empty result list and record zero-result telemetry. | Closed | Closed — verify |
| Low | P1-032 | `va_legal_agent/deep_read.py:167-183` | Deep-read synthesis emitted `- (nothing relevant)` sentinel lines; fixed to filter the sentinel and return an empty summary when no usable digest remains. | Closed | Closed — verify |

## Pass 2 / Pass 3 status

Both source documents are present in this working tree and contain no open findings:

- **Pass 2:** `P2-001` … `P2-007` — 2 High, 3 Medium, 2 Low; all Closed.
- **Pass 3:** `P3-001` … `P3-022` — 3 High, 10 Medium, 9 Low; all Closed.

They are not row-expanded in this maintenance update because the remaining-core audit
report is the P1 scope being reconciled here. Their source documents remain the audit trail
for evidence and remediation. Do not infer their IDs from code; append them verbatim when
the register is next consolidated.

## Appending later passes

Pass 4 is reserved. If Pass 2 or Pass 3 rows are expanded into this table later, preserve
their source IDs exactly and keep the six-column schema:

- IDs: `P2-###`, `P3-###`, `P4-###`; never reuse or renumber an existing ID.
- Columns: `Severity | ID | File and line range | One-line defect | Status | Suggested disposition`.
- Sorting: High → Medium → Low; within a band, Open → Partial → Closed, then numeric ID.
- Supersession: keep the original row; change its disposition to `Superseded by P3-###`
  (or add a note) rather than deleting it.
- Recompute [Summary](#summary) and [Source status](#source-status) after each append.

Template row:

```markdown
| Low | P1-029 | `va_legal_agent/fetch.py:341-355` | Six U.S.C. citations exhausted the cap, dropping all C.F.R. regulations; fixed to reserve C.F.R. slots. | Closed | Closed — verify |
```

## Notes

- Line ranges are current for the remediated P1 rows where the audit report supplies current
  evidence. Relocate before editing.
- Severity is not re-triaged here. If a later pass disagrees, keep the source severity and
  record the disagreement in the disposition or a note rather than silently changing rank.
- `Closed — verify` means the fix and its regression test are in the tree. It does not mean
  this register independently re-ran that test.
- The source findings remain the audit trail for evidence and remediation detail; the ID in
  this register points back to them.
- Unregistered legal-premise observations (the § 5103/§ 5103A hint conflation and the
  "citable as binding authority" comments) remain in `docs/audit-pass1.md`; they have no
  finding ID or severity and are not added here as new defects.
