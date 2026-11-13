"""Focused tests for missing-survivor handling in the mutation baseline gate."""

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_mutation_baseline",
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "check_mutation_baseline.py",
)
assert _SPEC is not None and _SPEC.loader is not None
checker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(checker)


def _missing_survivors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"config.py": 41}), encoding="utf-8")
    survivor_dir = tmp_path / "survivors"
    survivor_dir.mkdir()
    monkeypatch.setattr(checker, "BASELINE_PATH", baseline_path)
    monkeypatch.setattr(checker, "SURVIVOR_DIR", survivor_dir)
    return survivor_dir


def test_missing_survivor_file_fails_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _missing_survivors(tmp_path, monkeypatch)

    assert checker.main([]) == 1

    output = capsys.readouterr().out
    assert "config.py" in output
    assert "no survivor file" in output
    assert "FAILED" in output


def test_allow_missing_flag_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _missing_survivors(tmp_path, monkeypatch)

    assert checker.main(["--allow-missing"]) == 0

    output = capsys.readouterr().out
    assert "no survivor file" in output
    assert "allow-missing" in output.lower()
    assert "not measured" in output.lower()


def test_unknown_flag_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _missing_survivors(tmp_path, monkeypatch)

    assert checker.main(["--bogus"]) == 2
    assert "unknown" in capsys.readouterr().err.lower()
