"""Agent integration tests for the advisory citator."""

import json

from va_legal_agent.agent import analyze_cases_for_claim
from va_legal_agent.models import (
    AppellateHistoryCheck,
    CitatorCheck,
)


def _stub_search(monkeypatch):
    results = [
        {
            "title": "Smith v. Wilkie",
            "url": "https://uscourts.cavc.gov/smith",
            "snippet": "service connection evidence nexus",
            "citation": "30 Vet.App. 123",
            "courtlistener_opinion_id": "111",
        }
    ]

    monkeypatch.setattr(
        "va_legal_agent.agent.search_all",
        lambda *args, **kwargs: [dict(result) for result in results],
    )
    monkeypatch.setattr(
        "va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {}
    )
    monkeypatch.setattr(
        "va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: _SyncExecutor(*a, **k)
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")


class _SyncExecutor:
    def __init__(self, *args, **kwargs):
        pass

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        future = Future()
        future.set_result(fn(*args, **kwargs))
        return future

    def shutdown(self, *args, **kwargs):
        pass


def test_citator_enabled_without_api_key_reports_skip(monkeypatch):
    _stub_search(monkeypatch)
    monkeypatch.setenv("CITATOR", "1")
    monkeypatch.setattr("va_legal_agent.agent.check_analysis_citations", lambda *a: [])

    analysis = analyze_cases_for_claim("service connection")

    assert analysis.citator_checks == []
    assert analysis.authority_queue == []
    assert analysis.citator_flags == [
        "CourtListener citator skipped: COURTLISTENER_API_KEY is not set."
    ]


def test_citator_enabled_with_api_key_invokes_runner_with_limits(monkeypatch):
    _stub_search(monkeypatch)
    monkeypatch.setenv("CITATOR", "1")
    monkeypatch.setenv("COURTLISTENER_API_KEY", "secret-token")
    monkeypatch.setenv("CITATOR_MAX_CASES", "1")
    monkeypatch.setenv("CITATOR_MAX_CITING_OPINIONS", "2")
    monkeypatch.setenv("CITATOR_MAX_PAGES", "3")
    monkeypatch.setattr("va_legal_agent.agent.check_analysis_citations", lambda *a: [])
    check = CitatorCheck(
        target_case="Smith v. Wilkie",
        status="unknown",
        flags=["Citator test flag."],
    )
    calls: list[tuple[object, str, int, int, int]] = []

    def fake_run(cases, provider, *, max_cases, max_citing_opinions, max_pages):
        calls.append((cases, type(provider).__name__, max_cases, max_citing_opinions, max_pages))
        return [check]

    monkeypatch.setattr("va_legal_agent.agent.run_citator", fake_run)

    analysis = analyze_cases_for_claim("service connection")

    assert analysis.citator_checks == [check]
    assert analysis.citator_flags == ["Citator test flag."]
    assert calls[0][0][0].title == "Smith v. Wilkie"
    assert calls[0][1] == "CourtListenerProvider"
    assert calls[0][2:] == (1, 2, 3)
    assert analysis.authority_queue[0].case_title == "Smith v. Wilkie"
    assert analysis.authority_queue[0].priority == 1


def test_citator_loads_verified_history_records_and_sets_their_source(
    monkeypatch, tmp_path
):
    _stub_search(monkeypatch)
    monkeypatch.setenv("CITATOR", "1")
    monkeypatch.setenv("COURTLISTENER_API_KEY", "secret-token")
    history_path = tmp_path / "history.json"
    history_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "target_cluster_id": "777",
                        "candidate_case": "Smith v. McDonough",
                        "candidate_cluster_id": "999",
                        "relationship": "direct_appeal",
                        "disposition": "affirmed",
                        "scope": "all",
                        "source_quality": "official",
                        "source_url": "https://www.cafc.uscourts.gov/opinions/1.pdf",
                        "reviewer": "Reviewer",
                        "reviewed_at": "2026-01-02T03:04:05+00:00",
                        "rationale": "Checked the docket.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPELLATE_HISTORY_FILE", str(history_path))
    monkeypatch.setattr("va_legal_agent.agent.check_analysis_citations", lambda *a: [])
    check = CitatorCheck(
        target_case="Smith v. Wilkie",
        status="unknown",
        appellate_history=AppellateHistoryCheck(target_case="Smith v. Wilkie"),
    )
    calls = []

    def fake_run(cases, provider, **kwargs):
        calls.append(kwargs)
        return [check]

    monkeypatch.setattr("va_legal_agent.agent.run_citator", fake_run)

    analysis = analyze_cases_for_claim("service connection")

    assert calls[0]["history_records"][0].candidate_case == "Smith v. McDonough"
    assert analysis.citator_checks[0].appellate_history.source == str(history_path)


def test_citator_reports_unreadable_history_records_without_aborting(monkeypatch):
    _stub_search(monkeypatch)
    monkeypatch.setenv("CITATOR", "1")
    monkeypatch.setenv("COURTLISTENER_API_KEY", "secret-token")
    monkeypatch.setenv("APPELLATE_HISTORY_FILE", "/missing/history.json")
    monkeypatch.setattr("va_legal_agent.agent.check_analysis_citations", lambda *a: [])
    check = CitatorCheck(target_case="Smith v. Wilkie", status="unknown")
    monkeypatch.setattr(
        "va_legal_agent.agent.run_citator",
        lambda cases, provider, **kwargs: [check],
    )

    analysis = analyze_cases_for_claim("service connection")

    assert any(
        "Appellate history verification records unavailable" in flag
        for flag in analysis.citator_flags
    )


def test_citator_disabled_leaves_fields_empty(monkeypatch):
    _stub_search(monkeypatch)
    monkeypatch.setattr("va_legal_agent.agent.check_analysis_citations", lambda *a: [])

    analysis = analyze_cases_for_claim("service connection")

    assert analysis.citator_checks == []
    assert analysis.citator_flags == []


def test_citator_passes_configured_snapshot_store(monkeypatch, tmp_path):
    _stub_search(monkeypatch)
    monkeypatch.setenv("CITATOR", "1")
    monkeypatch.setenv("COURTLISTENER_API_KEY", "secret-token")
    monkeypatch.setenv("SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr("va_legal_agent.agent.check_analysis_citations", lambda *a: [])
    check = CitatorCheck(target_case="Smith v. Wilkie", status="unknown")
    received = {}

    def fake_run(cases, provider, **kwargs):
        received.update(kwargs)
        return [check]

    monkeypatch.setattr("va_legal_agent.agent.run_citator", fake_run)

    analyze_cases_for_claim("service connection")

    assert received["snapshot_store"].root == tmp_path / "snapshots"
    assert (tmp_path / "snapshots").is_dir()


def test_citator_reports_unwritable_snapshot_directory_without_aborting(
    monkeypatch, tmp_path
):
    _stub_search(monkeypatch)
    monkeypatch.setenv("CITATOR", "1")
    monkeypatch.setenv("COURTLISTENER_API_KEY", "secret-token")
    blocker = tmp_path / "snapshot-file"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("SNAPSHOT_DIR", str(blocker))
    monkeypatch.setattr("va_legal_agent.agent.check_analysis_citations", lambda *a: [])
    check = CitatorCheck(target_case="Smith v. Wilkie", status="unknown")
    received = {}

    def fake_run(cases, provider, **kwargs):
        received.update(kwargs)
        return [check]

    monkeypatch.setattr("va_legal_agent.agent.run_citator", fake_run)

    analysis = analyze_cases_for_claim("service connection")

    assert "snapshot_store" not in received
    assert any(
        "Evidence snapshots unavailable" in flag
        for flag in analysis.citator_flags
    )
