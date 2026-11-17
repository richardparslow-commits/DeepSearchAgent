# AUD4-005 through AUD4-008: root cause and remediation

## Scope

This pass resolves four High-severity defects in the post-AUD4-002/003/004
tree. It preserves the evidence-first citator adjudication queue and its public
API, then adds the missing VBA adjudication, LLM provenance, impact-profile, and
source-reliability contracts.

## AUD4-005 — citator queue is not VBA adjudication

`va_legal_agent/adjudication.py` is documented at lines 1-8 as a human review
queue for citator findings. Its public symbols used by
`scripts/adjudicate_citator_queue.py` and `tests/test_adjudication.py` are:

- `DECISIONS`
- `build_adjudication_queue`
- `load_queue`
- `save_queue`
- `record_decision`
- `render_queue_text`

The file also exposes two thin wrapper playbooks (`rvsr_operational_playbook`
and `senior_rater_operational_playbook`) that delegate to the deterministic
domain registry in `va_domain.py`. There was no workflow model for an RVSR, a
Senior Rater/DRO, or a QRS; no HLR path under 38 C.F.R. § 3.2601; no DTA-error
return path; and no CUE path under 38 C.F.R. § 3.105(a), 38 U.S.C. § 5109A,
§ 20.1403(e), or *George v. McDonough*.

Root cause: the module's name described citator-edge adjudication, while the
downstream VA role guidance was only prose. There was no structured object for
a role workflow to persist or render.

## AUD4-006 — unvalidated LLM JSON

`llm.py` parsed arbitrary JSON in `_parse_reasoning_response` and accepted any
string entries under `reconciled_principles` plus any string `synthesis`.
`interpretation.py` then adopted both directly. No check compared proposed case
citations, docket numbers, quotations, statutory/regulatory references, or
M21-1 references against the retrieved opinion corpus.

`provenance_compare.py` is unrelated: it compares source-audit sidecars for
configuration/data drift. It does not validate model-generated prose.

Root cause: the LLM path treated syntactically valid JSON as legally validated
content.

## AUD4-007 — ImpactProfile discarded; no role workflow models

`ImpactProfile` is defined in `models.py` and returned by
`impact.analyze_case_impact`. `agent.py` computed it and retained only
`profile.nuance` and `profile.provision_impacts`. `CaseRecord` and
`SurfacedCaseRecord` did not persist the profile. `LegalAnalysis` had no
`impact_profile` or `adjudication_guidance` field, and no QRS workflow model
existed. The text renderer showed only the legacy RVSR/Senior-Rater prose.

Root cause: the impact layer returned a rich object to an agent that flattened
it immediately, and the output contract had no slot for role workflows.

## AUD4-008 — source_reliability dropped before rendering

`agent.normalize_case` stamped `CaseRecord.source_reliability` from
`reliability.classify_source`. That function classified only
`official|secondary|unknown`; there was no authority tier or official boolean.
`LegalAnalysis` had no aggregate summary, ranking did not use reliability as a
tie-breaker, interpretation confidence ignored it, and neither text nor web
output displayed it.

Root cause: reliability was an internal label with no schema field, aggregate,
ranking use, confidence use, or renderer consumer.

## Remediation contract

1. Keep every citator-queue symbol and behavior intact.
2. Add `RvsrAdjudicationWorkflow`, `SeniorRaterDroWorkflow`,
   `QrsAuditWorkflow`, and `AdjudicationRoleGuidance`; populate them from the
   deterministic VM domain registry and render dedicated role sections.
3. Persist `impact_profile` on `CaseRecord`, `SurfacedCaseRecord`, and
   `LegalAnalysis` while keeping `.nuance` populated for compatibility.
4. Add strict LLM JSON shape checks and
   `validate_llm_synthesis_provenance()`. Reject fabricated case citations,
   dockets, quotations, statutes, regulations, and M21-1 references; filter
   unanchored principles; fall back to deterministic principles/narrative with
   explicit provenance warnings.
5. Add `authority_tier` and `is_official_authority`, aggregate reliability
   counts, official-first tie-breaking, reliability-aware high-confidence
   scoring, and explicit Official Primary vs. Secondary/Unverified badges in
   CLI, CSV/JSON, snapshots, and web output.
6. Preserve all existing D1, abstract, provision-impact, citator-queue, and
   provenance-sidecar contracts.
