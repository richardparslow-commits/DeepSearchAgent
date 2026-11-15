# Labeled adversarial citator evaluation

This benchmark measures the production `check_case` citator against a committed,
hand-labeled set of synthetic opinions. It is deterministic and offline: a
fixture provider supplies the citation graph, metadata, and opinion text, so the
benchmark never contacts CourtListener. Real-opinion attribution is measured
separately in [`benchmarks/citator_real`](../citator_real/).

The corpus is **not legal authority**. It uses short synthetic passages to test
whether the citator attributes negative treatment to the correct target, applies
the opinion-role/precedential-status gate, resolves the whole cluster, and
reports incomplete coverage honestly.

## Run it

```sh
make benchmark-citator
```

The runner reads `cases.json`, builds a `CaseRecord` and fixture provider for
each case, calls the production citator, and compares the result with the case's
gold labels. It exits non-zero on any miss that is not listed in
`baseline.json`.

## What is labeled

Each case contains:

- `target` — the synthetic target case (`CaseRecord` fields).
- `metadata` — opinion metadata keyed by CourtListener opinion id, including
  `opinion_type`, `precedential_status`, `citations`, `cluster_id`,
  `cluster_resolved`, and `opinion_ids`.
- `texts` — citing-opinion bodies keyed by opinion id.
- `edges` — forward-citation pages. Each page has an opinion id, cursor, count,
  citing ids, and optional next cursor. Pages for the same opinion model
  pagination.
- `lookup`, `same_docket`, and `errors` — optional resolution/advisory fixtures.
- `limits` — optional `max_citing_opinions` / `max_pages` overrides.
- `expected` — gold labels:
  - `status`: `negative`, `good_law`, or `unknown`.
  - `coverage_complete`: expected `CitatorCheck.coverage.complete`.
  - `target_cluster_id`, `target_citations`, `target_opinion_ids`: expected
    cluster resolution.
  - `authoritative`: exact signatures expected in
    `CitatorCheck.negative_treatments`.
  - `gated`: exact signatures expected in
    `CitatorCheck.gated_treatment_findings`.
  - `flags_contain`: every listed phrase must appear in at least one citator
    flag.
  - `same_docket_count`: expected number of separate same-docket decisions.

Finding signatures compare `citing_url`, `treatment`, `scope`, `opinion_role`,
and `precedential_status`.

## Corpus

There are 19 cases across these adversarial categories:

| Category | Cases | What it tests |
| --- | --- | --- |
| Authoritative negative | `auth-majority-parallel-overrule` | A Published lead opinion overrules the target, identified only by a parallel WL citation. |
| Role gate | `dissent-only-criticism` | A dissent's criticism is retained as gated, not counted as negative. |
| Role and status gate | `concurrence-unpublished-limit` | A nonprecedential concurrence is ineligible for both reasons. |
| Status gate | `lead-unpublished-overrule`, `lead-unknown-status-question` | Nonprecedential and unknown statuses never set `negative`. |
| Clean review | `clean-good-law` | Approving language produces `good_law`, not a treatment finding. |
| Negation | `negated-overrule`, `negated-after-target` | "Not overruled" is ignored before and after the target reference. |
| Whole cluster | `sibling-opinion-edge`, `lookup-whole-cluster` | Edges attached to another sub-opinion are still reviewed; lookup resolves all sub-opinions and parallel citations. |
| Mixed signals | `mixed-authoritative-gated` | Authoritative and gated findings remain separate. |
| Coverage | `truncated-page-cap`, `missing-target-text` | A page cap or unverifiable body makes coverage incomplete. |
| Resolution | `ambiguous-lookup`, `direct-cluster-error` | Ambiguous lookups are not guessed; failed cluster expansion falls back to one opinion and marks coverage incomplete. |
| Advisory separation | `same-docket-advisory` | Same-docket context does not affect treatment status. |
| Adversarial false positive | `same-name-different-citation`, `dissent-attribution-trap` | A negative verb must bind to the target citation and to the court's own holding, not a same-named case or a dissent. |
| Cap prioritization | `cap-prioritizes-later-authority` | Under a citing-opinion cap, older API-order citations are skipped so the later decisions are reviewed first. |

## Baseline

`baseline.json` records the current known misses. It is empty: every case has
an expected label that the production citator must satisfy.

Current score: **19/19 cases pass; 0 known misses; 0 new misses.**

CI fails if a case not listed in `known_misses` fails. A listed case that starts
passing is reported as `fixed`; remove it from the baseline when the production
behavior is corrected. Do not weaken a gold label merely because the current
implementation returns something different.

## Adding a case

1. Add the synthetic target, metadata, text, edges, and expected labels to
   `cases.json`.
2. If the case intentionally records a current limitation, add its id to
   `baseline.json` and describe the limitation in this README.
3. Run `make benchmark-citator`, `make lint`, and the coverage gate.

## CI

The `check` job in `.github/workflows/ci.yml` runs `make benchmark-citator`
after the real-document extraction benchmark. The evaluation is offline and uses
the same editable installation as the rest of CI.
