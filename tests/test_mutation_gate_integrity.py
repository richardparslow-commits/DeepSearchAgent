"""Regression tests for mutation-gate failure propagation."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "mutation.yml"


def _heavy_step() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "Mutation pass — heavy modules (providers, queries)"
    assert marker in text
    block = text.split(marker, 1)[1]
    return block.split("- name:", 1)[0]


def test_workflow_does_not_swallow_heavy_pass_failures() -> None:
    """A failed providers/queries pass must fail the workflow step."""
    block = _heavy_step()

    assert "scripts/mutmut_pass.py providers.py" in block
    assert "scripts/mutmut_pass.py queries.py" in block
    assert "set -euo pipefail" in block
    assert "||" not in block
    assert "WARNING" not in block


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
