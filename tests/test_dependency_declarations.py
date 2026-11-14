"""Regression tests for single-source dependency declarations."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _requirement_lines(name: str) -> list[str]:
    lines = (REPO_ROOT / name).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def test_requirements_files_delegate_to_pyproject() -> None:
    """The wrappers must not duplicate or diverge from pyproject dependencies."""
    assert _requirement_lines("requirements.txt") == ["-e ."]
    assert _requirement_lines("requirements-dev.txt") == ["-e .[dev,llm,web]"]


def test_ci_installs_the_same_extras_as_dev_requirements() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert 'pip install -e ".[dev,llm,web]"' in workflow
