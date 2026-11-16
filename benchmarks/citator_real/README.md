# Real-opinion citator attribution evaluation

This benchmark measures the production citator's extraction and attribution on
hand-labeled passages from real Supreme Court and federal appellate overruling
chains. It is separate from [`benchmarks/citator`](../citator/), which uses
synthetic adversarial fixtures. The real set answers a narrower question: when a
human has identified a target-bound treatment passage, does the citator find
that passage and put it in the right attribution bucket?

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
| `briskin-ama` | [AMA Multimedia, LLC v. Wanat, 970 F.3d 1201 (9th Cir. 2020)](https://www.courtlistener.com/opinion/4776501/ama-multimedia-llc-v-marcin-wanat/) | [Briskin v. Shopify, Inc., 135 F.4th 739 (9th Cir. 2025) (en banc)](https://www.courtlistener.com/opinion/10381924/brandon-briskin-v-shopify-inc/) |
| `aldf-veneman` | [Animal Legal Defense Fund v. Veneman, 469 F.3d 826 (9th Cir. 2006)](https://www.courtlistener.com/opinion/3039597/animal-legal-defense-fund-v-veneman/) | [Animal Legal Defense Fund v. Veneman, 490 F.3d 725 (9th Cir. 2007) (en banc order)](https://www.courtlistener.com/opinion/798126/animal-legal-defense-fund-v-veneman/) |
| `coleman-jeffries` | [Jeffries v. Wood, 114 F.3d 1484 (9th Cir. 1997) (en banc)](https://www.courtlistener.com/opinion/741729/patrick-james-jeffries-v-tana-wood-superintendent/) | [Coleman v. Brown, 756 F. App'x 677 (9th Cir. 2018)](https://www.courtlistener.com/opinion/4568077/ralph-coleman-v-edmund-brown-jr/) |

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
citator's output. The current sample contains 13 positive passages and four
controls. Twelve positives are authoritative findings in published
majority/lead opinions; one is a gated finding from an unpublished Ninth
Circuit memorandum. The synthetic benchmark remains the place where separate
opinion roles and the full gate matrix are tested.

The sample deliberately covers:

- short-form target names (`Roe`, `Casey`, `Chevron`, `Abood`, `Austin`, `AMA`);
- reporter spacing (`494 U. S. 652` versus `494 U.S. 652`);
- noun/adjective uses of negative words (`question`, `limited`);
- a citation parenthetical that records subsequent history and is gated
  because the citing opinion is nonprecedential;
- an en banc opinion overruling a precedent by short name; and
- a published en banc order vacating a panel opinion.

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

| Case | Gold positives | Predicted | TP | FP | FN | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `roe-dobbs` | 2 | 2 | 2 | 0 | 0 | pass |
| `casey-dobbs` | 2 | 2 | 2 | 0 | 0 | pass |
| `chevron-loper` | 1 | 1 | 1 | 0 | 0 | pass |
| `abood-janus` | 2 | 2 | 2 | 0 | 0 | pass |
| `austin-citizens` | 2 | 2 | 2 | 0 | 0 | pass |
| `bowers-lawrence` | 1 | 1 | 1 | 0 | 0 | pass |
| `briskin-ama` | 1 | 1 | 1 | 0 | 0 | pass |
| `aldf-veneman` | 1 | 1 | 1 | 0 | 0 | pass |
| `coleman-jeffries` | 1 | 1 | 1 | 0 | 0 | pass |
| **Total** | **13** | **13** | **13** | **0** | **0** | **9/9 exact** |

- **Target attribution:** precision **100.0%**, recall **100.0%**.
- **Authoritative findings:** precision **100.0%**, recall **100.0%**.
- **Matched-passage attribution:** speaker/role **100.0%**, treatment
  **100.0%**.
- **Controls:** 4 passages; 0 were predicted as treatment.
- **Status:** 9/9 matched the expected status; no unsafe `good_law`.

The initial sample measured 1/6 exact. The expansion and the fixes it drove
resolved these gaps:

1. **Short-form target names.** Target location and binding now use one
   conservative short name per party, skipping generic institutional words.
   Short-name findings are marked `attribution_basis="short_name"` and
   `attribution_ambiguous=True` so review is required; they are not treated as
   equivalent to an explicit reporter citation.
2. **Reporter spacing.** Citation lookup now normalizes the citation text, so a
   passage printing `494 U. S. 652` binds to the canonical `494 U.S. 652`.
3. **Non-treatment uses of negative words.** `question`/`questions` as nouns and
   adjectival `limited to the question ...` are no longer emitted as
   `questioned` or `limited`. The verb forms remain eligible.
4. **Parenthetical subsequent history.** A history note such as
   `Jeffries, 114 F.3d 1484 (9th Cir. 1997) (en banc), overruled on other
   grounds by Gonzalez` is extracted as explicit negative language. In the
   Coleman fixture the citing opinion is an unpublished memorandum, so the
   finding is gated and cannot support an authoritative `negative` status or
   `good_law`. Ordinary passive findings such as `Smith was overruled by the
   Supreme Court` remain eligible and are flagged for review.
5. **En banc and vacated opinions.** A published en banc opinion can supply an
   authoritative holding, and `vacated` is now an explicit negative treatment
   with all-opinion scope. The conservative status rule is unchanged: any
   authoritative negative finding makes the status `negative`, gated-only
   findings leave it `unknown`, and `good_law` still requires a clean, complete
   review.

The Bowers/Lawrence full-name holding remains the positive control for target
binding, and the Coleman/Jeffries parenthetical exercises gated parenthetical
treatment without allowing an unpublished memorandum to set authoritative
status.

## Baseline and CI

`baseline.json` has no known misses and records a floor of 1.0 for target and
authoritative precision/recall. CI fails on:

- a miss not listed in `known_misses` (currently none);
- target or authoritative precision/recall below the floor; or
- a `good_law` result for a case with a gold positive.

A case that starts failing should be investigated as a regression. Do not change
a gold label merely because the current implementation returns something
different; record the gap here or in the baseline instead.

## Adding or refreshing a case

1. Capture the target and citing opinion from CourtListener. If an API token is
   available, preserve the raw responses and record them alongside the
   normalized fixture; otherwise keep `capture.raw_api_captured` false.
2. Add the target/citing metadata, selected verbatim text, and one partial edge
   page to a fixture under `fixtures/`.
3. Add the case to `cases.json` with exact gold excerpts and attribution labels.
   The runner verifies that every excerpt appears verbatim in its citing text.
4. Run `make benchmark-citator-real` and add focused extraction tests for any
   new failure mode. Update `baseline.json` only after confirming that a failure
   is a real implementation limitation.
