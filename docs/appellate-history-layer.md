# Verified appellate-history layer

**Status:** Implemented as a shadow candidate layer with a human-verification
gate. Automated cross-docket source ingestion remains future work. The layer
never infers an appeal from a citation signal, and it never exposes a history
label without a validated reviewer record.

## Purpose

Answer a narrow question the citator deliberately avoids: *is there a verified
appellate or later proceeding for this case, and what happened?*

The layer produces **candidates for human review**, with provenance. Only a
human-confirmed record tied to an official or licensed source becomes a
history label. Absence of a candidate is not proof that no appeal exists.

## Candidate signals

These signals are inputs to review, not conclusions.

1. **Same-docket decisions.** Starting from the target cluster's docket, list
   other clusters on that docket, with dates, citations, and links. A later
   cluster may be a rehearing, an en banc order, an amended opinion, or a
   post-remand decision. It is still only an intra-court signal: an appeal to
   the Federal Circuit or Supreme Court creates a new docket.
2. **Later citing opinions.** Use CourtListener's forward-citation graph to
   find opinions that cite the target. A citing opinion is a candidate for
   review, not proof of an appeal. It may cite the target in an unrelated case,
   and a later appeal may not cite the target at all—especially a Rule 36
   affirmance or a disposition without an opinion.

Candidate matching may use court, party names, docket numbers, lower-court
metadata, and dates to order the queue, but name-only matches must never create
a label. Docket numbers are not globally unique, and VA Secretary caption
changes make party-name matching unreliable.

Every candidate carries its signal type, source URL, target and candidate
identifiers, date, court, match reasons, and coverage limitations. Candidate
scoring is only for review priority.

## Implemented contract

The implementation lives in `va_legal_agent/appellate_history.py` and attaches
an `AppellateHistoryCheck` to every citator check:

- `candidates` — same-docket and later-citing signals, merged when both point
  to the same cluster/citation. Each starts as `verification_status=candidate`.
- `verified_history` — only `review_status=confirmed` records whose
  relationship is not `no_relationship`, matched to the target by cluster id
  or citation (never by case name alone).
- `status` — one of:
  - `verified`: at least one confirmed human record exists.
  - `insufficient_source`: candidates exist but no confirmed record does.
  - `not_assessed`: no candidate signals were available.
  It is never `no_history`; an empty candidate list is not a finding that no
  appeal occurred.

`subsequent_history_assessed` is `true` only when `status=verified`. The
citator itself still does not derive appellate history.

## Persisted evidence and response snapshots

When `SNAPSHOT_DIR` (or `--snapshot-dir`) is set, each citator check also
persists an evidence snapshot and every provider call used to build it
persists a response snapshot. The layout is:

```text
manifest.jsonl
snapshots/evidence.citator/<id>.json
snapshots/courtlistener.opinion_metadata/<id>.json
snapshots/courtlistener.citing_edges/<id>.json
snapshots/courtlistener.same_docket/<id>.json
snapshots/courtlistener.opinion_text/<id>.json
snapshots/courtlistener.lookup/<id>.json
```

Payloads are content-addressed by kind and canonical JSON, written atomically,
and deduplicated. The append-only manifest records the id, kind, capture time,
relative path, source URL, SHA-256, and audit metadata. Evidence snapshots hold
the complete normalized `CitatorCheck`, including coverage, treatment findings,
history candidates, verified records, and flags. Response snapshots hold the
request identifiers and public response body; authentication headers and API
keys are never stored. Failed provider calls are recorded with their error text
and then re-raised so the citator's existing failure handling is unchanged.

Snapshot failure never blocks an advisory result. An evidence-write failure is
reported in `flags`; a response-write failure is logged and the provider result
is returned normally. Snapshots are audit material, not a substitute for the
analysis output or queue, and a verified record still requires human review of
the authoritative source. The snapshot gives a later reviewer the exact
provider payload and normalized evidence that were available during the run.

## Human verification file

Set `APPELLATE_HISTORY_FILE` to a JSON array or `{"records": [...]}` object.
Example record:

```json
{
  "records": [
    {
      "target_case": "Smith v. Wilkie",
      "target_cluster_id": "777",
      "target_citation": "30 Vet.App. 123",
      "candidate_case": "Smith v. McDonough",
      "candidate_cluster_id": "999",
      "candidate_citation": "32 Vet.App. 9",
      "relationship": "direct_appeal",
      "disposition": "affirmed",
      "scope": "all",
      "source_quality": "official",
      "source_url": "https://www.cafc.uscourts.gov/opinions/example.pdf",
      "reviewer": "Reviewer name",
      "reviewed_at": "2026-01-02T03:04:05+00:00",
      "rationale": "Checked the docket link, disposition, and opinion.",
      "review_status": "confirmed"
    }
  ]
}
```

Required for a confirmed label: target cluster id or citation, candidate case,
candidate cluster id or citation, a supported relationship and disposition, a
scope of `all` or `part`, `source_quality` of `official` or `licensed`, an
HTTP(S) source URL, reviewer, timestamp, and rationale. A record with
`review_status=rejected` is retained for audit but never becomes a label.
`relationship=no_relationship` is also not a history label; it records a
reviewed non-match and is excluded from `verified_history`.

If a matching record is incomplete, the layer reports
`insufficient_source`, marks the candidate's verification status, and preserves
the reviewer-facing reason. It does not abort the citator check.

## Human-review gate

No history label may be created automatically.

A reviewer must inspect the target, the candidate, and the best available
authoritative source, then confirm:

- **Relationship:** direct appeal, rehearing, remand, collateral proceeding, or
  no relationship.
- **Disposition:** affirmed, reversed, vacated, remanded, dismissed, denied,
  granted, or another supported outcome.
- **Scope and effect:** whole decision or part; merits versus procedural order;
  whether the target was actually disturbed.
- **Source quality:** official docket/opinion, licensed citator, or both;
  coverage date and any missing documents.

A single candidate signal is not sufficient. A confirmed label requires an
authoritative source and a human reviewer. If the record is incomplete, the
result remains `not_assessed` or `insufficient_source`; it must not become
"no history."

Review records preserve the reviewer, timestamp, rationale, source links, and
the evidence used. Rejected and no-relationship candidates are retained so the
same false positive is not repeatedly presented as a live signal.

## What a licensed or official source must provide

A source is not adequate merely because it has docket pages or citation counts.
It must provide:

- Stable identifiers for the court, docket, case, and opinion/order.
- An explicit link between the lower-court and appellate dockets—not just a
  court name or party-name match.
- Coverage of non-opinion dispositions: CAVC clerk and single-judge orders,
  Federal Circuit Rule 36 judgments and orders, and Supreme Court certiorari
  grants and denials.
- Structured disposition codes and dates, including affirm, reverse, vacate,
  remand, dismiss, deny, grant, rehearing, and mandate.
- The underlying order or opinion text, or a reliable link to it, for human
  verification.
- Coverage dates, known gaps, update frequency, and correction/version history.
- Bulk or API access with clear licensing for display, caching, derivative
  work, and audit use.
- Provenance and versioning so a label can be reproduced later.

If the source cannot supply the cross-docket link and non-opinion
dispositions, it can support candidate generation but not confirmed history
labels. The current release therefore accepts verified records from a
human-curated file rather than pretending that same-docket or citation data is
an authoritative cross-docket link.

## Rollout and acceptance

Run the layer in shadow mode: generate candidates, require review, and show no
labels in normal output. Validate against a sample that includes CAVC panel and
single-judge dispositions, Federal Circuit Rule 36 affirmances, appeals that do
not cite the lower decision, same-docket rehearsings and remands, and at least
one full CAVC-to-Federal-Circuit-to-Supreme-Court chain.

Measure candidate recall and precision separately, review time, and false-label
rate. The release gate is zero unreviewed history labels. Even after launch, the
layer must not change ranking, `superseded_by`, or the meaning of `good_law`
unless a separate, explicit decision changes those contracts.

## Non-goals

- No automatic history label from same-docket clusters, citation edges, party
  names, or docket-number matches.
- No inference that absence of a candidate means no appeal exists.
- No change to ranking or `superseded_by` as part of this layer.
- No presentation of a history label as a final "good law" conclusion.
- No claim that a human-curated file is an automated cross-docket source
  adapter; the source still has to satisfy the quality requirements above.
