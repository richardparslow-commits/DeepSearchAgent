# Real-opinion citator attribution evaluation

This benchmark measures the production citator's extraction and attribution on
hand-labeled passages from real Supreme Court overruling chains. It is separate
from [`benchmarks/citator`](../citator/), which uses synthetic adversarial
fixtures. The real set answers a narrower question: when a human has identified
a target-bound treatment passage, does the citator find that passage and put it
in the right attribution bucket?

The benchmark is deterministic and offline. It calls the production `check_case`
path with recorded provider fixtures and never contacts CourtListener.

## Run it

```sh
make benchmark-citator-real
```

The runner reads `cases.json`, loads each case's fixture from `fixtures/`, and
compares the production findings with the hand labels. It exits non-zero on an
unlisted miss, a metric regression below `baseline.json`'s floor, or an unsafe
`good_law` result.

## Source and provenance

The cases are from public CourtListener opinion pages for:

| Case | Target opinion | Citing opinion |
| --- | --- | --- |
| `roe-dobbs` | [Roe v. Wade, 410 U.S. 113 (1973)](https://www.courtlistener.com/opinion/108713/roe-v-wade/) | [Dobbs v. Jackson Women's Health Organization, 597 U.S. 215 (2022)](https://www.courtlistener.com/opinion/6481357/dobbs-v-jackson-womens-health-organization/) |
| `casey-dobbs` | [Planned Parenthood of Southeastern Pennsylvania v. Casey, 505 U.S. 833 (1992)](https://www.courtlistener.com/opinion/112786/planned-parenthood-of-southeastern-pa-v-casey/) | [Dobbs, 597 U.S. 215 (2022)](https://www.courtlistener.com/opinion/6481357/dobbs-v-jackson-womens-health-organization/) |
| `chevron-loper` | [Chevron U. S. A. Inc. v. Natural Resources Defense Council, Inc., 467 U.S. 837 (1984)](https://www.courtlistener.com/opinion/111221/chevron-u-s-a-inc-v-natural-resources-defense-council-inc/) | [Loper Bright Enterprises v. Raimondo, 603 U.S. 369 (2024)](https://www.courtlistener.com/opinion/9986254/loper-bright-enterprises-v-raimondo/) |
| `abood-janus` | [Abood v. Detroit Board of Education, 431 U.S. 209 (1977)](https://www.courtlistener.com/opinion/109661/abood-v-detroit-board-of-education/) | [Janus v. American Federation of State, County, and Municipal Employees, 585 U.S. 878 (2018)](https://www.courtlistener.com/opinion/4511640/janus-v-state-county-and-municipal-employees/) |
| `austin-citizens` | [Austin v. Michigan State Chamber of Commerce, 494 U.S. 652 (1990)](https://www.courtlistener.com/opinion/112398/austin-v-michigan-state-chamber-of-commerce/) | [Citizens United v. Federal Election Commission, 558 U.S. 310 (2010)](https://www.courtlistener.com/opinion/1741/citizens-united-v-federal-election-commission/) |
| `bowers-lawrence` | [Bowers v. Hardwick, 478 U.S. 186 (1986)](https://www.courtlistener.com/opinion/111738/bowers-v-hardwick/) | [Lawrence v. Texas, 539 U.S. 558 (2003)](https://www.courtlistener.com/opinion/130160/lawrence-v-texas/) |

Each fixture records the public-page URLs, the normalized CourtListener v4
endpoint URLs, the retrieval date, and selected verbatim passages. The passages
are **not** the full opinions.

### Important fixture limitation

The CourtListener v4 API requires authentication. In the environment that
produced these fixtures, `COURTLISTENER_API_KEY` was not available and the API
returned HTTP 401. The checked-in fixtures are therefore **normalized provider
fixtures built from the public opinion pages**, not raw API response bodies.
Their metadata, text, and edge objects match the provider surface `check_case`
consumes, and `capture.raw_api_captured` is `false` in every fixture. A raw API
refresh requires a CourtListener token; do not treat these files as evidence of
an authenticated API capture.

Each edge page records only the one citing opinion used for the labels and sets
the provider count to one greater than the captured set. That deliberately keeps
coverage incomplete, so a missed extraction cannot become `good_law` merely
because the fixture is small. The count is a local sentinel, not CourtListener's
real cited-by total. The benchmark scores passage attribution, not coverage or
complete good-law status.

## What is labeled

Each gold entry names the citing opinion, the exact sentence, the canonical
treatment (or empty for a control), and one attribution:

- `authoritative` — the citing opinion's own majority/lead/combined holding.
- `gated` — a separate, plurality, or nonprecedential voice.
- `none` — a question, party argument, competing case, conditional, or other
  non-treatment reference.

A positive label (`authoritative` or `gated`) is a target-bound negative
treatment passage. A control label (`none`) is a passage where the target is
mentioned in the vicinity of negative language but the citing court is not
negatively treating it.

The labels were read against the public opinion text, not generated from the
citator's output. The current sample contains 10 positive passages and four
controls. It has no `gated` positive passages; the real set exercises only
majority opinions. The synthetic benchmark remains the place where the
role/status gate and gated findings are tested.

## Metrics

For each case, a prediction is a finding in `negative_treatments` or
`gated_treatment_findings`. Matching is by normalized excerpt.

| Metric | Definition |
| --- | --- |
| Target attribution precision | Unique positive passages matched / (unique positive passages matched + findings on controls, duplicates, or unlabeled passages). |
| Target attribution recall | Unique positive passages matched / all positive passages. |
| Authoritative precision | Findings in `negative_treatments` on gold authoritative passages / all findings in `negative_treatments`. |
| Authoritative recall | Gold authoritative passages with an authoritative finding / all gold authoritative passages. |
| Speaker/role accuracy | Matched positive passages with the expected `authoritative`/`gated` bucket / matched positive passages. |
| Treatment accuracy | Matched positive passages with the expected canonical treatment / matched positive passages. |

Status is reported for context but is not part of the attribution score. The
runner does fail if any case with a positive label returns `good_law`, because
that is an unsafe citator status.

## Current measurement

The baseline is a **measurement**, not a claim that the citator is correct.

| Case | Gold positives | Predicted | TP | FP | FN | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `roe-dobbs` | 2 | 0 | 0 | 0 | 2 | known miss |
| `casey-dobbs` | 2 | 0 | 0 | 0 | 2 | known miss |
| `chevron-loper` | 1 | 2 | 0 | 2 | 1 | known miss |
| `abood-janus` | 2 | 0 | 0 | 0 | 2 | known miss |
| `austin-citizens` | 2 | 0 | 0 | 0 | 2 | known miss |
| `bowers-lawrence` | 1 | 1 | 1 | 0 | 0 | pass |
| **Total** | **10** | **3** | **1** | **2** | **9** | **1/6 exact** |

- **Target attribution:** precision **33.3%** (1 TP / 2 FP), recall **10.0%**
  (1 TP / 9 FN).
- **Authoritative findings:** precision **33.3%**, recall **10.0%**.
- **Matched-passage attribution:** speaker/role **100.0%**, treatment
  **100.0%** (1 matched passage).
- **Controls:** 4 passages; 2 were predicted as treatment (both in the Chevron
  question-presented sentence).
- **Status:** 2/6 matched the expected status; no unsafe `good_law`.

The three measured generalization gaps are:

1. **Short-form target names.** The target locator requires the full case title
   or a reporter citation. Passages that say only “Roe,” “Casey,” “Chevron,” or
   “Abood” are not located, so the holdings are not extracted. This accounts
   for eight of the nine false negatives.
2. **Reporter spacing.** The Austin holding prints “494 U. S. 652,” while the
   canonical citation is “494 U.S. 652.” The exact-phrase target locator does
   not match across the spaces inside “U. S.”, so even the citation-bearing
   Austin holding is missed. This is the ninth false negative.
3. **Non-treatment uses of negative words.** The Loper Bright question-presented
   sentence contains the noun “question” and the adjective “limited.” The
   extractor emits `questioned` and `limited` as authoritative findings, creating
   two false positives and a `negative` status for the wrong reason.

The Bowers/Lawrence full-name holding is the positive control and passes.

## Baseline and CI

`baseline.json` lists the five known misses and records a floor for target and
authoritative precision/recall. CI fails on:

- a miss not listed in `known_misses`;
- target or authoritative precision/recall below the floor; or
- a `good_law` result for a case with a gold positive.

A known miss that starts passing is reported as `fixed`; remove it from
`known_misses` when that happens. Do not change a gold label merely because the
current implementation returns something different; record the gap here or in
the baseline instead.

## Adding or refreshing a case

1. Capture the target and citing opinion from CourtListener. If an API token is
   available, preserve the raw responses and record them alongside the
   normalized fixture; otherwise keep `capture.raw_api_captured` false.
2. Add the target/citing metadata, selected verbatim text, and one partial edge
   page to a fixture under `fixtures/`.
3. Add the case to `cases.json` with exact gold excerpts and attribution labels.
   The runner verifies that every excerpt appears verbatim in its citing text.
4. Update `baseline.json` only after confirming that a failure is a real
   implementation limitation, then run `make benchmark-citator-real` and
   `make lint`.
