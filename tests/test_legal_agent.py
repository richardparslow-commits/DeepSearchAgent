import threading
from concurrent.futures import Future, ThreadPoolExecutor

import pytest

from va_legal_agent.fetch import extract_holding_sentence, extract_statutes
from va_legal_agent.interpretation import InterpretiveAnalysis
from va_legal_agent.models import CaseRecord, PrincipleFinding
from va_legal_agent.search import SearchError
from va_legal_agent.agent import (
    _DaemonThreadPoolExecutor,
    analyze_cases_for_claim,
    build_case_queries,
    detect_court_name,
    enrich_top_cases,
    fetch_cases_for_issue,
    normalize_case,
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
        fut.set_result(fn(*args, **kwargs))
        return fut

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def _join_workers(pool, timeout=5.0):
    """Wait for a daemon pool's workers to drain their queue and exit."""
    for worker in pool._workers:
        worker.join(timeout=timeout)


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
        assert future.result(timeout=5) == 5
    finally:
        pool.shutdown()
        _join_workers(pool)
        assert pool._futures == set()  # completed futures are discarded, not leaked


def test_daemon_pool_forwards_kwargs_to_fn():
    pool = _DaemonThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(lambda x=0, y=0: x + y, x=2, y=3)
        assert future.result(timeout=5) == 5
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
            future.result(timeout=5)
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
        assert started.wait(timeout=5)  # first task is running on the lone worker
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
        assert started.wait(timeout=5)
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
    _stub_search(monkeypatch, results=[])

    with pytest.raises(ValueError, match="No cases found"):
        analyze_cases_for_claim("obscure issue")


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
    assert case.holding == ""
    assert case.impact == ""
    assert case.authority_rank == 0
    assert case.relevance_score == 0
    assert case.issue == "tinnitus"
    assert case.authority_weight == 2  # CAVC tier


def test_fetch_cases_defaults_claim_type_and_max_results(monkeypatch):
    calls = _stub_search_recording(monkeypatch, results=[])

    fetch_cases_for_issue("tinnitus")

    # Execution order is nondeterministic (worker threads), so compare as a
    # multiset: every query uses the default claim type and max_results.
    queries = build_case_queries("tinnitus", "Compensation")
    assert len(calls) == len(queries)
    assert sorted(c[0] for c in calls) == sorted(queries)
    assert all(c[1] == 10 for c in calls)


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

    with pytest.raises(SearchError, match="before any results"):
        fetch_cases_for_issue("tinnitus", max_wall_seconds=0.5)

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

    monkeypatch.setattr("va_legal_agent.agent._DaemonThreadPoolExecutor", RecordingExecutor)
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "3")
    _stub_search(monkeypatch, results=[])

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


def test_analyze_cases_forwards_all_args_to_fetch(monkeypatch):
    seen: dict[str, object] = {}

    def recording_fetch(issue, claim_type, max_results=10, enrich=True, telemetry=None, max_wall_seconds=None):
        seen.update(
            issue=issue,
            claim_type=claim_type,
            max_results=max_results,
            enrich=enrich,
            telemetry=telemetry,
            max_wall_seconds=max_wall_seconds,
        )
        return [
            CaseRecord(
                title="Smith v. Wilkie",
                court="Court of Appeals for Veterans Claims",
                url="https://uscourts.cavc.gov/smith",
                snippet="service connection evidence nexus",
            )
        ]

    monkeypatch.setattr("va_legal_agent.agent.fetch_cases_for_issue", recording_fetch)
    telemetry: list[dict[str, object]] = []

    analysis = analyze_cases_for_claim(
        "service connection",
        claim_type="Disability",
        max_results=3,
        telemetry=telemetry,
        max_wall_seconds=2.5,
    )

    assert seen["issue"] == "service connection"
    assert seen["claim_type"] == "Disability"
    assert seen["max_results"] == 3
    assert seen["enrich"] is True
    assert seen["telemetry"] is telemetry
    assert seen["max_wall_seconds"] == 2.5
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

    analyze_cases_for_claim("tinnitus")  # both args use their defaults

    assert len(calls) == len(build_case_queries("tinnitus", "Compensation"))
    assert '"Compensation"' in calls[0][0]
    assert calls[0][1] == 10  # max_results default flows through to search


def test_analyze_cases_honors_enrich_false(monkeypatch):
    calls: list[str] = []

    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
        return [{"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection"}]

    def forbid_fetch(url, timeout=None):
        calls.append(url)
        raise AssertionError("enrichment must not run when enrich=False")

    monkeypatch.setattr("va_legal_agent.agent.search_all", fake_search_all)
    monkeypatch.setattr("va_legal_agent.agent.fetch_case_details", forbid_fetch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")

    analyze_cases_for_claim("tinnitus", enrich=False)

    assert calls == []  # the enrich=False flag reached fetch_cases_for_issue


def test_analyze_cases_carries_search_telemetry_and_flags(monkeypatch):
    def fake_search_all(query, max_results=10, telemetry=None, deadline=None):
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
    analysis = analyze_cases_for_claim("tinnitus", telemetry=telemetry)

    assert analysis.search_telemetry  # rolled from the pipeline telemetry
    assert analysis.search_telemetry["duckduckgo"]["failures"] > 0
    assert analysis.search_flags  # the failure surfaces as a low-recall flag


def test_agent_main_guard_prints_analysis(monkeypatch, capsys):
    """Re-execute the module as __main__ with the network layer stubbed."""
    import runpy
    import warnings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0")  # no inter-query sleeps

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
