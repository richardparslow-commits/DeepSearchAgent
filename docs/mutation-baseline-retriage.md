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

## Scope extension: `redaction.py` is now gated

The gate covered **15 of 30** package modules — 9,766 of 25,933 lines. The
unmeasured 62% included `redaction.py`, whose entire job is preventing credential
disclosure (AUD4-001 was **Critical**). Audit finding **DOC5** had flagged this
scope gap, but it was closed by *rewording the README* ("No code behavior
changes"), leaving the hole open. Nothing enforced scope completeness:
`test_mutation_gate_integrity.py` only compared the baseline to the matrix.

Measuring `redaction.py` against its own slice found **84 untracked survivors**,
including genuinely killable security-critical ones. Demonstrated concretely —
mutating `_coerce_secrets`' longest-first sort to `key=None` turns

```
Authorization: Bearer [REDACTED]          (correct)
Authorization: Bearer AAAAA[REDACTED]BBBB (mutant: credential fragments leak)
```

and the **entire 2,498-test suite still passed**. Longest-first matters because
when one secret is a substring of another, replacing the shorter one first
leaves fragments of the longer credential behind.

`redaction.py` is now in `MUTATE_MODULES` and the CI matrix (slice
`tests/test_redaction.py tests/test_audit_invariants.py`, 10-minute leg). New
tests kill the security-critical mutants; each was **verified by applying the
mutation and confirming the suite fails** (14/14 killed):

| Mutant class | Examples |
|---|---|
| Secret ordering | `sort(key=len, reverse=True)` → `key=None`, `reverse=False`, no sort |
| Length floor | `len(text) < 4` → `<= 4`, `< 5` (would skip 4-char keys) |
| Filter precedence | the `or` chain → `and` (breaks dedup / short-value skip) |
| Query fidelity | `keep_blank_values=True` → `False`; `urlencode(safe="[]")` dropped |
| Secret propagation | `redact_secrets(rebuilt, extra_secrets)` → `None` |
| Name normalization | `replace(" ", "_")` broken in `_parameter_is_sensitive` |
| Header scrubbing | `sensitive or redacted != value` → `and` |
| Type guard | `not isinstance(...) or not text` → `and` |
| Attribute coverage | `"reason"` dropped from the scrubbed-attribute tuple |

Survivors: **84 → 64**. The remainder are the documented equivalent classes
(default/`None` arguments, `getattr` defaults, and variant-encoding orderings
where no variant is a substring of another).

### Scope-completeness guard

`tests/test_mutation_gate_integrity.py` now also asserts:

* `MUTATE_MODULES` and the CI matrix measure **the same modules and slices**;
* **every** package module is either measured or carries an explicit, reasoned
  entry in `MUTATION_EXEMPT` (11 modules, listed with reasons);
* `redaction.py`, `citator.py`, `va_domain.py` and `snapshots.py` stay measured.

Verified: dropping a new unaccounted module into the package fails the guard with
an actionable message naming the module. The gap is now tracked debt that cannot
silently reopen, rather than an invisible hole.

## Scope extension: `citator.py` is now gated

`citator.py` is the largest module in the package (3,826 lines / 1,388
statements — comparable in *statements* to `providers.py`, so a scoped pass is
affordable). It owns the highest legal-correctness risk: whether a decision is
reported as **good law**, **negative**, or **unknown**.

Measured against the four test files that import it
(`test_citator.py`, `test_source_validity.py`, `test_source_audit_properties.py`,
`test_fetch_error_kind.py`): **762 survivors, 0 timeouts**.

**The core status decision was already safe.** The critical
`elif complete and reviewed and latest: status = "good_law"` guard is *not* among
the survivors — existing tests kill its `and`/`or` and boundary mutants. That was
verified rather than assumed, and it is the single most dangerous mutation in the
module (an `or` there would assert "good law" on incomplete coverage).

### Killable legal-correctness mutants that were killed

Each was verified by applying the mutation and confirming the suite fails
(**9/9 killed**):

| Area | Mutation | Consequence if uncaught |
|---|---|---|
| `_scope_for` "all" set | `abrogated`, `superseded`, `adhered to` dropped | a whole-precedent overruling is reported with `unclear` scope |
| `_scope_for` partial scan | `re.I` removed | `OVERRULED IN PART` misclassified as `all` instead of `part` |
| `_is_negated` | window start `max(0, …)` → `max(1, …)` | misses negation at the start of a short sentence ("not overruled") |
| `_is_negated` | window `80` → `81` characters | honours a negation outside the intended window |
| `_is_non_verb_negative` | plural `questions` dropped from the noun set | the noun "questions presented" becomes a treatment |
| `_is_non_verb_negative` | verb-object scan `re.I` removed | uppercase determiner/`WHETHER` no longer marks the verb form |
| `_apply_source_validity_enforcement` | `good_law and good_law_as_of` → `or` | demotes a clean `good_law` that never carried an as-of date |

The negation-window tests are exact boundary pins (a negation at offset 0, and one
that only fits an 81-character window), not just happy-path assertions.

Survivors: **762 → 749**. The remainder are dominated by advisory detail/reason
strings and default/`None` arguments in the source-audit and candidate-review
helpers; they are **not** all claimed provably equivalent (see the limitation
below).

`citator.py` is now in `MUTATE_MODULES` and the CI matrix with a **45-minute**
leg, and `test_citator_is_mutation_measured` keeps it gated.

## Scope extension: `va_domain.py` is now gated

`va_domain.py` (1,633 lines / 476 statements) maps decisions onto VA adjudication:
provision impacts, provenance verification, D1 metadata and role guidance.

Wiring it surfaced a **second instance of the sandbox-mirroring crash**: its slice
includes `tests/test_audit_pass5.py`, which reads `deploy/install.sh` and
`scripts/`. The earlier fix mirrored only `tests/` and `benchmarks/`, so mutmut
again exited 1. That was fixed generally (all repo-root reads mirrored, plus a
completeness guard) and shipped separately before this scope change.

Measured: **962 survivors, 0 timeouts**. Over half (492) are guidance-string
literals in `build_rvsr_guidance` / `build_senior_rater_guidance` — advisory
playbook text, the documented equivalent class.

### Killable correctness mutants that were killed

Verified by applying each mutation and confirming the suite fails (**6/6**):

| Mutation | Consequence if uncaught |
|---|---|
| `verified = host_verified and anchor_confirmed` → `or` | provenance claimed on official-host membership alone, with no citation-bearing anchor |
| `anchor_confirmed = bool(quote.strip()) and (…)` → `or` | an empty anchor still "confirms" the citation |
| `host_verified = bool(host) and (…)` → `or` | a secondary host counts as an official source |
| `host_verified = bool(host)` → `bool(None)` | provenance never verified at all |
| `if subsection and not spec.citation.endswith(…)` → `or` | the AUD5 subsection guard breaks: `§ 5107(a)` text emits the `§ 5107(b)` benefit-of-the-doubt impact |
| `(match.group(1) or "")` → `or "XXXX"` | a bare alias (`§ 5107`) stops mapping to its registry provision |

The new tests pin all three provenance combinations (official+anchor, official+empty
anchor, secondary+anchor) and all three subsection cases (bare, matching,
mismatched).

**Disproved rather than assumed:** the canonical-citation boundary and
`re.IGNORECASE` mutants looked killable but are **equivalent** — all 49 registry
specs are U.S.C./C.F.R. with an alias fallback that yields the same impact, so
breaking the canonical pattern does not change the result.

Survivors: **962 → 941**. `va_domain.py` is now in `MUTATE_MODULES` and the CI
matrix with a **40-minute** leg, and `test_va_domain_is_mutation_measured` keeps it
gated.

## Scope extension: `snapshots.py` is now gated

`snapshots.py` (426 lines / 202 statements) is the durable evidence store: content
identity, atomic payload writes, the manifest, and provider-response capture.

Measured: **187 survivors, 0 timeouts**. Killed 4 killable identity/durability
mutants, each verified by applying the mutation and confirming the suite fails
(**4/4**):

| Mutation | Consequence if uncaught |
|---|---|
| `snapshot_id = digest[:32]` → `[:33]` | snapshot identity length drifts from the documented 32 hex chars |
| `root.mkdir(parents=True, …)` → `parents=False` / dropped | a nested state directory raises instead of being created |
| `.replace(microsecond=0)` → `1` | timestamps stop being stable whole seconds |

Survivors: **187 → 180**. Deliberately **not** targeted: the `"MANIFEST.JSONL"` /
`"SNAPSHOTS"` filename-case mutants. macOS's case-insensitive filesystem resolves
them to the same file, so killing them locally would not reproduce on the Linux
runner — exactly the platform-divergence trap hit earlier with providers. They are
left as baselined rather than pinned with a platform-dependent assertion.

`snapshots.py` is now in `MUTATE_MODULES` and the CI matrix with a **15-minute**
leg, and `test_snapshots_is_mutation_measured` keeps it gated.

### Note on the earlier "potentially killable" estimate

The first categorisation pass flagged ~252 logic-shaped survivors across the
original 15 modules. Inspecting them individually shows that estimate was **too
high**: many are provably equivalent, e.g. in `providers.py`

* `"\u201C"` vs `"\u201c"` — hex escapes are case-insensitive, so the *same* character;
* `encoding="UTF-8"` vs `"utf-8"` — codec aliases;
* `url.rsplit("/", 1)[+1]` vs `[-1]`, and `rsplit("/", 2)` / `rsplit("/")` — all
  yield the last segment for a multi-slash URL.

Those are correctly baselined, not gaps. The owed kill-pass should therefore
expect a smaller yield than the raw REVIEW count suggested.

## Per-module counts

| Module | Baseline before | First measured pass | After killing tests |
|---|---:|---:|---:|
| `agent.py` | 140 | 275 | 275 |
| `batch.py` | 21 | 30 | 28 |
| `citator.py` | not gated | 762 | 749 |
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
| `redaction.py` | not gated | 84 | 64 |
| `reliability.py` | 2 | 28 | 18 |
| `search.py` | 40 | 43 | 40 |
| `snapshots.py` | not gated | 187 | 180 |
| `topics.py` | 0 | 0 | 0 |
| `va_domain.py` | not gated | 962 | 941 |
| **Total** | **640** (15 gated) | **3475** | **3374** (19 gated) |

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

Validation after this re-triage: **2524 tests pass** (also under
`PYTHONWARNINGS=error`), **100% line + branch coverage** (`--cov-fail-under=100`),
ruff clean, and the kill-gate reports `match` for all 19 modules with
**0 timeouts**.
