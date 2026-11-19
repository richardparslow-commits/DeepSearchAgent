"""Regression tests for mutation-gate failure propagation."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "mutation.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
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


def test_workflows_grant_only_read_access() -> None:
    """Both workflows must not inherit write-capable default permissions."""
    pattern = re.compile(r"(?m)^permissions:\n  contents: read$")
    for workflow in (CI_WORKFLOW, WORKFLOW):
        assert pattern.search(workflow.read_text(encoding="utf-8")), workflow


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


MAKEFILE = REPO_ROOT / "Makefile"
PACKAGE_DIR = REPO_ROOT / "va_legal_agent"

# Modules deliberately OUTSIDE the mutation gate, each with a reason. This is
# tracked debt, not a silent hole: a new package module must either be measured
# or be listed here, otherwise the suite fails. Shrink this list over time —
# see docs/mutation-baseline-retriage.md for the follow-up kill-pass order.
MUTATION_EXEMPT = {
    "__main__.py": "3.1k lines of CLI/render wiring; slice not yet chosen",
    "models.py": "pydantic validators; mostly declarative, slice not yet chosen",
    "va_domain.py": "1.6k lines of domain guidance; needs a dedicated kill-pass",
    "authorities.py": "external-authority verification; slice not yet chosen",
    "regulations.py": "Regulations.gov client; slice not yet chosen",
    "congress.py": "Congress.gov client; slice not yet chosen",
    "adjudication.py": "adjudication queue persistence; slice not yet chosen",
    "provenance_compare.py": "provenance sidecar comparison; slice not yet chosen",
    "snapshots.py": "snapshot store; slice not yet chosen",
    "authority_queue.py": "authority queue ordering; slice not yet chosen",
    "appellate_history.py": "appellate history records; slice not yet chosen",
    "extraction.py": "quote-first extraction; slice not yet chosen",
    "courtlistener_cache.py": "response cache; slice not yet chosen",
}


def _makefile_scope() -> dict[str, str]:
    """Parse MUTATE_MODULES into {module: test-slice string}."""
    text = MAKEFILE.read_text(encoding="utf-8")
    block = text.split("MUTATE_MODULES =", 1)[1].split("\n\n", 1)[0]
    scope = dict(re.findall(r'"([a-z_]+\.py)\s+([^"]+)"', block))
    assert scope, "Makefile has no MUTATE_MODULES entries"
    return scope


def test_makefile_and_workflow_measure_the_same_modules() -> None:
    """`make mutate` locally and the CI matrix must not drift apart."""
    scope = _makefile_scope()
    entries = _matrix_entries(_workflow_text())

    assert set(scope) == set(entries)
    for module, tests in scope.items():
        assert entries[module][0].split() == tests.split(), module


def test_every_package_module_is_measured_or_explicitly_exempt() -> None:
    """The gate must not silently skip a module (the DOC5 scope gap).

    Adding a module to the package without either wiring it into the mutation
    scope or recording an explicit exemption fails here.
    """
    measured = set(_makefile_scope())
    present = {
        path.name for path in PACKAGE_DIR.glob("*.py") if path.name != "__init__.py"
    }
    exempt = set(MUTATION_EXEMPT)

    assert not (measured & exempt), (
        f"modules are both measured and exempt: {sorted(measured & exempt)}"
    )
    unaccounted = present - measured - exempt
    assert not unaccounted, (
        "package modules are neither mutation-measured nor explicitly exempt: "
        f"{sorted(unaccounted)}. Add them to MUTATE_MODULES + the workflow "
        "matrix, or record a reason in MUTATION_EXEMPT."
    )
    stale = exempt - present
    assert not stale, f"exemptions name modules that no longer exist: {sorted(stale)}"
    assert all(reason.strip() for reason in MUTATION_EXEMPT.values())


def test_redaction_is_mutation_measured() -> None:
    """Credential scrubbing is security-critical and must stay gated."""
    scope = _makefile_scope()
    assert "redaction.py" in scope
    assert "tests/test_redaction.py" in scope["redaction.py"]
    assert "redaction.py" not in MUTATION_EXEMPT


def test_citator_is_mutation_measured() -> None:
    """Good-law / negative-treatment correctness must stay gated."""
    scope = _makefile_scope()
    assert "citator.py" in scope
    assert "tests/test_citator.py" in scope["citator.py"]
    assert "citator.py" not in MUTATION_EXEMPT


def test_sandbox_mirrors_every_repo_path_the_tests_read() -> None:
    """mutmut runs the slice from mutants/, so repo-root reads must be mirrored.

    A test reading e.g. ``deploy/install.sh`` resolves it under the sandbox
    root. If the pass script does not mirror that path, mutmut's stats run dies
    on FileNotFoundError and exits 1 — the original AUD4 gate crash, and again
    when va_domain's slice pulled in test_audit_pass5.py.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_mutmut_pass_probe", REPO_ROOT / "scripts" / "mutmut_pass.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    mirrored = set(module._SANDBOX_CONTEXT_DIRS) | set(module._SANDBOX_CONTEXT_FILES)

    pattern = re.compile(r'\b(?:root|ROOT|REPO_ROOT|PROJECT_ROOT) / "([^"]+)"')
    referenced: set[str] = set()
    for path in (REPO_ROOT / "tests").glob("*.py"):
        referenced |= set(pattern.findall(path.read_text(encoding="utf-8")))

    # Only real repo-root paths matter; tmp_path-relative names do not exist here.
    needed = {name for name in referenced if (REPO_ROOT / name).exists()}
    # The package itself is mirrored separately as the mutable copy.
    needed.discard("va_legal_agent")

    missing = needed - mirrored
    assert not missing, (
        f"tests read repo paths that mutmut_pass.py does not mirror: {sorted(missing)}. "
        "Add them to _SANDBOX_CONTEXT_DIRS / _SANDBOX_CONTEXT_FILES."
    )
