"""Tests for the persistent CourtListener read-through cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from va_legal_agent.courtlistener_cache import (
    CACHE_VERSION,
    CourtListenerCache,
)
from va_legal_agent.providers import CourtListenerProvider


class _Clock:
    def __init__(self, value: float = 1_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _entries(cache: CourtListenerCache) -> list[Path]:
    return sorted(cache.root.rglob("*.json"))


def test_cache_persists_across_instances_and_expires_with_ttl(tmp_path):
    clock = _Clock()
    first = CourtListenerCache(tmp_path / "cache", ttl_seconds=10, now=clock)
    first.set(
        "opinion",
        "7",
        {"plain_text": "cached body"},
        source_url="https://example.test/opinions/7/",
        params={"format": "json"},
    )

    second = CourtListenerCache(tmp_path / "cache", ttl_seconds=10, now=clock)
    assert second.get("opinion", "7", {"format": "json"}) == {
        "plain_text": "cached body"
    }

    clock.value += 10
    assert second.get("opinion", "7", {"format": "json"}) is None
    assert _entries(second) == []


def test_cache_set_is_noop_when_ttl_is_zero(tmp_path):
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=0)

    cache.set("opinion", "7", {"plain_text": "body"})

    assert cache.enabled is False
    assert cache.get("opinion", "7") is None
    assert list((tmp_path / "cache").rglob("*.json")) == []


def test_cache_invalidation_filters_and_clear(tmp_path):
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=60)
    cache.set("opinion", "7", {"id": 7})
    cache.set("cluster", "7", {"id": 7})
    cache.set("docket", "7", {"id": 7})
    cache.set("citation_edges", "cited_opinion:7", {"count": 1})

    assert cache.invalidate(kind="opinion") == 1
    assert cache.get("opinion", "7") is None
    assert cache.get("cluster", "7") == {"id": 7}
    assert cache.invalidate(identifier="7") == 2
    assert cache.get("docket", "7") is None
    assert cache.get("citation_edges", "cited_opinion:7") == {"count": 1}
    assert cache.clear() == 1
    assert cache.get("citation_edges", "cited_opinion:7") is None
    assert cache.invalidate() == 0


def test_cache_prune_expired_removes_only_expired_entries(tmp_path):
    clock = _Clock(0)
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=5, now=clock)
    cache.set("opinion", "1", {"id": 1})
    clock.value = 10
    cache.set("opinion", "2", {"id": 2})

    assert cache.prune_expired() == 1
    assert cache.get("opinion", "1") is None
    assert cache.get("opinion", "2") == {"id": 2}
    assert cache.prune_expired() == 0


def test_cache_treats_corrupt_and_unknown_entries_as_misses(tmp_path):
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=60)
    entries = cache.root / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    (entries / "corrupt.json").write_text("{", encoding="utf-8")
    (entries / "not-dict.json").write_text("[]", encoding="utf-8")
    (entries / "old-version.json").write_text(
        json.dumps({"version": CACHE_VERSION + 1}), encoding="utf-8"
    )

    assert cache.get("opinion", "missing") is None
    assert cache.prune_expired() == 0
    assert cache.invalidate(kind="opinion") == 0
    assert cache.clear() == 3


def test_cache_rejects_unsafe_kinds(tmp_path):
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=60)

    with pytest.raises(ValueError, match="safe characters"):
        cache.set("", "1", {})
    with pytest.raises(ValueError, match="safe characters"):
        cache.set("..", "1", {})
    with pytest.raises(ValueError, match="safe characters"):
        cache.invalidate(kind="..")


def test_cache_cleans_temporary_file_after_write_failure(tmp_path, monkeypatch):
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=60)

    def fail_dump(*args, **kwargs):
        raise RuntimeError("serialization failed")

    monkeypatch.setattr("va_legal_agent.courtlistener_cache.json.dump", fail_dump)

    with pytest.raises(RuntimeError, match="serialization failed"):
        cache.set("opinion", "7", {"id": 7})

    assert list(cache.root.rglob("*.tmp")) == []


def test_cache_never_stores_sensitive_query_or_params(tmp_path):
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=60)
    cache.set(
        "opinion",
        "7",
        {"id": 7},
        source_url=(
            "https://example.test/opinions/7/?cursor=abc&token=unique-secret"
        ),
        params={"page_size": 10, "Authorization": "unique-secret"},
    )

    serialized = "\n".join(
        path.read_text(encoding="utf-8") for path in _entries(cache)
    )
    assert "unique-secret" not in serialized
    assert "Authorization" not in serialized
    assert "cursor=abc" in serialized
    assert '"page_size": 10' in serialized


def test_cache_handles_invalid_source_url(tmp_path):
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=60)

    cache.set("opinion", "7", {"id": 7}, source_url="http://[::1")

    assert cache.get("opinion", "7") == {"id": 7}


def test_cache_invalid_expiry_is_a_miss_and_prune_removes_it(tmp_path):
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=60)
    path = cache._path("opinion", "7")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "kind": "opinion",
                "identifier": "7",
                "expires_at": "not-a-number",
                "response": {"id": 7},
            }
        ),
        encoding="utf-8",
    )

    assert cache.get("opinion", "7") is None

    path.write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "kind": "opinion",
                "identifier": "7",
                "expires_at": "not-a-number",
                "response": {"id": 7},
            }
        ),
        encoding="utf-8",
    )
    assert cache.prune_expired() == 1


def test_cache_unlink_failures_are_best_effort(tmp_path, monkeypatch):
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=1)
    cache.set("opinion", "1", {"id": 1})
    cache.set("opinion", "2", {"id": 2})

    def fail_unlink(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    assert cache.get("opinion", "1") == {"id": 1}
    clock = cache._now
    cache._now = lambda: clock() + 10
    assert cache.get("opinion", "1") is None
    assert cache.invalidate(kind="opinion") == 0
    assert cache.prune_expired() == 0
    assert cache.clear() == 0


def _opinion_payload():
    return {
        "id": 7,
        "absolute_url": "/opinion/7/smith-v-wilkie/",
        "cluster": (
            "https://www.courtlistener.com/api/rest/v4/clusters/70/"
        ),
        "plain_text": "The full opinion body.",
        "type": "020lead",
    }


def _cluster_payload():
    return {
        "id": 70,
        "case_name": "Smith v. Wilkie",
        "date_filed": "2020-01-01",
        "docket": (
            "https://www.courtlistener.com/api/rest/v4/dockets/700/"
        ),
        "citations": [{"cite": "30 Vet.App. 123"}],
        "sub_opinions": [],
    }


def _docket_payload():
    return {
        "id": 700,
        "docket_number": "19-1234",
        "court_id": "cavc",
    }


def _second_opinion_payload():
    return {
        "id": 8,
        "absolute_url": "/opinion/8/jones-v-mcdonough/",
        "cluster": (
            "https://www.courtlistener.com/api/rest/v4/clusters/70/"
        ),
        "plain_text": "Another opinion in the same cluster.",
        "type": "020lead",
    }


def test_provider_cache_is_shared_across_instances_and_never_stores_auth(
    tmp_path, monkeypatch
):
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=60)
    responses = {
        CourtListenerProvider.API_URL + "7/": _opinion_payload(),
        CourtListenerProvider.API_URL + "8/": _second_opinion_payload(),
        CourtListenerProvider.CLUSTERS_URL + "70/": _cluster_payload(),
        CourtListenerProvider.DOCKETS_URL + "700/": _docket_payload(),
    }
    calls: list[tuple[str, dict | None, dict | None]] = []

    def fake_get(url, params=None, headers=None, **kwargs):
        calls.append((url, params, headers))
        return _Response(responses[url])

    monkeypatch.setenv("COURTLISTENER_API_KEY", "unique-secret-token")
    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    first = CourtListenerProvider(cache=cache)
    metadata = first._get_opinion(7)
    assert metadata is not None
    assert [call[0] for call in calls] == [
        CourtListenerProvider.API_URL + "7/",
        CourtListenerProvider.CLUSTERS_URL + "70/",
        CourtListenerProvider.DOCKETS_URL + "700/",
    ]
    assert calls[0][2]["Authorization"] == "Token unique-secret-token"

    second = CourtListenerProvider(cache=cache)
    assert second._get_opinion(7) == metadata
    assert second.fetch_opinion_text(7) == "The full opinion body."
    assert len(calls) == 3

    # A different target in the same cluster reuses the cached cluster and
    # docket and fetches only its own opinion response.
    shared = second._get_opinion(8)
    assert shared is not None
    assert shared["cluster_id"] == "70"
    assert calls[-1][0] == CourtListenerProvider.API_URL + "8/"
    assert len(calls) == 4

    serialized = "\n".join(
        path.read_text(encoding="utf-8") for path in _entries(cache)
    )
    assert "unique-secret-token" not in serialized
    assert "Authorization" not in serialized
    assert '"kind": "opinion"' in serialized
    assert '"kind": "cluster"' in serialized
    assert '"kind": "docket"' in serialized


def test_provider_caches_citation_edges_by_opinion_and_page(tmp_path, monkeypatch):
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=60)
    calls: list[dict | None] = []

    def fake_get(url, params=None, headers=None, **kwargs):
        calls.append(params)
        return _Response(
            {
                "count": 1,
                "results": [{"citing_opinion": "9"}],
                "next": None,
            }
        )

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)
    provider = CourtListenerProvider(cache=cache)

    first = provider.citing_edges(7, page_size=25, cursor="abc")
    second = CourtListenerProvider(cache=cache).citing_edges(
        7, page_size=25, cursor="abc"
    )

    assert first == second
    assert len(calls) == 1
    assert calls[0]["cited_opinion"] == 7
    assert calls[0]["cursor"] == "abc"


def test_provider_cache_target_classification(tmp_path, monkeypatch):
    monkeypatch.delenv("COURTLISTENER_CACHE_DIR", raising=False)
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=60)
    provider = CourtListenerProvider(cache=cache)

    assert provider._cache_target(provider.API_URL + "7/", None) == (
        "opinion",
        "7",
        {},
    )
    assert provider._cache_target(provider.CLUSTERS_URL + "70/", None) == (
        "cluster",
        "70",
        {},
    )
    assert provider._cache_target(provider.DOCKETS_URL + "700/", None) == (
        "docket",
        "700",
        {},
    )
    assert provider._cache_target(
        provider.CITATIONS_URL, {"citing_opinion": "9"}
    ) == ("citation_edges", "citing_opinion:9", {"citing_opinion": "9"})
    assert provider._cache_target(provider.CITATIONS_URL, None) == (
        "citation_edges",
        "collection",
        {},
    )
    assert provider._cache_target(provider.API_URL, {"id__in": "7"}) is None
    assert provider._cache_target(provider.SEARCH_URL, None) is None
    assert provider._cache_target("http://[::1", None) is None
    assert (
        CourtListenerProvider(cache=None)._cache_target(
            provider.API_URL + "7/", None
        )
        is None
    )


def test_provider_reads_cache_configuration_from_environment(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("COURTLISTENER_CACHE_DIR", str(tmp_path / "configured"))
    monkeypatch.setenv("COURTLISTENER_CACHE_TTL_SECONDS", "60")

    provider = CourtListenerProvider()

    assert provider._cache is not None
    assert provider._cache.root == tmp_path / "configured"
    assert provider._cache.ttl_seconds == 60

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setenv("COURTLISTENER_CACHE_DIR", str(blocker))
    provider = CourtListenerProvider()
    assert provider._cache is None
    assert "CourtListener cache unavailable" in caplog.text


def test_provider_cache_invalidation_and_write_failure(
    tmp_path, monkeypatch, caplog
):
    cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=60)
    cache.set("opinion", "7", {"id": 7})
    provider = CourtListenerProvider(cache=cache)

    assert provider.invalidate_cache(kind="opinion", identifier="7") == 1
    assert provider.invalidate_cache() == 0
    assert CourtListenerProvider(cache=None).invalidate_cache() == 0

    class FailingCache(CourtListenerCache):
        def set(self, *args, **kwargs):
            raise OSError("disk full")

    failing = CourtListenerProvider(cache=FailingCache(tmp_path / "other", 60))

    def fake_get(url, params=None, headers=None, **kwargs):
        return _Response({"id": 7})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)
    assert failing._get_json(failing.API_URL + "7/") == {"id": 7}
    assert "Could not write CourtListener cache entry" in caplog.text
