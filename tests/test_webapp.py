"""Focused tests for the web app's env validation and permit cleanup."""

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

pytest.importorskip("flask")

# webapp.py loads persisted jobs at import time. Point it at a throwaway
# directory before importing so the test run cannot create .webapp/ in the repo.
os.environ["WEBAPP_STATE_DIR"] = tempfile.mkdtemp(prefix="webapp-test-")
os.environ.pop("WEBAPP_MAX_CONCURRENT", None)
os.environ.pop("PORT", None)

import webapp  # noqa: E402


def _run_import(**env: str) -> subprocess.CompletedProcess[str]:
    child_env = os.environ.copy()
    child_env.update(env)
    child_env["WEBAPP_STATE_DIR"] = os.environ["WEBAPP_STATE_DIR"]
    return subprocess.run(
        [sys.executable, "-c", "import webapp"],
        cwd=Path(__file__).resolve().parents[1],
        env=child_env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("value", ["0", "-1", "33", "not-a-number"])
def test_invalid_max_concurrent_fails_at_import(value: str) -> None:
    """A bad WEBAPP_MAX_CONCURRENT must name the variable and its range."""
    proc = _run_import(WEBAPP_MAX_CONCURRENT=value)
    assert proc.returncode != 0
    assert "WEBAPP_MAX_CONCURRENT" in proc.stderr
    assert "1" in proc.stderr
    assert "32" in proc.stderr


def test_env_int_rejects_out_of_range_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "70000")
    with pytest.raises(SystemExit) as exc_info:
        webapp._env_int("PORT", 8080, 1, 65535)
    message = str(exc_info.value)
    assert "PORT" in message
    assert "65535" in message


def test_env_int_returns_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "8080")
    assert webapp._env_int("PORT", 8080, 1, 65535) == 8080


def test_save_job_failure_releases_permit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failure after acquiring the permit must not strand a concurrency slot."""
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(webapp, "_SEM", sem)
    monkeypatch.setattr(webapp, "_JOBS", {})
    monkeypatch.setattr(webapp, "STATE_DIR", tmp_path)

    def fail_save(job: dict[str, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(webapp, "_save_job", fail_save)

    payload, status = webapp._start_job("service connection for tinnitus", 1, None)

    assert status == 500
    assert payload == {"error": "could not start the run"}
    assert webapp._JOBS == {}
    assert sem.acquire(blocking=False) is True
    sem.release()
