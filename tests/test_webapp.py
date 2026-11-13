"""Focused tests for the web app's env validation and permit cleanup."""

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock

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


@pytest.fixture
def isolated_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Give each test a clean job map and its own WEBAPP_STATE_DIR."""
    monkeypatch.setenv("WEBAPP_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(webapp, "STATE_DIR", tmp_path)
    monkeypatch.setattr(webapp, "_JOBS", {})
    return webapp.app.test_client()


def test_status_serializes_snapshot(isolated_client, monkeypatch) -> None:
    """A status poll must not serialize the live dict the worker mutates."""
    webapp._JOBS["snapshot"] = {
        "job_id": "snapshot",
        "issue": "tinnitus",
        "status": "running",
        "started_at": "2026-01-01T00:00:00+00:00",
    }
    original_jsonify = webapp.jsonify

    def mutate_payload(payload: dict[str, object]):
        payload["late_mutation"] = True
        return original_jsonify(payload)

    monkeypatch.setattr(webapp, "jsonify", mutate_payload)

    response = isolated_client.get("/api/status/snapshot")

    assert response.status_code == 200
    assert response.get_json()["job_id"] == "snapshot"
    assert "late_mutation" not in webapp._JOBS["snapshot"]


def test_prune_keeps_running_jobs(
    isolated_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning over the cap must not evict a job whose thread is still running."""
    jobs: dict[str, dict[str, object]] = {
        "running-oldest": {
            "job_id": "running-oldest",
            "issue": "tinnitus",
            "status": "running",
            "started_at": "2026-01-01T00:00:00+00:00",
        }
    }
    for index in range(webapp._MAX_JOBS):
        job_id = f"done-{index}"
        jobs[job_id] = {
            "job_id": job_id,
            "issue": "tinnitus",
            "status": "done",
            "started_at": f"2026-01-02T00:00:{index:02d}+00:00",
        }
    monkeypatch.setattr(webapp, "_JOBS", jobs)

    webapp._prune()

    assert len(webapp._JOBS) == webapp._MAX_JOBS
    response = isolated_client.get("/api/status/running-oldest")
    assert response.status_code == 200
    assert response.get_json()["status"] == "running"


def test_load_jobs_persists_restart_correction(
    isolated_client, tmp_path: Path
) -> None:
    """A restarted running job must be corrected on disk, not just in memory."""
    persisted = tmp_path / "restart.json"
    persisted.write_text(
        json.dumps(
            {
                "job_id": "restart",
                "issue": "tinnitus",
                "status": "running",
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    webapp._load_jobs()

    response = isolated_client.get("/api/status/restart")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "error"
    assert body["error"] == "interrupted by server restart"
    on_disk = json.loads(persisted.read_text(encoding="utf-8"))
    assert on_disk["status"] == "error"
    assert on_disk["error"] == "interrupted by server restart"
    assert on_disk["finished_at"]


def test_foreign_origin_is_rejected(isolated_client) -> None:
    """A cross-site page must not be able to start a quota-spending run."""
    headers = {"Origin": "https://evil.example"}
    responses = [
        isolated_client.post("/api/analyze", json={"issue": "tinnitus"}, headers=headers),
        isolated_client.post("/analyze", data={"issue": "tinnitus"}, headers=headers),
    ]

    for response in responses:
        assert response.status_code == 403
        assert response.get_json()["error"] == "cross-origin request rejected"
    assert webapp._JOBS == {}


def test_rebound_host_is_rejected(isolated_client) -> None:
    """Matching Origin and Host must not bypass the loopback Host check."""
    headers = {
        "Host": "rebound.example:8080",
        "Origin": "http://rebound.example:8080",
        "Sec-Fetch-Site": "same-origin",
    }

    response = isolated_client.post(
        "/api/analyze", json={"issue": "tinnitus"}, headers=headers
    )

    assert response.status_code == 400
    assert webapp._JOBS == {}


@pytest.mark.parametrize("host", ["localhost:8080", "127.0.0.1:8080"])
def test_loopback_hosts_are_accepted(isolated_client, host: str) -> None:
    """The Host allowlist still accepts normal loopback URLs."""
    response = isolated_client.get("/", headers={"Host": host})

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/analyze", {"data": {"issue": "tinnitus"}}),
        ("/api/analyze", {"json": {"issue": "tinnitus"}}),
    ],
)
def test_same_origin_submissions_are_accepted(
    isolated_client, monkeypatch: pytest.MonkeyPatch, path: str, payload: dict
) -> None:
    """Valid same-origin form/API submissions must pass the Host guard."""
    # Keep the accepted request from starting a real analysis.
    sem = Mock()
    sem.acquire.return_value = True
    monkeypatch.setattr(webapp, "_SEM", sem)
    monkeypatch.setattr(webapp.threading, "Thread", Mock())
    headers = {
        "Origin": "http://localhost",
        "Sec-Fetch-Site": "same-origin",
    }

    response = isolated_client.post(path, headers=headers, **payload)

    assert response.status_code == 200
    assert response.get_json()["run_id"]


def test_issue_length_is_capped(isolated_client) -> None:
    response = isolated_client.post(
        "/api/analyze", json={"issue": "x" * (webapp._MAX_ISSUE_LENGTH + 1)}
    )

    assert response.status_code == 400
    assert "at most 2000 characters" in response.get_json()["error"]
    assert webapp._JOBS == {}
