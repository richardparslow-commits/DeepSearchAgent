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
import requests

from audit_invariants import assert_exception_chain_redacted

pytest.importorskip("flask")

# webapp.py loads persisted jobs at import time. Point it at a throwaway
# directory before importing so the test run cannot create .webapp/ in the repo.
os.environ["WEBAPP_STATE_DIR"] = tempfile.mkdtemp(prefix="webapp-test-")
os.environ.pop("WEBAPP_MAX_CONCURRENT", None)
os.environ.pop("PORT", None)

import webapp  # noqa: E402

from va_legal_agent.models import (  # noqa: E402
    AdjudicationRoleGuidance,
    AuthorityReliabilitySummary,
    CaseAbstract,
    ImpactProfile,
    LegalAnalysis,
    ProvisionImpact,
    QrsAuditWorkflow,
    RvsrAdjudicationWorkflow,
    SeniorRaterDroWorkflow,
    SurfacedCaseRecord,
)


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


def _prevent_job_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an accepted request from starting a real analysis."""
    sem = Mock()
    sem.acquire.return_value = True
    monkeypatch.setattr(webapp, "_SEM", sem)
    monkeypatch.setattr(webapp.threading, "Thread", Mock())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_results", 0),
        ("max_results", -1),
        ("max_results", 999999),
        ("max_results", True),
        ("max_results", 1.5),
        ("max_results", float("inf")),
        ("max_results", float("-inf")),
        ("max_results", float("nan")),
        ("max_results", "NaN"),
        ("max_results", "Infinity"),
        ("max_results", "-Infinity"),
        ("max_wall_seconds", 0),
        ("max_wall_seconds", -1),
        ("max_wall_seconds", 999999),
        ("max_wall_seconds", True),
        ("max_wall_seconds", float("inf")),
        ("max_wall_seconds", float("-inf")),
        ("max_wall_seconds", float("nan")),
        ("max_wall_seconds", "NaN"),
        ("max_wall_seconds", "Infinity"),
        ("max_wall_seconds", "-Infinity"),
    ],
)
def test_invalid_job_bounds_are_rejected_before_dispatch(
    isolated_client, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    """Non-finite, boolean, and out-of-range request bounds are HTTP 400s."""
    _prevent_job_start(monkeypatch)
    payload = {"issue": "tinnitus", field: value}

    response = isolated_client.post(
        "/api/analyze",
        data=json.dumps(payload, allow_nan=True),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert field in response.get_json()["error"]
    assert webapp._JOBS == {}
    webapp._SEM.acquire.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_results", 1),
        ("max_results", "50"),
        ("max_wall_seconds", 1),
        ("max_wall_seconds", "300"),
    ],
)
def test_valid_job_bounds_are_accepted(
    isolated_client, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    _prevent_job_start(monkeypatch)

    response = isolated_client.post(
        "/api/analyze", json={"issue": "tinnitus", field: value}
    )

    assert response.status_code == 200
    assert response.get_json()["run_id"]


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


def test_load_jobs_skips_non_object_state(isolated_client, tmp_path: Path) -> None:
    """A valid JSON value that is not a job object must not abort startup."""
    (tmp_path / "bad.json").write_text("[]", encoding="utf-8")

    webapp._load_jobs()

    assert webapp._JOBS == {}


def test_job_error_redacts_chained_api_key(
    isolated_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 URL key must not reach the persisted job or status response."""
    secret = "SUPERSECRET123"
    raised: list[BaseException] = []

    def fail(*args, **kwargs):
        try:
            raise requests.HTTPError(
                "401 Client Error: Unauthorized for url: "
                f"https://api.govinfo.gov/search?api_key={secret}"
            )
        except requests.HTTPError as cause:
            error = RuntimeError("analysis failed")
            error.__cause__ = cause
            raised.append(error)
            raise error

    monkeypatch.setattr(webapp, "analyze_cases_for_claim", fail)
    monkeypatch.setattr(webapp, "_save_job", lambda job: None)
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    monkeypatch.setattr(webapp, "_SEM", sem)
    monkeypatch.setattr(
        webapp,
        "_JOBS",
        {
            "aud4-001": {
                "job_id": "aud4-001",
                "issue": "tinnitus",
                "status": "running",
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        },
    )

    webapp._run_job("aud4-001", "tinnitus", 1, None)

    assert raised
    assert_exception_chain_redacted(raised[0], secret)
    assert secret not in webapp._JOBS["aud4-001"]["error"]
    response = isolated_client.get("/api/status/aud4-001")
    assert response.status_code == 200
    assert secret not in response.get_data(as_text=True)


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


def test_missing_origin_without_fetch_metadata_is_allowed(
    isolated_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-browser clients without Origin or Sec-Fetch-Site may POST."""
    _prevent_job_start(monkeypatch)

    response = isolated_client.post("/api/analyze", json={"issue": "tinnitus"})

    assert response.status_code == 200
    assert response.get_json()["run_id"]


def test_missing_origin_with_cross_site_fetch_metadata_is_rejected(
    isolated_client,
) -> None:
    """Missing Origin must not bypass a cross-site Sec-Fetch-Site signal."""
    response = isolated_client.post(
        "/api/analyze",
        json={"issue": "tinnitus"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

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
    _prevent_job_start(monkeypatch)
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


def test_webapp_job_persists_structured_d1_payload(
    isolated_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser job's JSON contract must retain D1, abstracts, and impacts."""
    analysis = LegalAnalysis(
        issue="PTSD rating",
        summary="- Smith v. Wilkie (Court of Appeals for Veterans Claims)",
        how_it_affects_va_claims="The Board must obtain an adequate examination.",
        top_cases=["Smith v. Wilkie (Court of Appeals for Veterans Claims)"],
        surfaced_cases=[
            SurfacedCaseRecord(
                case_title="Smith v. Wilkie",
                court="Court of Appeals for Veterans Claims",
                docket_number="21-1234",
                reporter_citation="33 Vet.App. 10",
                decision_date="2024-01-02",
                official_transcript_url="https://uscourts.cavc.gov/opinions/smith",
                transcript_source_type="official_court_opinion",
                provenance_verified=True,
                retrieved_url="https://uscourts.cavc.gov/opinions/smith",
                source_reliability="official",
                authority_tier="official_primary_binding",
                is_official_authority=True,
            )
        ],
        case_abstracts=[
            CaseAbstract(
                case_title="Smith v. Wilkie",
                court="Court of Appeals for Veterans Claims",
                claimant_and_conditions="Veteran claimed PTSD.",
                reasoning_summary="Full reasoning " + ("y" * 400),
                abstract_complete=True,
                abstract_source="full_text",
            )
        ],
        provision_impacts=[
            ProvisionImpact(
                citation="38 C.F.R. § 4.130",
                stage="rating_dc_evaluation",
                legal_effect="clarifying",
                provenance_anchor="The Board must apply 38 C.F.R. § 4.130.",
                m21_1_citations=["M21-1, Part V, Subpart ii"],
            )
        ],
        impact_profile=ImpactProfile(
            issue_tags=["rating"],
            outcome="remanded",
            statutes=["38 C.F.R. § 4.130"],
            authority_note="Binding on the Board.",
            provision_impacts=[
                ProvisionImpact(
                    citation="38 C.F.R. § 4.130",
                    stage="rating_dc_evaluation",
                    legal_effect="clarifying",
                    provenance_anchor="The Board must apply 38 C.F.R. § 4.130.",
                    m21_1_citations=["M21-1, Part V, Subpart ii"],
                )
            ],
            nuance="This ruling is relevant to rating in VA compensation claims.",
        ),
        impact_profiles=[
            ImpactProfile(
                issue_tags=["rating"],
                outcome="remanded",
                statutes=["38 C.F.R. § 4.130"],
                authority_note="Binding on the Board.",
                provision_impacts=[
                    ProvisionImpact(
                        citation="38 C.F.R. § 4.130",
                        stage="rating_dc_evaluation",
                        legal_effect="clarifying",
                        provenance_anchor="The Board must apply 38 C.F.R. § 4.130.",
                        m21_1_citations=["M21-1, Part V, Subpart ii"],
                    )
                ],
                nuance="This ruling is relevant to rating in VA compensation claims.",
            )
        ],
        adjudication_guidance=AdjudicationRoleGuidance(
            issue="PTSD rating",
            rvsr=RvsrAdjudicationWorkflow(
                evidence_review_checklist=["Review the claims file."],
                lay_evidence_competence=["Apply 38 U.S.C. § 5107(b)."],
                duty_to_assist_exam_actions=["Request an adequate opinion."],
                rating_codesheet_actions=["Apply Diagnostic Code 9411."],
                effective_date_actions=["Apply 38 C.F.R. § 3.400."],
                reasons_for_decision_requirements=["Address 38 U.S.C. § 5104(b)."],
            ),
            senior_rater_dro=SeniorRaterDroWorkflow(
                hlr_de_novo_actions=["Conduct de novo review under 38 C.F.R. § 3.2601."],
                dta_error_returns=["Return for the missing examination."],
                cue_analysis=["Apply 38 C.F.R. § 3.105(a) and George v. McDonough."],
                precedent_m21_conflict_resolution=["Binding precedent controls M21-1."],
                dro_disposition_actions=["Issue a new decision."],
            ),
            qrs=QrsAuditWorkflow(
                second_signature_checkpoints=["Verify the diagnostic code."],
                nqr_qrt_error_calls=["Call a duty-to-assist error."],
                sample_review_focus=["Sample rating and HLR decisions."],
            ),
        ),
        source_reliability_summary=AuthorityReliabilitySummary(
            official_count=1, official_primary_binding_count=1
        ),
    )
    monkeypatch.setattr(webapp, "analyze_cases_for_claim", lambda *a, **k: analysis)
    monkeypatch.setattr(webapp, "_save_job", lambda job: None)
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    monkeypatch.setattr(webapp, "_SEM", sem)
    monkeypatch.setattr(
        webapp,
        "_JOBS",
        {
            "aud4-domain": {
                "job_id": "aud4-domain",
                "issue": "PTSD rating",
                "status": "running",
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        },
    )

    webapp._run_job("aud4-domain", "PTSD rating", 1, None)

    job = webapp._JOBS["aud4-domain"]
    assert job["status"] == "done"
    assert "Surfaced case records (D1):" in job["result"]
    assert job["result_json"]["surfaced_cases"][0]["docket_number"] == "21-1234"
    assert job["result_json"]["case_abstracts"][0]["abstract_complete"] is True
    assert job["result_json"]["provision_impacts"][0]["m21_1_citations"]
    assert "Full impact profiles (beyond nuance):" in job["result"]
    assert "VA Rater (RVSR) Application:" in job["result"]
    assert "Senior VA Rater / DRO Application (HLR & CUE):" in job["result"]
    assert "Quality Review (QRS) Checkpoints:" in job["result"]
    assert "Official Court Transcript / Primary Authority" in job["result"]
    assert job["result_json"]["impact_profile"]["nuance"]
    assert job["result_json"]["adjudication_guidance"]["qrs"]["nqr_qrt_error_calls"]
    assert job["result_json"]["source_reliability_summary"]["official_count"] == 1
    assert "Official Court Transcript / Primary Authority" in webapp._INDEX_HTML
    assert "Secondary Summary / Non-Binding Source" in webapp._INDEX_HTML
