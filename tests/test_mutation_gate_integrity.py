"""Regression tests for mutation-gate failure propagation."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "mutation.yml"
BASELINE = REPO_ROOT / ".mutation-baseline.json"
MATRIX_ENTRY_RE = re.compile(
    r"^\s*- module: (?P<module>\S+)\n"
    r"\s+tests: (?P<tests>.+)\n"
    r"\s+timeout: (?P<timeout>\d+)$",
    re.MULTILINE,
)


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _matrix_entries(text: str) -> dict[str, tuple[str, int]]:
    entries = {
        match.group("module"): (match.group("tests"), int(match.group("timeout")))
        for match in MATRIX_ENTRY_RE.finditer(text)
    }
    assert entries, "mutation workflow has no matrix entries"
    return entries


def test_every_baseline_module_has_its_own_matrix_leg() -> None:
    """One timed-out pass must not skip the modules after it."""
    modules = json.loads(BASELINE.read_text(encoding="utf-8"))
    text = _workflow_text()
    entries = _matrix_entries(text)

    assert set(entries) == set(modules)
    assert all(timeout > 0 for _, timeout in entries.values())
    assert all(tests.startswith("tests/") for tests, _ in entries.values())
    assert text.count("scripts/mutmut_pass.py") == 1
    assert (
        "scripts/mutmut_pass.py ${{ matrix.module }} ${{ matrix.tests }}"
        in text
    )
    assert "Mutation pass — remaining modules" not in text


def test_gate_runs_after_a_failed_or_timed_out_leg() -> None:
    """The gate must run after failures and inspect every downloaded report."""
    gate = _workflow_text().split("  gate:", 1)[1]

    assert "needs: mutate" in gate
    assert "if: ${{ !cancelled() }}" in gate
    assert "pattern: survivors-*" in gate
    assert "merge-multiple: true" in gate
    assert "scripts/check_mutation_baseline.py" in gate


def test_each_leg_uploads_its_report_and_has_a_separate_timeout() -> None:
    """A timeout only kills one module's leg, and its report still uploads."""
    text = _workflow_text()

    assert "fail-fast: false" in text
    assert "timeout-minutes: ${{ matrix.timeout }}" in text
    assert "name: survivors-${{ matrix.module }}" in text
    assert "if: always()" in text
    assert "if-no-files-found: warn" in text


def test_workflow_does_not_swallow_mutation_failures() -> None:
    """No failed pass may be converted into a warning or ignored exit code."""
    text = _workflow_text()

    assert "set -euo pipefail" in text
    assert "||" not in text
    assert "WARNING" not in text


def test_baseline_records_survivor_names() -> None:
    """Names, unlike counts alone, catch an equal-count swap."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert baseline
    assert "models.py" not in baseline
    for module, names in baseline.items():
        assert isinstance(names, list), module
        assert all(isinstance(name, str) and name for name in names), module
        assert len(names) == len(set(names)), module


def test_make_mutate_stops_on_first_failed_pass(tmp_path: Path) -> None:
    """The shell loop must not continue to later modules after a failure."""
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is not installed")

    log = tmp_path / "calls.log"
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        f"""#!/bin/sh
printf '%s\\n' "$2" >> "{log}"
case "$2" in
  *fail.py*) exit 7 ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    modules = (
        '"one.py tests/test_one.py" '
        '"fail.py tests/test_fail.py" '
        '"three.py tests/test_three.py"'
    )

    proc = subprocess.run(
        [
            make,
            "-C",
            str(REPO_ROOT),
            "-f",
            "Makefile",
            "mutate",
            f"PYTHON={fake_python}",
            f"MUTATE_MODULES={modules}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode != 0
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert "one.py" in calls[0]
    assert "fail.py" in calls[1]
