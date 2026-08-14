import pytest

from va_legal_agent.models import CaseRecord
from va_legal_agent.search import SearchError
from va_legal_agent.agent import (
    analyze_cases_for_claim,
    build_case_queries,
    fetch_cases_for_issue,
    score_case_relevance,
    summarize_case_impact,
)


def test_build_queries_include_major_courts():
    queries = build_case_queries("service connection", "cancer")
    assert any("uscourts.cavc.gov" in q for q in queries)
    assert any("caafc.uscourts.gov" in q for q in queries)
    assert any("supremecourt.gov" in q for q in queries)
    assert any("bva.va.gov" in q for q in queries)


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


def _stub_search(monkeypatch, results=None, error=None):
    """Replace search_web (and the inter-query sleep) with deterministic stubs."""
    calls: list[str] = []
    sleeps: list[float] = []

    def fake_search_web(query, max_results=10):
        calls.append(query)
        if error is not None:
            raise error
        return [dict(r) for r in (results or [])]

    monkeypatch.setattr("va_legal_agent.agent.search_web", fake_search_web)
    monkeypatch.setattr("va_legal_agent.agent.time.sleep", sleeps.append)
    return calls, sleeps


def test_fetch_cases_dedupes_ranks_and_truncates(monkeypatch):
    results = [
        {"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection veterans evidence"},
        {"title": "Case A", "url": "https://uscourts.cavc.gov/a", "snippet": "service connection veterans evidence"},
        {"title": "Case B", "url": "https://cafc.uscourts.gov/b", "snippet": "unrelated procedural history"},
    ]
    calls, sleeps = _stub_search(monkeypatch, results=results)

    cases = fetch_cases_for_issue("service connection", max_results=1)

    assert len(calls) == 8  # one search per court query
    assert len(cases) == 1
    assert cases[0].title == "Case A"  # higher relevance wins dedupe order
    assert cases[0].authority_rank == 1


def test_fetch_cases_throttles_between_queries(monkeypatch):
    monkeypatch.setenv("SEARCH_DELAY_SECONDS", "0.25")
    calls, sleeps = _stub_search(monkeypatch, results=[])

    fetch_cases_for_issue("tinnitus")

    assert len(calls) == 8
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


def test_analyze_cases_for_claim_raises_when_no_cases(monkeypatch):
    _stub_search(monkeypatch, results=[])

    with pytest.raises(ValueError, match="No cases found"):
        analyze_cases_for_claim("obscure issue")
