"""Provider-level tests for citator-specific CourtListener calls."""

import pytest

from va_legal_agent.providers import CourtListenerProvider
from va_legal_agent.search import SearchError


def test_courtlistener_citing_edges_uses_cursor_and_page_size(monkeypatch):
    provider = CourtListenerProvider()
    captured: dict[str, object] = {}

    def fake_get_json(url, params=None):
        captured.update(url=url, params=params)
        return {"count": 1, "results": [], "next": None}

    monkeypatch.setattr(provider, "_get_json", fake_get_json)

    result = provider.citing_edges(123, page_size=500, cursor="next-token")

    assert result == {"count": 1, "results": [], "next": None}
    assert captured["url"] == CourtListenerProvider.CITATIONS_URL
    assert captured["params"] == {
        "cited_opinion": 123,
        "page_size": 100,
        "format": "json",
        "cursor": "next-token",
    }


def test_courtlistener_citing_edges_wraps_errors(monkeypatch):
    provider = CourtListenerProvider()

    def fail(url, params=None):
        raise SearchError("throttled")

    monkeypatch.setattr(provider, "_get_json", fail)

    with pytest.raises(SearchError, match="citing opinions failed"):
        provider.citing_edges(123)


def test_courtlistener_opinion_metadata_delegates(monkeypatch):
    provider = CourtListenerProvider()
    monkeypatch.setattr(provider, "_get_opinion", lambda opinion_id: {"title": str(opinion_id)})

    assert provider.opinion_metadata(123) == {"title": "123"}


def test_same_docket_decisions_maps_other_clusters(monkeypatch):
    provider = CourtListenerProvider()
    opinion_url = CourtListenerProvider.API_URL + "111/"
    target_cluster = "https://www.courtlistener.com/api/rest/v4/clusters/1/"
    docket_url = "https://www.courtlistener.com/api/rest/v4/dockets/9/"
    other_cluster = "https://www.courtlistener.com/api/rest/v4/clusters/2/"
    second_cluster = "https://www.courtlistener.com/api/rest/v4/clusters/3/"
    responses = {
        opinion_url: {"cluster": target_cluster},
        target_cluster: {"docket": docket_url},
        docket_url: {
            "court_id": "cavc",
            "clusters": ["", target_cluster, other_cluster, second_cluster],
        },
        other_cluster: {
            "id": 2,
            "case_name": "Smith v. McDonough",
            "date_filed": "2019-01-01",
            "absolute_url": "/opinion/2/smith-v-mcdonough/",
            "citations": [{"cite": "32 Vet.App. 9"}],
        },
        second_cluster: {
            "case_name": "Smith v. Shulkin",
            "date_filed": "2018-01-01",
            "citations": ["31 Vet.App. 8"],
        },
    }
    monkeypatch.setattr(provider, "_get_json", lambda url, params=None: responses[url])

    decisions = provider.same_docket_decisions(111, max_results=2)

    assert decisions == [
        {
            "case_name": "Smith v. McDonough",
            "citation": "32 Vet.App. 9",
            "court": "Court of Appeals for Veterans Claims",
            "decision_date": "2019-01-01",
            "url": "https://www.courtlistener.com/opinion/2/smith-v-mcdonough/",
            "cluster_id": "2",
        },
        {
            "case_name": "Smith v. Shulkin",
            "citation": "31 Vet.App. 8",
            "court": "Court of Appeals for Veterans Claims",
            "decision_date": "2018-01-01",
            "url": "",
            "cluster_id": "3",
        },
    ]


def test_same_docket_decisions_returns_empty_without_cluster_or_docket(monkeypatch):
    provider = CourtListenerProvider()
    opinion_url = CourtListenerProvider.API_URL + "111/"
    cluster_url = "https://www.courtlistener.com/api/rest/v4/clusters/1/"
    docket_url = "https://www.courtlistener.com/api/rest/v4/dockets/9/"
    responses = {
        opinion_url: {},
        cluster_url: {},
        docket_url: {},
    }
    monkeypatch.setattr(provider, "_get_json", lambda url, params=None: responses[url])

    assert provider.same_docket_decisions(111) == []

    responses[opinion_url] = {"cluster": cluster_url}
    assert provider.same_docket_decisions(111) == []

    responses[cluster_url] = {"docket": docket_url}
    assert provider.same_docket_decisions(111) == []


def test_same_docket_decisions_wraps_fetch_errors(monkeypatch):
    provider = CourtListenerProvider()

    def fail(url, params=None):
        raise SearchError("throttled")

    monkeypatch.setattr(provider, "_get_json", fail)

    with pytest.raises(SearchError, match="same-docket lookup failed"):
        provider.same_docket_decisions(111)
