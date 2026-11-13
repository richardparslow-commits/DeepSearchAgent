"""Focused integration tests for deploy/install.sh argument parsing."""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "TARGET"}
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _blocked_target(tmp_path: Path) -> Path:
    """A path whose parent is a file, so mkdir fails before any install work."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    return blocker / "target"


def test_target_flag_is_parsed(tmp_path: Path) -> None:
    target = _blocked_target(tmp_path)

    proc = _run("--target", str(target))

    assert proc.returncode != 0
    assert f"Installing DeepSearchAgent to {target}" in proc.stdout
    assert "/opt/va-legal-agent" not in proc.stdout


def test_target_flag_requires_a_value() -> None:
    proc = _run("--target")

    assert proc.returncode == 2
    assert "--target" in proc.stderr
    assert "requires" in proc.stderr.lower()


def test_unknown_flag_is_rejected() -> None:
    proc = _run("--bogus")

    assert proc.returncode == 2
    assert "unknown" in proc.stderr.lower()


def test_target_and_cron_can_be_combined(tmp_path: Path) -> None:
    target = _blocked_target(tmp_path)

    proc = _run("--target", str(target), "--cron")

    assert proc.returncode != 0
    assert f"Installing DeepSearchAgent to {target}" in proc.stdout
    assert "unknown argument" not in proc.stderr.lower()


def test_copy_step_excludes_secrets_and_runtime_state() -> None:
    script = INSTALL_SH.read_text(encoding="utf-8")

    for excluded in (".env", ".webapp", "issues.txt", "issues.retry"):
        assert f"--exclude='{excluded}'" in script
