# Real-document extraction benchmark

This benchmark measures the production `extract_case_details` function against
committed snapshots of real public decisions from the Board of Veterans'
Appeals (BVA) and the U.S. Court of Appeals for Veterans Claims (CAVC). It is
deterministic, offline, and does not modify production extraction behavior.

## Run it

```sh
make benchmark-extraction
```

The runner reads `cases.json`, verifies each fixture's SHA-256, extracts the
case details, and compares five gold fields per case:

| Field | Comparison |
| --- | --- |
| `citation` | Exact normalized match. Empty means the document prints no self-citation. |
| `decision_date` | Exact ISO date (`YYYY-MM-DD`). |
| `holding` | Normalized containment: the gold holding sentence must appear in the extracted holdings. Additional extracted sentences are allowed. |
| `legal_standard` | Exact canonical label. Empty means no standard is stated. |
| `outcome` | Exact canonical disposition. |

Whitespace, case, and typographic quotation marks are normalized before
comparison.

## Corpus

| ID | Source | Document | Fields |
| --- | --- | --- | --- |
| `bva-a25049742` | BVA | [A25049742](https://www.va.gov/vetapp25/Files6/A25049742.txt) | Grant of secondary service connection for sleep apnea |
| `bva-a25110452` | BVA | [A25110452](https://www.va.gov/vetapp25/Files12/A25110452.txt) | Grant of service connection for tinnitus |
| `cavc-adams-23-5064` | CAVC | [Adams v. Collins, No. 23-5064](https://www.uscourts.cavc.gov/documents/AdamsMW_23-5064.pdf) | Published panel opinion on obesity and secondary service connection |
| `cavc-westervelt-23-0024` | CAVC | [Westervelt v. Collins, No. 23-0024](https://s.uscourts.cavc.gov/decisions/PANEL.CVA/WesterveltET_23-0024.pdf) | Published panel opinion on CUE and PTSD/TBI ratings |

The BVA fixtures are the public `.txt` decisions decoded from their original
Windows-1252 encoding. The CAVC fixtures are text snapshots extracted from the
public PDFs with the project's `pypdf` dependency; page headers and line breaks
from the PDF text layer are retained. Each fixture's source URL, retrieval date,
and checksum are recorded in `cases.json`. The benchmark itself performs no
network requests.

## Baseline

`baseline.json` records the current known misses:

| Field | Current behavior |
| --- | --- |
| `cavc-adams-23-5064.citation` | A slip opinion has no reporter self-citation, but the extractor returns `7 Vet.App. 439` from a cited case. |
| `cavc-adams-23-5064.outcome` | The opinion vacates and remands, but the extractor returns `denied` from the disposition fallback. |
| `cavc-westervelt-23-0024.citation` | The slip opinion has no reporter self-citation, but the extractor returns `29 Vet.App. 174` from a cited case. |
| `cavc-westervelt-23-0024.outcome` | The opinion vacates and remands, but the extractor returns `denied` from the disposition fallback. |

Current score: **16/20 fields (80%)**.

CI fails if any field not listed in `known_misses` fails. A listed miss that
starts passing is reported as `fixed` but does not fail; remove it from the
baseline when the production behavior is fixed. The baseline is intentionally
separate from the gold fields so the benchmark does not redefine correctness to
match current output.

## Adding or refreshing a case

1. Download the public decision and save a UTF-8 text snapshot under
   `fixtures/`.
2. Review the citation, date, holding, standard, and disposition against the
   document and add a case to `cases.json` with its source URL, retrieval date,
   and SHA-256.
3. Update `field_count` in `baseline.json` and record any new known misses.
4. Run `make benchmark-extraction` and `make lint`.

Do not replace a gold value merely because the extractor returns something
different. The baseline is the place to record a known gap.

## CI

The `check` job in `.github/workflows/ci.yml` runs
`make benchmark-extraction` after the test suite. The benchmark is offline and
uses the same editable installation as the rest of CI.
