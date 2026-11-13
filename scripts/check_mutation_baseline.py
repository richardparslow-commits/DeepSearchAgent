"""Enforce the mutation kill-property against the triaged baseline.

Reads the per-module survivor files written by ``make mutate``
(``/tmp/mutmut_survivors_<module>.txt``) and exits non-zero when any module's
survivor names differ from its list in ``.mutation-baseline.json``, or when
any mutant reached ``timeout`` status.

The baseline holds the *provably equivalent* survivors left after manual
triage: log-message text/argument swaps, identical ``.get``/default arguments,
model defaults matching explicitly-passed values, and unreachable fallbacks.
A changed survivor set means one of two things:

* a *killable* mutant slipped through - it must be killed by strengthening a
  test, not by editing the baseline; or
* a genuinely *new equivalent* appeared (e.g. a new log message) - triage the
  diff in ``/tmp/mutmut_survivors_<module>.txt`` by hand, and only then add the
  new survivor name to the baseline.

Comparing names, rather than only their count, catches an equal-count swap
where an equivalent is killed and a different, killable mutant starts
surviving.

A ``timeout`` is never an equivalent: it means the mutant's test run hung
(usually a killable mutant that loops instead of failing). Failing only on the
survivor set would let a hang hide when the timed-out mutant replaces another
survivor, so any timed-out mutant fails the gate outright -- the killing test
must fail fast instead.

Usage::

    make mutate
    python scripts/check_mutation_baseline.py

A partial local run can pass ``--allow-missing`` to keep missing modules visible
without failing; CI never passes it, so an unmeasured module still fails the
gate there.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / ".mutation-baseline.json"
SURVIVOR_DIR = Path("/tmp")
HEADER_RE = re.compile(
    r"^### (?P<module>.+): (?P<count>\d+) survivors"
    r"(?: \(\d+ timeout\))?$"
)
IDENTITY_RE = re.compile(r"^# (?P<name>.+): (?P<status>[a-z]+)$")


def read_survivor_report(module: str) -> tuple[set[str], int]:
    """Return the survivor names and timeout count from a module report."""
    path = SURVIVOR_DIR / f"mutmut_survivors_{module}.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ValueError("survivor file is empty")
    header = HEADER_RE.match(lines[0])
    if header is None:
        raise ValueError(f"unparseable survivor header: {lines[0]!r}")
    if header.group("module") != module:
        raise ValueError(
            f"survivor header names {header.group('module')!r}, expected {module!r}"
        )

    names: list[str] = []
    timeout_count = 0
    for line in lines[1:]:
        match = IDENTITY_RE.match(line)
        if match is None:
            continue
        names.append(match.group("name"))
        if match.group("status") == "timeout":
            timeout_count += 1

    expected_count = int(header.group("count"))
    if len(names) != expected_count:
        raise ValueError(
            f"survivor header says {expected_count}, but the file contains "
            f"{len(names)} mutant identities"
        )
    if len(set(names)) != len(names):
        raise ValueError("survivor file contains duplicate mutant identities")
    return set(names), timeout_count


def read_baseline() -> dict[str, set[str]]:
    """Read and validate the module -> survivor-name mapping."""
    raw = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError("baseline must be a non-empty object")
    baseline: dict[str, set[str]] = {}
    for module, names in raw.items():
        if not isinstance(names, list) or not all(
            isinstance(name, str) and name for name in names
        ):
            raise ValueError(f"{module}: expected a list of survivor names")
        if len(set(names)) != len(names):
            raise ValueError(f"{module}: duplicate survivor names")
        baseline[module] = set(names)
    return baseline


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    allow_missing = args == ["--allow-missing"]
    if args and not allow_missing:
        print(
            "usage: check_mutation_baseline.py [--allow-missing]",
            file=sys.stderr,
        )
        return 2

    try:
        baseline = read_baseline()
    except (OSError, ValueError) as exc:
        print(f"error: invalid mutation baseline: {exc}", file=sys.stderr)
        return 1

    failures: list[str] = []
    missing: list[str] = []
    width = max(len(module) for module in baseline)
    print(f"{'module':<{width}}  survivors  names")
    for module, expected in sorted(baseline.items()):
        try:
            actual, timeout_count = read_survivor_report(module)
        except FileNotFoundError:
            missing.append(module)
            print(f"{module:<{width}}  {'MISSING':>9}  -")
            if not allow_missing:
                failures.append(
                    f"{module}: no survivor file — the pass did not run; re-run it "
                    "or pass --allow-missing for a partial local check"
                )
            continue
        except (OSError, ValueError) as exc:
            print(f"{module:<{width}}  {'INVALID':>9}  -")
            failures.append(f"{module}: invalid survivor file: {exc}")
            continue

        added = actual - expected
        removed = expected - actual
        identity = "match" if not added and not removed else "changed"
        print(f"{module:<{width}}  {len(actual):>9}  {identity}")

        if timeout_count > 0:
            failures.append(
                f"{module}: {timeout_count} mutant(s) TIMED OUT (hung test run) - "
                "a hang is never a triaged equivalent; make the killing test fail "
                "fast so the mutant is killed instead of absorbed into the baseline"
            )
        if added or removed:
            same_count = " at the same count" if len(actual) == len(expected) else ""
            failures.append(
                f"{module}: survivor names changed{same_count} "
                f"(+{len(added)} new, -{len(removed)} missing); "
                f"new: {', '.join(sorted(added)) or '-'}; "
                f"missing: {', '.join(sorted(removed)) or '-'}"
            )

    if allow_missing and missing:
        print(
            f"\nNote: {len(missing)} module(s) were not measured "
            "(--allow-missing)."
        )

    if failures:
        print("\nMutation kill-gate FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(
        "\nMutation kill-gate passed: every completed module matches its "
        "triaged survivor names."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
