"""Focused tests for deploy/run_batch.sh configuration handling."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_BATCH = REPO_ROOT / "deploy" / "run_batch.sh"


def _env(**overrides: str) -> dict[str, str]:
    cleared = {
        "LOG_KEEP",
        "RUN_ID",
        "PY",
        "ISSUES_FILE",
        "LOG_DIR",
        "MODE",
        "MAX_BATCH_REQUESTS",
    }
    env = {key: value for key, value in os.environ.items() if key not in cleared}
    env.update(overrides)
    return env


def _fake_python(tmp_path: Path) -> tuple[Path, Path]:
    args_log = tmp_path / "args.log"
    fake = tmp_path / "python"
    fake.write_text(
        f"""#!/bin/sh
printf '%s\\n' "$@" > "{args_log}"
exit 0
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake, args_log


def _run(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    return subprocess.run(
        [bash, str(RUN_BATCH)],
        cwd=REPO_ROOT,
        env=_env(**overrides),
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("value", ["abc", "0", "-1", "1.5"])
def test_invalid_log_keep_fails_before_running(tmp_path: Path, value: str) -> None:
    proc = _run(tmp_path, LOG_KEEP=value)

    assert proc.returncode != 0
    assert "LOG_KEEP" in proc.stderr
    assert "positive integer" in proc.stderr


def test_valid_log_keep_passes_run_id_to_the_cli(tmp_path: Path) -> None:
    fake_python, args_log = _fake_python(tmp_path)
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("service connection for tinnitus\n", encoding="utf-8")
    log_dir = tmp_path / "logs"

    proc = _run(
        tmp_path,
        PY=str(fake_python),
        ISSUES_FILE=str(issues_file),
        LOG_DIR=str(log_dir),
        LOG_KEEP="3",
        RUN_ID="batch-test",
        MODE="plan",
    )

    assert proc.returncode == 0, proc.stderr
    args = args_log.read_text(encoding="utf-8").splitlines()
    assert "--batch-dry-run" in args
    assert args[args.index("--issues-file") + 1] == str(issues_file)
    assert args[args.index("--run-id") + 1] == "batch-test"
    assert list(log_dir.glob("batch-*.log"))
