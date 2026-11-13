"""Focused tests for the survivor-name mutation baseline gate."""

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


def _write_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    survivors: dict[str, list[str]],
) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(survivors), encoding="utf-8")
    monkeypatch.setattr(checker, "BASELINE_PATH", baseline_path)


def _write_survivors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    names: list[str],
    *,
    timeouts: set[str] | None = None,
) -> Path:
    survivor_dir = tmp_path / "survivors"
    survivor_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(checker, "SURVIVOR_DIR", survivor_dir)
    timeouts = timeouts or set()
    timeout_count = sum(1 for name in names if name in timeouts)
    suffix = f" ({timeout_count} timeout)" if timeout_count else ""
    lines = [f"### {module}: {len(names)} survivors{suffix}", ""]
    for name in names:
        status = "timeout" if name in timeouts else "survived"
        lines.extend([f"# {name}: {status}", f"--- diff for {name}", ""])
    path = survivor_dir / f"mutmut_survivors_{module}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_exact_survivor_set_passes_in_any_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_baseline(tmp_path, monkeypatch, {"config.py": ["mutant-b", "mutant-a"]})
    _write_survivors(
        tmp_path, monkeypatch, "config.py", ["mutant-a", "mutant-b"]
    )

    assert checker.main([]) == 0

    output = capsys.readouterr().out
    assert "config.py" in output
    assert "match" in output
    assert "triaged survivor names" in output


def test_equal_count_survivor_swap_fails_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A same-count replacement must not inherit the old equivalent's pass."""
    _write_baseline(tmp_path, monkeypatch, {"config.py": ["mutant-a", "mutant-b"]})
    _write_survivors(
        tmp_path, monkeypatch, "config.py", ["mutant-a", "mutant-c"]
    )

    assert checker.main([]) == 1

    output = capsys.readouterr().out
    assert "survivor names changed at the same count" in output
    assert "new: mutant-c" in output
    assert "missing: mutant-b" in output


def test_timeout_fails_even_when_names_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_baseline(tmp_path, monkeypatch, {"config.py": ["mutant-a"]})
    _write_survivors(
        tmp_path,
        monkeypatch,
        "config.py",
        ["mutant-a"],
        timeouts={"mutant-a"},
    )

    assert checker.main([]) == 1

    assert "TIMED OUT" in capsys.readouterr().out


def test_missing_survivor_file_fails_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_baseline(tmp_path, monkeypatch, {"config.py": ["mutant-a"]})
    survivor_dir = tmp_path / "survivors"
    survivor_dir.mkdir()
    monkeypatch.setattr(checker, "SURVIVOR_DIR", survivor_dir)

    assert checker.main([]) == 1

    output = capsys.readouterr().out
    assert "config.py" in output
    assert "MISSING" in output
    assert "no survivor file" in output


def test_allow_missing_flag_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_baseline(tmp_path, monkeypatch, {"config.py": ["mutant-a"]})
    survivor_dir = tmp_path / "survivors"
    survivor_dir.mkdir()
    monkeypatch.setattr(checker, "SURVIVOR_DIR", survivor_dir)

    assert checker.main(["--allow-missing"]) == 0

    output = capsys.readouterr().out
    assert "MISSING" in output
    assert "not measured" in output


def test_truncated_identity_list_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_baseline(tmp_path, monkeypatch, {"config.py": ["mutant-a", "mutant-b"]})
    path = _write_survivors(
        tmp_path, monkeypatch, "config.py", ["mutant-a", "mutant-b"]
    )
    path.write_text(
        "### config.py: 2 survivors\n\n# mutant-a: survived\n",
        encoding="utf-8",
    )

    assert checker.main([]) == 1

    output = capsys.readouterr().out
    assert "INVALID" in output
    assert "1 mutant identities" in output


def test_unknown_flag_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_baseline(tmp_path, monkeypatch, {"config.py": []})

    assert checker.main(["--bogus"]) == 2
    assert "usage" in capsys.readouterr().err.lower()
