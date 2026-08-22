"""Enforce the mutation kill-property against the triaged baseline.

Reads the per-module survivor files written by ``make mutate``
(``/tmp/mutmut_survivors_<module>.txt``) and exits non-zero when any module's
survivor count exceeds its entry in ``.mutation-baseline.json`` -- and,
separately, when any mutant reached ``timeout`` status.

The baseline holds the *provably equivalent* survivors left after manual
triage: log-message text/argument swaps, identical ``.get``/default arguments,
model defaults matching explicitly-passed values, and unreachable fallbacks.
A count above the baseline means one of two things:

* a *killable* mutant slipped through - it must be killed by strengthening a
  test, not by editing the baseline; or
* a genuinely *new equivalent* appeared (e.g. a new log message) - triage the
  diff in ``/tmp/mutmut_survivors_<module>.txt`` by hand, and only then bump
  the baseline entry.

A ``timeout`` is never an equivalent: it means the mutant's test run hung
(usually a killable mutant that loops instead of failing). Failing only on the
count would let a hang hide in a module's headroom below its baseline, so any
timed-out mutant fails the gate outright regardless of count -- the killing
test must fail fast instead.

Usage::

    make mutate
    python scripts/check_mutation_baseline.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / ".mutation-baseline.json"
SURVIVOR_DIR = Path("/tmp")
HEADER_RE = re.compile(r"^### (.+): (\d+) survivors(?: \((\d+) timeout\))?$")


def read_survivor_counts(module: str) -> tuple[int, int]:
    """Return ``(survivor_count, timeout_count)`` from the module's survivor file.

    The ``timeout`` component is optional in the header (older files and files
    from a clean pass omit it), defaulting to zero.
    """
    header = (SURVIVOR_DIR / f"mutmut_survivors_{module}.txt").read_text().splitlines()[0]
    match = HEADER_RE.match(header)
    if match is None:
        raise ValueError(f"Unparseable survivor header: {header!r}")
    return int(match.group(2)), int(match.group(3) or 0)


def main() -> int:
    baseline = json.loads(BASELINE_PATH.read_text())
    violations: list[str] = []
    warnings: list[str] = []
    rows: list[tuple[str, int, int, int]] = []  # module, survivors, timeouts, baseline
    for module, expected in sorted(baseline.items()):
        try:
            actual, timeout_count = read_survivor_counts(module)
        except FileNotFoundError:
            warnings.append(
                f"{module}: no survivor file — the pass did not run (likely "
                "timed out on CI); verify locally with `make mutate-check`"
            )
            rows.append((module, -1, 0, expected))
            continue
        rows.append((module, actual, timeout_count, expected))
        if timeout_count > 0:
            violations.append(
                f"{module}: {timeout_count} mutant(s) TIMED OUT (hung test run) - a hang is "
                "never a triaged equivalent; make the killing test fail fast so the "
                "mutant is killed instead of absorbed into baseline headroom"
            )
        if actual > expected:
            violations.append(
                f"{module}: {actual} survivors exceed the triaged baseline of {expected} "
                f"(triage /tmp/mutmut_survivors_{module}.txt; kill real gaps, then "
                "bump the baseline only for newly-proven equivalents)"
            )

    width = max(len(module) for module, _, _, _ in rows)
    has_timeouts = any(timeout_count > 0 for _, _, timeout_count, _ in rows)
    has_missing = any(actual < 0 for _, actual, _, _ in rows)
    if has_timeouts or has_missing:
        print(f"{'module':<{width}}  survivors  baseline  timeout")
        for module, actual, timeout_count, expected in rows:
            actual_label = "MISSING" if actual < 0 else str(actual)
            print(f"{module:<{width}}  {actual_label:>9}  {expected:>8}  {timeout_count:>7}")
    else:
        print(f"{'module':<{width}}  survivors  baseline")
        for module, actual, _timeout_count, expected in rows:
            actual_label = "MISSING" if actual < 0 else str(actual)
            print(f"{module:<{width}}  {actual_label:>9}  {expected:>8}")

    if warnings:
        print("\nNotes:")
        for warning in warnings:
            print(f"  - {warning}")

    if violations:
        print("\nMutation kill-gate FAILED:")
        for violation in violations:
            print(f"  - {violation}")
        return 1
    print("\nMutation kill-gate passed: every completed module is at or under its triaged baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
