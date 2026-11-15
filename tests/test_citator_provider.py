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


def _collection(rows, next_url=None):
    return {"count": len(rows), "results": rows, "next": next_url}


def _batch_opinion(opinion_id, cluster_id=None, **overrides):
    row = {
        "id": opinion_id,
        "absolute_url": f"/opinion/{opinion_id}/case-{opinion_id}/",
        "type": "020lead",
    }
    if cluster_id is not None:
        row["cluster"] = (
            "https://www.courtlistener.com/api/rest/v4/clusters/"
            f"{cluster_id}/"
        )
    row.update(overrides)
    return row


def _batch_cluster(cluster_id, docket_id=None, **overrides):
    row = {
        "id": cluster_id,
        "case_name": f"Case {cluster_id} v. Wilkie",
        "citations": [{"cite": f"{cluster_id} Vet.App. 1"}],
        "sub_opinions": [],
        "date_filed": "2020-01-01",
        "judges": "Judge Example",
        "precedential_status": "Published",
    }
    if docket_id is not None:
        row["docket"] = (
            "https://www.courtlistener.com/api/rest/v4/dockets/"
            f"{docket_id}/"
        )
    row.update(overrides)
    return row


def test_opinion_metadata_batch_uses_three_collection_requests(monkeypatch):
    provider = CourtListenerProvider()
    calls: list[tuple[str, object]] = []

    def fake_get_json(url, params=None):
        calls.append((url, params))
        if url == provider.API_URL:
            return _collection(
                [
                    _batch_opinion(111, 11),
                    _batch_opinion(112, 12),
                ]
            )
        if url == provider.CLUSTERS_URL:
            return _collection(
                [_batch_cluster(11, 21), _batch_cluster(12, 22)]
            )
        if url == provider.DOCKETS_URL:
            return _collection(
                [
                    {"id": 21, "docket_number": "19-1", "court_id": "cavc"},
                    {"id": 22, "docket_number": "19-2", "court_id": "cafc"},
                ]
            )
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(provider, "_get_json", fake_get_json)

    result = provider.opinion_metadata_batch([111, 112, 113, 111])

    assert [call[0] for call in calls] == [
        provider.API_URL,
        provider.CLUSTERS_URL,
        provider.DOCKETS_URL,
    ]
    assert calls[0][1] == {
        "id__in": "111,112,113",
        "page_size": provider.BATCH_SIZE,
        "format": "json",
    }
    assert calls[1][1]["id__in"] == "11,12"  # type: ignore[index]
    assert calls[2][1]["id__in"] == "21,22"  # type: ignore[index]
    assert set(result) == {111, 112}
    assert result[111]["title"] == "Case 11 v. Wilkie"
    assert result[111]["court"] == "Court of Appeals for Veterans Claims"
    assert result[111]["docket"] == "19-1"
    assert result[111]["citation"] == "11 Vet.App. 1"
    assert result[111]["cluster_id"] == "11"
    assert result[111]["cluster_resolved"] is True
    assert result[111]["courtlistener_opinion_id"] == "111"
    assert result[112]["court"] == "U.S. Court of Appeals for the Federal Circuit"


def test_opinion_metadata_batch_skips_cluster_requests_without_links(monkeypatch):
    provider = CourtListenerProvider()
    calls: list[str] = []

    def fake_get_json(url, params=None):
        calls.append(url)
        return _collection([_batch_opinion(111)])

    monkeypatch.setattr(provider, "_get_json", fake_get_json)

    result = provider.opinion_metadata_batch([111])

    assert calls == [provider.API_URL]
    assert result[111]["cluster_resolved"] is False
    assert result[111]["opinion_ids"] == [111]
    assert result[111]["citation"] == ""
    assert provider.opinion_metadata_batch([]) == {}


def test_opinion_metadata_batch_omits_missing_or_malformed_links(monkeypatch):
    provider = CourtListenerProvider()
    opinions = [
        _batch_opinion(111, 11),
        _batch_opinion(112, 12),  # cluster 12 is absent from the response
        _batch_opinion(113, 13),  # docket 23 is absent from the response
        _batch_opinion(
            114,
            cluster="https://www.courtlistener.com/api/rest/v4/clusters/x/",
        ),
        _batch_opinion(115, 15),
        _batch_opinion(116, absolute_url=""),  # no usable URL -> result omitted
    ]
    clusters = [
        {"case_name": "Unidentifiable cluster"},
        _batch_cluster(11, 21),
        _batch_cluster(13, 23),
        _batch_cluster(
            15,
            docket="https://www.courtlistener.com/api/rest/v4/dockets/x/",
        ),
    ]

    def fake_get_json(url, params=None):
        if url == provider.API_URL:
            return _collection(opinions)
        if url == provider.CLUSTERS_URL:
            return _collection(clusters)
        if url == provider.DOCKETS_URL:
            # The id-only row also exercises the fallback when resource_uri and
            # url are absent from a collection response.
            return _collection([{"id": 21, "court_id": "cavc"}])
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(provider, "_get_json", fake_get_json)

    result = provider.opinion_metadata_batch(
        [111, 112, 113, 114, 115, 116]
    )

    assert set(result) == {111}
    assert result[111]["docket"] == ""
    assert result[111]["court"] == "Court of Appeals for Veterans Claims"


def test_opinion_metadata_batch_follows_collection_pages(monkeypatch):
    provider = CourtListenerProvider()
    next_url = provider.API_URL + "?cursor=next"
    calls: list[tuple[str, object]] = []

    def fake_get_json(url, params=None):
        calls.append((url, params))
        if url == provider.API_URL:
            return _collection([_batch_opinion(111, 11)], next_url=next_url)
        if url == next_url:
            return _collection([_batch_opinion(112, 12)])
        if url == provider.CLUSTERS_URL:
            return _collection(
                [_batch_cluster(11, 21), _batch_cluster(12, 22)]
            )
        if url == provider.DOCKETS_URL:
            return _collection(
                [
                    {"id": 21, "court_id": "cavc"},
                    {"id": 22, "court_id": "cavc"},
                ]
            )
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(provider, "_get_json", fake_get_json)

    result = provider.opinion_metadata_batch([111, 112])

    assert set(result) == {111, 112}
    assert (next_url, None) in calls


def test_opinion_metadata_batch_rejects_non_collection_responses(monkeypatch):
    provider = CourtListenerProvider()
    monkeypatch.setattr(
        provider, "_get_json", lambda url, params=None: {"results": "bad"}
    )

    with pytest.raises(SearchError, match="batch opinion response was not a collection"):
        provider.opinion_metadata_batch([111])

    monkeypatch.setattr(provider, "_get_json", lambda url, params=None: [])
    with pytest.raises(SearchError, match="batch opinion response was not a collection"):
        provider.opinion_metadata_batch([111])


def test_opinion_metadata_batch_wraps_collection_errors(monkeypatch):
    provider = CourtListenerProvider()

    def fail(url, params=None):
        raise SearchError("throttled")

    monkeypatch.setattr(provider, "_get_json", fail)

    with pytest.raises(SearchError, match="batch opinion fetch failed: throttled"):
        provider.opinion_metadata_batch([111])


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
