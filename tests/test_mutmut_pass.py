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


def _prepare_pass_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(_ORIGINAL, encoding="utf-8")
    package = tmp_path / "va_legal_agent"
    package.mkdir()
    (package / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_config.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["mutmut_pass.py", "config.py", "tests/test_config.py"]
    )


def _fake_pass_run(
    *, fail_match: str | None = None, results_stdout: str = "config.py: killed\n"
):
    def fake_run(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        cmd = [str(part) for part in args]
        joined = " ".join(cmd)
        if fail_match and fail_match in joined:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=7,
                stdout="",
                stderr=f"{fail_match} exploded",
            )
        if "mutmut" in cmd and "results" in cmd:
            stdout = results_stdout
            if "--all" not in cmd:
                # mutmut hides killed mutants unless --all is requested.
                stdout = "".join(
                    f"{line}\n"
                    for line in stdout.splitlines()
                    if not line.strip().endswith(": killed")
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=stdout, stderr=""
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    return fake_run


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


def test_main_restores_pyproject_when_pass_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real main() must restore the file even when it bails out."""
    _prepare_pass_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mutmut_pass.subprocess,
        "run",
        _fake_pass_run(results_stdout="config.py: not checked\n"),
    )

    with pytest.raises(SystemExit) as exc_info:
        mutmut_pass.main()

    assert exc_info.value.code == 1
    assert "refusing to report a vacuous pass" in capsys.readouterr().err
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == _ORIGINAL


@pytest.mark.parametrize(
    ("fail_match", "label"),
    [
        ("--cov", "pytest coverage run"),
        ("mutmut run", "mutmut run"),
        ("mutmut results", "mutmut results"),
    ],
)
def test_main_fails_when_required_command_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail_match: str,
    label: str,
) -> None:
    _prepare_pass_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mutmut_pass.subprocess, "run", _fake_pass_run(fail_match=fail_match)
    )

    with pytest.raises(SystemExit) as exc_info:
        mutmut_pass.main()

    assert exc_info.value.code == 1
    error = capsys.readouterr().err
    assert f"FATAL: {label} failed with exit code 7" in error
    assert f"{fail_match} exploded" in error
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == _ORIGINAL


def test_main_accepts_all_killed_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An all-killed pass must succeed rather than look like no mutants."""
    _prepare_pass_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mutmut_pass.subprocess,
        "run",
        _fake_pass_run(results_stdout="config.py: killed\n"),
    )

    mutmut_pass.main()

    assert "config.py: 0 survivors" in capsys.readouterr().out
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == _ORIGINAL


def test_main_rejects_empty_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No generated mutants must fail instead of reporting zero survivors."""
    _prepare_pass_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mutmut_pass.subprocess, "run", _fake_pass_run(results_stdout="")
    )

    with pytest.raises(SystemExit) as exc_info:
        mutmut_pass.main()

    assert exc_info.value.code == 1
    assert "mutmut reported no mutants" in capsys.readouterr().err
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == _ORIGINAL
