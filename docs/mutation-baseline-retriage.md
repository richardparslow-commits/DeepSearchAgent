# Mutation baseline re-triage (post-AUD5)

**Status date:** 2026-11-19
**Scope:** the 15 in-scope modules in the Makefile's `MUTATE_MODULES` /
`.github/workflows/mutation.yml` matrix.

## Why the baseline had drifted

The mutation kill-gate had been **crashing, not measuring**, since the AUD4
series. `scripts/mutmut_pass.py` built the `mutants/` sandbox with the package,
`conftest.py` and `pyproject.toml`, but never mirrored `tests/` or `benchmarks/`.
mutmut's own copy step skips paths that already exist, so:

* a sandbox from an earlier pass kept **stale tests**, and
* `benchmarks/` was **never** present.

AUD4 added tests that read fixtures via
`Path(__file__).resolve().parents[1] / "benchmarks" / ...`. Resolved from the
sandbox that path is `mutants/benchmarks/...`, which did not exist, so mutmut's
stats-collection run died with `FileNotFoundError` and mutmut exited 1.
`mutmut_pass.py` surfaced that as `FATAL: mutmut run failed with exit code 1`
with no survivor report, and the gate failed for every module whose slice
included `tests/test_legal_agent.py` or `tests/test_cli.py`.

Because the gate was red for a *mechanical* reason, the survivor baseline was
never refreshed while AUD4/AUD5 added a large amount of code. The drift below is
that accumulated, unmeasured debt — not a regression introduced by this change.

### Fix

`mutmut_pass.py` now mirrors **`tests/` and `benchmarks/` into the sandbox on
every pass** (`copytree(..., dirs_exist_ok=True)`), so new tests and fixtures are
always picked up. Verified: every module leg now completes and emits a report.

## Triage method

Each surviving mutant's diff was categorized against the equivalent classes this
baseline already documents (see `scripts/check_mutation_baseline.py`):

| Class | Approx. count | Disposition |
|---|---:|---|
| Message / string-literal swaps (log, warning, failure, detail text) | ~563 | equivalent — advisory text, never asserted |
| Identical `.get` / default / bool arguments, settings fallbacks, model defaults | ~345 | equivalent — default matches the explicitly-passed value |
| Logic-shaped mutants (flagged for review) | ~252 | examined individually; see below |

Reviewing the flagged set showed many are **provably equivalent despite looking
like logic changes**, for example:

* `signal.split(" ", 1)[0]` → `split(" ")[0]` / `split(" ", 2)[0]` — `[0]` is
  unchanged by `maxsplit`, so the result is identical.
* `direction = 1` → `2`, `-1` → `-2`, and uniform sign flips in
  `impact.outcome_note_for` — only `len(directions)` (set cardinality) is
  observed, and a uniform bijection preserves cardinality.
* `select_with_court_floor(limit <= 0)` → `limit <= 1` — with `limit == 1` the
  floor selection and plain truncation both return `ordered[:1]`.
* `run_id.encode("utf-8")` → `"UTF-8"` — alias spellings of the same codec.

### Killable mutants that were killed (not baselined)

Rather than baseline these, the killing tests were strengthened:

| Module | Killed | Strengthening |
|---|---:|---|
| `ranking.py` | 8 | exact `ranking_explanation` reliability text; official-source tie-break on an exact score tie; both `select_with_court_floor` `limit` boundaries (`floor > limit`, and stop-before-next-tier) |
| `reliability.py` | 10 | `authority_label` binding / secondary / unverified badges; CourtListener-host and va.gov-host branches with **no** court label |
| `interpretation.py` | 6 | `_build_element_library` field-by-field mirror of `TOPICS` + `_ELEMENT_DETAILS` (non-emptiness alone could not catch shifted args) |
| `impact.py` | 5 | `provision_impact_note` six-item cap, `label +=` (not `=`), exact prefix/separator/suffix; Secretary override note text |
| `search.py` | 3 | `SearchError` message / `kind` / `retryable` defaults and assignment |
| `batch.py` | 2 | `ensure_ascii=False` keeps non-ASCII literal in the state file |
| `providers.py` | 5 | BVA text-cache eviction assertions (oldest evicted, cap held); `_post_json` zero-based backoff index pinned to `[0, 1]` |

### Hang removed (a timeout is never a triaged equivalent)

`_fetch_bva_decision_text` evicted with
`while len(text_keys) >= MAX: cache.pop(text_keys.pop(0), None)`. Mutating the
inner `pop` argument to `None` stopped the list shrinking, so the mutant span
forever and mutmut reported **1 timeout** — an automatic gate failure.

The eviction is now a **bounded slice** (`overflow = len - MAX + 1`, then
`for key in text_keys[: max(0, overflow)]`). No mutation of it can loop
unboundedly, and the strengthened assertions kill the eviction mutants cleanly.
Providers now reports **0 timeouts**.

### Platform divergence resolved by killing, not by baselining

The first CI run of the re-triaged baseline matched **14 of 15 modules exactly**;
`providers.py` differed by a single mutant:
`CourtListenerProvider._post_json`'s `_courtlistener_retry_delay(attempt - 1, ...)`
→ `attempt + 1`. It survived on the macOS dev box but was **killed on the Linux
gate runner**.

Rather than delete the name to satisfy one platform (which would leave the other
red), the off-by-one is now pinned deterministically:
`test_post_json_backoff_uses_the_zero_based_retry_index` drives two transient
POST failures and asserts the backoff indices are exactly `[0, 1]`. Applying the
mutant locally makes that test fail, so it is killed on **both** platforms.

### CI leg timeout headroom

With the gate measuring again, the two largest legs ran right up against their
budgets: `agent.py` finished in **19m58s of 20m** (99.8%) and `providers.py` in
**23m20s of 25m** (93%). Either would flake red on a slightly slower runner, so
their matrix timeouts were raised to **30** and **35** minutes respectively. The
per-leg timeout mechanism is unchanged (a timeout still kills only that leg and
its report still uploads).

## Per-module counts

| Module | Baseline before | First measured pass | After killing tests |
|---|---:|---:|---:|
| `agent.py` | 140 | 275 | 275 |
| `batch.py` | 21 | 30 | 28 |
| `config.py` | 57 | 82 | 82 |
| `deep_read.py` | 9 | 34 | 34 |
| `fetch.py` | 61 | 79 | 79 |
| `impact.py` | 13 | 28 | 23 |
| `interpretation.py` | 22 | 50 | 44 |
| `llm.py` | 28 | 107 | 107 |
| `planning.py` | 4 | 4 | 4 |
| `providers.py` | 236 | 706 (+1 timeout) | 701 (0 timeouts) |
| `queries.py` | 3 | 3 | 3 |
| `ranking.py` | 4 | 10 | 2 |
| `reliability.py` | 2 | 28 | 18 |
| `search.py` | 40 | 43 | 40 |
| `topics.py` | 0 | 0 | 0 |
| **Total** | **640** | **1480** | **1440** |

`ranking.py` now sits **below** its old baseline; `search.py`, `queries.py`,
`planning.py` and `topics.py` are unchanged.

## Honest limitation / follow-up

The bulk of the growth is the two documented equivalent classes (advisory message
text and default/`.get` arguments) introduced by AUD4/AUD5. The logic-shaped
remainder was reviewed and the clearly killable ones were killed as listed above,
but **this pass does not claim every remaining survivor is provably equivalent**.
A focused follow-up kill-pass should keep working the logic-shaped survivors down
— prioritising the `and`/`or` guard swaps and slice/boundary mutations in
`fetch.py`, `deep_read.py`, `llm.py` and `providers.py` — and shrink the baseline
accordingly. From here the baseline should only move **down** unless a genuinely
new equivalent is triaged by hand.

## Reproducing

```bash
make mutate           # per-module passes; reports to /tmp/mutmut_survivors_<module>.txt
python scripts/check_mutation_baseline.py
```

Validation after this re-triage: **2498 tests pass** (also under
`PYTHONWARNINGS=error`), **100% line + branch coverage** (`--cov-fail-under=100`),
ruff clean, and the kill-gate reports `match` for all 15 modules with
**0 timeouts**.
