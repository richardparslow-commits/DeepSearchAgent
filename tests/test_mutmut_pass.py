"""Focused tests for the temporary pyproject scoping in mutmut_pass."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "mutmut_pass",
    Path(__file__).resolve().parent.parent / "scripts" / "mutmut_pass.py",
)
assert _SPEC is not None and _SPEC.loader is not None
mutmut_pass = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mutmut_pass)

_ORIGINAL = """[tool.mutmut]
source_paths = ["va_legal_agent/agent.py"]
pytest_add_cli_args_test_selection = ["tests/test_legal_agent.py"]
"""


def test_scoped_pyproject_restores_after_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(_ORIGINAL, encoding="utf-8")

    with pytest.raises(RuntimeError):
        with mutmut_pass._scoped_pyproject(
            "va_legal_agent/config.py", ["tests/test_config.py"]
        ):
            scoped = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
            assert 'source_paths = ["va_legal_agent/config.py"]' in scoped
            assert (
                'pytest_add_cli_args_test_selection = ["tests/test_config.py"]' in scoped
            )
            raise RuntimeError("boom")

    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == _ORIGINAL


def test_main_restores_pyproject_when_pass_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real main() must restore the file even when it bails out."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(_ORIGINAL, encoding="utf-8")
    package = tmp_path / "va_legal_agent"
    package.mkdir()
    (package / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_config.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["mutmut_pass.py", "config.py", "tests/test_config.py"]
    )

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="config.py: not checked\n", stderr=""
        )

    monkeypatch.setattr(mutmut_pass.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        mutmut_pass.main()

    assert exc_info.value.code == 1
    assert "refusing to report a vacuous pass" in capsys.readouterr().err
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == _ORIGINAL


def test_scoped_pyproject_restores_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(_ORIGINAL, encoding="utf-8")

    with mutmut_pass._scoped_pyproject(
        "va_legal_agent/config.py", ["tests/test_config.py"]
    ):
        assert 'source_paths = ["va_legal_agent/config.py"]' in (
            tmp_path / "pyproject.toml"
        ).read_text(encoding="utf-8")

    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == _ORIGINAL
