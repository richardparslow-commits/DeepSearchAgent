# AUD4-002 / AUD4-003 / AUD4-004 — Root Cause, Remediation, and Verification

**Status:** Closed in the working tree. Verification: 688 targeted tests, 2,294 full-suite tests, ruff clean, and deterministic fixture checks against both named CAVC opinions.

## Phase 1 — Issue identification and root cause

### AUD4-002 — D1 case metadata was discarded at the analysis boundary

Before this change:

- `va_legal_agent/models.py:1564-1571` had only `top_cases: list[str]` on `LegalAnalysis`; the rich `CaseRecord` (`models.py:141-227`) survived retrieval and ranking but had no downstream analysis field.
- `va_legal_agent/agent.py:1229` built `top_cases = [f"{case.title} ({case.court})" ...]`, deliberately discarding docket, reporter citation, decision date, precedential status, appellate history, transcript URL, and provenance state.
- `va_legal_agent/__main__.py:1023` rendered only those strings in text; `:1194` wrote the strings to CSV; `:1270` serialized the `LegalAnalysis` model, which had no D1 field to serialize.
- `webapp.py:231-232` persisted the same text and model dump, so the browser API inherited the loss.
- `va_legal_agent/snapshots.py` had no analysis-snapshot contract, so the evidence store could not preserve a surfaced case's D1 fields.

Broken contract: a lossless `CaseRecord` entered `_build_analysis`, but every output boundary exposed only `Title (court)`.

### AUD4-003 — No provision-level VBA impact engine

Before this change:

- `va_legal_agent/topics.py:164-175` was a nine-entry `STATUTE_HINTS` table with no Part 3/Part 4 rating schedule, diagnostic-code, staged-rating, or medication-effect vocabulary.
- `va_legal_agent/impact.py:205-212` reduced statutory analysis to `statute_note_for`, a single generic sentence derived from those hints.
- `va_legal_agent/regulations.py:1-12` was (and remains) a read-only Federal Register/Regulations.gov rulemaking docket client. It was not an M21-1 or C.F.R. provision analyzer, so docket context could not answer "which adjudication stage does this decision change?"
- No production object carried an affected citation, adjudication stage, legal effect, M21-1 placement, diagnostic code, staged-rating flag, medication-effect flag, or verbatim opinion anchor.

Broken contract: the report could say a case was "relevant to rating" but could not name the governing provision, the affected stage, or the sentence supporting the mapping.

### AUD4-004 — Facts were truncated and next steps were generic

Before this change:

- `va_legal_agent/deep_read.py:58-80` extracted at most three fact sentences and then cut the result at 300 characters (`result[:297] + "..."`). That severed conditions, citations, and procedural facts mid-sentence.
- `va_legal_agent/deep_read.py:37-55` recognized only a narrow set of fact headers; no structured abstract model existed.
- `va_legal_agent/deep_read.py:215-241` returned only a string summary; the full opinion body was not retained in a typed abstract.
- `va_legal_agent/interpretation.py:154-157` defined `GENERIC_NEXT_STEPS` as "compare the facts..." and a disclaimer, so every report emitted research prompts rather than adjudicator actions.
- `va_legal_agent/adjudication.py` was (and remains) an evidence-first citator review queue, not an HLR/DRO/CUE playbook.

Broken contract: the output had no non-truncated `CaseAbstract`, and no role-specific RVSR or Senior Rater/DRO operational instructions.

## Phase 2 — Architecture and remediation plan

### D1 contract

`va_legal_agent/models.py` now defines:

- `SurfacedCaseRecord` with `case_title`, `court`, `docket_number`, `reporter_citation`, `decision_date`, `precedential_status`, `panel`, `panel_status`, `appellate_history`, `history_status`, `citator_status`, `citator_confidence`, `citator_as_of`, `official_transcript_url`, `retrieved_url`, `transcript_source_type`, `provenance_verified`, `source_reliability`, warnings, the structured abstract, and provision impacts.
- `AppellateHistoryEntry` for a status/case/citation/source/verification tuple.
- `LegalAnalysis.surfaced_cases` aligned with the legacy `top_cases` display strings.
- `impact.py` profiles and `CaseRecord` now preserve `provision_impacts`.
- `CaseRecord.case_abstract` preserves the full structured abstract.

`va_legal_agent/agent.py` builds surfaced records in `_build_analysis` for every top case. After the optional citator runs, `apply_citator_checks` copies its status, confidence, as-of date, verified history, and authoritative negative treatments into the D1 record. `__main__.py` renders all D1 fields in text, CSV, and JSON, including panel status, citator state, and explicit warnings for secondary transcript sources, single-judge memoranda, unknown precedential status, and unassessed appellate history. `webapp.py` persists the same model dump, and `SnapshotStore.record_analysis` writes the lossless analysis payload.

### Provision-impact engine

`va_legal_agent/va_domain.py` adds a versioned (`2026.11`) registry covering:

- 38 U.S.C. §§ 1110, 1131, 1154(b), 1155, 5103, 5103A, 5104(b), 5107(b), 5110, 7104(d)(1), 7105, 7252, and 7261.
- 38 C.F.R. Part 3: 3.102, 3.103, 3.105(a), 3.156, 3.159, 3.2501, 3.2600, 3.2601, 3.303, 3.304(f), 3.306, 3.307, 3.309, 3.310, 3.320, 3.321, and 3.400.
- 38 C.F.R. Part 4: 4.1, 4.2, 4.3, 4.7, 4.10, 4.14, 4.16, 4.20, 4.21, 4.25, 4.26, 4.40, 4.45, 4.59, 4.125, 4.126, and 4.130.
- Diagnostic Codes, including hyphenated analogous codes; staged-rating triggers under *Fenderson* / *Hart*; and medication-effect triggers under *Jones* / *McCarroll*.
- M21-1 placements for duty to assist/notice (`Part III, Subpart i, Chapter 2`), examination sufficiency (`Part IV, Subpart i, Chapter 3, Sections A-C`), rating determinations (`Part V, Subpart ii`), and HLR/CUE (`Part X, Subpart ii`).

Each `ProvisionImpact` carries the citation, authority type, adjudication stage, legal effect, M21-1 location, optional diagnostic code/staged/medication flags, official source URL, registry version, and a normalized verbatim sentence from the opinion. `extract_provision_impacts` only emits an object when the text contains the provision or doctrine; bare mentions do not become findings. `regulations.py` keeps its rulemaking client and exposes `provision_level_impacts` as a clearly separate wrapper. `topics.py` carries the expanded hint vocabulary without changing the existing query-planner budget.

### Structured abstracts and role guidance

`va_legal_agent/deep_read.py` no longer truncates at 300 characters. `va_domain.build_case_abstract` parses CAVC/BVA/Supreme Court/Federal Circuit section headings into:

- `claimant_and_conditions`
- `procedural_history`
- `issues_presented`
- `holding_and_legal_rule`
- `reasoning_summary`
- `disposition`
- `remand_instructions`

It marks `abstract_complete=False` and `abstract_source="record_metadata"` when only record metadata was available. Every surfaced case still receives that explicitly incomplete abstract rather than a fabricated full-text summary.

`va_domain.build_rvsr_guidance` emits concrete evidence-review, examination-development, rating-codesheet, and reasons-for-decision actions. `build_senior_rater_guidance` emits HLR de novo, duty-to-assist, CUE, precedent-versus-M21-1, and NQR checkpoints. `interpretation.py` uses these playbooks for `next_steps`, and `adjudication.py` exposes both under explicit wrapper names. The playbooks include case-specific *Adams* (obesity secondary service connection) and *Westervelt* (DRO CUE separate from the merits) directions when those authorities are present.

## Phase 3 — Implementation map

- `va_legal_agent/models.py`: new D1, abstract, impact, and guidance models.
- `va_legal_agent/va_domain.py`: deterministic registry, abstract builder, D1 builder, provision extractor, role playbooks.
- `va_legal_agent/agent.py`: preserve abstracts/impacts and surfaced cases; attach role guidance.
- `va_legal_agent/deep_read.py`: remove 300-character facts cut; set full abstract from fetched text.
- `va_legal_agent/impact.py`: structured provision impacts and nuance note.
- `va_legal_agent/topics.py`: expanded statutory/regulatory hints.
- `va_legal_agent/regulations.py`: separated provision wrapper; docket client unchanged.
- `va_legal_agent/interpretation.py`: role-specific next steps and guidance fields.
- `va_legal_agent/adjudication.py`: RVSR and Senior Rater/DRO playbook entry points.
- `va_legal_agent/__main__.py`: text/CSV/JSON D1, abstract, impact, and playbook output.
- `va_legal_agent/snapshots.py`: lossless analysis snapshot.
- `docs/defect-register.md`: AUD4-002/003/004 closure rows.
- Tests: `test_legal_agent.py`, `test_topics.py`, `test_regulations.py`, `test_impact.py`, `test_deep_read.py`, `test_interpretation.py`, `test_adjudication.py`, `test_cli.py`, `test_webapp.py`, `test_audit_invariants.py`, `test_snapshots.py`, `audit_invariants.py`.

## Phase 4 — Verification

- Targeted suite: **688 passed**.
- Full repository suite: **2,294 passed**.
- Ruff: **clean**.
- Invariant helper `assert_aud4_domain_invariants` checks D1 alignment, valid precedential/source vocabularies, warning labels, abstract presence/completeness, and impact stage/effect/anchor/M21-1 coverage.
- Real fixtures:
  - `cavc-adams-23-5064.txt`: surfaced record (docket 23-5064, official CAVC transcript), full abstract, 38 U.S.C. § 1110, 38 C.F.R. § 3.310, Diagnostic Code 7907, M21-1 placements, and role guidance.
  - `cavc-westervelt-23-0024.txt`: surfaced record (docket 23-0024, official CAVC transcript), full abstract, 38 C.F.R. § 3.105(a), 38 C.F.R. § 4.14, Diagnostic Code 8045-9304, a staged-rating trigger, and M21-1 placements.
- The tests also assert the text, CSV, JSON, web-job, and snapshot contracts contain the new structures rather than only the legacy display strings.
