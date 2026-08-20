import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor

import pytest

from va_legal_agent.fetch import extract_holding_sentence, extract_statutes
from va_legal_agent.interpretation import InterpretiveAnalysis
from va_legal_agent.llm import ReasoningResult
from va_legal_agent.models import CaseRecord, Contradiction, PrincipleFinding
from va_legal_agent.planning import ResearchPlan, SubTask
from va_legal_agent.search import SearchError
from va_legal_agent.agent import (
    _DaemonThreadPoolExecutor,
    _observe,
    analyze_cases_for_claim,
    build_case_queries,
    detect_court_name,
    enrich_top_cases,
    fetch_cases_for_issue,
    normalize_case,
    research_issue,
    score_case_relevance,
    summarize_case_impact,
)
from va_legal_agent.topics import (
    COURT_BVA,
    COURT_CAVC,
    COURT_FEDERAL_CIRCUIT,
    COURT_SUPREME,
    COURT_UNKNOWN,
)


def test_build_queries_include_major_courts():
    queries = build_case_queries("service connection", "cancer")
    assert any("uscourts.cavc.gov" in q for q in queries)
    assert any("cafc.uscourts.gov" in q for q in queries)
    assert any("supremecourt.gov" in q for q in queries)
    assert any("bva.va.gov" in q for q in queries)


def test_build_queries_empty_issue_falls_back_to_generic():
    queries = build_case_queries("   ", "Compensation")
    assert '"VA compensation" "Compensation"' in queries[0]


def test_detect_court_name_handles_all_courts():
    assert (
        detect_court_name("https://www.supremecourt.gov/opinions/21-123") == COURT_SUPREME
    )
    assert detect_court_name("https://www.bva.va.gov/vetapp25/Files6/A1.txt") == COURT_BVA
    assert detect_court_name("https://example.com/other-source") == COURT_UNKNOWN
    assert detect_court_name("https://uscourts.cavc.gov/opinions/1") == COURT_CAVC
    assert detect_court_name("https://cafc.uscourts.gov/opinions/2") == COURT_FEDERAL_CIRCUIT


def test_summarize_case_impact_handles_service_connection_language():
    case = CaseRecord(
        title="Holton v. Shinseki",
        court="Court of Appeals for Veterans Claims",
        citation="2011 WL 123456",
        url="https://example.com/holton",
        snippet="Service connection requires competent evidence; benefit of the doubt applies in close cases.",
        decision_date="2011-01-01",
        issue="service connection",
        holding="The Board must provide reasons and bases and address evidence supporting a claim.",
        impact="This affects whether the VA must consider all evidence.",
    )

    summary = summarize_case_impact(case)
    assert "service connection" in summary.lower()
    assert "benefit of the doubt" in summary.lower() or "reasons and bases" in summary.lower()


def test_score_case_relevance_includes_legal_keywords():
    case = CaseRecord(
        title="Smith v. Wilkie",
        court="Court of Appeals for Veterans Claims",
        citation="2024 WL 1",
        url="https://example.com/smith",
        snippet="Service connection requires competent evidence; the Board must provide reasons and bases for its decision.",
        decision_date="2024-01-01",
        issue="service connection",
        holding="The Board failed to provide adequate reasons and bases.",
        impact="A claimant can win if the Board fails to explain its reasoning.",
    )

    score = score_case_relevance(case, "service connection")
    assert score >= 3
    assert "service connection" in summarize_case_impact(case).lower()


class _SyncExecutor:
    """ThreadPoolExecutor stand-in that runs each query inline and records
    submissions synchronously.

    The real pool runs ``search_all`` on worker threads, so assertions on
    ``seen``/``len(calls)`` race with the main thread's fake clock in the
    budget tests. Running the function inline at ``submit()`` time makes every
    budget test deterministic.
    """

    def __init__(self, *args, **kwargs):
        self.submitted: list[str] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append(args[0])
        fut = Future()
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - mirror executor semantics
            fut.set_exception(exc)
        else:
            fut.set_result(result)
        return fut

    def shutdown(self, wait=True, cancel_futures=False):
        pass


@pytest.fixture(autouse=True)
def _fanout_runs_inline(monkeypatch):
    """Run every pool fan-out on the inline executor unless a test opts out.

    A broken-pool mutant (corrupted worker loop, submit, or thread creation)
    makes submitted futures never resolve, so any test waiting on the real
    pool either blocks until its wall budget (slow) or forever (mutmut then
    reports a SIGXCPU "timeout" instead of a clean kill). Routing every
    fan-out through ``_SyncExecutor`` makes those mutants fail fast in every
    test; the dedicated daemon-pool tests still construct the real pool via
    their imported name, and ``test_fetch_cases_budget_times_out_in_flight_query``
    restores it to exercise in-flight abandonment semantics.
    """
    monkeypatch.setattr(
        "va_legal_agent.agent._DaemonThreadPoolExecutor",
        lambda *a, **k: _SyncExecutor(*a, **k),
    )


def _join_workers(pool, timeout=5.0):
    """Wait for a daemon pool's workers to drain their queue and exit."""
    for worker in pool._workers:
        worker.join(timeout=timeout)


# Wait used when a submitted fn or worker must signal before an assertion can
# proceed. A healthy pool starts a thread and runs a trivial fn in
# milliseconds, so one second is generous; keeping it short makes a mutant
# that turns the workers into no-ops fail these tests in ~1s each rather than
# 5s, so the whole mutant run stays under mutmut's wall-clock limit and is
# classified "killed" instead of a SIGXCPU "timeout".
_RESOLVE_TIMEOUT = 1.0


def test_daemon_pool_workers_are_daemon_and_bounded():
    pool = _DaemonThreadPoolExecutor(max_workers=3)
    try:
        assert len(pool._workers) == 3
        assert all(worker.daemon for worker in pool._workers)
    finally:
        pool.shutdown()
        _join_workers(pool)


def test_daemon_pool_runs_and_resolves_submitted_fn():
    pool = _DaemonThreadPoolExecutor(max_workers=2)
    try:
        future = pool.submit(lambda x, y: x + y, 2, 3)
        assert future.result(timeout=_RESOLVE_TIMEOUT) == 5
    finally:
        pool.shutdown()
        _join_workers(pool)
        assert pool._futures == set()  # completed futures are discarded, not leaked


def test_daemon_pool_forwards_kwargs_to_fn():
    pool = _DaemonThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(lambda x=0, y=0: x + y, x=2, y=3)
        assert future.result(timeout=_RESOLVE_TIMEOUT) == 5
    finally:
        pool.shutdown()
        _join_workers(pool)


def test_daemon_pool_propagates_worker_exception():
    pool = _DaemonThreadPoolExecutor(max_workers=1)
    try:
        def boom():
            raise ValueError("kaboom")

        future = pool.submit(boom)
        with pytest.raises(ValueError, match="kaboom"):
            future.result(timeout=_RESOLVE_TIMEOUT)
    finally:
        pool.shutdown()
        _join_workers(pool)


def test_daemon_pool_cancel_futures_skips_queued_work():
    started = threading.Event()
    release = threading.Event()
    pool = _DaemonThreadPoolExecutor(max_workers=1)
    try:
        def blocking():
            started.set()
            release.wait(timeout=5)
            return "first"

        first = pool.submit(blocking)
        assert started.wait(timeout=_RESOLVE_TIMEOUT)  # first task is running on the lone worker
        second = pool.submit(lambda: "second")  # queued behind it

        pool.shutdown(cancel_futures=True)
        release.set()  # let the worker drain; the queued task must be skipped

        assert first.result(timeout=5) == "first"
        assert second.cancelled()
        _join_workers(pool)
    finally:
        release.set()
        pool.shutdown()
        _join_workers(pool)


def test_daemon_pool_shutdown_without_cancel_runs_queued_work():
    started = threading.Event()
    release = threading.Event()
    pool = _DaemonThreadPoolExecutor(max_workers=1)
    try:
        def blocking():
            started.set()
            release.wait(timeout=5)
            return "first"

        first = pool.submit(blocking)
        assert started.wait(timeout=_RESOLVE_TIMEOUT)
        second = pool.submit(lambda: "second")

        # Default shutdown: cancel_futures defaults to False, so queued work
        # still runs before the sentinels are reached.
        pool.shutdown()
        release.set()

        assert first.result(timeout=5) == "first"
        assert second.result(timeout=5) == "second"
        _join_workers(pool)
    finally:
        release.set()
        pool.shutdown()
        _join_workers(pool)


def _stub_search(monkeypatch, results=None, error=None):
    """Stub search_all and detail fetching, disable the LLM, and record queries."""
    calls: list[str] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        calls.append(query)
        if error is not None:
            raise error
        return [dict(r) for r in (results or [])]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})
    # Run the fan-out inline: these tests assert on ordering/dedup/enrichment,
    # not thread scheduling, and a synchronous executor keeps them deterministic
    # and lets a broken-pool mutant fail fast instead of blocking forever.
    monkeypatch.setattr(
        "va_legal_agent.agent._DaemonThreadPoolExecutor",
        lambda *a, **k: _SyncExecutor(*a, **k),
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")  # keep tests fast and hermetic vs .env
    return calls


def _stub_search_recording(monkeypatch, results=None, error=None):
    """Like _stub_search but records (query, max_results, telemetry) tuples."""
    calls: list[tuple[str, int, object]] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        calls.append((query, max_results, telemetry))
        if error is not None:
            raise error
        return [dict(r) for r in (results or [])]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})
    monkeypatch.setattr(
        "va_legal_agent.agent._DaemonThreadPoolExecutor",
        lambda *a, **k: _SyncExecutor(*a, **k),
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    return calls


def test_fetch_cases_orders_by_authority_then_relevance(monkeypatch):
    results = [
        {"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection veterans evidence"},
        {"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection veterans evidence"},
        {"title": "Case B", "url": "https://cafc.uscourts.gov/b", "snippet": "unrelated procedural history"},
        {"title": "Case C", "url": "https://uscourts.cavc.gov/c", "snippet": "rating evaluation schedule"},
    ]
    calls = _stub_search(monkeypatch, results=results)

    cases = fetch_cases_for_issue("service connection", max_results=3)

    # Every planned query ran: the 8 broad court recalls plus the
    # statute-anchored searches derived from the detected element.
    assert len(calls) == len(build_case_queries("service connection", "Compensation"))
    # Federal Circuit outranks CAVC regardless of relevance; within a tier relevance breaks ties.
    assert [case.title for case in cases] == ["Case B", "Case A", "Case C"]
    assert [case.authority_rank for case in cases] == [1, 2, 3]
    assert cases[0].authority_weight == 3
    assert cases[1].authority_weight == 2
    assert cases[1].relevance_score > cases[2].relevance_score
    # The ranking layer assigns an explainable composite score consistent with the order.
    assert cases[0].composite_score > cases[1].composite_score > cases[2].composite_score
    assert "authority tier 3" in cases[0].ranking_explanation


def test_fetch_cases_carries_provider_structured_fields(monkeypatch):
    results = [
        {
            "title": "Smith v. McDonough",
            "url": "https://www.courtlistener.com/opinion/12345/",
            "snippet": "",
            "court": "Court of Appeals for Veterans Claims",
            "citation": "35 Vet.App. 123",
            "decision_date": "2023-05-01",
            "docket": "19-4433",
            "judge": "Judge Mary J. Smith",
        }
    ]
    _stub_search(monkeypatch, results=results)

    cases = fetch_cases_for_issue("service connection", max_results=5)

    assert cases[0].court == "Court of Appeals for Veterans Claims"
    assert cases[0].citation == "35 Vet.App. 123"
    assert cases[0].decision_date == "2023-05-01"
    assert cases[0].docket == "19-4433"
    assert cases[0].judge == "Judge Mary J. Smith"
    assert cases[0].authority_weight == 2  # CAVC tier


def test_fetch_cases_enriches_top_case_details(monkeypatch):
    results = [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}]
    _stub_search(monkeypatch, results=results)
    monkeypatch.setattr(
        "va_legal_agent.agent.fetch_case_details",
        lambda url, timeout=None: {
            "citation": "23 Vet.App. 1",
            "decision_date": "2011-03-15",
            "holding": "The Board must provide adequate reasons and bases.",
            "docket": "09-1234",
            "judge": "Mary J. Smith",
            "statutes": ["38 U.S.C. § 7104(d)(1)"],
            "outcome": "vacated and remanded",
        },
    )

    cases = fetch_cases_for_issue("service connection", max_results=5)

    assert cases[0].citation == "23 Vet.App. 1"
    assert cases[0].decision_date == "2011-03-15"
    assert cases[0].holding == "The Board must provide adequate reasons and bases."
    assert cases[0].docket == "09-1234"
    assert cases[0].judge == "Mary J. Smith"
    assert cases[0].statutes == ["38 U.S.C. § 7104(d)(1)"]
    assert cases[0].outcome == "vacated and remanded"
    assert cases[0].impact  # impact summary assigned after enrichment


def test_fetch_cases_skips_enrichment_when_disabled(monkeypatch):
    results = [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}]
    _stub_search(monkeypatch, results=results)

    def exploding_fetch(url, timeout=None):
        raise AssertionError("fetch_case_details should not be called when enrich=False")

    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", exploding_fetch)

    cases = fetch_cases_for_issue("service connection", max_results=5, enrich=False)

    assert cases[0].citation == ""


def test_deep_read_triggered_when_enabled(monkeypatch):
    """DEEP_READ=1 routes the top cases through deep_read_cases in place."""
    results = [
        {"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"},
        {"title": "Case B", "url": "https://uscourts.cavc.gov/b", "snippet": "rating evidence"},
        {"title": "Case C", "url": "https://uscourts.cavc.gov/c", "snippet": "nexus opinion"},
    ]
    _stub_search(monkeypatch, results=results)
    monkeypatch.setenv("DEEP_READ", "1")
    seen: dict[str, object] = {}

    def recording_deep_read(cases, issue, limit=None):
        seen["cases"] = list(cases)
        seen["issue"] = issue
        seen["limit"] = limit
        for case in cases:
            case.deep_summary = f"Deep summary of {case.title}."

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", recording_deep_read)

    cases = fetch_cases_for_issue("service connection", max_results=5)

    assert seen["issue"] == "service connection"
    assert seen["limit"] == min(3, len(cases))  # DEEP_READ_LIMIT default, capped by case count
    assert [c.title for c in seen["cases"]] == [c.title for c in cases]
    assert all(c.deep_summary for c in cases)


def test_deep_read_skipped_when_disabled(monkeypatch):
    """Deep-read is off by default: deep_read_cases is never invoked."""
    results = [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}]
    _stub_search(monkeypatch, results=results)
    called: list[object] = []

    def should_not_run(cases, issue, limit=None):
        called.append((cases, issue, limit))
        raise AssertionError("deep_read_cases must not run when DEEP_READ is off")

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", should_not_run)

    fetch_cases_for_issue("service connection", max_results=5)

    assert called == []


def test_deep_read_limit_respected(monkeypatch):
    """DEEP_READ_LIMIT caps how many top cases are deep-read."""
    results = [
        {"title": f"Case {i}", "url": f"https://uscourts.cavc.gov/{i}", "snippet": "service connection"}
        for i in range(5)
    ]
    _stub_search(monkeypatch, results=results)
    monkeypatch.setenv("DEEP_READ", "1")
    monkeypatch.setenv("DEEP_READ_LIMIT", "2")
    seen: dict[str, object] = {}

    def recording_deep_read(cases, issue, limit=None):
        seen["limit"] = limit

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", recording_deep_read)

    fetch_cases_for_issue("service connection", max_results=5)

    assert seen["limit"] == 2


def test_deep_read_selection_applies_court_floor(monkeypatch):
    """Deep-read mirrors the ranking floor so the lowest tier (BVA) is ingested."""
    results = [
        {"title": "FC 1", "url": "https://cafc.uscourts.gov/1", "snippet": "service connection rating", "court": COURT_FEDERAL_CIRCUIT},
        {"title": "FC 2", "url": "https://cafc.uscourts.gov/2", "snippet": "service connection rating", "court": COURT_FEDERAL_CIRCUIT},
        {"title": "CAVC 1", "url": "https://uscourts.cavc.gov/1", "snippet": "service connection rating", "court": COURT_CAVC},
        {"title": "CAVC 2", "url": "https://uscourts.cavc.gov/2", "snippet": "service connection rating", "court": COURT_CAVC},
        {"title": "BVA 1", "url": "https://www.va.gov/vetapp25/1.txt", "snippet": "service connection rating", "court": COURT_BVA},
        {"title": "BVA 2", "url": "https://www.va.gov/vetapp25/2.txt", "snippet": "service connection rating", "court": COURT_BVA},
    ]
    _stub_search(monkeypatch, results=results)
    monkeypatch.setenv("DEEP_READ", "1")
    seen: list[list[str]] = []

    def recording_deep_read(cases, issue, limit=None):
        seen.append([c.title for c in cases])

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", recording_deep_read)

    fetch_cases_for_issue("service connection", max_results=6, deep_read_limit=6)

    # The floor reserves two slots per authority tier, so both BVA decisions
    # are deep-read alongside the binding Federal Circuit/CAVC cases.
    assert sorted(seen[0]) == ["BVA 1", "BVA 2", "CAVC 1", "CAVC 2", "FC 1", "FC 2"]


def test_research_issue_usage_guard_aborts_before_search(monkeypatch):
    """The CLI/batch entry pre-flights the budget before the first round."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    searched: list[str] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        searched.append(query)
        return []

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr(
        "va_legal_agent.agent.check_courtlistener_daily_budget",
        lambda min_remaining: (_ for _ in ()).throw(
            SearchError(
                "CourtListener daily request budget too low: 0 remaining, need 8 "
                "for this run (used 125/125). Daily window resets at "
                "2026-08-18T05:12:28+00:00."
            )
        ),
    )
    _stub_search(monkeypatch, results=[])

    with pytest.raises(SearchError, match="resets at 2026-08-18T05:12:28"):
        research_issue("tinnitus")

    assert searched == []  # round 1 never started


def test_research_issue_usage_guard_rechecks_gap_rounds(monkeypatch):
    """With headroom, the loop proceeds and re-checks the budget per gap round."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    demands: list[int] = []

    def recording_guard(min_remaining):
        demands.append(min_remaining)
        return {"used": 0, "limit": 125, "remaining": 125, "reset_at": None}

    monkeypatch.setattr(
        "va_legal_agent.agent.check_courtlistener_daily_budget", recording_guard
    )
    states = iter([({"rating"}, []), (set(), [])])  # one uncovered gap, then done
    monkeypatch.setattr(
        "va_legal_agent.agent._observe",
        lambda issue, cases, telemetry: next(states),
    )
    calls = _stub_search(
        monkeypatch,
        results=[{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection rating"}],
    )

    telemetry: list[dict[str, object]] = []
    research_issue("service connection and rating", max_wall_seconds=1.0, telemetry=telemetry)

    # Round 1 and the gap round were each pre-flighted with a real query list
    # (a None argument would crash the estimate, killing the mutant).
    assert len(demands) == 2
    assert demands[0] > 0 and demands[1] > 0
    # The gap round must search only the refined gap query, never re-running a
    # round-1 query (kills the ready-filter `or`/`in` mutants deterministically).
    round1 = build_case_queries("service connection and rating", "Compensation")
    assert calls == round1 + ['"rating" "service connection and rating" veterans law']
    # Each passing check records a quota snapshot for the run output.
    assert [record.get("courtlistener_quota") for record in telemetry] == [
        {"used": 0, "limit": 125, "remaining": 125, "reset_at": None},
        {"used": 0, "limit": 125, "remaining": 125, "reset_at": None},
    ]


def test_research_issue_deep_read_receives_issue(monkeypatch):
    """The loop forwards the claim issue to the deep-read pass (None mutant)."""
    results = [
        {"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection rating"}
    ]
    _stub_search(monkeypatch, results=results)
    monkeypatch.setenv("DEEP_READ", "1")
    issues: list[str] = []

    def recording_deep_read(cases, issue, limit=None):
        issues.append(issue)

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", recording_deep_read)

    research_issue("service connection and rating")

    assert issues == ["service connection and rating"]


def test_deep_read_flag_overrides_env(monkeypatch):
    """An explicit deep_read arg beats the DEEP_READ env setting (both ways)."""
    results = [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}]
    _stub_search(monkeypatch, results=results)
    calls: list[bool] = []

    def recording_deep_read(cases, issue, limit=None):
        calls.append(True)

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", recording_deep_read)

    # env off, arg on -> deep-read runs
    monkeypatch.setenv("DEEP_READ", "0")
    fetch_cases_for_issue("service connection", max_results=5, deep_read=True)
    assert calls == [True]

    # env on, arg off -> deep-read is skipped
    calls.clear()
    monkeypatch.setenv("DEEP_READ", "1")

    def should_not_run(cases, issue, limit=None):
        calls.append(True)
        raise AssertionError("deep_read=False must override DEEP_READ=1")

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", should_not_run)
    fetch_cases_for_issue("service connection", max_results=5, deep_read=False)
    assert calls == []


def test_deep_read_flag_none_falls_back_to_env(monkeypatch):
    """deep_read=None reads the DEEP_READ setting as before."""
    results = [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}]
    _stub_search(monkeypatch, results=results)
    monkeypatch.setenv("DEEP_READ", "1")
    calls: list[bool] = []

    def recording_deep_read(cases, issue, limit=None):
        calls.append(True)

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", recording_deep_read)

    fetch_cases_for_issue("service connection", max_results=5)  # deep_read defaults to None

    assert calls == [True]


def test_deep_read_limit_flag_forwards_to_deep_read_cases(monkeypatch):
    """deep_read_limit arg caps how many cases are deep-read (overrides env)."""
    results = [
        {"title": f"Case {i}", "url": f"https://uscourts.cavc.gov/{i}", "snippet": "service connection"}
        for i in range(5)
    ]
    _stub_search(monkeypatch, results=results)
    monkeypatch.setenv("DEEP_READ", "1")
    monkeypatch.setenv("DEEP_READ_LIMIT", "3")  # env says 3; the explicit arg must win
    seen: dict[str, object] = {}

    def recording_deep_read(cases, issue, limit=None):
        seen["limit"] = limit

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", recording_deep_read)

    fetch_cases_for_issue("service connection", max_results=5, deep_read_limit=2)

    assert seen["limit"] == 2


def test_deep_read_limit_flag_none_falls_back_to_env(monkeypatch):
    """deep_read_limit=None reads the DEEP_READ_LIMIT setting as before."""
    results = [
        {"title": f"Case {i}", "url": f"https://uscourts.cavc.gov/{i}", "snippet": "service connection"}
        for i in range(5)
    ]
    _stub_search(monkeypatch, results=results)
    monkeypatch.setenv("DEEP_READ", "1")
    monkeypatch.setenv("DEEP_READ_LIMIT", "2")
    seen: dict[str, object] = {}

    def recording_deep_read(cases, issue, limit=None):
        seen["limit"] = limit

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", recording_deep_read)

    fetch_cases_for_issue("service connection", max_results=5)  # deep_read_limit defaults to None

    assert seen["limit"] == 2

def test_research_issue_deep_read_limit_forwards(monkeypatch):
    """research_issue forwards an explicit deep_read_limit to the deep-read pass."""
    results = [
        {"title": f"Case {i}", "url": f"https://uscourts.cavc.gov/{i}", "snippet": "service connection rating"}
        for i in range(5)
    ]
    _stub_search(monkeypatch, results=results)
    monkeypatch.setenv("DEEP_READ", "1")
    monkeypatch.setenv("DEEP_READ_LIMIT", "3")  # env says 3; the explicit 1 must win
    seen: dict[str, object] = {}

    def recording_deep_read(cases, issue, limit=None):
        seen["limit"] = limit

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", recording_deep_read)

    research_issue("service connection and rating", deep_read_limit=1)

    assert seen["limit"] == 1


def test_research_issue_deep_read_flag_overrides_env(monkeypatch):
    """research_issue forwards an explicit deep_read arg to the deep-read pass."""
    results = [
        {"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection rating"}
    ]
    _stub_search(monkeypatch, results=results)
    monkeypatch.setenv("DEEP_READ", "0")  # env off; the explicit arg must win
    calls: list[bool] = []

    def recording_deep_read(cases, issue, limit=None):
        calls.append(True)

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", recording_deep_read)

    research_issue("service connection and rating", deep_read=True)

    assert calls == [True]


def test_analyze_cases_deep_summaries_in_output(monkeypatch):
    """Deep-read summaries ride along with the top cases in the analysis."""
    results = [
        {"title": "Smith v. Wilkie", "url": "https://uscourts.cavc.gov/smith", "snippet": "service connection evidence nexus"}
    ]
    _stub_search(monkeypatch, results=results)

    def recording_deep_read(cases, issue, limit=None):
        for case in cases:
            case.deep_summary = f"Deep summary of {case.title}."

    monkeypatch.setattr("va_legal_agent.agent.deep_read_cases", recording_deep_read)

    analysis = analyze_cases_for_claim("service connection", deep_read=True)

    assert analysis.deep_summaries == [
        {"case": "Smith v. Wilkie (Court of Appeals for Veterans Claims)", "summary": "Deep summary of Smith v. Wilkie."}
    ]


def test_analyze_cases_deep_summaries_empty_when_off(monkeypatch):
    """Without deep-read, every summary is empty but the entries still align."""
    results = [
        {"title": "Smith v. Wilkie", "url": "https://uscourts.cavc.gov/smith", "snippet": "service connection evidence nexus"}
    ]
    _stub_search(monkeypatch, results=results)

    analysis = analyze_cases_for_claim("service connection")

    assert analysis.deep_summaries == [
        {"case": "Smith v. Wilkie (Court of Appeals for Veterans Claims)", "summary": ""}
    ]


def test_fetch_cases_staggers_query_submissions(monkeypatch):
    sleeps: list[float] = []
    _stub_search(monkeypatch, results=[])
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0.25")  # after _stub_search, which pins delay to 0
    monkeypatch.setattr("va_legal_agent.agent.time.sleep", sleeps.append)

    fetch_cases_for_issue("tinnitus")

    assert len(sleeps) == 7  # no delay before the first query
    assert all(delay == 0.25 for delay in sleeps)


def test_fetch_cases_raises_search_error_when_all_queries_fail(monkeypatch):
    _stub_search(monkeypatch, error=SearchError("blocked by provider"))

    with pytest.raises(SearchError, match="searches failed"):
        fetch_cases_for_issue("tinnitus")


def test_analyze_cases_for_claim_returns_structured_analysis(monkeypatch):
    results = [
        {"title": "Smith v. Wilkie", "url": "https://uscourts.cavc.gov/smith", "snippet": "service connection evidence nexus"},
    ]
    _stub_search(monkeypatch, results=results)

    analysis = analyze_cases_for_claim("service connection")

    assert analysis.issue == "service connection"
    assert analysis.summary
    assert analysis.how_it_affects_va_claims
    assert any("Smith v. Wilkie" in case for case in analysis.top_cases)
    # Interpretive analysis layer: principles derived from the cases, not static text.
    assert analysis.likely_applicable_principles
    assert any("nexus" in principle.lower() for principle in analysis.likely_applicable_principles)
    assert [element.name for element in analysis.detected_elements] == ["service connection"]
    assert analysis.coverage_score == 1.0
    assert analysis.interpretation_source == "template"  # OPENAI_API_KEY removed by stub
    assert analysis.strengths


def test_analyze_cases_flows_llm_reasoning_contradictions(monkeypatch):
    results = [
        {
            "title": "Smith v. Wilkie",
            "url": "https://uscourts.cavc.gov/smith",
            "snippet": "service connection evidence nexus",
        }
    ]
    _stub_search(monkeypatch, results=results)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    reasoning = ReasoningResult(
        reconciled_principles=["Service connection requires a nexus (Smith v. Wilkie)."],
        contradictions=[
            Contradiction(
                statement="The two decisions split on the nexus standard.",
                case_a="Smith v. Wilkie",
                case_b="Jones v. McDonough",
            )
        ],
        synthesis="Smith controls; Jones is distinguishable.",
    )
    monkeypatch.setattr(
        "va_legal_agent.interpretation.reason_cases",
        lambda issue, claim_type, cases: reasoning,
    )

    analysis = analyze_cases_for_claim("service connection")

    assert analysis.interpretation_source == "llm"
    assert analysis.how_it_affects_va_claims == "Smith controls; Jones is distinguishable."
    assert analysis.likely_applicable_principles == [
        "Service connection requires a nexus (Smith v. Wilkie)."
    ]
    assert len(analysis.contradictions) == 1
    assert analysis.contradictions[0].statement == "The two decisions split on the nexus standard."
    assert analysis.contradictions[0].case_a == "Smith v. Wilkie"
    assert analysis.contradictions[0].case_b == "Jones v. McDonough"


def test_statute_extraction_handles_va_citation_variants():
    assert "38 C.F.R. § 4.1" in extract_statutes("See 38 C.F.R. § 4.1 (2023).")
    assert "38 C.F.R. § 4.71a" in extract_statutes("See 38 C.F.R. § 4.71a (DC 5260).")
    assert "38 C.F.R. § 3.1" in extract_statutes("See 38 C.F.R. 3.1.")
    assert "38 U.S.C.A. § 1151" in extract_statutes("See 38 U.S.C.A. § 1151 (West).")


def test_holding_extraction_handles_statute_citations_and_varying_phrasing():
    holding = extract_holding_sentence("We hold that 38 U.S.C. § 1151 does not bar the claim.")
    assert "38 U.S.C. § 1151" in holding
    assert "does not bar the claim" in holding.lower()

    holding2 = extract_holding_sentence("The Court holds that the Board erred under 38 U.S.C. § 7104(d)(1).")
    assert "Board erred" in holding2
    assert "38 U.S.C. § 7104(d)(1)" in holding2


def test_analyze_cases_for_claim_raises_when_no_cases(monkeypatch):
    calls = _stub_search(monkeypatch, results=[])

    with pytest.raises(ValueError, match="No cases found"):
        analyze_cases_for_claim("obscure issue", max_wall_seconds=1.0)

    # The gap loop must never re-run a round-1 query. A ready-filter mutant
    # (`and` -> `or` or `not in` -> `in`) re-searches done tasks until the wall
    # deadline, which would otherwise hang this test into a mutmut timeout on
    # runners whose set-hash test order puts it before the query-pinning tests.
    # The no-repeat invariant fails on that mutant's very first gap query.
    assert len(calls) == len(set(calls))


def test_enrich_top_cases_limit_reads_env(monkeypatch):
    monkeypatch.setenv("ENRICH_CASE_LIMIT", "1")
    fetched: list[str] = []
    monkeypatch.setattr(
        "va_legal_agent.agent.fetch_case_details",
        lambda url, timeout=None: fetched.append(url) or {},
    )
    cases = [
        CaseRecord(
            title=f"Case {i}",
            court="Court of Appeals for Veterans Claims",
            url=f"https://example.com/{i}",
        )
        for i in range(3)
    ]

    enrich_top_cases(cases)

    assert fetched == ["https://example.com/0"]


def test_enrich_skips_cases_without_url(monkeypatch):
    case = CaseRecord(title="No URL", court="Court of Appeals for Veterans Claims")
    calls = {"n": 0}

    def fake_fetch(url):
        calls["n"] += 1
        return {}

    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", fake_fetch)

    enrich_top_cases([case], limit=1)

    assert calls["n"] == 0
    assert case.citation == ""


def test_enrich_falls_back_when_fetch_raises(monkeypatch, caplog):
    case = CaseRecord(
        title="Fetch fails",
        court="Court of Appeals for Veterans Claims",
        url="https://example.com/fails",
    )

    def fake_fetch(url):
        raise RuntimeError("connection reset")

    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", fake_fetch)

    result = enrich_top_cases([case], limit=1)

    assert result == [case]
    assert case.holding == ""  # best-effort enrichment: unchanged on failure
    assert any("Could not fetch details" in r.message for r in caplog.records)


def test_enrich_uses_courtlistener_api_when_opinion_id_present(monkeypatch):
    case = CaseRecord(
        title="Ito v. Copper River",
        court="Court of Appeals for Veterans Claims",
        url="https://www.courtlistener.com/opinion/9497426/yvonne-ito-v-copper-river/",
        courtlistener_opinion_id="9497426",
    )
    api_ids: list[int] = []
    scraped: list[str] = []

    class FakeProvider:
        def fetch_opinion_text(self, opinion_id):
            api_ids.append(opinion_id)
            return (
                "The Court holds that service connection requires a nexus "
                "under 38 U.S.C. § 5107."
            )

    monkeypatch.setattr("va_legal_agent.agent.CourtListenerProvider", lambda: FakeProvider())
    monkeypatch.setattr(
        "va_legal_agent.agent.fetch_case_details",
        lambda url, timeout=None: scraped.append(url) or {},
    )

    enrich_top_cases([case], limit=1)

    assert api_ids == [9497426]
    assert scraped == []  # the WAF-challenged frontend is never scraped
    assert case.holding  # holding extracted from API full text
    assert case.statutes == ["38 U.S.C. § 5107"]


def test_enrich_courtlistener_api_empty_text_degrades_gracefully(monkeypatch):
    case = CaseRecord(
        title="Empty opinion",
        court="Court of Appeals for Veterans Claims",
        url="https://www.courtlistener.com/opinion/1/empty/",
        courtlistener_opinion_id="1",
    )
    scraped: list[str] = []

    class FakeProvider:
        def fetch_opinion_text(self, opinion_id):
            return ""

    monkeypatch.setattr("va_legal_agent.agent.CourtListenerProvider", lambda: FakeProvider())
    monkeypatch.setattr(
        "va_legal_agent.agent.fetch_case_details",
        lambda url, timeout=None: scraped.append(url) or {},
    )

    enrich_top_cases([case], limit=1)

    assert case.holding == ""  # empty API text -> no details, no crash
    assert scraped == []  # still never scrapes the frontend


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
def test_build_queries_uses_issue_and_strips_quotes():
    queries = build_case_queries('he"aring loss', "cancer")
    assert '"hearing loss" "cancer"' in queries[0]
    assert '"hearing loss"' in queries[1]  # normalized issue, no embedded quotes
    assert '"' * 3 not in queries[0]  # quote stripped, not doubled


def test_build_queries_nonempty_issue_not_replaced():
    queries = build_case_queries("hearing loss", "cancer")
    assert '"hearing loss" "cancer"' in queries[0]
    assert '"VA compensation"' not in queries[0]


def test_detect_court_name_handles_caafc_domain():
    assert detect_court_name("https://caafc.uscourts.gov/opinions/3") == COURT_FEDERAL_CIRCUIT


def test_score_case_relevance_benefit_of_doubt_alone():
    case = CaseRecord(
        title="Close Case",
        court="",
        snippet="The benefit of the doubt applies in close cases.",
    )
    assert score_case_relevance(case, "tinnitus") == 2


def test_score_case_relevance_reasons_and_bases_alone():
    case = CaseRecord(
        title="Reasons Case",
        court="",
        snippet="The Board must provide reasons and bases for its decision.",
    )
    assert score_case_relevance(case, "tinnitus") == 2


def test_score_case_relevance_issue_match_exact():
    """An issue that appears only via the case's own issue field scores exactly 2."""
    case = CaseRecord(title="T", court="", snippet="", holding="", impact="", issue="tinnitus")
    assert score_case_relevance(case, "tinnitus") == 2


def test_score_case_relevance_topic_synonym_exact():
    """A topic synonym match adds exactly 2 on top of the issue-match 2."""
    case = CaseRecord(
        title="T",
        court="",
        snippet="The injury was aggravated by military service.",
        issue="aggravation",
    )
    assert score_case_relevance(case, "aggravation") == 4


def test_score_case_relevance_veterans_court_and_service_connection_exact():
    """Veterans-text (+1), service connection (+2), and a Veterans court (+1) total exactly 4."""
    case = CaseRecord(
        title="T",
        court="United States Court of Appeals for Veterans Claims",
        snippet="Veterans service connection for hearing loss is supported.",
    )
    assert score_case_relevance(case, "tinnitus") == 4


def test_score_case_relevance_federal_circuit_court_only():
    """A non-Veterans court must not add the court point."""
    case = CaseRecord(
        title="T",
        court="United States Court of Appeals for the Federal Circuit",
        snippet="The claim is denied.",
    )
    assert score_case_relevance(case, "tinnitus") == 0


def test_normalize_case_applies_all_fallbacks():
    case = normalize_case({}, COURT_CAVC, "tinnitus")
    assert case.title == "Unknown case"
    assert case.url == ""
    assert case.snippet == ""
    assert case.citation == ""
    assert case.decision_date == ""
    assert case.docket == ""
    assert case.judge == ""
    assert case.courtlistener_opinion_id == ""
    assert case.holding == ""
    assert case.impact == ""
    assert case.authority_rank == 0
    assert case.relevance_score == 0
    assert case.issue == "tinnitus"
    assert case.authority_weight == 2  # CAVC tier


def test_normalize_case_carries_courtlistener_opinion_id():
    case = normalize_case(
        {"title": "X", "url": "https://example.com", "courtlistener_opinion_id": "5286139"},
        COURT_CAVC,
        "tinnitus",
    )
    assert case.courtlistener_opinion_id == "5286139"


def test_fetch_cases_defaults_claim_type_and_max_results(monkeypatch):
    calls = _stub_search_recording(monkeypatch, results=[])

    fetch_cases_for_issue("tinnitus")

    # Execution order is nondeterministic (worker threads), so compare as a
    # multiset: every query uses the default claim type and max_results.
    queries = build_case_queries("tinnitus", "Compensation")
    assert len(calls) == len(queries)
    assert sorted(c[0] for c in calls) == sorted(queries)
    assert all(c[1] == 10 for c in calls)


def test_fetch_cases_usage_guard_aborts_when_budget_exhausted(monkeypatch, caplog):
    """A failed pre-flight abort prevents any search from starting."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    searched: list[str] = []
    original_error = (
        "CourtListener daily request budget too low: 0 remaining, need 8 "
        "for this run (used 125/125). Daily window resets at "
        "2026-08-18T05:12:28+00:00."
    )

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        searched.append(query)
        return []

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr(
        "va_legal_agent.agent.check_courtlistener_daily_budget",
        lambda min_remaining: (_ for _ in ()).throw(SearchError(original_error)),
    )

    with pytest.raises(SearchError, match="resets at 2026-08-18T05:12:28"):
        fetch_cases_for_issue("tinnitus")

    assert searched == []  # the guard fired before the first query
    # The abort warning is logged verbatim (not None'd, wrapped, or lowered).
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "CourtListener usage guard aborted run: " + original_error == r.message
        for r in warnings
    )


def test_fetch_cases_usage_guard_floor_when_variants_disabled(monkeypatch, caplog):
    """Variants=0 and the default 1 page still cost queries x 1 x 1."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "1")
    demands: list[int] = []

    def recording_guard(min_remaining):
        demands.append(min_remaining)
        return {"used": 0, "limit": 125, "remaining": 125, "reset_at": None}

    monkeypatch.setattr(
        "va_legal_agent.agent.check_courtlistener_daily_budget", recording_guard
    )
    calls = _stub_search_recording(monkeypatch, results=[])

    fetch_cases_for_issue("tinnitus")

    expected = len(build_case_queries("tinnitus", "Compensation"))
    assert demands == [expected]  # max(variants, 1) and max(pages, 1) floors apply
    assert len(calls) == expected


def test_fetch_cases_usage_guard_passes_settings_value_to_resolver(monkeypatch):
    """The resolver is given the raw SEARCH_PROVIDERS string, not None."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    seen: list[object] = []

    def recording_resolver(raw):
        seen.append(raw)
        return ["courtlistener"]

    monkeypatch.setattr(
        "va_legal_agent.agent.resolve_search_providers", recording_resolver
    )
    monkeypatch.setattr(
        "va_legal_agent.agent.check_courtlistener_daily_budget",
        lambda min_remaining: {"used": 0, "limit": 125, "remaining": 125, "reset_at": None},
    )
    _stub_search_recording(monkeypatch, results=[])

    fetch_cases_for_issue("tinnitus")

    assert seen == ["courtlistener"]


def test_fetch_cases_usage_guard_estimates_run_cost(monkeypatch, caplog):
    """The guard asks for queries x variants x pages of daily headroom."""
    caplog.set_level("INFO")
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    # Provider override beats the global variant count, including a global 0.
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS_BY_PROVIDER", "courtlistener=4")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "1")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY_BY_PROVIDER", "courtlistener=3")
    demands: list[int] = []

    def recording_guard(min_remaining):
        demands.append(min_remaining)
        return {"used": 0, "limit": 125, "remaining": 125, "reset_at": None}

    monkeypatch.setattr(
        "va_legal_agent.agent.check_courtlistener_daily_budget", recording_guard
    )
    calls = _stub_search_recording(monkeypatch, results=[])

    fetch_cases_for_issue("tinnitus")

    expected = len(build_case_queries("tinnitus", "Compensation")) * 4 * 3
    assert demands == [expected]
    # The run proceeded normally after the guard passed.
    assert len(calls) == expected // (4 * 3)
    # The success log carries the exact numbers (kills arg/message mutants).
    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert any(
        f"CourtListener daily budget OK for run (need {expected}, 125 remaining)."
        == r.message
        for r in infos
    )


def test_fetch_cases_usage_guard_records_quota_snapshot(monkeypatch):
    """A passing pre-flight records the live daily-window snapshot in telemetry."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    budget = {
        "used": 8, "limit": 125, "remaining": 117, "reset_at": "2026-08-18T05:12:28+00:00"
    }
    monkeypatch.setattr(
        "va_legal_agent.agent.check_courtlistener_daily_budget",
        lambda min_remaining: dict(budget),
    )
    _stub_search_recording(monkeypatch, results=[])

    telemetry: list[dict[str, object]] = []
    fetch_cases_for_issue("tinnitus", telemetry=telemetry)

    assert telemetry == [{"courtlistener_quota": budget}]


def test_fetch_cases_usage_guard_skipped_without_courtlistener(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo,bva")
    demands: list[int] = []

    def recording_guard(min_remaining):
        demands.append(min_remaining)
        return {"used": 0, "limit": 125, "remaining": 125, "reset_at": None}

    monkeypatch.setattr(
        "va_legal_agent.agent.check_courtlistener_daily_budget", recording_guard
    )
    calls = _stub_search_recording(monkeypatch, results=[])

    telemetry: list[dict[str, object]] = []
    fetch_cases_for_issue("tinnitus", telemetry=telemetry)

    assert demands == []  # no CourtListener -> no pre-flight check
    assert telemetry == []  # ...and therefore no quota snapshot either
    assert len(calls) == len(build_case_queries("tinnitus", "Compensation"))


def test_fetch_cases_usage_guard_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setenv("COURTLISTENER_USAGE_GUARD", "0")
    demands: list[int] = []

    def recording_guard(min_remaining):
        demands.append(min_remaining)
        return {"used": 0, "limit": 125, "remaining": 125, "reset_at": None}

    monkeypatch.setattr(
        "va_legal_agent.agent.check_courtlistener_daily_budget", recording_guard
    )
    calls = _stub_search_recording(monkeypatch, results=[])

    telemetry: list[dict[str, object]] = []
    fetch_cases_for_issue("tinnitus", telemetry=telemetry)

    assert demands == []  # COURTLISTENER_USAGE_GUARD=0 opts out
    assert telemetry == []  # ...so no quota snapshot is recorded either
    assert len(calls) == len(build_case_queries("tinnitus", "Compensation"))


def test_fetch_cases_budget_skips_stagger_when_none_remains(monkeypatch):
    """When the stagger would start at exactly the deadline, no sleep happens."""
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "4")
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0.5")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = {"n": 0}
    sleeps: list[float] = []

    def fake_monotonic():
        # deadline = first_call + 0.5 = 100.5; the clock crosses it right at
        # the stagger computation (call 4), leaving exactly 0 budget to sleep.
        state["n"] += 1
        return 100.0 if state["n"] <= 3 else 100.5

    monkeypatch.setattr("va_legal_agent.agent.time.monotonic", fake_monotonic)
    monkeypatch.setattr("va_legal_agent.agent.time.sleep", sleeps.append)
    executor = _SyncExecutor()
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: executor)
    seen: list[str] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        seen.append(query)
        return []

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    with pytest.raises(SearchError, match="wall-time budget"):
        fetch_cases_for_issue("tinnitus", max_wall_seconds=0.5)

    assert sleeps == []  # zero remaining budget -> the stagger sleep is skipped
    assert len(executor.submitted) == 1  # the deadline check aborts even exactly AT the deadline


def test_fetch_cases_budget_negative_disables(monkeypatch):
    """A non-positive budget value disables the deadline entirely."""
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "1")
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    deadlines: list[float | None] = []
    n = {"i": 0}

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        deadlines.append(deadline)
        n["i"] += 1
        return [
            {
                "title": f"Case {n['i']}",
                "url": f"https://uscourts.cavc.gov/{n['i']}",
                "snippet": "x",
            }
        ]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    cases = fetch_cases_for_issue("tinnitus", max_wall_seconds=-1)

    assert len(cases) == 8  # every query ran; no deadline was imposed
    assert deadlines == [None] * 8


def test_fetch_cases_budget_aborts_before_first_submission_at_deadline(monkeypatch):
    """The pre-submission check aborts even when the clock is exactly at the deadline."""
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "4")
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = {"n": 0}
    seen: list[str] = []

    def fake_monotonic():
        # deadline = first call + 0.5 = 100.5; the second query's pre-submission
        # check (call 3) runs exactly at the deadline.
        state["n"] += 1
        return 100.0 if state["n"] <= 2 else 100.5

    monkeypatch.setattr("va_legal_agent.agent.time.monotonic", fake_monotonic)
    monkeypatch.setattr("va_legal_agent.agent.time.sleep", lambda s: None)
    executor = _SyncExecutor()
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: executor)
    seen: list[str] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        seen.append(query)
        return []

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    with pytest.raises(SearchError, match="wall-time budget"):
        fetch_cases_for_issue("tinnitus", max_wall_seconds=0.5)

    assert len(executor.submitted) == 1  # >= deadline aborts before the second query is submitted


def test_fetch_cases_budget_expires_before_first_query(monkeypatch):
    """A budget already spent before the first query starts raises, not silently returns."""
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "4")
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = {"n": 0}

    def fake_monotonic():
        state["n"] += 1
        return 100.0 if state["n"] == 1 else 100.5

    monkeypatch.setattr("va_legal_agent.agent.time.monotonic", fake_monotonic)
    monkeypatch.setattr("va_legal_agent.agent.time.sleep", lambda s: None)
    executor = _SyncExecutor()
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: executor)
    monkeypatch.setattr(
        "va_legal_agent.agent.search_all",
        lambda query, max_results=10, telemetry=None, deadline=None: [],
    )
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    with pytest.raises(SearchError) as excinfo:
        fetch_cases_for_issue("tinnitus", max_wall_seconds=0.5)

    assert str(excinfo.value) == (
        "Search wall-time budget of 0.5s exhausted for issue 'tinnitus' "
        "before any results were returned."
    )
    # The deadline check fired on the very first query: nothing was submitted.
    assert executor.submitted == []


def test_fetch_cases_budget_zero_remaining_still_retrieves_completed_results(monkeypatch):
    """remaining == 0 counts as expired: a completed query is not retrieved past the deadline."""
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "4")
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = {"n": 0}

    def fake_monotonic():
        state["n"] += 1
        return 100.0 if state["n"] <= 2 else 101.0

    monkeypatch.setattr("va_legal_agent.agent.time.monotonic", fake_monotonic)
    monkeypatch.setattr("va_legal_agent.agent.time.sleep", lambda s: None)
    executor = _SyncExecutor()
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: executor)
    results = [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}]
    monkeypatch.setattr(
        "va_legal_agent.agent.search_all",
        lambda query, max_results=10, telemetry=None, deadline=None: [dict(r) for r in results],
    )
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    with pytest.raises(SearchError, match="wall-time budget"):
        fetch_cases_for_issue("tinnitus", max_wall_seconds=1.0)

    assert len(executor.submitted) == 1  # only the first query ran before the deadline


def test_fetch_cases_budget_reads_env_default(monkeypatch):
    """Without an explicit budget, SEARCH_MAX_WALL_SECONDS supplies the deadline."""
    monkeypatch.setenv("SEARCH_MAX_WALL_SECONDS", "5")
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "1")
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    deadlines: list[float | None] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        deadlines.append(deadline)
        return [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "x"}]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    fetch_cases_for_issue("tinnitus")

    assert deadlines and all(d is not None for d in deadlines)


def test_fetch_cases_budget_times_out_in_flight_query(monkeypatch):
    """A query still running at the deadline is abandoned, not waited on forever."""
    import threading

    monkeypatch.setenv("SEARCH_MAX_WORKERS", "1")
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Opt out of the autouse inline-executor fixture: in-flight abandonment
    # requires real worker threads (inline execution would run the slow query
    # on the main thread and block it instead of abandoning it at the deadline).
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", _DaemonThreadPoolExecutor)
    release = threading.Event()

    def slow_search_all(query, max_results=10, telemetry=None, deadline=None):
        release.wait(timeout=5)  # in-flight longer than the budget; never returns on its own
        return [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "x"}]

    monkeypatch.setattr("va_legal_agent.agent.search_all", slow_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    with pytest.raises(SearchError, match="wall-time budget"):
        fetch_cases_for_issue("tinnitus", max_wall_seconds=0.2)
    release.set()  # let the in-flight worker drain so the pool shuts down


def test_fetch_cases_passes_max_results_and_telemetry(monkeypatch):
    telemetry: list[dict[str, object]] = []
    calls = _stub_search_recording(monkeypatch, results=[])

    fetch_cases_for_issue("tinnitus", max_results=4, telemetry=telemetry)

    for query, max_results, passed_telemetry in calls:
        assert max_results == 4
        assert passed_telemetry is telemetry


def test_fetch_cases_continues_after_one_query_fails(monkeypatch, caplog):
    results = [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}]
    # Fail deterministically by query text (not by a shared counter, which
    # races with the worker pool's scheduling) so exactly one query fails.
    first_query = build_case_queries("service connection", "Compensation")[0]

    def flaky_search_all(query, max_results=10, telemetry=None, deadline=None):
        if query == first_query:
            raise SearchError("blocked by provider")
        return [dict(r) for r in results]

    monkeypatch.setattr("va_legal_agent.agent.search_all", flaky_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")

    cases = fetch_cases_for_issue("service connection", max_results=2)

    assert len(cases) == 1  # the failing first query did not abort later queries
    # The warning names the failing query and its error verbatim.
    assert any(
        r.getMessage() == f"Search failed for query {first_query!r}: blocked by provider"
        for r in caplog.records
    )


def test_fetch_cases_all_fail_reports_last_error(monkeypatch):
    queries = build_case_queries("tinnitus", "Compensation")
    attempted: list[str] = []

    def failing_search_all(query, max_results=10, telemetry=None, deadline=None):
        attempted.append(query)
        raise SearchError(f"provider error for query: {query}")

    monkeypatch.setattr("va_legal_agent.agent.search_all", failing_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")

    with pytest.raises(SearchError) as excinfo:
        fetch_cases_for_issue("tinnitus")

    # Exhaustion: every query is attempted exactly once - a failing query does
    # not abort the others. (The pool runs them concurrently, so compare as a
    # multiset rather than relying on execution order.)
    assert len(attempted) == len(queries)
    assert sorted(attempted) == sorted(queries)

    # The error that propagates is the LAST query's error (submission order, so
    # deterministic per-query rather than depending on thread scheduling) ...
    assert str(excinfo.value.__cause__) == f"provider error for query: {queries[-1]}"
    assert str(excinfo.value.__cause__) != f"provider error for query: {queries[0]}"

    # Per-query failure counting: all len(queries) failures are collected and
    # reported in the message.
    message = str(excinfo.value)
    expected = (
        f"All {len(queries)} searches failed for issue 'tinnitus'. "
        "The search provider may be blocking or rate-limiting automated requests. "
        f"Last error: provider error for query: {queries[-1]}"
    )
    assert message == expected


def test_research_issue_refines_uncovered_elements(monkeypatch, caplog):
    """An uncovered claim element triggers a targeted gap re-search round."""
    caplog.set_level(logging.INFO)
    calls: list[tuple[str, int, object]] = []
    telemetry: list[dict[str, object]] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        calls.append((query, max_results, telemetry))
        return [
            {"title": "Smith v. Wilkie", "url": "https://uscourts.cavc.gov/smith", "snippet": "service connection requires a nexus"}
        ]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})
    # Run the fan-out inline so the bounded refinement loop does not spawn a
    # fresh thread pool per iteration (which slows a re-search-loop mutant past
    # mutmut's wall-clock limit and mislabels the kill as a timeout).
    monkeypatch.setattr(
        "va_legal_agent.agent._DaemonThreadPoolExecutor",
        lambda *a, **k: _SyncExecutor(*a, **k),
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")

    cases = research_issue("service connection and rating", telemetry=telemetry, max_wall_seconds=1.0)

    # All 13 round-1/gap results collapse to the single deduped case.
    assert [c.title for c in cases] == ["Smith v. Wilkie"]
    gap_query = '"rating" "service connection and rating" veterans law'
    round1 = build_case_queries("service connection and rating", "Compensation")
    # Pin the exact query sequence, not just the count: a ready-filter mutant
    # (`or` re-searches done tasks, `in` searches only done tasks) emits a
    # different first gap query, so this kills both on the first gap query
    # even when the wall-clock deadline truncates the round on a slow runner.
    assert [c[0] for c in calls] == round1 + [gap_query]
    # The gap round reuses the same max_results and telemetry sink as round 1.
    gap_call = next(c for c in calls if c[0] == gap_query)
    assert gap_call[1] == 10
    assert gap_call[2] is telemetry
    # The observation and the post-refinement result are logged verbatim.
    assert any(
        r.getMessage() == "Research gap detected: uncovered=['rating'] search_flags=[]; refining queries."
        for r in caplog.records
    )
    assert any(
        r.getMessage() == "Research refinement complete: uncovered=['rating'] search_flags=[]."
        for r in caplog.records
    )
    # The loop ended because the ready filter emptied with uncovered remaining
    # and rounds below the cap (1 < 3), so the limit log must NOT fire -- a
    # mutant that widens the condition to `or` would log here.
    assert not any(
        r.getMessage().startswith("Refinement round limit of")
        for r in caplog.records
    )


def test_research_issue_refinement_rounds_are_bounded_without_deadline(monkeypatch, caplog):
    """A ready-filter that never empties cannot spin the gap loop forever.

    Regression for the ready-filter mutants (``and`` -> ``or``, ``not in`` ->
    ``in``): with ``SEARCH_MAX_WALL_SECONDS=0`` there is no wall-clock deadline,
    and a refine_plan that mints a fresh task every round would re-search done
    tasks forever. The deterministic round cap terminates the loop after
    ``SEARCH_MAX_REFINEMENT_ROUNDS`` rounds regardless of the deadline.
    """
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("SEARCH_MAX_WALL_SECONDS", "0")  # deadline disabled
    monkeypatch.setenv("SEARCH_MAX_REFINEMENT_ROUNDS", "1")
    calls = _stub_search(
        monkeypatch,
        results=[
            {"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}
        ],
    )

    # Pathological refine_plan: a fresh task every round, so the ready filter
    # never empties (mirrors what a broken ready-filter does to the loop).
    counter = {"n": 0}

    def spinning_refine(plan, uncovered=()):
        counter["n"] += 1
        task = SubTask(
            id=f"gap-{counter['n']}",
            kind="search",
            goal="gap",
            query='"rating" "tinnitus" veterans law',
        )
        return ResearchPlan(issue=plan.issue, claim_type=plan.claim_type, subtasks=(task,))

    monkeypatch.setattr("va_legal_agent.agent.refine_plan", spinning_refine)

    observations = {"n": 0}

    def counting_observe(issue, cases, telemetry):
        observations["n"] += 1
        if observations["n"] > 2:  # initial + exactly 1 capped gap round
            raise AssertionError("refinement loop spun past the round cap")
        return ({"rating"}, [])

    monkeypatch.setattr("va_legal_agent.agent._observe", counting_observe)

    cases = research_issue("tinnitus", max_wall_seconds=0.0)

    # The loop stops after the capped refinement round instead of spinning:
    # round-1 queries + exactly one gap query, and the limit is logged.
    assert len(cases) >= 1
    assert observations["n"] == 2
    assert len(calls) == len(build_case_queries("tinnitus", "Compensation")) + 1
    assert any(
        r.getMessage().startswith("Refinement round limit of 1 reached")
        for r in caplog.records
    )


def test_research_issue_refinement_rounds_run_exactly_to_the_cap(monkeypatch, caplog):
    """The loop runs exactly SEARCH_MAX_REFINEMENT_ROUNDS gap rounds, no more.

    Kills the round-arithmetic mutants (`rounds = 1`, `rounds -= 1`, `rounds +=
    2`): each changes how many gap rounds run before the cap stops the loop.
    The observation stub raises if it is ever called past the expected count
    (initial + capped rounds), so a runaway mutant fails fast instead of
    hanging the run into a mutmut timeout.
    """
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("SEARCH_MAX_WALL_SECONDS", "0")  # deadline disabled
    monkeypatch.setenv("SEARCH_MAX_REFINEMENT_ROUNDS", "2")
    calls = _stub_search(
        monkeypatch,
        results=[
            {"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}
        ],
    )

    counter = {"n": 0}

    def spinning_refine(plan, uncovered=()):
        counter["n"] += 1
        task = SubTask(
            id=f"gap-{counter['n']}",
            kind="search",
            goal="gap",
            query='"rating" "tinnitus" veterans law',
        )
        return ResearchPlan(issue=plan.issue, claim_type=plan.claim_type, subtasks=(task,))

    monkeypatch.setattr("va_legal_agent.agent.refine_plan", spinning_refine)

    observations = {"n": 0}

    def counting_observe(issue, cases, telemetry):
        observations["n"] += 1
        if observations["n"] > 3:  # initial + exactly 2 capped gap rounds
            raise AssertionError("refinement loop spun past the round cap")
        return ({"rating"}, [])

    monkeypatch.setattr("va_legal_agent.agent._observe", counting_observe)

    research_issue("tinnitus", max_wall_seconds=0.0)

    # Exactly two gap rounds ran (not one, not unbounded), and the limit log
    # fired after the second.
    assert observations["n"] == 3
    assert len(calls) == len(build_case_queries("tinnitus", "Compensation")) + 2
    assert any(
        r.getMessage().startswith("Refinement round limit of 2 reached")
        for r in caplog.records
    )


def test_research_issue_skips_refinement_when_coverage_complete(monkeypatch):
    queries_seen: list[str] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        queries_seen.append(query)
        return [
            {"title": "Smith v. Wilkie", "url": "https://uscourts.cavc.gov/smith", "snippet": "service connection rating evidence"}
        ]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")

    cases = research_issue("service connection and rating")

    assert [c.title for c in cases] == ["Smith v. Wilkie"]
    # Both elements are covered, so only the initial plan queries ran.
    assert len(queries_seen) == len(build_case_queries("service connection and rating", "Compensation"))
    assert not any(q.startswith('"rating" "service connection') for q in queries_seen)


def test_research_issue_citation_traversal_merges_new_opinions(monkeypatch):
    """When opted in, the loop follows citation trails and merges the new opinions."""
    monkeypatch.setenv("CITATION_TRAVERSAL", "1")
    monkeypatch.setenv("CITATION_TRAVERSE_LIMIT", "1")
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        return [
            {
                "title": "Smith v. Wilkie",
                "url": "https://uscourts.cavc.gov/smith",
                "snippet": "service connection rating evidence",
            }
        ]

    def fake_traverse(urls, max_results=10):
        assert urls == ["https://uscourts.cavc.gov/smith"]
        assert max_results == 5  # the loop forwards its own max_results, not a default
        return [
            {
                "title": "Jones v. McDonough",
                "url": "https://www.courtlistener.com/opinion/99/jones/",
                "court": "Court of Appeals for Veterans Claims",
                "snippet": "rating evidence",
            },
            {
                "title": "Wilson v. Wilkie",
                "url": "https://uscourts.cavc.gov/wilson",  # no court key -> derived from URL
                "snippet": "rating evidence",
            },
        ]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.traverse_citations", fake_traverse)

    cases = research_issue("service connection and rating", max_results=5)

    assert {c.title for c in cases} == {"Smith v. Wilkie", "Jones v. McDonough", "Wilson v. Wilkie"}
    jones = next(c for c in cases if c.title == "Jones v. McDonough")
    # The traversed opinion keeps its provider court label and is classified
    # as official (primary legal data) by the reliability layer.
    assert jones.court == COURT_CAVC
    assert jones.source_reliability == "official"
    # Without a provider court label, the court is derived from the URL.
    wilson = next(c for c in cases if c.title == "Wilson v. Wilkie")
    assert wilson.court == COURT_CAVC


def test_research_issue_raises_when_round_one_all_fails(monkeypatch):
    queries = build_case_queries("tinnitus", "Compensation")

    def failing_search_all(query, max_results=10, telemetry=None, deadline=None):
        raise SearchError(f"provider error for query: {query}")

    monkeypatch.setattr("va_legal_agent.agent.search_all", failing_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")

    with pytest.raises(SearchError) as excinfo:
        research_issue("tinnitus")

    # The propagating error names the issue verbatim (not just a generic match).
    assert str(excinfo.value) == (
        f"All {len(queries)} searches failed for issue 'tinnitus'. "
        "The search provider may be blocking or rate-limiting automated requests. "
        f"Last error: provider error for query: {queries[-1]}"
    )


def test_research_issue_budget_stops_refinement(monkeypatch, caplog):
    """When the budget expires after round 1, no refinement round starts."""
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    executor = _SyncExecutor()
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: executor)
    clock = {"now": 100.0}
    monkeypatch.setattr("va_legal_agent.agent.time.monotonic", lambda: clock["now"])
    queries_seen: list[str] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        queries_seen.append(query)
        return [
            {"title": "Smith v. Wilkie", "url": "https://uscourts.cavc.gov/smith", "snippet": "service connection requires a nexus"}
        ]

    # The real _observe (imported at the top) is the original function object,
    # so wrapping it expires the budget exactly at the observation boundary -
    # after round 1 finished, before the refinement while-check reads the clock.
    def expiring_observe(claim_issue, cases, telemetry):
        result = _observe(claim_issue, cases, telemetry)
        clock["now"] = 101.0  # exactly the deadline (100.0 + 1.0)
        return result

    monkeypatch.setattr("va_legal_agent.agent._observe", expiring_observe)
    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    cases = research_issue("service connection and rating", max_wall_seconds=1.0)

    # Round 1 completed fully, but the budget expired during observation, so
    # the uncovered "rating" element did not trigger a refinement round.
    assert [c.title for c in cases] == ["Smith v. Wilkie"]
    assert len(queries_seen) == len(build_case_queries("service connection and rating", "Compensation"))
    # A budget sitting exactly AT the deadline must not start a refinement
    # round: entering the loop would log "gap detected" and then abort the gap
    # fan-out at its own pre-submission check, emitting a misleading partial
    # budget warning for work that never ran.
    assert not any(r.getMessage().startswith("Research gap detected") for r in caplog.records)
    assert not any("returning partial results" in r.getMessage() for r in caplog.records)


def test_research_issue_defaults_claim_type_max_results_and_enrich(monkeypatch):
    """Every default on research_issue's signature flows through to the search."""
    queries_seen: list[str] = []
    max_results_seen: list[int] = []
    fetched: list[str] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        queries_seen.append(query)
        max_results_seen.append(max_results)
        return [
            {"title": "Smith v. Wilkie", "url": "https://uscourts.cavc.gov/smith", "snippet": "service connection requires a nexus"}
        ]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr(
        "va_legal_agent.agent.fetch_case_details",
        lambda url, timeout=None: fetched.append(url) or {},
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")

    research_issue("service connection")  # every argument uses its default

    assert any('"Compensation"' in q for q in queries_seen)  # default claim type
    assert max_results_seen and all(mr == 10 for mr in max_results_seen)  # default max_results
    assert fetched == ["https://uscourts.cavc.gov/smith"]  # default enrich=True


def test_research_issue_uses_env_wall_time_default_and_shared_deadline(monkeypatch):
    """SEARCH_MAX_WALL_SECONDS supplies one deadline shared by every round."""
    monkeypatch.setenv("SEARCH_MAX_WALL_SECONDS", "5")
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: _SyncExecutor())
    deadlines: list[float | None] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        deadlines.append(deadline)
        return [
            {"title": "Smith v. Wilkie", "url": "https://uscourts.cavc.gov/smith", "snippet": "service connection requires a nexus"}
        ]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    research_issue("service connection and rating")

    # Round 1 (12 queries) plus the gap round (1 query) all share one non-None
    # deadline derived from the env default.
    assert len(deadlines) == len(build_case_queries("service connection and rating", "Compensation")) + 1
    assert all(d is not None for d in deadlines)
    assert len(set(deadlines)) == 1


def test_research_issue_budget_expires_mid_round_returns_partial(monkeypatch, caplog):
    """A budget exhausted mid-round-1 returns what completed, with a warning."""
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: _SyncExecutor())
    values = iter([100.0, 100.0, 100.0, 200.0, 100.0, 200.0])
    monkeypatch.setattr("va_legal_agent.agent.time.monotonic", lambda: next(values))
    monkeypatch.setattr("va_legal_agent.agent.time.sleep", lambda s: None)
    # Bare "tinnitus" now detects the full element library, which would add a
    # gap round and consume an extra monotonic call; this test targets the
    # budget-expiry path, so pin observation to "no gaps".
    monkeypatch.setattr("va_legal_agent.agent._observe", lambda issue, cases, telemetry: ((), []))
    results = [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}]

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        return [dict(r) for r in results]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    cases = research_issue("tinnitus", max_wall_seconds=1.0)

    assert [c.title for c in cases] == ["Case A"]
    assert any(
        r.getMessage() == "Search wall-time budget of 1.0s exhausted for issue 'tinnitus'; returning partial results."
        for r in caplog.records
    )


def test_research_issue_budget_expires_before_results_raises(monkeypatch):
    """A budget already spent before round 1 raised nothing: the error names it."""
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: _SyncExecutor())
    state = {"n": 0}

    def fake_monotonic():
        state["n"] += 1
        return 100.0 if state["n"] == 1 else 100.5

    monkeypatch.setattr("va_legal_agent.agent.time.monotonic", fake_monotonic)
    monkeypatch.setattr("va_legal_agent.agent.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "va_legal_agent.agent.search_all",
        lambda query, max_results=10, telemetry=None, deadline=None: [],
    )
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    with pytest.raises(SearchError) as excinfo:
        research_issue("tinnitus", max_wall_seconds=0.5)

    assert str(excinfo.value) == (
        "Search wall-time budget of 0.5s exhausted for issue 'tinnitus' "
        "before any results were returned."
    )


def test_research_issue_continues_after_one_query_fails(monkeypatch):
    first_query = build_case_queries("service connection", "Compensation")[0]
    results = [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}]

    def flaky_search_all(query, max_results=10, telemetry=None, deadline=None):
        if query == first_query:
            raise SearchError("blocked by provider")
        return [dict(r) for r in results]

    monkeypatch.setattr("va_legal_agent.agent.search_all", flaky_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")

    cases = research_issue("service connection", max_results=2)

    # A partial failure does not abort the run: the successful queries' cases return.
    assert [c.title for c in cases] == ["Case A"]


def test_research_issue_logs_search_flags_during_refinement(monkeypatch, caplog):
    """The loop observes and logs low-recall flags alongside coverage gaps."""
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: _SyncExecutor())
    telemetry: list[dict[str, object]] = []
    queries_seen: list[str] = []
    recorded = {"done": False}
    flag = "Search provider duckduckgo had 1 failed query attempt(s); results may be incomplete."

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        queries_seen.append(query)
        if not recorded["done"]:
            recorded["done"] = True
            telemetry.append(
                {"provider": "duckduckgo", "queries_issued": 1, "results": 0, "deduped": 0, "failures": 1}
            )
        return [
            {"title": "Smith v. Wilkie", "url": "https://uscourts.cavc.gov/smith", "snippet": "service connection requires a nexus"}
        ]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    # The gap query still returns a case that never mentions "rating", so the
    # loop only stops because refine_plan's done-guard yields no new tasks. A
    # wall budget makes a mutant that breaks that guard (re-searching done
    # tasks) fail this test in ~1s instead of hanging forever - a hang mutmut
    # reports as a SIGXCPU "timeout" rather than a clean kill.
    research_issue("service connection and rating", telemetry=telemetry, max_wall_seconds=1.0)

    # Round 1 plus exactly one gap round: the done-guard forbids re-searching.
    assert len(queries_seen) == len(build_case_queries("service connection and rating", "Compensation")) + 1
    assert any(
        r.getMessage() == f"Research gap detected: uncovered=['rating'] search_flags=['{flag}']; refining queries."
        for r in caplog.records
    )
    assert any(
        r.getMessage() == f"Research refinement complete: uncovered=['rating'] search_flags=['{flag}']."
        for r in caplog.records
    )


def test_research_issue_gap_round_budget_warning(monkeypatch, caplog):
    """The gap round is bounded by the same budget and reports when it is spent."""
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: _SyncExecutor())
    clock = {"now": 100.0}
    monkeypatch.setattr("va_legal_agent.agent.time.monotonic", lambda: clock["now"])
    round_one = len(build_case_queries("service connection and rating", "Compensation"))
    calls = {"n": 0}

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        calls["n"] += 1
        if calls["n"] > round_one:
            # The gap round burns the budget. Advance on the first search after
            # round 1 rather than on the gap query itself: a mutant that
            # re-searches done tasks never issues the gap query, so keying on
            # it would leave the clock frozen and the refinement loop spinning
            # forever (mutmut reports that hang as a SIGXCPU "timeout").
            clock["now"] = 200.0
            return []
        return [
            {"title": "Smith v. Wilkie", "url": "https://uscourts.cavc.gov/smith", "snippet": "service connection requires a nexus"}
        ]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    cases = research_issue("service connection and rating", max_wall_seconds=1.0)

    assert [c.title for c in cases] == ["Smith v. Wilkie"]
    assert any(
        r.getMessage()
        == "Search wall-time budget of 1.0s exhausted for issue 'service connection and rating'; returning partial results."
        for r in caplog.records
    )


def test_observe_returns_uncovered_elements_and_flags():
    cases = [
        CaseRecord(
            title="Smith v. Wilkie",
            court="Court of Appeals for Veterans Claims",
            snippet="service connection requires a nexus",
        )
    ]
    telemetry = [
        {"provider": "duckduckgo", "queries_issued": 1, "results": 0, "deduped": 0, "failures": 1}
    ]

    uncovered, flags = _observe("service connection and rating", cases, telemetry)

    assert uncovered == ("rating",)
    assert flags == [
        "Search provider duckduckgo had 1 failed query attempt(s); results may be incomplete."
    ]


def test_observe_returns_empty_when_no_gaps_or_flags():
    cases = [
        CaseRecord(
            title="Smith v. Wilkie",
            court="Court of Appeals for Veterans Claims",
            snippet="service connection rating evidence",
        )
    ]

    uncovered, flags = _observe("service connection and rating", cases, None)

    assert uncovered == ()
    assert flags == []


def test_analyze_cases_for_claim_refines_before_analysis(monkeypatch):
    """analyze_cases_for_claim runs the adaptive loop, not a single pass."""
    queries_seen: list[str] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        queries_seen.append(query)
        return [
            {"title": "Smith v. Wilkie", "url": "https://uscourts.cavc.gov/smith", "snippet": "service connection requires a nexus"}
        ]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")

    # The stub never covers "rating", so the loop only stops because the
    # done-guard yields no new tasks. A wall budget makes a mutant that breaks
    # that guard (re-searching done tasks) fail in ~1s instead of hanging
    # forever with no deadline.
    analysis = analyze_cases_for_claim("service connection and rating", max_wall_seconds=1.0)

    # The gap query ran, but the stub never covers "rating", so coverage stays half.
    assert '"rating" "service connection and rating" veterans law' in queries_seen
    assert analysis.coverage_score == 0.5
    assert [e.name for e in analysis.detected_elements] == ["service connection", "rating"]


def test_fetch_cases_no_sleep_when_delay_zero(monkeypatch):
    sleeps: list[float] = []
    _stub_search(monkeypatch, results=[])  # SEARCH_DELAY_SECONDS=0
    monkeypatch.setattr("va_legal_agent.agent.time.sleep", sleeps.append)

    fetch_cases_for_issue("tinnitus")

    assert sleeps == []  # stagger only fires when the delay is positive


def test_fetch_cases_uses_configured_worker_count(monkeypatch):
    recorded: dict[str, object] = {}

    class RecordingExecutor:
        def __init__(self, *args, **kwargs):
            recorded["max_workers"] = kwargs.get("max_workers")
            self._inner = ThreadPoolExecutor(*args, **kwargs)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def submit(self, fn, *args, **kwargs):
            return self._inner.submit(fn, *args, **kwargs)

        def shutdown(self, wait=True, cancel_futures=False):
            self._inner.shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setenv("SEARCH_MAX_WORKERS", "3")
    _stub_search(monkeypatch, results=[])
    # Override the helper's synchronous pool with the recording executor so the
    # configured worker count is observed at construction time.
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", RecordingExecutor)

    fetch_cases_for_issue("tinnitus")

    assert recorded["max_workers"] == 3


def test_fetch_cases_budget_returns_partial_results(monkeypatch, caplog):
    """When the wall-time budget expires mid-run, already-found results are returned."""
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "1")  # run queries strictly in order
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Clock sequence (deterministic under the synchronous executor): deadline
    # computed at 100.0; both queries submitted while the clock is still 100.0;
    # the second query's pre-submission check sees 200.0 (budget burned); then
    # result retrieval reads 100.0 for q1 (in time) and 200.0 for q2 (expired).
    values = iter([100.0, 100.0, 100.0, 200.0, 100.0, 200.0])
    deadlines: list[float | None] = []
    monkeypatch.setattr("va_legal_agent.agent.time.monotonic", lambda: next(values))
    monkeypatch.setattr("va_legal_agent.agent.time.sleep", lambda s: None)
    results = [
        {"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}
    ]

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        deadlines.append(deadline)
        return [dict(r) for r in results]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})
    executor = _SyncExecutor()
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: executor)

    cases = fetch_cases_for_issue("tinnitus", max_wall_seconds=1.0)

    assert len(cases) == 1  # partial: the query that finished before the deadline
    assert cases[0].title == "Case A"
    # The deadline is threaded into search_all so in-flight work stops cooperatively.
    assert deadlines and all(d is not None for d in deadlines)
    assert any(
        r.message
        == "Search wall-time budget of 1.0s exhausted for issue 'tinnitus'; returning partial results."
        for r in caplog.records
    )


def test_fetch_cases_budget_exhausted_no_results_raises(monkeypatch):
    """Budget expiry before anything was found raises a clear SearchError.

    Also pins that the pool is torn down without waiting: pending futures are
    cancelled so the call returns at the deadline instead of draining them.
    """
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "1")
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    clock = {"now": 100.0}
    recorded: dict[str, object] = {}
    monkeypatch.setattr("va_legal_agent.agent.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "va_legal_agent.agent.time.sleep",
        lambda s: clock.__setitem__("now", clock["now"] + s),
    )

    class RecordingExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def submit(self, fn, *args, **kwargs):
            fut = Future()
            fut.set_result(fn(*args, **kwargs))
            return fut

        def shutdown(self, wait=True, cancel_futures=False):
            recorded["shutdown"] = (wait, cancel_futures)

    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", RecordingExecutor)

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        clock["now"] = 200.0  # burns the entire budget immediately
        return []

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    with pytest.raises(SearchError, match="wall-time budget"):
        fetch_cases_for_issue("tinnitus", max_wall_seconds=1.0)

    # Non-blocking teardown with cancellation: the call returns at the deadline
    # rather than waiting for pending queries to finish.
    assert recorded["shutdown"] == (False, True)


def test_fetch_cases_budget_clamps_stagger_and_stops_submitting(monkeypatch):
    """The stagger is clamped to the remaining budget, and nothing is submitted past the deadline."""
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "4")
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0.5")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    clock = {"now": 100.0}
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.agent.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "va_legal_agent.agent.time.sleep",
        lambda s: sleeps.append(s) or clock.__setitem__("now", clock["now"] + s),
    )
    executor = _SyncExecutor()
    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", lambda *a, **k: executor)
    seen: list[str] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        seen.append(query)
        return []

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})

    with pytest.raises(SearchError, match="wall-time budget"):
        fetch_cases_for_issue("tinnitus", max_wall_seconds=0.1)

    # The 0.1 s budget is smaller than the 0.5 s stagger: only the first query
    # is submitted, and the stagger is clamped to the 0.1 s that remained.
    assert len(executor.submitted) == 1
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.1)  # clamped, modulo binary float drift


def test_fetch_cases_enrich_limit_uses_min_with_max_results(monkeypatch):
    seen: dict[str, object] = {}

    def recording_enrich(cases, limit=None):
        seen["limit"] = limit
        return cases

    monkeypatch.setattr("va_legal_agent.agent.enrich_top_cases", recording_enrich)
    _stub_search(
        monkeypatch,
        results=[{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}],
    )

    fetch_cases_for_issue("tinnitus", max_results=1)

    assert seen["limit"] == 1  # min(ENRICH_CASE_LIMIT, max(max_results, 1))


def test_fetch_cases_enriches_highest_authority_first(monkeypatch):
    fetched: list[str] = []
    results = [
        {"title": "Low authority", "url": "https://example.com/low", "snippet": "unrelated"},
        {"title": "High authority", "url": "https://uscourts.cavc.gov/high", "snippet": "service connection"},
    ]
    _stub_search(monkeypatch, results=results)
    monkeypatch.setattr(
        "va_legal_agent.agent.fetch_case_details",
        lambda url, timeout=None: fetched.append(url) or {},
    )

    fetch_cases_for_issue("service connection", max_results=2)

    assert fetched and fetched[0] == "https://uscourts.cavc.gov/high"


def test_enrich_skips_no_url_but_continues(monkeypatch):
    fetched: list[str] = []
    cases = [
        CaseRecord(title="No URL", court="Court of Appeals for Veterans Claims"),
        CaseRecord(title="With URL", court="Court of Appeals for Veterans Claims", url="https://example.com/ok"),
    ]
    monkeypatch.setattr(
        "va_legal_agent.agent.fetch_case_details",
        lambda url, timeout=None: fetched.append(url) or {},
    )

    enrich_top_cases(cases, limit=2)

    assert fetched == ["https://example.com/ok"]  # the url-less case is skipped, not a stop


def test_enrich_continues_after_fetch_failure(monkeypatch, caplog):
    fetched: list[str] = []

    def flaky_fetch(url, timeout=None):
        if url == "https://example.com/fails":
            raise RuntimeError("boom")
        fetched.append(url)
        return {}

    cases = [
        CaseRecord(title="Fails", court="Court of Appeals for Veterans Claims", url="https://example.com/fails"),
        CaseRecord(title="Ok", court="Court of Appeals for Veterans Claims", url="https://example.com/ok"),
    ]
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", flaky_fetch)

    enrich_top_cases(cases, limit=2)

    assert fetched == ["https://example.com/ok"]
    assert any(
        r.getMessage() == "Could not fetch details for https://example.com/fails: boom"
        for r in caplog.records
    )


def test_analyze_cases_forwards_all_args_to_research_loop(monkeypatch):
    seen: dict[str, object] = {}

    def recording_research(
        issue, claim_type, max_results=10, enrich=True, telemetry=None,
        max_wall_seconds=None, deep_read=None, deep_read_limit=None,
    ):
        seen.update(
            issue=issue,
            claim_type=claim_type,
            max_results=max_results,
            enrich=enrich,
            telemetry=telemetry,
            max_wall_seconds=max_wall_seconds,
            deep_read=deep_read,
            deep_read_limit=deep_read_limit,
        )
        return [
            CaseRecord(
                title="Smith v. Wilkie",
                court="Court of Appeals for Veterans Claims",
                url="https://uscourts.cavc.gov/smith",
                snippet="service connection evidence nexus",
            )
        ]

    monkeypatch.setattr("va_legal_agent.agent.research_issue", recording_research)
    telemetry: list[dict[str, object]] = []

    analysis = analyze_cases_for_claim(
        "service connection",
        claim_type="Disability",
        max_results=3,
        telemetry=telemetry,
        max_wall_seconds=2.5,
        deep_read=True,
        deep_read_limit=4,
    )

    assert seen["issue"] == "service connection"
    assert seen["claim_type"] == "Disability"
    assert seen["max_results"] == 3
    assert seen["enrich"] is True
    assert seen["telemetry"] is telemetry
    assert seen["max_wall_seconds"] == 2.5
    assert seen["deep_read"] is True
    assert seen["deep_read_limit"] == 4
    assert analysis.top_cases == ["Smith v. Wilkie (Court of Appeals for Veterans Claims)"]


def test_analyze_cases_summary_uses_newlines_and_caps_at_five(monkeypatch):
    results = [
        {"title": f"Case {i}", "url": f"https://uscourts.cavc.gov/{i}", "snippet": "service connection"}
        for i in range(6)
    ]
    _stub_search(monkeypatch, results=results)

    analysis = analyze_cases_for_claim("service connection", max_results=6)

    assert "XX\nXX" not in analysis.summary
    assert len(analysis.summary.splitlines()) == 5
    assert len(analysis.top_cases) == 5


def test_analyze_cases_passes_claim_type_to_interpretation(monkeypatch):
    seen: dict[str, object] = {}

    def recording_interpretive(issue, claim_type, cases):
        seen["claim_type"] = claim_type
        return InterpretiveAnalysis(
            how_it_affects_va_claims="how",
            likely_applicable_principles=["principle"],
            next_steps=["step"],
            strengths=["strength"],
            gaps=["gap"],
            principle_findings=[PrincipleFinding(principle="p", source_cases=["c"])],
            coverage_score=0.8,
            interpretation_source="llm",
        )

    monkeypatch.setattr("va_legal_agent.agent.build_interpretive_analysis", recording_interpretive)
    _stub_search(
        monkeypatch,
        results=[{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}],
    )

    analysis = analyze_cases_for_claim("service connection", claim_type="Disability")

    assert seen["claim_type"] == "Disability"
    assert analysis.next_steps == ["step"]
    assert analysis.gaps == ["gap"]
    assert analysis.principle_findings == [PrincipleFinding(principle="p", source_cases=["c"])]
    assert analysis.coverage_score == 0.8
    # A non-default source value proves the field flows through the analysis
    # result rather than silently relying on the model default ("template").
    assert analysis.interpretation_source == "llm"
    assert analysis.search_flags == []


def test_analyze_cases_default_claim_type_and_max_results(monkeypatch):
    calls = _stub_search_recording(
        monkeypatch,
        results=[{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}],
    )
    # Bare "tinnitus" now detects the full element library and would trigger
    # gap re-searches; this test only verifies the default claim_type and
    # max_results flow through, so pin observation to "no gaps".
    monkeypatch.setattr("va_legal_agent.agent._observe", lambda issue, cases, telemetry: ((), []))

    analyze_cases_for_claim("tinnitus")  # both args use their defaults

    assert len(calls) == len(build_case_queries("tinnitus", "Compensation"))
    assert '"Compensation"' in calls[0][0]
    assert calls[0][1] == 10  # max_results default flows through to search


def test_analyze_cases_honors_enrich_false(monkeypatch):
    calls: list[str] = []
    queries: list[str] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        queries.append(query)
        return [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}]

    def forbid_fetch(url, timeout=None):
        calls.append(url)
        raise AssertionError("enrichment must not run when enrich=False")

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", forbid_fetch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")

    analyze_cases_for_claim("tinnitus", enrich=False, max_wall_seconds=1.0)

    assert calls == []  # the enrich=False flag reached fetch_cases_for_issue
    # Bound the refinement loop AND pin the no-repeat invariant so the
    # ready-filter mutants fail fast here instead of timing out on Linux.
    assert len(queries) == len(set(queries))


def test_analyze_cases_carries_search_telemetry_and_flags(monkeypatch):
    queries: list[str] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        queries.append(query)
        if telemetry is not None:
            telemetry.append(
                {"provider": "duckduckgo", "queries_issued": 1, "results": 0, "failures": 1, "deduped": 0}
            )
        return [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")

    telemetry: list[dict[str, object]] = []
    analysis = analyze_cases_for_claim("tinnitus", telemetry=telemetry, max_wall_seconds=1.0)

    # A ready-filter mutant re-searches round-1 queries, which would hang this
    # test into a mutmut timeout on Linux; the deadline bounds the loop and the
    # no-repeat invariant kills the mutant on its first re-run query.
    assert len(queries) == len(set(queries))

    assert analysis.search_telemetry  # rolled from the pipeline telemetry
    assert analysis.search_telemetry["duckduckgo"]["failures"] > 0
    assert analysis.search_flags  # the failure surfaces as a low-recall flag


def test_analyze_cases_surfaces_last_courtlistener_quota(monkeypatch):
    """The output carries the freshest usage-guard snapshot of the daily window."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    budgets = iter(
        [
            {"used": 8, "limit": 125, "remaining": 117, "reset_at": "2026-08-18T05:12:28+00:00"},
            {"used": 16, "limit": 125, "remaining": 109, "reset_at": "2026-08-18T05:12:28+00:00"},
        ]
    )
    monkeypatch.setattr(
        "va_legal_agent.agent.check_courtlistener_daily_budget",
        lambda min_remaining: next(budgets),
    )
    states = iter([({"rating"}, []), (set(), [])])  # force exactly one gap round
    monkeypatch.setattr(
        "va_legal_agent.agent._observe",
        lambda issue, cases, telemetry: next(states),
    )

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        if telemetry is not None:
            telemetry.append(
                {
                    "provider": "courtlistener",
                    "queries_issued": 1,
                    "results": 1,
                    "deduped": 0,
                    "failures": 0,
                }
            )
        return [
            {"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection rating"}
        ]

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", lambda url, timeout=None: {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")

    analysis = analyze_cases_for_claim("service connection and rating", max_wall_seconds=1.0)

    # Two pre-flight checks ran (round 1 + gap round); the last snapshot wins,
    # and the provider telemetry records in between do not disturb it.
    assert analysis.courtlistener_quota == {
        "used": 16,
        "limit": 125,
        "remaining": 109,
        "reset_at": "2026-08-18T05:12:28+00:00",
    }
    assert analysis.search_telemetry["courtlistener"]["queries_issued"] > 0


def test_analyze_cases_quota_none_without_courtlistener(monkeypatch):
    """No quota snapshot when CourtListener is not a configured provider."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    _stub_search(
        monkeypatch,
        results=[
            {"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}
        ],
    )

    analysis = analyze_cases_for_claim("service connection")

    assert analysis.courtlistener_quota is None


def test_agent_main_guard_prints_analysis(monkeypatch, capsys):
    """Re-execute the module as __main__ with the network layer stubbed."""
    import runpy
    import warnings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")  # no inter-query sleeps
    # runpy re-executes the module, which re-creates the real pool class and
    # bypasses the autouse inline-executor fixture. A wall budget makes a
    # broken-pool mutant terminate (~1s, budget exhausted -> SearchError)
    # instead of hanging on futures that never resolve.
    monkeypatch.setenv("SEARCH_MAX_WALL_SECONDS", "1")

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        return [
            {
                "title": "Smith v. McDonough",
                "url": "https://example.com/smith",
                "snippet": "Service connection requires competent evidence.",
                "court": "Court of Appeals for Veterans Claims",
            }
        ]

    monkeypatch.setattr("va_legal_agent.providers.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.fetch.fetch_case_details", lambda url: {})

    # runpy warns when a module is re-run while already imported (it is: this
    # file imports it at the top). That warning is benign here — we deliberately
    # re-execute the module under its __main__ guard — so suppress it rather
    # than letting PYTHONWARNINGS=error promote it.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*found in sys.modules after import of package.*",
            category=RuntimeWarning,
        )
        runpy.run_module("va_legal_agent.agent", run_name="__main__")

    out = capsys.readouterr().out
    assert "Smith v. McDonough" in out
