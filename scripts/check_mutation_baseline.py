"""Enforce the mutation kill-property against the triaged baseline.

Reads the per-module survivor files written by ``make mutate``
(``/tmp/mutmut_survivors_<module>.txt``) and exits non-zero when any module's
survivor count exceeds its entry in ``.mutation-baseline.json``.

The baseline holds the *provably equivalent* survivors left after manual
triage: log-message text/argument swaps, identical ``.get``/default arguments,
model defaults matching explicitly-passed values, and unreachable fallbacks.
A count above the baseline means one of two things:

* a *killable* mutant slipped through - it must be killed by strengthening a
  test, not by editing the baseline; or
* a genuinely *new equivalent* appeared (e.g. a new log message) - triage the
  diff in ``/tmp/mutmut_survivors_<module>.txt`` by hand, and only then bump
  the baseline entry.

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
HEADER_RE = re.compile(r"^### (.+): (\d+) survivors$")


def read_survivor_count(module: str) -> int | None:
    """Return the survivor count from the module's survivor file, or None if absent."""
    header = (SURVIVOR_DIR / f"mutmut_survivors_{module}.txt").read_text().splitlines()[0]
    match = HEADER_RE.match(header)
    if match is None:
        raise ValueError(f"Unparseable survivor header: {header!r}")
    return int(match.group(2))


def main() -> int:
    baseline = json.loads(BASELINE_PATH.read_text())
    violations: list[str] = []
    rows: list[tuple[str, int, int]] = []
    for module, expected in sorted(baseline.items()):
        try:
            actual = read_survivor_count(module)
        except FileNotFoundError:
            violations.append(
                f"{module}: no survivor file - the pass did not run for this module"
            )
            rows.append((module, -1, expected))
            continue
        rows.append((module, actual, expected))
        if actual > expected:
            violations.append(
                f"{module}: {actual} survivors exceed the triaged baseline of {expected} "
                f"(triage /tmp/mutmut_survivors_{module}.txt; kill real gaps, then "
                "bump the baseline only for newly-proven equivalents)"
            )

    width = max(len(module) for module, _, _ in rows)
    print(f"{'module':<{width}}  survivors  baseline")
    for module, actual, expected in rows:
        actual_label = "MISSING" if actual < 0 else str(actual)
        print(f"{module:<{width}}  {actual_label:>9}  {expected:>8}")

    if violations:
        print("\nMutation kill-gate FAILED:")
        for violation in violations:
            print(f"  - {violation}")
        return 1
    print("\nMutation kill-gate passed: every module is at or under its triaged baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
