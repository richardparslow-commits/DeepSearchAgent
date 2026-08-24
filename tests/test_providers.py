"""Tests for the search provider abstraction (va_legal_agent.providers)."""

import email.utils
import json
import os
import threading
import time
from datetime import datetime, timezone

import pytest
import requests

from va_legal_agent.providers import (
    _bva_leaf_sitemap,
    _bva_local_build,
    _bva_local_index_paths,
    _bva_local_load,
    _bva_local_meta_path,
    _bva_local_needs_rebuild,
    _bva_local_search_corpus,
    _bva_local_write_meta,
    _bva_sitemap_index,
    _bva_text_matches,
    _courtlistener_query,
    _fetch_bva_decision_text,
    _minimize_bva_query,
    _parse_bva_leaf_sitemap,
    _parse_bva_sitemap_index,
    _parse_retry_after,
    BVAProvider,
    BvaLocalIndexProvider,
    BvaSitemapProvider,
    CourtListenerProvider,
    DuckDuckGoProvider,
    SearchError,
    USAGE_URL,
    check_courtlistener_daily_budget,
    check_courtlistener_minute_budget,
    courtlistener_daily_budget,
    courtlistener_minute_budget,
    fetch_courtlistener_usage,
    get_provider,
    resolve_search_providers,
    rollup_search_telemetry,
    search_all,
    validate_search_providers,
)
from va_legal_agent.topics import COURT_UNKNOWN


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else ""
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


class _FakeSession:
    """Minimal curl_cffi.Session stand-in: forwards .get to a fake callable."""

    def __init__(self, get_fn):
        self._get_fn = get_fn

    def get(self, url, **kwargs):
        return self._get_fn(url, **kwargs)


def test_get_provider_known_names():
    assert isinstance(get_provider("duckduckgo"), DuckDuckGoProvider)
    assert isinstance(get_provider("courtlistener"), CourtListenerProvider)
    assert isinstance(get_provider("bva"), BVAProvider)
    assert isinstance(get_provider("bvasitemap"), BvaSitemapProvider)
    assert isinstance(get_provider("bvalocal"), BvaLocalIndexProvider)


def test_get_provider_unknown_name():
    with pytest.raises(ValueError, match="Unknown search provider"):
        get_provider("google")


def test_courtlistener_get_json_passes_proxy_when_configured(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_KEY", "tok")
    monkeypatch.setenv("SEARCH_HTTP_PROXY", "http://user:pass@proxy.example:8080")
    captured: dict[str, object] = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        captured["proxies"] = kwargs.get("proxies")
        return FakeResponse({"results": []})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    CourtListenerProvider()._get_json("https://www.courtlistener.com/api/rest/v4/search/")

    assert captured["proxies"] == {
        "http": "http://user:pass@proxy.example:8080",
        "https": "http://user:pass@proxy.example:8080",
    }


def test_bva_search_passes_proxy_when_configured(monkeypatch):
    monkeypatch.setenv("SEARCH_HTTP_PROXY", "http://proxy.example:8080")
    captured: dict[str, object] = {}
    payload = '{"resultsData": {"results": [{"url": "https://www.va.gov/vetapp/x.txt", "title": "A1.txt", "description": "x"}]}}'

    def fake_get(url, params=None, timeout=None, **kwargs):
        captured["proxies"] = kwargs.get("proxies")
        return FakeResponse(payload)

    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session", lambda: _FakeSession(fake_get)
    )

    BVAProvider().search("tinnitus")

    assert captured["proxies"] == {
        "http": "http://proxy.example:8080",
        "https": "http://proxy.example:8080",
    }


def test_resolve_search_providers_filters_without_logging(monkeypatch, caplog):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo,bva,google")

    assert resolve_search_providers() == ["duckduckgo", "bva"]
    assert caplog.records == []  # pure filter: no warnings
    assert resolve_search_providers("") == ["duckduckgo"]
    assert resolve_search_providers("google") == []


def test_validate_search_providers_warns_and_filters(monkeypatch, caplog):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckdduckgo,duckduckgo,bva")

    valid = validate_search_providers()

    assert valid == ["duckduckgo", "bva"]
    assert any("Unknown search provider" in r.message for r in caplog.records)
    assert any("duckdduckgo" in r.message for r in caplog.records)
    # A fully-unknown list yields no providers rather than a silent surprise.
    monkeypatch.setenv("SEARCH_PROVIDERS", "google,google")
    assert validate_search_providers() == []


def test_validate_search_providers_defaults_and_raw_override(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "bva")
    # Empty (unset/blank) resolves to the duckduckgo default.
    assert validate_search_providers("") == ["duckduckgo"]
    assert validate_search_providers("  ") == ["duckduckgo"]
    # An explicit raw value wins over the environment.
    assert validate_search_providers("courtlistener") == ["courtlistener"]


def _cl_result(**overrides):
    # Real v4 /search/ response shape (camelCase keys): the search endpoint is
    # the only one that returns case metadata inline. The opinions list/detail
    # endpoints return no case_name/court/date/docket/judge fields at all.
    item = {
        "caseName": "Smith v. McDonough",
        "absolute_url": "/opinion/12345/smith-v-mcdonough/",
        "court_id": "cavc",
        "dateFiled": "2023-05-01",
        "docketNumber": "19-4433",
        "judge": "Judge Mary J. Smith",
        "citation": ["35 Vet.App. 123"],
        # The search endpoint nests up to 500 chars of opinion text here.
        "opinions": [{"snippet": "UNITED STATES COURT OF APPEALS FOR VETERANS CLAIMS"}],
    }
    item.update(overrides)
    return item


def _cl_opinion_detail(**overrides):
    """Real v4 opinions detail shape: text plus links, no case metadata."""
    item = {
        "absolute_url": "/opinion/12345/smith-v-mcdonough/",
        "cluster": "https://www.courtlistener.com/api/rest/v4/clusters/98765/",
        "plain_text": "",
    }
    item.update(overrides)
    return item


def _cl_cluster_detail(**overrides):
    """Real v4 clusters shape: case metadata plus a docket link."""
    item = {
        "case_name": "Smith v. McDonough",
        "citations": [{"cite": "35 Vet.App. 123"}],
        "date_filed": "2023-05-01",
        "judges": "Judge Mary J. Smith",
        "docket": "https://www.courtlistener.com/api/rest/v4/dockets/55555/",
    }
    item.update(overrides)
    return item


def _cl_docket_detail(**overrides):
    """Real v4 dockets shape: docket number and court link."""
    item = {
        "docket_number": "19-4433",
        "court_id": "cavc",
    }
    item.update(overrides)
    return item


def test_courtlistener_maps_structured_fields(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return FakeResponse({"results": [_cl_result()]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    results = CourtListenerProvider().search("service connection", max_results=5)

    assert len(results) == 1
    result = results[0]
    assert result["title"] == "Smith v. McDonough"
    assert result["url"] == "https://www.courtlistener.com/opinion/12345/smith-v-mcdonough/"
    assert result["court"] == "Court of Appeals for Veterans Claims"
    assert result["citation"] == "35 Vet.App. 123"
    assert result["decision_date"] == "2023-05-01"
    assert result["docket"] == "19-4433"
    assert result["judge"] == "Judge Mary J. Smith"
    assert result["snippet"] == "UNITED STATES COURT OF APPEALS FOR VETERANS CLAIMS"
    assert captured["url"] == CourtListenerProvider.SEARCH_URL
    assert captured["params"]["q"] == (
        "(service connection) AND court_id:(cavc OR cafc OR scotus)"
    )
    assert "court" not in captured["params"]


def test_courtlistener_query_embeds_court_filter_in_q():
    """The court filter must be a fielded ``q`` clause, not a repeated ``court``
    GET param: the search engine only honors the LAST repeated value, so
    ``court=cavc&court=cafc&court=scotus`` silently filtered to ``scotus``
    (zero hits for most VA issues). Pin both the normal and empty-query forms.
    """
    assert _courtlistener_query("tinnitus") == (
        "(tinnitus) AND court_id:(cavc OR cafc OR scotus)"
    )
    assert _courtlistener_query("") == "court_id:(cavc OR cafc OR scotus)"


def test_courtlistener_maps_nested_opinion_snippet(monkeypatch):
    """The opinion text nested under opinions[0].snippet becomes the snippet.

    Without it, CourtListener cases carry empty text and the LLM/holding
    extraction gets nothing to synthesize (verified live: the field holds up
    to 500 chars of the opinion's opening). Missing or empty opinions degrade
    to an empty snippet rather than raising.
    """
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    from va_legal_agent.providers import _map_courtlistener_opinion

    assert _map_courtlistener_opinion(
        _cl_result(opinions=[{"snippet": "  We hold that service connection requires a nexus.  "}])
    )["snippet"] == "We hold that service connection requires a nexus."
    assert _map_courtlistener_opinion(_cl_result(opinions=[]))["snippet"] == ""
    assert _map_courtlistener_opinion(_cl_result(opinions=[{"snippet": None}]))["snippet"] == ""
    assert _map_courtlistener_opinion(_cl_result())["snippet"] == (
        "UNITED STATES COURT OF APPEALS FOR VETERANS CLAIMS"
    )


def test_courtlistener_maps_nested_opinion_id(monkeypatch):
    """The nested opinion id rides along so deep-read can fetch the full body."""
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    from va_legal_agent.providers import _map_courtlistener_opinion

    assert _map_courtlistener_opinion(
        _cl_result(opinions=[{"snippet": "x", "id": 5286139}])
    )["courtlistener_opinion_id"] == "5286139"
    # Missing id, missing opinions, and id=None all degrade to "".
    assert _map_courtlistener_opinion(
        _cl_result(opinions=[{"snippet": "x"}])
    )["courtlistener_opinion_id"] == ""
    assert _map_courtlistener_opinion(
        _cl_result(opinions=[])
    )["courtlistener_opinion_id"] == ""
    assert _map_courtlistener_opinion(
        _cl_result(opinions=[{"snippet": "x", "id": None}])
    )["courtlistener_opinion_id"] == ""


def test_courtlistener_fetch_opinion_text_prefers_plain_text(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    from va_legal_agent.providers import CourtListenerProvider

    captured: dict[str, object] = {}

    def recording_get_json(self, url):
        captured["url"] = url
        return _cl_opinion_detail(plain_text="  The full opinion text.  ")

    monkeypatch.setattr(
        "va_legal_agent.providers.CourtListenerProvider._get_json", recording_get_json
    )

    assert CourtListenerProvider().fetch_opinion_text(12345) == "The full opinion text."
    # The opinion id is embedded in the detail-endpoint URL.
    assert captured["url"] == "https://www.courtlistener.com/api/rest/v4/opinions/12345/"


def test_courtlistener_fetch_opinion_text_strips_html(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    from va_legal_agent.providers import CourtListenerProvider

    monkeypatch.setattr(
        "va_legal_agent.providers.CourtListenerProvider._get_json",
        lambda self, url: _cl_opinion_detail(
            plain_text="", html_with_citations="<p>The <strong>holding</strong> text.</p>"
        ),
    )

    assert CourtListenerProvider().fetch_opinion_text(12345) == "The holding text."


def test_courtlistener_fetch_opinion_text_falls_through_html_variants(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    from va_legal_agent.providers import CourtListenerProvider

    monkeypatch.setattr(
        "va_legal_agent.providers.CourtListenerProvider._get_json",
        lambda self, url: _cl_opinion_detail(
            plain_text="",
            html_with_citations="",
            html="",
            html_columbia="",
            html_lawbox="<p>lawbox text</p>",
        ),
    )

    assert CourtListenerProvider().fetch_opinion_text(12345) == "lawbox text"


def test_courtlistener_fetch_opinion_text_uses_html_field(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    from va_legal_agent.providers import CourtListenerProvider

    monkeypatch.setattr(
        "va_legal_agent.providers.CourtListenerProvider._get_json",
        lambda self, url: _cl_opinion_detail(plain_text="", html="<p>html body</p>"),
    )

    assert CourtListenerProvider().fetch_opinion_text(12345) == "html body"


def test_courtlistener_fetch_opinion_text_uses_html_columbia(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    from va_legal_agent.providers import CourtListenerProvider

    monkeypatch.setattr(
        "va_legal_agent.providers.CourtListenerProvider._get_json",
        lambda self, url: _cl_opinion_detail(
            plain_text="",
            html_with_citations="",
            html="",
            html_columbia="<p>columbia body</p>",
        ),
    )

    assert CourtListenerProvider().fetch_opinion_text(12345) == "columbia body"


def test_courtlistener_fetch_opinion_text_uses_html_anon_2020(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    from va_legal_agent.providers import CourtListenerProvider

    monkeypatch.setattr(
        "va_legal_agent.providers.CourtListenerProvider._get_json",
        lambda self, url: _cl_opinion_detail(
            plain_text="",
            html_with_citations="",
            html="",
            html_columbia="",
            html_lawbox="",
            html_anon_2020="<p>anon body</p>",
        ),
    )

    assert CourtListenerProvider().fetch_opinion_text(12345) == "anon body"


def test_courtlistener_fetch_opinion_text_empty_when_no_text(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    from va_legal_agent.providers import CourtListenerProvider

    monkeypatch.setattr(
        "va_legal_agent.providers.CourtListenerProvider._get_json",
        lambda self, url: _cl_opinion_detail(plain_text="", html_with_citations=""),
    )

    assert CourtListenerProvider().fetch_opinion_text(12345) == ""


def test_courtlistener_sends_api_key_when_set(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_KEY", "secret-token")
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["headers"] = headers
        return FakeResponse({"results": [_cl_result()]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    CourtListenerProvider().search("tinnitus")

    assert captured["headers"]["Authorization"] == "Token secret-token"


def test_courtlistener_raises_on_http_error(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")

    def fake_get(url, params=None, headers=None, timeout=None):
        return FakeResponse({}, status_code=500)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError, match="CourtListener"):
        CourtListenerProvider().search("tinnitus")


def test_courtlistener_401_without_token_gets_actionable_hint(monkeypatch):
    monkeypatch.delenv("COURTLISTENER_API_KEY", raising=False)

    def fake_get(url, params=None, headers=None, timeout=None):
        raise requests.HTTPError("401 Unauthorized", response=FakeResponse({}, status_code=401))

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError, match="COURTLISTENER_API_KEY"):
        CourtListenerProvider().search("tinnitus")


def test_search_all_uses_default_provider(monkeypatch):
    monkeypatch.delenv("SEARCH_PROVIDERS", raising=False)
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    monkeypatch.setattr("va_legal_agent.providers.requests.get", None)  # ensure DDG path used
    # Just verify default resolves to duckduckgo by checking search_all calls it.
    called = {"n": 0}

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            called["n"] += 1
            return [{"title": "x", "url": "https://example.com/x", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    results = search_all("service connection", max_results=3)

    assert called["n"] == 1
    assert results[0]["url"] == "https://example.com/x"


def test_search_all_merges_providers_and_dedupes(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo,courtlistener")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "1")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return [
                {"title": "DDG A", "url": "https://example.com/a", "snippet": "s"},
                {"title": "DDG dup", "url": "https://courtlistener.com/opinion/1/", "snippet": ""},
            ]

    class _FakeCL:
        name = "courtlistener"

        def search(self, query, max_results=10, page=1):
            return [{"title": "CL A", "url": "https://courtlistener.com/opinion/1/", "court": "cavc"}]

    monkeypatch.setattr(
        "va_legal_agent.providers.get_provider",
        lambda name: {"duckduckgo": _FakeDDG(), "courtlistener": _FakeCL()}[name],
    )

    results = search_all("service connection", max_results=10)

    urls = [r["url"] for r in results]
    assert len(urls) == 2  # duplicate URL from CL provider dropped
    assert "https://example.com/a" in urls
    assert "https://courtlistener.com/opinion/1/" in urls


def test_search_all_shares_cap_across_providers(monkeypatch):
    """The first-listed provider must not fill the cap alone and starve the rest."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo,courtlistener")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "1")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return [
                {"title": f"DDG {i}", "url": f"https://example.com/ddg/{i}", "snippet": ""}
                for i in range(max_results)
            ]

    class _FakeCL:
        name = "courtlistener"

        def search(self, query, max_results=10, page=1):
            return [
                {"title": f"CL {i}", "url": f"https://example.com/cl/{i}", "snippet": ""}
                for i in range(max_results)
            ]

    monkeypatch.setattr(
        "va_legal_agent.providers.get_provider",
        lambda name: {"duckduckgo": _FakeDDG(), "courtlistener": _FakeCL()}[name],
    )

    results = search_all("tinnitus", max_results=6)

    sources = {r["title"].split()[0] for r in results}
    assert sources == {"DDG", "CL"}  # both providers contributed
    assert len(results) == 6  # still capped overall


def test_search_all_floor_keeps_every_provider_when_cap_below_count(monkeypatch):
    """When max_results <= provider count, each provider still gets one slot."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo,courtlistener")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "1")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return [
                {"title": f"DDG {i}", "url": f"https://example.com/ddg/{i}", "snippet": ""}
                for i in range(max_results)
            ]

    class _FakeCL:
        name = "courtlistener"

        def search(self, query, max_results=10, page=1):
            return [
                {"title": f"CL {i}", "url": f"https://example.com/cl/{i}", "snippet": ""}
                for i in range(max_results)
            ]

    monkeypatch.setattr(
        "va_legal_agent.providers.get_provider",
        lambda name: {"duckduckgo": _FakeDDG(), "courtlistener": _FakeCL()}[name],
    )

    results = search_all("tinnitus", max_results=2)

    # 2 slots for 2 providers: each gets 1, rather than the first taking both.
    sources = {r["title"].split()[0] for r in results}
    assert sources == {"DDG", "CL"}
    assert len(results) == 2


def test_search_all_stops_paging_when_share_exhausted(monkeypatch):
    """A provider whose share is spent mid-pagination stops paging, not over-fetches."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo,courtlistener")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "2")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    pages_seen: list[tuple[str, int]] = []

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            pages_seen.append(("DDG", page))
            return [
                {"title": f"DDG {i}", "url": f"https://example.com/ddg/{i}", "snippet": ""}
                for i in range(max_results)
            ]

    class _FakeCL:
        name = "courtlistener"

        def search(self, query, max_results=10, page=1):
            pages_seen.append(("CL", page))
            return [
                {"title": f"CL {i}", "url": f"https://example.com/cl/{i}", "snippet": ""}
                for i in range(max_results)
            ]

    monkeypatch.setattr(
        "va_legal_agent.providers.get_provider",
        lambda name: {"duckduckgo": _FakeDDG(), "courtlistener": _FakeCL()}[name],
    )

    results = search_all("tinnitus", max_results=6)

    assert len(results) == 6
    # DDG's 3-slot share is spent after page 1, so it must not fetch page 2.
    assert ("DDG", 2) not in pages_seen
    assert ("CL", 1) in pages_seen


def test_search_all_paginates(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "3")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    pages_seen: list[int] = []

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            pages_seen.append(page)
            return [{"title": f"p{page}", "url": f"https://example.com/p{page}", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    results = search_all("tinnitus", max_results=10)

    assert pages_seen == [1, 2, 3]
    assert len(results) == 3


def test_search_all_stops_at_max_results(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "5")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return [{"title": f"p{page}", "url": f"https://example.com/p{page}", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    results = search_all("tinnitus", max_results=2)

    assert len(results) == 2  # capped; no need to fetch further pages


def test_search_all_raises_when_all_providers_fail(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            raise SearchError("provider down")

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    with pytest.raises(SearchError, match="provider down"):
        search_all("tinnitus")


def test_search_all_returns_empty_when_no_results_no_errors(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return []

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    assert search_all("tinnitus") == []


def test_search_all_skips_unknown_provider(monkeypatch, caplog):
    monkeypatch.setenv("SEARCH_PROVIDERS", "google,duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return [{"title": "x", "url": "https://example.com/x", "snippet": ""}]

    def fake_get_provider(name):
        if name == "google":
            raise ValueError("Unknown search provider: 'google'")
        return _FakeDDG()

    monkeypatch.setattr("va_legal_agent.providers.get_provider", fake_get_provider)

    results = search_all("tinnitus")

    assert len(results) == 1
    assert any("Unknown search provider" in r.message for r in caplog.records)


def test_search_all_all_unknown_providers_returns_empty(monkeypatch):
    """A fully-unknown SEARCH_PROVIDERS list yields no providers, not a crash.

    validate_search_providers resolves "google,google" to [] (documented
    behavior), and search_all must treat that as an empty result instead of
    dividing by zero on the per-provider budget.
    """
    monkeypatch.setenv("SEARCH_PROVIDERS", "google,google")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    assert search_all("tinnitus") == []


def test_search_all_expands_query_variants(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "2")
    queries_seen: list[str] = []

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            queries_seen.append(query)
            return [{"title": "x", "url": "https://example.com/x", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    results = search_all("service connection", max_results=10)

    assert len(results) == 1  # deduped by URL
    assert len(queries_seen) == 3  # original + 2 variants
    assert queries_seen[0] == "service connection"
    assert queries_seen[1] != queries_seen[0]


def test_search_all_zero_variants_sends_only_original(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    queries_seen: list[str] = []

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            queries_seen.append(query)
            return [{"title": "x", "url": "https://example.com/x", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    search_all("service connection", max_results=10)

    assert queries_seen == ["service connection"]


def test_search_all_filters_excluded_terms_from_results(monkeypatch):
    """Results whose title or snippet match an excluded term are dropped."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    monkeypatch.setenv("SEARCH_EXCLUDE_TERMS", "knee,back")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return [
                {"title": "tinnitus case", "url": "https://example.com/1", "snippet": "hearing loss"},
                {"title": "knee rating decision", "url": "https://example.com/2", "snippet": "irrelevant"},
                {"title": "back injury claim", "url": "https://example.com/3", "snippet": ""},
                {"title": "another tinnitus", "url": "https://example.com/4", "snippet": "lower back pain"},
            ]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    results = search_all("tinnitus", max_results=10)

    # result 2 has "knee" in title, result 3 has "back" in title,
    # result 4 has "back" in snippet — only result 1 survives.
    titles = [r["title"] for r in results]
    assert titles == ["tinnitus case"]


def test_search_all_exclude_terms_empty_or_short_does_nothing(monkeypatch):
    """Empty or short-term exclude strings are a no-op."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return [
                {"title": "knee pain", "url": "https://example.com/1", "snippet": ""},
            ]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    # Empty string
    monkeypatch.setenv("SEARCH_EXCLUDE_TERMS", "")
    results1 = search_all("tinnitus", max_results=10)
    assert len(results1) == 1

    # Token shorter than 2 chars (just "k") is ignored.
    monkeypatch.setenv("SEARCH_EXCLUDE_TERMS", "k,a")
    results2 = search_all("tinnitus", max_results=10)
    assert len(results2) == 1


def test_search_all_exclude_terms_case_insensitive(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    monkeypatch.setenv("SEARCH_EXCLUDE_TERMS", "KNEE")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return [
                {"title": "Tinnitus", "url": "https://example.com/1", "snippet": "knee examination was normal"},
            ]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    # "KNEE" matches "knee" in snippet (case-insensitive).
    assert search_all("tinnitus", max_results=10) == []


def test_courtlistener_strips_site_prefixes(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["q"] = params["q"]
        return FakeResponse({"results": [_cl_result()]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    CourtListenerProvider().search("site:uscourts.cavc.gov tinnitus")

    assert "site:" not in captured["q"]
    assert "tinnitus" in captured["q"]


def test_derive_variants_from_topics_and_statutes():
    from va_legal_agent.queries import derive_variants

    variants = derive_variants("site:uscourts.cavc.gov \"service connection\"", limit=3)

    assert variants[0] == "site:uscourts.cavc.gov \"service connection\""
    assert len(variants) == 4
    # Variants broaden the query with topic synonyms / statute fragments.
    assert all(v != variants[0] for v in variants[1:])
    assert all('"' in v for v in variants[1:])


def test_derive_variants_is_issue_aware():
    from va_legal_agent.queries import derive_variants

    # A rating issue should pull rating/evidence phrasing, not service-connection terms.
    variants = derive_variants('site:cafc.uscourts.gov "rating" "Compensation"', limit=10)
    joined = " ".join(variants)

    assert "\"rating\"" in joined
    assert "\"schedular\"" in joined
    # Service-connection-only synonym (nexus) must not leak into a rating search.
    assert "\"nexus\"" not in joined


def test_derive_variants_issue_parsed_from_quoted_query():
    from va_legal_agent.queries import derive_variants

    # The issue is the first quoted phrase; statute fragments are still appended.
    variants = derive_variants('site:uscourts.cavc.gov "benefit of the doubt"', limit=10)
    joined = " ".join(variants)

    assert "\"reasonable doubt\"" in joined  # benefit-of-the-doubt synonym
    assert "\"5107\"" in joined  # statute fragment


def test_derive_variants_unmatched_issue_adds_no_statutes():
    from va_legal_agent.queries import derive_variants

    # An issue matching no topic implicates no statute fragments, so no
    # unrelated 5107/7104 variants are tacked on.
    variants = derive_variants('site:bva.va.gov "tinnitus"', limit=2)

    assert variants == ['site:bva.va.gov "tinnitus"']


def test_expansion_statutes_are_issue_aware():
    from va_legal_agent.queries import _expansion_phrases

    # A service-connection issue pulls its own statutes but not another
    # topic's (benefit-of-the-doubt 5107, rating 4.1).
    service = _expansion_phrases("service connection")
    assert "1110" in service and "1131" in service and "3.303" in service
    assert "5107" not in service and "4.1" not in service

    # A rating issue pulls only its own statute.
    rating = _expansion_phrases("rating")
    assert "4.1" in rating
    assert "5107" not in rating and "1110" not in rating and "3.303" not in rating


def test_derive_variants_limit_zero_returns_original():
    from va_legal_agent.queries import derive_variants

    assert derive_variants("tinnitus", limit=0) == ["tinnitus"]


def test_derive_variants_explicit_issue_overrides_query():
    from va_legal_agent.queries import derive_variants

    variants = derive_variants("unquoted query", limit=1, issue="rating")

    assert "\"evaluation\"" in variants[1]  # rating topic synonym, from explicit issue
    assert "\"rating\"" not in variants[1]  # keyword already in the issue, skipped
    assert "\"nexus\"" not in variants[1]  # unrelated topic not pulled in


def test_issue_from_query_returns_first_quoted_phrase():
    from va_legal_agent.queries import _issue_from_query

    # The issue is the first quoted phrase; the fallback strips site: tokens only
    # when nothing is quoted.
    assert _issue_from_query('site:uscourts.cavc.gov "benefit of the doubt"') == "benefit of the doubt"
    assert _issue_from_query('site:bva.va.gov "tinnitus" "Compensation"') == "tinnitus"


def test_issue_from_query_falls_back_to_stripped_query():
    from va_legal_agent.queries import _issue_from_query

    assert _issue_from_query("site:bva.va.gov tinnitus") == "tinnitus"
    assert _issue_from_query("") == ""


def test_expansion_dedupes_duplicate_statute_fragments(monkeypatch):
    from va_legal_agent.queries import _expansion_phrases

    # Two identical fragments: only the first is added, even though neither is
    # already a topic synonym (so the dedup must happen inside the statute loop).
    monkeypatch.setattr(
        "va_legal_agent.queries.relevant_statutes", lambda topics: ("5107", "5107")
    )

    phrases = _expansion_phrases("rating")

    assert phrases.count("5107") == 1


def test_derive_variants_default_limit_bounds_variant_count():
    from va_legal_agent.queries import derive_variants

    # Default limit is 3: base query plus at most three variants. A rating issue
    # yields at least three expansion phrases, so the count is pinned by limit.
    variants = derive_variants('site:bva.va.gov "rating" "Compensation"')

    assert len(variants) == 4


def test_derive_variants_default_limit_caps_high_phrase_issues():
    from va_legal_agent.queries import derive_variants

    # "service connection" yields six expansion phrases (synonyms + 1110/1131/
    # 3.303), so the default limit of 3 is what actually caps the variant count.
    variants = derive_variants('site:uscourts.cavc.gov "service connection"')

    assert len(variants) == 4


def test_adapt_query_for_provider_strips_site_for_courtlistener():
    from va_legal_agent.queries import adapt_query_for_provider

    query = "site:uscourts.cavc.gov tinnitus"
    assert adapt_query_for_provider(query, "duckduckgo") == query
    assert "site:" not in adapt_query_for_provider(query, "courtlistener")


def test_search_all_adapts_query_per_provider(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo,courtlistener")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    queries_by_provider: dict[str, list[str]] = {"duckduckgo": [], "courtlistener": []}

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            queries_by_provider["duckduckgo"].append(query)
            return []

    class _FakeCL:
        name = "courtlistener"

        def search(self, query, max_results=10, page=1):
            queries_by_provider["courtlistener"].append(query)
            return []

    monkeypatch.setattr(
        "va_legal_agent.providers.get_provider",
        lambda name: {"duckduckgo": _FakeDDG(), "courtlistener": _FakeCL()}[name],
    )

    search_all("site:uscourts.cavc.gov tinnitus", max_results=10)

    assert "site:" in queries_by_provider["duckduckgo"][0]
    assert "site:" not in queries_by_provider["courtlistener"][0]


BVA_HTML = """
<html><body>
<script>
  {&quot;some&quot;:&quot;data&quot;,&quot;resultsData&quot;:{&quot;results&quot;:[
    {&quot;title&quot;:&quot;A25049742.txt&quot;,&quot;url&quot;:&quot;https://www.va.gov/vetapp25/Files6/A25049742.txt&quot;,&quot;description&quot;:&quot;service connection for &lt;strong&gt;tinnitus&lt;/strong&gt; is granted&quot;,&quot;updatedDate&quot;:&quot;June 5th, 2025&quot;,&quot;fileType&quot;:&quot;TXT&quot;},
    {&quot;title&quot;:&quot;A25110452.txt&quot;,&quot;url&quot;:&quot;https://www.va.gov/vetapp25/Files12/A25110452.txt&quot;,&quot;description&quot;:&quot;Entitlement to &lt;strong&gt;service connection&lt;/strong&gt; for tinnitus is granted&quot;,&quot;updatedDate&quot;:&quot;February 27th, 2026&quot;,&quot;fileType&quot;:&quot;TXT&quot;}
  ]},&quot;next&quot;:true}
</script>
</body></html>
"""


def test_bva_provider_parses_results_data(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["params"] = params
        return FakeResponse(BVA_HTML)

    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session", lambda: _FakeSession(fake_get)
    )

    results = BVAProvider().search("tinnitus", max_results=5)

    assert captured["params"]["affiliate"] == "bvadecisions"
    assert captured["params"]["query"] == "tinnitus"
    assert len(results) == 2
    assert results[0]["title"] == "A25049742"
    assert results[0]["url"] == "https://www.va.gov/vetapp25/Files6/A25049742.txt"
    assert results[0]["court"] == "Board of Veterans' Appeals"
    assert results[0]["citation"] == "A25049742"
    assert "<strong>" not in results[0]["snippet"]


def test_bva_provider_strips_site_prefix(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None, **kwargs):
        captured["params"] = params
        return FakeResponse(BVA_HTML)

    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session", lambda: _FakeSession(fake_get)
    )

    BVAProvider().search("site:bva.va.gov tinnitus")

    assert "site:" not in captured["params"]["query"]


def test_minimize_bva_query_broad_recall():
    # The broad recall query puts the issue first in quotes; the claim type
    # and trailing boilerplate are dropped so only the issue phrase is sent.
    assert (
        _minimize_bva_query(
            'site:bva.va.gov "service connection for tinnitus" "Compensation" '
            "veterans compensation"
        )
        == "service connection for tinnitus"
    )


def test_minimize_bva_query_statute_anchor_skipped():
    # Statute-anchored queries put the fragment first; skip it and take the
    # issue that follows.
    assert _minimize_bva_query('"1110" "tinnitus" veterans compensation') == "tinnitus"
    assert _minimize_bva_query('"38 U.S.C. 1110" "tinnitus" veterans') == "tinnitus"
    assert _minimize_bva_query('"3.303" "hearing loss" veterans') == "hearing loss"


def test_minimize_bva_query_unquoted_sheds_boilerplate():
    # The unquoted broad recall is issue + trailing boilerplate; the suffix is
    # stripped so the bare issue remains.
    assert (
        _minimize_bva_query("service connection for tinnitus veterans compensation decision")
        == "service connection for tinnitus"
    )
    assert _minimize_bva_query("tinnitus veterans benefits law") == "tinnitus"
    assert _minimize_bva_query("tinnitus veterans benefits court") == "tinnitus"


def test_minimize_bva_query_unquoted_service_connection_suffix():
    # The CAVC unquoted recall appends "service connection veterans law"; the
    # whole suffix is shed so the issue's own "service connection" survives.
    assert (
        _minimize_bva_query("service connection for tinnitus service connection veterans law")
        == "service connection for tinnitus"
    )


def test_minimize_bva_query_variant_synonym_does_not_replace_issue():
    # derive_variants appends a quoted synonym to an unquoted query; the issue
    # is the leading unquoted text, not the appended phrase.
    assert (
        _minimize_bva_query('service connection for tinnitus veterans compensation decision "hearing loss"')
        == "service connection for tinnitus"
    )


def test_minimize_bva_query_never_empties_on_stopword_issue():
    # An issue that is itself a boilerplate word must not be stripped away.
    assert _minimize_bva_query("compensation veterans law") == "compensation"


def test_minimize_bva_query_short_query_unchanged():
    # A short, unquoted query (what a direct CLI call sends) is already the
    # issue and passes through untouched.
    assert _minimize_bva_query("tinnitus") == "tinnitus"
    assert _minimize_bva_query("service connection tinnitus") == "service connection tinnitus"


def test_minimize_bva_query_statute_only_anchor():
    # A query with only statute anchors (no issue phrase) exhausts the quoted
    # phrases without selecting one and degrades to an empty query rather than
    # sending a statute fragment to the Board index.
    assert _minimize_bva_query('"1110" veterans compensation') == ""


def test_minimize_bva_query_boilerplate_only():
    # A query that is entirely boilerplate (no issue) matches the suffix but
    # the candidate is empty, so the query is never stripped to nothing.
    assert _minimize_bva_query("veterans compensation") == "veterans"


def test_bva_provider_minimizes_query_before_request(monkeypatch):
    # search.usa.gov's WAF challenges long, quote-bearing, boilerplate-laden
    # recall queries, so BVA reduces the query to the issue phrase before
    # sending the request.
    captured = {}

    def fake_get(url, params=None, timeout=None, **kwargs):
        captured["params"] = params
        return FakeResponse(BVA_HTML)

    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session", lambda: _FakeSession(fake_get)
    )

    BVAProvider().search('"service connection for tinnitus" "Compensation" veterans')

    assert '"' not in captured["params"]["query"]
    assert captured["params"]["query"] == "service connection for tinnitus"


def test_bva_provider_raises_on_challenge_page(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session",
        lambda: _FakeSession(
            lambda url, params=None, timeout=None, **kwargs: FakeResponse(
                "<html>anomaly challenge</html>", status_code=202
            )
        ),
    )

    with pytest.raises(SearchError, match="rate-limit/anomaly"):
        BVAProvider().search("tinnitus")


def test_bva_provider_raises_on_http_error(monkeypatch):
    # BVA now goes through curl_cffi, so the fake must raise a cffi
    # RequestException for the provider's except clause to catch it.
    from curl_cffi.requests.exceptions import HTTPError as CffiHTTPError

    class _CffiFakeResponse(FakeResponse):
        def raise_for_status(self):
            if self.status_code >= 400:
                raise CffiHTTPError(f"HTTP {self.status_code}", response=self)

    def fake_get(url, params=None, timeout=None, **kwargs):
        return _CffiFakeResponse({}, status_code=500)

    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session", lambda: _FakeSession(fake_get)
    )

    with pytest.raises(SearchError, match="BVA"):
        BVAProvider().search("tinnitus")


def test_bva_provider_raises_when_no_results(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session",
        lambda: _FakeSession(
            lambda url, params=None, timeout=None, **kwargs: FakeResponse(
                "<html></html>"
            )
        ),
    )

    with pytest.raises(SearchError, match="No search results"):
        BVAProvider().search("tinnitus")


def test_bva_session_impersonates_chrome_and_is_reused(monkeypatch):
    import va_legal_agent.providers as providers

    # Force a fresh singleton so the assertions below test the real lazy init.
    monkeypatch.setattr(providers, "_bva_session", None)

    first = providers._get_bva_session()
    second = providers._get_bva_session()

    # One reused session keeps the AWSALB cookie jar across the whole run,
    # which is what stops search.usa.gov's WAF from treating each query as a
    # brand-new anonymous client.
    assert first is second
    assert getattr(first, "impersonate", None) == "chrome"


def test_bva_impersonation_never_sends_bot_user_agent(monkeypatch):
    """The on-the-wire User-Agent is curl_cffi's Chrome UA, never the bot UA.

    search.usa.gov's WAF challenges the TLS fingerprint of plain HTTP clients,
    so the BVA provider reuses a ``curl_cffi`` Session impersonating Chrome.
    An explicit ``User-Agent`` header would override the impersonated browser
    header and re-trigger the block. Pin this at the header level by capturing
    the real User-Agent a loopback server receives from the un-mocked session.
    """
    import http.server
    import threading

    from curl_cffi import requests as cffi_requests

    # The hermetic fixture clears SEARCH_HTTP_PROXY (the app's own setting),
    # but curl_cffi/libcurl also honors the standard proxy env vars; clear
    # those too so the request reaches the loopback server directly.
    for var in (
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)

    received: dict[str, str | None] = {"user_agent": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            received["user_agent"] = self.headers.get("User-Agent")
            body = b"<html><body></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # keep test output clean

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Redirect the search.usa.gov endpoint to the loopback server and hand
        # the provider a real impersonated session, so the browser handshake
        # is exercised end-to-end (empty resultsData -> SearchError).
        monkeypatch.setattr(
            BVAProvider, "SEARCH_URL", f"http://127.0.0.1:{server.server_port}/search"
        )
        monkeypatch.setattr(
            "va_legal_agent.providers._get_bva_session",
            lambda: cffi_requests.Session(impersonate="chrome"),
        )
        with pytest.raises(SearchError):
            BVAProvider().search("tinnitus")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    ua = received["user_agent"]
    assert ua is not None, "the loopback server received the request"
    # Browser Chrome UA on the wire...
    assert "Chrome" in ua
    # ...and never the app's bot-identifying USER_AGENT, which would re-trigger
    # the WAF/TLS challenge the impersonation exists to bypass.
    assert "VA-Legal-Agent" not in ua


# --- BVA sitemap provider (direct va.gov index, WAF-free) ---

_INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="https://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.va.gov/vetapp26/sitemap.xml</loc><lastmod>2026-08-04</lastmod></sitemap>
  <sitemap><loc>https://www.va.gov/vetapp25/sitemap.xml</loc><lastmod>2026-03-05</lastmod></sitemap>
</sitemapindex>
"""

_LEAF_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="https://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.va.gov/vetapp26/Files1/26000001.txt</loc><lastmod>2026-03-31</lastmod></url>
  <url><loc>https://www.va.gov/vetapp26/Files1/26000002.txt</loc><lastmod>2026-03-31</lastmod></url>
  <url><loc>https://www.va.gov/vetapp26/Files4/A26042155.txt</loc><lastmod>2026-07-01</lastmod></url>
</urlset>
"""


def _clear_bva_sitemap_cache(monkeypatch):
    monkeypatch.setattr("va_legal_agent.providers._bva_sitemap_cache", {})


def _mock_bva_requests(monkeypatch, bodies: dict[str, str]):
    """Route requests.get for the sitemap provider to canned XML/text bodies."""

    def fake_get(url, headers=None, timeout=None, **kwargs):
        for key, body in bodies.items():
            if key in url:
                return FakeResponse(body)
        raise requests.HTTPError(f"unexpected URL {url}")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)


def test_parse_bva_sitemap_index():
    assert _parse_bva_sitemap_index(_INDEX_XML) == [
        ("26", "https://www.va.gov/vetapp26/sitemap.xml"),
        ("25", "https://www.va.gov/vetapp25/sitemap.xml"),
    ]
    # Non-sitemap <loc> entries are ignored.
    assert _parse_bva_sitemap_index("<loc>https://example.com/other</loc>") == []


def test_parse_bva_leaf_sitemap_sorts_newest_first():
    leaf = _parse_bva_leaf_sitemap(_LEAF_XML)
    assert leaf[0] == (
        "https://www.va.gov/vetapp26/Files4/A26042155.txt",
        "2026-07-01",
    )
    assert len(leaf) == 3


def test_parse_bva_leaf_sitemap_sorts_by_lastmod_not_url():
    # A URL that sorts AFTER another lexicographically but carries a LATER
    # lastmod must still win: the key is (lastmod, url), not the raw tuple.
    # (B0000002 > A0000001 lexicographically, but A0000001 has the later date.)
    xml = (
        "<urlset><url><loc>https://www.va.gov/vetapp26/Files1/A0000001.txt</loc>"
        "<lastmod>2026-07-01</lastmod></url>"
        "<url><loc>https://www.va.gov/vetapp26/Files1/B0000002.txt</loc>"
        "<lastmod>2026-03-31</lastmod></url></urlset>"
    )
    leaf = _parse_bva_leaf_sitemap(xml)
    assert leaf[0][0].endswith("A0000001.txt")
    assert leaf[0][1] == "2026-07-01"


def test_parse_bva_leaf_sitemap_newest_first_within_month():
    # Within the same lastmod, the newest decision (higher file number) sorts
    # first by URL tie-break.
    xml = (
        "<urlset><url><loc>https://www.va.gov/vetapp26/Files1/26000001.txt</loc>"
        "<lastmod>2026-03-31</lastmod></url>"
        "<url><loc>https://www.va.gov/vetapp26/Files1/26000002.txt</loc>"
        "<lastmod>2026-03-31</lastmod></url></urlset>"
    )
    leaf = _parse_bva_leaf_sitemap(xml)
    assert leaf[0][0].endswith("26000002.txt")


def test_bva_text_matches_significant_tokens():
    assert _bva_text_matches("the veteran's tinnitus is chronic", "tinnitus")
    assert _bva_text_matches(
        "service connection for tinnitus was granted", "service connection for tinnitus"
    )
    # Case-insensitive, and every significant token must be present.
    assert not _bva_text_matches("hearing loss only", "tinnitus")
    assert not _bva_text_matches("", "tinnitus")
    # A stopword-only issue matches nothing (no significant tokens).
    assert not _bva_text_matches("for the of", "for the of")


def test_bva_sitemap_index_fetched_and_cached(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None, **kwargs):
        calls["n"] += 1
        return FakeResponse(_INDEX_XML)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    assert _bva_sitemap_index() == [
        ("26", "https://www.va.gov/vetapp26/sitemap.xml"),
        ("25", "https://www.va.gov/vetapp25/sitemap.xml"),
    ]
    assert _bva_sitemap_index() == _bva_sitemap_index()  # cached: same object
    assert calls["n"] == 1  # fetched once per process


def test_bva_sitemap_index_raises_on_fetch_error(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)

    def fake_get(url, headers=None, timeout=None, **kwargs):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError, match="sitemap index fetch failed"):
        _bva_sitemap_index()


def test_bva_sitemap_index_sends_user_agent_timeout_and_proxy(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_HTTP_PROXY", "http://proxy.example:8080")
    captured: dict[str, object] = {}

    def fake_get(url, headers=None, timeout=None, **kwargs):
        captured["headers"] = headers
        captured["timeout"] = timeout
        captured["proxies"] = kwargs.get("proxies")
        return FakeResponse(_INDEX_XML)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)
    from va_legal_agent.config import get_settings

    _bva_sitemap_index()

    assert captured["headers"] == {"User-Agent": get_settings().user_agent}
    assert captured["timeout"] == (10, get_settings().request_timeout_seconds)
    assert captured["proxies"] == {
        "http": "http://proxy.example:8080",
        "https": "http://proxy.example:8080",
    }


def test_bva_leaf_sitemap_fetched_and_cached(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None, **kwargs):
        calls["n"] += 1
        return FakeResponse(_INDEX_XML if "sitemap_bva" in url else _LEAF_XML)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    leaf = _bva_leaf_sitemap("26")
    assert leaf and leaf[0][0].endswith("A26042155.txt")
    _bva_leaf_sitemap("26")
    assert calls["n"] == 2  # index + one leaf fetch, then cached


def test_bva_leaf_sitemap_missing_year_returns_empty(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)

    def fake_get(url, headers=None, timeout=None, **kwargs):
        return FakeResponse(_INDEX_XML)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    assert _bva_leaf_sitemap("99") == []


def test_bva_leaf_sitemap_cache_is_per_year(monkeypatch):
    # Different year codes must not share a cache slot: fetching 26 then 25
    # returns each year's own leaf.
    _clear_bva_sitemap_cache(monkeypatch)
    leaf25 = (
        "<urlset><url><loc>https://www.va.gov/vetapp25/Files1/25000001.txt</loc>"
        "<lastmod>2026-03-05</lastmod></url></urlset>"
    )

    def fake_get(url, headers=None, timeout=None, **kwargs):
        if "vetapp25" in url:
            return FakeResponse(leaf25)
        if "vetapp26" in url:
            return FakeResponse(_LEAF_XML)
        return FakeResponse(_INDEX_XML)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    leaf26 = _bva_leaf_sitemap("26")
    leaf25 = _bva_leaf_sitemap("25")
    assert leaf26[0][0].endswith("A26042155.txt")
    assert leaf25[0][0].endswith("25000001.txt")


def test_bva_leaf_sitemap_sends_user_agent_timeout_and_proxy(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_HTTP_PROXY", "http://proxy.example:8080")
    captured: dict[str, object] = {}

    def fake_get(url, headers=None, timeout=None, **kwargs):
        captured["headers"] = headers
        captured["timeout"] = timeout
        captured["proxies"] = kwargs.get("proxies")
        return FakeResponse(_INDEX_XML if "sitemap_bva" in url else _LEAF_XML)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)
    from va_legal_agent.config import get_settings

    _bva_leaf_sitemap("26")

    assert captured["headers"] == {"User-Agent": get_settings().user_agent}
    assert captured["timeout"] == (10, get_settings().request_timeout_seconds)
    assert captured["proxies"] == {
        "http": "http://proxy.example:8080",
        "https": "http://proxy.example:8080",
    }


def test_bva_fetch_decision_text_cached(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None, **kwargs):
        calls["n"] += 1
        return FakeResponse("decision body")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    assert _fetch_bva_decision_text("https://www.va.gov/vetapp26/Files1/26000001.txt") == "decision body"
    assert _fetch_bva_decision_text("https://www.va.gov/vetapp26/Files1/26000001.txt") == "decision body"
    assert calls["n"] == 1


def test_bva_fetch_decision_text_sends_user_agent_timeout_and_proxy(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_HTTP_PROXY", "http://proxy.example:8080")
    captured: dict[str, object] = {}

    def fake_get(url, headers=None, timeout=None, **kwargs):
        captured["headers"] = headers
        captured["timeout"] = timeout
        captured["proxies"] = kwargs.get("proxies")
        return FakeResponse("decision body")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)
    from va_legal_agent.config import get_settings

    _fetch_bva_decision_text("https://www.va.gov/vetapp26/Files1/26000001.txt")

    assert captured["headers"] == {"User-Agent": get_settings().user_agent}
    assert captured["timeout"] == (10, get_settings().request_timeout_seconds)
    assert captured["proxies"] == {
        "http": "http://proxy.example:8080",
        "https": "http://proxy.example:8080",
    }


def test_bva_fetch_decision_text_error_message_mentions_url(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)

    def fake_get(url, headers=None, timeout=None, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError, match="BVA decision fetch failed for https://www.va.gov/vetapp26/Files1/26000001.txt"):
        _fetch_bva_decision_text("https://www.va.gov/vetapp26/Files1/26000001.txt")


def test_bva_sitemap_search_returns_matching_decisions(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    monkeypatch.setenv("SEARCH_BVA_SITEMAP_SCAN_LIMIT", "10")
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": _INDEX_XML,
            "vetapp26/sitemap": _LEAF_XML,
            "26000001.txt": "service connection for tinnitus was granted.",
            "26000002.txt": "hearing loss only.",
            "A26042155.txt": "tinnitus not shown as chronic in service.",
        },
    )

    results = BvaSitemapProvider().search("tinnitus", max_results=5)

    assert len(results) == 2
    titles = {r["title"] for r in results}
    assert titles == {"26000001", "A26042155"}
    assert all(r["court"] == "Board of Veterans' Appeals" for r in results)
    assert all(r["url"].startswith("https://www.va.gov/vetapp26/") for r in results)
    assert all(r["citation"] == r["title"] for r in results)
    # Newest match (A26042155, lastmod 2026-07-01) surfaces first.
    assert results[0]["title"] == "A26042155"
    assert "tinnitus" in results[0]["snippet"]


def test_bva_sitemap_search_raises_when_no_matches(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": _INDEX_XML,
            "vetapp26/sitemap": _LEAF_XML,
            ".txt": "unrelated text about hearing loss.",
        },
    )

    with pytest.raises(SearchError, match="No search results"):
        BvaSitemapProvider().search("tinnitus")


def test_bva_sitemap_search_raises_when_index_unavailable(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")

    def fake_get(url, headers=None, timeout=None, **kwargs):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError, match="sitemap"):
        BvaSitemapProvider().search("tinnitus")


def test_bva_leaf_sitemap_raises_on_fetch_error(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)

    def fake_get(url, headers=None, timeout=None, **kwargs):
        if "sitemap_bva" in url:
            return FakeResponse(_INDEX_XML)
        raise requests.ConnectionError("leaf down")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError, match="leaf sitemap fetch failed"):
        _bva_leaf_sitemap("26")


def test_bva_sitemap_search_raises_when_index_has_no_years(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    # An index that parses to zero year entries leaves nothing to scan.
    empty_index = "<sitemapindex><sitemap><loc>https://example.com/x.xml</loc></sitemap></sitemapindex>"

    def fake_get(url, headers=None, timeout=None, **kwargs):
        return FakeResponse(empty_index)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(
        SearchError,
        match=r"^No BVA decisions indexed; the va\.gov sitemap is unavailable\.$",
    ):
        BvaSitemapProvider().search("tinnitus")


def test_bva_sitemap_search_default_max_results(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    leaf_xml = "".join(
        f"<url><loc>https://www.va.gov/vetapp26/Files1/{i:08d}.txt</loc><lastmod>2026-07-01</lastmod></url>"
        for i in range(1, 12)
    )
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": _INDEX_XML,
            "vetapp26/sitemap": leaf_xml,
            ".txt": "tinnitus tinnitus tinnitus",
        },
    )

    # Default max_results is 10: a window of 11 matches caps at 10.
    results = BvaSitemapProvider().search("tinnitus")
    assert len(results) == 10


def test_bva_sitemap_search_falls_back_when_current_year_absent(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    # Force the provider to look for a year that is not in the index, so the
    # ``newest year present`` fallback path in _most_recent_leaf runs. The
    # index includes 99 (1999) so the fallback must compare year codes
    # numerically — a string max would pick 99 and scan a decade-old year.
    index_xml = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="https://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.va.gov/vetapp99/sitemap.xml</loc><lastmod>2000-01-01</lastmod></sitemap>
  <sitemap><loc>https://www.va.gov/vetapp26/sitemap.xml</loc><lastmod>2026-08-04</lastmod></sitemap>
</sitemapindex>
"""
    monkeypatch.setattr(
        BvaSitemapProvider, "_current_year_code", staticmethod(lambda: "27")
    )
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": index_xml,
            "vetapp26/sitemap": _LEAF_XML,
            "vetapp99/sitemap": _LEAF_XML,
            "26000001.txt": "tinnitus granted.",
            "26000002.txt": "tinnitus denied.",
            "A26042155.txt": "tinnitus remanded.",
        },
    )

    results = BvaSitemapProvider().search("tinnitus")
    assert results and results[0]["title"] == "A26042155"


def test_bva_sitemap_most_recent_leaf_numeric_max_wins_over_lexicographic(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    # Index holds 99 (1999) and 26 (2026); current year 27 is absent. The
    # fallback must pick 26 by numeric comparison — a lexicographic max
    # would pick "99" and scan a decade-old year. The two years return
    # different leaf bodies so the chosen year is observable.
    index_xml = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="https://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.va.gov/vetapp99/sitemap.xml</loc><lastmod>2000-01-01</lastmod></sitemap>
  <sitemap><loc>https://www.va.gov/vetapp26/sitemap.xml</loc><lastmod>2026-08-04</lastmod></sitemap>
</sitemapindex>
"""
    monkeypatch.setattr(
        BvaSitemapProvider, "_current_year_code", staticmethod(lambda: "27")
    )
    leaf26 = (
        "<urlset><url><loc>https://www.va.gov/vetapp26/Files1/26000001.txt</loc>"
        "<lastmod>2026-07-01</lastmod></url></urlset>"
    )
    leaf99 = (
        "<urlset><url><loc>https://www.va.gov/vetapp99/Files1/99000001.txt</loc>"
        "<lastmod>2000-01-01</lastmod></url></urlset>"
    )
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": index_xml,
            "vetapp26/sitemap": leaf26,
            "vetapp99/sitemap": leaf99,
            "26000001.txt": "tinnitus granted.",
        },
    )

    results = BvaSitemapProvider().search("tinnitus")
    assert results and results[0]["title"] == "26000001"


def test_bva_sitemap_year_boundary_maps_92_99_to_19xx(monkeypatch):
    # Codes 92-99 are 1992-1999 and codes 00-91 are 2000-20xx; the fallback
    # must map to the full year before comparing, so a mixed index with 99
    # (1999) and 00 (2000) picks 00 as newest.
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    index_xml = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="https://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.va.gov/vetapp92/sitemap.xml</loc><lastmod>1993-01-01</lastmod></sitemap>
  <sitemap><loc>https://www.va.gov/vetapp99/sitemap.xml</loc><lastmod>2000-01-01</lastmod></sitemap>
  <sitemap><loc>https://www.va.gov/vetapp00/sitemap.xml</loc><lastmod>2000-06-01</lastmod></sitemap>
</sitemapindex>
"""
    monkeypatch.setattr(
        BvaSitemapProvider, "_current_year_code", staticmethod(lambda: "27")
    )
    # 92 (1992), 99 (1999), and 00 (2000): a boundary slip like ``>= 93`` or
    # ``> 92`` would mis-map 92 to 2092 and wrongly pick it as newest, so the
    # match must come from the 00 leaf (2000), not the 92 leaf (1992).
    leaf92 = (
        "<urlset><url><loc>https://www.va.gov/vetapp92/Files1/92000001.txt</loc>"
        "<lastmod>1993-01-01</lastmod></url></urlset>"
    )
    leaf99 = (
        "<urlset><url><loc>https://www.va.gov/vetapp99/Files1/99000001.txt</loc>"
        "<lastmod>2000-01-01</lastmod></url></urlset>"
    )
    leaf00 = (
        "<urlset><url><loc>https://www.va.gov/vetapp00/Files1/00000001.txt</loc>"
        "<lastmod>2000-06-01</lastmod></url></urlset>"
    )
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": index_xml,
            "vetapp92/sitemap": leaf92,
            "vetapp99/sitemap": leaf99,
            "vetapp00/sitemap": leaf00,
            "00000001.txt": "tinnitus granted.",
        },
    )

    results = BvaSitemapProvider().search("tinnitus")
    assert results and results[0]["title"] == "00000001"


def test_bva_sitemap_search_empty_issue_after_minimization(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": _INDEX_XML,
            "vetapp26/sitemap": _LEAF_XML,
            ".txt": "a decision body without issues",
        },
    )

    # The minimize step reduces a statute-anchor-only query to the empty
    # string; search then falls back to the site-stripped query (also empty)
    # and finds no matches, surfacing the standard no-results error.
    with pytest.raises(SearchError, match="No search results"):
        BvaSitemapProvider().search('site:bva.va.gov "1110"')


def test_bva_sitemap_search_uses_minimized_issue_for_matching(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    # A court-recall query minimizes to the issue phrase; matching must use
    # that minimized phrase, not the site-stripped full recall string (which
    # would require every word — claims type, statute — in the text).
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": _INDEX_XML,
            "vetapp26/sitemap": _LEAF_XML,
            ".txt": "service connection for tinnitus was granted.",
        },
    )

    results = BvaSitemapProvider().search(
        'site:bva.va.gov "service connection for tinnitus" "Compensation" '
        "veterans compensation"
    )
    assert results


def test_bva_sitemap_search_minimized_empty_falls_back_to_stripped(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    # "1110" minimizes to the empty string, so search falls back to the
    # site-stripped query — which here still carries the statute text and
    # therefore matches a decision that cites it.
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": _INDEX_XML,
            "vetapp26/sitemap": _LEAF_XML,
            ".txt": "service connection is granted under 38 U.S.C. 1110.",
        },
    )

    results = BvaSitemapProvider().search('"1110"')
    assert results


def test_bva_sitemap_search_non_txt_url_keeps_title(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    leaf_xml = (
        "<urlset><url><loc>https://www.va.gov/vetapp26/Files1/26000001.html</loc>"
        "<lastmod>2026-07-01</lastmod></url></urlset>"
    )
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": _INDEX_XML,
            "vetapp26/sitemap": leaf_xml,
            ".html": "tinnitus granted.",
        },
    )

    results = BvaSitemapProvider().search("tinnitus")
    assert results[0]["title"] == "26000001.html"


def test_bva_snippet_falls_back_when_no_tokens_found():
    from va_legal_agent.providers import _bva_snippet

    # Stopword-only issue: no significant tokens, snippet is the text head.
    assert _bva_snippet("some decision text here", "for the of") == "some decision text here"
    # Token absent from the text (cannot happen via search, but the helper
    # must not raise): same fallback, capped at 280 chars even for long text
    # (a [:281] slip would keep one extra character).
    long_body = "z" * 500
    assert len(_bva_snippet(long_body, "no match here")) == 280
    assert _bva_snippet("no mention at all", "tinnitus") == "no mention at all"


def test_bva_snippet_exact_window_around_token():
    from va_legal_agent.providers import _bva_snippet

    # A controlled text pins the exact windowing arithmetic: the excerpt
    # starts 120 chars before the first token and runs 300 chars past it,
    # whitespace-collapsed and capped at 280 chars. The same text must not
    # produce the raw head (which would mean the token lookup was skipped or
    # the case folding changed).
    # The token sits 200 chars in, so the 120-char back-window starts at 80
    # and the excerpt is a strict slice of the text, not the raw head.
    body = "x" * 200 + "tinnitus the veteran's symptom" + "y" * 200
    idx = body.find("tinnitus")
    expected = " ".join(body[max(0, idx - 120) : idx + 300].split())[:280]
    assert _bva_snippet(body, "tinnitus") == expected
    assert _bva_snippet(body, "tinnitus") != " ".join(body.split())[:280]


def test_bva_snippet_uses_first_occurrence_not_last():
    from va_legal_agent.providers import _bva_snippet

    # Two occurrences: the excerpt anchors on the FIRST token (find), not the
    # last (rfind), so the window covers the first mention's context.
    body = "p" * 50 + "tinnitus first" + "q" * 400 + "tinnitus second" + "r" * 50
    first = body.find("tinnitus")
    expected = " ".join(body[max(0, first - 120) : first + 300].split())[:280]
    assert _bva_snippet(body, "tinnitus") == expected
    assert "tinnitus first" in _bva_snippet(body, "tinnitus")
    assert "tinnitus second" not in _bva_snippet(body, "tinnitus")


def test_bva_snippet_anchors_when_token_at_position_zero():
    from va_legal_agent.providers import _bva_snippet

    # The token is the very first text: idx == 0 must still select the window
    # (>= 0), not fall through to the head fallback (which would require the
    # token at a strictly positive index). The long run of spaces collapses in
    # the window's join but not in the head's, so the two outputs differ.
    body = "tinnitus" + " " * 300 + "tail text beyond the window"
    assert _bva_snippet(body, "tinnitus") == "tinnitus"
    assert _bva_snippet(body, "tinnitus") != " ".join(body.split())[:280]


def test_bva_snippet_window_width_is_exactly_300():
    from va_legal_agent.providers import _bva_snippet

    # The window runs exactly idx + 300 (not + 301): with the token at 50 and
    # a long tail that keeps the joined excerpt under the 280 cap, one extra
    # character changes the excerpt's length, so a width slip is observable.
    body = "x" * 50 + "tinnitus" + " " * 100 + "y" * 300
    idx = body.find("tinnitus")
    expected = " ".join(body[0 : idx + 300].split())
    assert _bva_snippet(body, "tinnitus") == expected
    assert len(_bva_snippet(body, "tinnitus")) == len(" ".join(body[0 : idx + 300].split()))


def test_bva_current_year_code_matches_calendar_year(monkeypatch):
    from datetime import datetime, timezone

    # % 101 would produce a different two-digit code for 2026 (6 vs 26); the
    # code must always equal the UTC calendar year mod 100.
    assert BvaSitemapProvider()._current_year_code() == f"{datetime.now(timezone.utc).year % 100:02d}"


def test_bva_sitemap_search_caps_at_max_results(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    leaf_xml = "".join(
        f"<url><loc>https://www.va.gov/vetapp26/Files1/{i:08d}.txt</loc><lastmod>2026-07-01</lastmod></url>"
        for i in range(1, 11)
    )
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": _INDEX_XML,
            "vetapp26/sitemap": leaf_xml,
            ".txt": "tinnitus is service connected.",
        },
    )

    results = BvaSitemapProvider().search("tinnitus", max_results=3)
    assert len(results) == 3


def test_bva_sitemap_search_skips_failed_fetches(monkeypatch, caplog):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    # Newest file (scanned first) fails to fetch; the scan must continue past
    # it and still surface the later matches.
    def fake_get(url, headers=None, timeout=None, **kwargs):
        if "sitemap_bva" in url:
            return FakeResponse(_INDEX_XML)
        if "vetapp26/sitemap" in url:
            return FakeResponse(_LEAF_XML)
        if "A26042155.txt" in url:
            raise requests.ConnectionError("failed")
        return FakeResponse("tinnitus granted.")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    results = BvaSitemapProvider().search("tinnitus", max_results=5)
    assert [r["title"] for r in results] == ["26000002", "26000001"]
    assert any(
        r.message.startswith(
            "BVA sitemap decision fetch failed for "
            "https://www.va.gov/vetapp26/Files4/A26042155.txt:"
        )
        for r in caplog.records
    )


def test_bva_sitemap_search_uses_scan_limit_window(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    monkeypatch.setenv("SEARCH_BVA_SITEMAP_SCAN_LIMIT", "2")
    leaf_xml = "".join(
        f"<url><loc>https://www.va.gov/vetapp26/Files1/{i:08d}.txt</loc><lastmod>2026-07-01</lastmod></url>"
        for i in range(1, 5)
    )
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": _INDEX_XML,
            "vetapp26/sitemap": leaf_xml,
            ".txt": "tinnitus tinnitus tinnitus",
        },
    )

    provider = BvaSitemapProvider()
    page1 = provider.search("tinnitus", max_results=5, page=1)
    page2 = provider.search("tinnitus", max_results=5, page=2)

    # Window of 2 per page: page 1 sees the two newest files, page 2 the next.
    assert [r["title"] for r in page1] == ["00000004", "00000003"]
    assert [r["title"] for r in page2] == ["00000002", "00000001"]


def test_bva_sitemap_search_scan_limit_floor_of_one(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    # SEARCH_BVA_SITEMAP_SCAN_LIMIT=1 (config's floor) must yield exactly a
    # 1-file window — the provider's own max(scan_limit, 1) floor keeps it at
    # 1 rather than 2, so the newest file is scanned and nothing more.
    monkeypatch.setenv("SEARCH_BVA_SITEMAP_SCAN_LIMIT", "1")
    leaf_xml = "".join(
        f"<url><loc>https://www.va.gov/vetapp26/Files1/{i:08d}.txt</loc><lastmod>2026-07-01</lastmod></url>"
        for i in range(1, 4)
    )
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": _INDEX_XML,
            "vetapp26/sitemap": leaf_xml,
            ".txt": "tinnitus tinnitus tinnitus",
        },
    )

    results = BvaSitemapProvider().search("tinnitus", max_results=5)
    # Exactly the newest file is scanned: 00000003 only.
    assert [r["title"] for r in results] == ["00000003"]


def test_bva_sitemap_search_throttles_with_provider_name(monkeypatch):
    _clear_bva_sitemap_cache(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDERS", "bvasitemap")
    captured: list[object] = []

    def fake_throttle(provider=None):
        captured.append(provider)

    monkeypatch.setattr("va_legal_agent.providers._throttle", fake_throttle)
    _mock_bva_requests(
        monkeypatch,
        {
            "sitemap_bva": _INDEX_XML,
            "vetapp26/sitemap": _LEAF_XML,
            ".txt": "tinnitus granted.",
        },
    )

    BvaSitemapProvider().search("tinnitus")
    # Every decision fetch is paced under the provider's own name so a
    # SEARCH_MAX_RPM_BY_PROVIDER=bvasitemap budget applies.
    assert captured and all(name == "bvasitemap" for name in captured)


def test_bva_sitemap_adapt_query_strips_site_prefix():
    from va_legal_agent.queries import adapt_query_for_provider

    assert adapt_query_for_provider("site:bva.va.gov tinnitus", "bvasitemap") == "tinnitus"


def test_adapt_query_for_provider_strips_site_for_bva():
    from va_legal_agent.queries import adapt_query_for_provider

    query = "site:bva.va.gov tinnitus"
    assert adapt_query_for_provider(query, "bva") == "tinnitus"
    assert "site:" in adapt_query_for_provider(query, "duckduckgo")


def test_search_all_records_telemetry(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo,courtlistener")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return [
                {"title": "A", "url": "https://example.com/a", "snippet": ""},
                {"title": "A dup", "url": "https://example.com/a", "snippet": ""},
            ]

    class _FakeCL:
        name = "courtlistener"

        def search(self, query, max_results=10, page=1):
            raise SearchError("rate limited")

    monkeypatch.setattr(
        "va_legal_agent.providers.get_provider",
        lambda name: {"duckduckgo": _FakeDDG(), "courtlistener": _FakeCL()}[name],
    )

    telemetry: list[dict[str, object]] = []
    results = search_all("tinnitus", max_results=10, telemetry=telemetry)

    assert len(results) == 1  # URL duplicate dropped
    by_provider = {t["provider"]: t for t in telemetry}
    assert by_provider["duckduckgo"]["queries_issued"] == 1
    assert by_provider["duckduckgo"]["results"] == 2
    assert by_provider["duckduckgo"]["deduped"] == 1
    assert by_provider["courtlistener"]["failures"] == 1
    assert by_provider["courtlistener"]["results"] == 0


def test_search_all_telemetry_counts_variants_and_pages(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "2")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "2")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return [{"title": f"{page}", "url": f"https://example.com/{page}", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    telemetry: list[dict[str, object]] = []
    search_all("rating", max_results=10, telemetry=telemetry)

    assert telemetry[0]["queries_issued"] == 6  # 3 variants x 2 pages
    assert telemetry[0]["results"] == 6
    variants = telemetry[0]["variants"]
    assert isinstance(variants, dict)
    assert len(variants) == 3  # one entry per expanded variant
    assert all(v == {"results": 2, "failures": 0} for v in variants.values())  # 2 pages each


def test_search_all_per_provider_variant_override(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo,courtlistener,bva")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "3")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "1")
    # duckduckgo: expansion disabled; courtlistener: limited to 2 extra
    # variants; bva: not listed, falls back to the global of 3.
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS_BY_PROVIDER", "duckduckgo=0,courtlistener=2")

    class _FakeProvider:
        def __init__(self, name, host):
            self.name = name
            self.host = host

        def search(self, query, max_results=10, page=1):
            return [{"title": "Hit", "url": f"https://{self.host}/{len(query)}", "snippet": ""}]

    fakes = {
        "duckduckgo": _FakeProvider("duckduckgo", "a.example"),
        "courtlistener": _FakeProvider("courtlistener", "b.example"),
        "bva": _FakeProvider("bva", "c.example"),
    }
    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: fakes[name])

    telemetry: list[dict[str, object]] = []
    search_all("rating", max_results=10, telemetry=telemetry)

    by_provider = {t["provider"]: t for t in telemetry}
    assert by_provider["duckduckgo"]["queries_issued"] == 1  # base query only
    assert by_provider["courtlistener"]["queries_issued"] == 3  # base + 2 overrides
    assert by_provider["bva"]["queries_issued"] == 4  # base + 3 global


def test_expansion_skips_statute_fragment_already_used_as_synonym(monkeypatch):
    from va_legal_agent.queries import _expansion_phrases

    # "evaluation" is both a rating-topic synonym and (via the patch) a
    # statute fragment; the fragment must be deduped against the synonym.
    monkeypatch.setattr(
        "va_legal_agent.queries.relevant_statutes", lambda topics: ("evaluation",)
    )

    phrases = _expansion_phrases("rating")

    assert "evaluation" in phrases
    assert phrases.count("evaluation") == 1


def test_derive_variants_with_no_phrases_returns_original_only():
    from va_legal_agent.queries import derive_variants

    # "tinnitus" matches no topic and implicates no statute fragments: the
    # expansion loop runs zero times and only the original query is returned.
    assert derive_variants("tinnitus", limit=3) == ["tinnitus"]


def test_search_all_per_provider_pages_override(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo,courtlistener,bva")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")  # isolate pagination
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "2")
    # duckduckgo: 1 page; courtlistener: unlisted, global 2; bva: 3 pages.
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY_BY_PROVIDER", "duckduckgo=1,bva=3")

    class _FakeProvider:
        def __init__(self, name, host):
            self.name = name
            self.host = host

        def search(self, query, max_results=10, page=1):
            return [{"title": "Hit", "url": f"https://{self.host}/{page}", "snippet": ""}]

    fakes = {
        "duckduckgo": _FakeProvider("duckduckgo", "a.example"),
        "courtlistener": _FakeProvider("courtlistener", "b.example"),
        "bva": _FakeProvider("bva", "c.example"),
    }
    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: fakes[name])

    telemetry: list[dict[str, object]] = []
    search_all("tinnitus", max_results=10, telemetry=telemetry)

    by_provider = {t["provider"]: t for t in telemetry}
    assert by_provider["duckduckgo"]["queries_issued"] == 1  # 1 page
    assert by_provider["courtlistener"]["queries_issued"] == 2  # global 2
    assert by_provider["bva"]["queries_issued"] == 3  # override 3


def test_search_all_telemetry_variants_report_failures_and_results(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "3")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "1")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            if "4.1" in query:
                raise SearchError("rate limited")
            return [{"title": "Hit", "url": f"https://example.com/{len(query)}", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    telemetry: list[dict[str, object]] = []
    search_all("rating", max_results=10, telemetry=telemetry)

    variants = telemetry[0]["variants"]
    assert isinstance(variants, dict)
    # Per-variant results/failures reconcile with the provider-level counters.
    assert sum(v["results"] for v in variants.values()) == telemetry[0]["results"]
    assert sum(v["failures"] for v in variants.values()) == telemetry[0]["failures"]
    # The 4.1-anchored variant was throttled; the others surfaced cases.
    assert any(v["failures"] > 0 for v in variants.values())
    assert any(v["results"] > 0 for v in variants.values())


def test_rollup_search_telemetry_merges_variants():
    from va_legal_agent.providers import rollup_search_telemetry

    records = [
        {
            "provider": "duckduckgo",
            "queries_issued": 2, "results": 5, "deduped": 1, "failures": 0,
            "variants": {
                'tinnitus "5107"': {"results": 5, "failures": 0},
                "tinnitus": {"results": 0, "failures": 1},
            },
        },
        {
            "provider": "duckduckgo",
            "queries_issued": 1, "results": 2, "deduped": 0, "failures": 1,
            "variants": {'tinnitus "5107"': {"results": 2, "failures": 1}},
        },
    ]

    rolled = rollup_search_telemetry(records)
    ddg = rolled["duckduckgo"]
    assert ddg["queries_issued"] == 3
    assert ddg["results"] == 7
    assert ddg["variants"] == {
        'tinnitus "5107"': {"results": 7, "failures": 1},
        "tinnitus": {"results": 0, "failures": 1},
    }


def test_search_all_telemetry_thread_safe_under_concurrency(monkeypatch):
    """Concurrent search_all calls sharing one telemetry list lose no records."""
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "1")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            time.sleep(0.001)
            return [
                {"title": "Hit", "url": f"https://example.com/{query}", "snippet": ""}
            ]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    telemetry: list[dict[str, object]] = []
    errors: list[Exception] = []

    def worker(q):
        try:
            search_all(q, max_results=10, telemetry=telemetry)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(worker, [f"query{i}" for i in range(16)]))

    assert errors == []
    assert len(telemetry) == 16  # one record per concurrent search_all call
    rolled = rollup_search_telemetry(telemetry)
    assert rolled["duckduckgo"]["queries_issued"] == 16
    assert rolled["duckduckgo"]["results"] == 16


def test_search_all_telemetry_none_by_default(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return [{"title": "A", "url": "https://example.com/a", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    search_all("tinnitus", max_results=10)  # no telemetry list passed; must not raise


def test_courtlistener_skips_item_without_url(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return FakeResponse({"results": [_cl_result(absolute_url="")]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError, match="No search results"):
        CourtListenerProvider().search("tinnitus")


def test_courtlistener_caps_at_max_results(monkeypatch):
    items = [_cl_result(absolute_url=f"/opinion/{i}/") for i in range(3)]
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse({"results": items}),
    )

    results = CourtListenerProvider().search("tinnitus", max_results=2)

    assert len(results) == 2


def test_courtlistener_raises_when_no_results(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse({"results": []}),
    )

    with pytest.raises(SearchError, match="No search results"):
        CourtListenerProvider().search("tinnitus")


def test_courtlistener_enriches_header_snippet_with_holding_excerpt(monkeypatch):
    """A header-only snippet is enriched with a holding excerpt from the body.

    Without this, a case whose opinion body discusses tinnitus extensively but
    whose snippet is just the court header gets a relevance score of 0 (the
    issue keyword doesn't appear in the header) and is truncated out of results.
    """
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    search_item = _cl_result(
        opinions=[{"snippet": "UNITED STATES COURT OF APPEALS FOR VETERANS CLAIMS\n\nNo. 18-3721", "id": 12345}]
    )
    opinion_detail = _cl_opinion_detail(
        plain_text=(
            "The veteran served in Vietnam. The Board denied service connection "
            "for tinnitus. We hold that the Board erred in its nexus analysis "
            "by failing to consider the medical opinion. 38 U.S.C. § 1110 applies."
        )
    )

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        if "/search/" in url:
            return FakeResponse({"results": [search_item]})
        # Opinion detail endpoint
        return FakeResponse(opinion_detail)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)
    results = CourtListenerProvider().search("tinnitus")

    assert len(results) == 1
    # The snippet is now the holding excerpt, not the header.
    assert "hold" in results[0]["snippet"].lower()
    assert "tinnitus" in results[0]["snippet"].lower()
    assert "COURT OF APPEALS" not in results[0]["snippet"]


def test_courtlistener_keeps_substantive_snippet_unenriched(monkeypatch):
    """A snippet that already has real text is NOT enriched (no wasted API call)."""
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    substantive = "We hold that service connection for tinnitus requires a nexus."
    search_item = _cl_result(
        opinions=[{"snippet": substantive, "id": 12345}]
    )
    opinion_calls: list[str] = []

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        if "/search/" in url:
            return FakeResponse({"results": [search_item]})
        opinion_calls.append(url)  # should never be called
        return FakeResponse(_cl_opinion_detail(plain_text="body"))

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)
    results = CourtListenerProvider().search("tinnitus")

    assert results[0]["snippet"] == substantive
    assert opinion_calls == []


def test_courtlistener_header_enrichment_best_effort_on_failure(monkeypatch):
    """When the opinion detail fetch fails, the header snippet is kept."""
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    header = "UNITED STATES COURT OF APPEALS FOR VETERANS CLAIMS\n\nNo. 18-3721"
    search_item = _cl_result(opinions=[{"snippet": header, "id": 12345}])

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        if "/search/" in url:
            return FakeResponse({"results": [search_item]})
        raise requests.ConnectionError("network down")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)
    results = CourtListenerProvider().search("tinnitus")

    # Header snippet is kept when enrichment fails (best-effort).
    assert results[0]["snippet"] == header


def test_courtlistener_header_enrichment_falls_back_to_first_paragraph(monkeypatch):
    """When the body has no explicit holding, the first substantive paragraph
    is used as the excerpt (covering the paragraph-fallback path)."""
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    header = "UNITED STATES COURT OF APPEALS FOR VETERANS CLAIMS\n\nNo. 18-3721"
    search_item = _cl_result(opinions=[{"snippet": header, "id": 12345}])
    opinion_detail = _cl_opinion_detail(
        plain_text=(
            "UNITED STATES COURT OF APPEALS FOR VETERANS CLAIMS\n\n"
            "No. 18-3721\n\n"
            "The veteran served in Vietnam and developed tinnitus during service. "
            "The Board denied the claim without addressing the medical evidence."
        )
    )

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        if "/search/" in url:
            return FakeResponse({"results": [search_item]})
        return FakeResponse(opinion_detail)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)
    results = CourtListenerProvider().search("tinnitus")

    # The snippet is now the first substantive paragraph, not the header.
    assert "vietnam" in results[0]["snippet"].lower()
    assert "COURT OF APPEALS" not in results[0]["snippet"]


def test_courtlistener_header_enrichment_handles_bad_opinion_id(monkeypatch):
    """A non-numeric opinion id (ValueError) is caught, header kept."""
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    header = "UNITED STATES COURT OF APPEALS FOR VETERANS CLAIMS\n\nNo. 18-3721"
    # Override the mapped opinion_id to a non-numeric string via the
    # opinions[].id field set to a non-int value.
    search_item = _cl_result(opinions=[{"snippet": header, "id": "not-a-number"}])

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        return FakeResponse({"results": [search_item]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)
    results = CourtListenerProvider().search("tinnitus")
    assert results[0]["snippet"] == header


def test_extract_holding_excerpt_holding_not_found_by_find():
    """When extract_holding_sentences finds a holding but body.find doesn't
    (whitespace normalization changes the text), the holding is returned as-is."""
    from va_legal_agent.providers import _extract_holding_excerpt
    # The holding pattern normalizes \s+ to single spaces, so a body with
    # newlines between words produces a holding that body.find can't locate.
    body = "We hold that\n\nthe Board erred in its nexus analysis."
    excerpt = _extract_holding_excerpt(body)
    assert "hold" in excerpt.lower()
    assert "nexus" in excerpt.lower()


def test_extract_holding_excerpt_no_substantive_paragraph():
    """When the body has only header markers and short fragments, returns ''."""
    from va_legal_agent.providers import _extract_holding_excerpt
    body = "UNITED STATES COURT OF APPEALS\n\nNo.\n\nCase: 23-24"
    assert _extract_holding_excerpt(body) == ""


def test_is_header_snippet_detects_court_header():
    from va_legal_agent.providers import _is_header_snippet
    assert _is_header_snippet("") is True
    assert _is_header_snippet("UNITED STATES COURT OF APPEALS\n\nNo. 18-3721") is True
    assert _is_header_snippet("We hold that the Board erred.") is False
    assert _is_header_snippet("Case: 23-24  Page: 1  Document: 40") is True


def test_extract_holding_excerpt_finds_holding():
    from va_legal_agent.providers import _extract_holding_excerpt
    body = (
        "UNITED STATES COURT OF APPEALS FOR VETERANS CLAIMS\n\n"
        "No. 18-3721\n\n"
        "The veteran served in Vietnam. "
        "We hold that the Board erred in its nexus analysis. "
        "38 U.S.C. § 1110 applies."
    )
    excerpt = _extract_holding_excerpt(body)
    assert "hold" in excerpt.lower()
    assert "nexus" in excerpt.lower()


def test_extract_holding_excerpt_falls_back_to_first_paragraph():
    from va_legal_agent.providers import _extract_holding_excerpt
    body = (
        "UNITED STATES COURT OF APPEALS FOR VETERANS CLAIMS\n\nNo. 18-3721\n\n"
        "The veteran served in Vietnam and developed tinnitus during service. "
        "The Board denied the claim without addressing the medical evidence."
    )
    excerpt = _extract_holding_excerpt(body)
    assert "vietnam" in excerpt.lower()
    assert excerpt  # non-empty


def test_extract_holding_excerpt_empty_body():
    from va_legal_agent.providers import _extract_holding_excerpt
    assert _extract_holding_excerpt("") == ""
    assert _extract_holding_excerpt("   ") == ""


def test_bva_sets_page_param_when_paged(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None, **kwargs):
        captured["params"] = params
        return FakeResponse(BVA_HTML)

    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session", lambda: _FakeSession(fake_get)
    )

    BVAProvider().search("tinnitus", page=2)

    assert captured["params"]["page"] == 2


def test_bva_skips_non_dict_and_urlless_items(monkeypatch):
    html = (
        '<html><script>{&quot;resultsData&quot;:{&quot;results&quot;:['
        "null,"
        '{&quot;title&quot;:&quot;A1.txt&quot;,&quot;url&quot;:&quot;&quot;,&quot;description&quot;:&quot;x&quot;},'
        '{&quot;title&quot;:&quot;A2.txt&quot;,&quot;url&quot;:&quot;https://www.va.gov/vetapp25/Files6/A2.txt&quot;,&quot;description&quot;:&quot;y&quot;}'
        "]}}</script></html>"
    )
    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session",
        lambda: _FakeSession(
            lambda url, params=None, timeout=None, **kwargs: FakeResponse(html)
        ),
    )

    results = BVAProvider().search("tinnitus")

    assert len(results) == 1
    assert results[0]["title"] == "A2"


def test_bva_title_falls_back_when_empty(monkeypatch):
    html = (
        '<html><script>{&quot;resultsData&quot;:{&quot;results&quot;:['
        '{&quot;title&quot;:&quot;&quot;,&quot;url&quot;:&quot;https://www.va.gov/vetapp25/Files6/A3.txt&quot;,&quot;description&quot;:&quot;z&quot;}'
        "]}}</script></html>"
    )
    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session",
        lambda: _FakeSession(
            lambda url, params=None, timeout=None, **kwargs: FakeResponse(html)
        ),
    )

    results = BVAProvider().search("tinnitus")

    assert results[0]["title"] == "Board of Veterans' Appeals decision"


def test_bva_caps_at_max_results(monkeypatch):
    items = ",".join(
        '{&quot;title&quot;:&quot;A%d.txt&quot;,&quot;url&quot;:&quot;https://www.va.gov/vetapp25/Files6/A%d.txt&quot;}' % (i, i)
        for i in range(1, 4)
    )
    html = f'<html><script>{{&quot;resultsData&quot;:{{&quot;results&quot;:[{items}]}}}}</script></html>'
    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session",
        lambda: _FakeSession(
            lambda url, params=None, timeout=None, **kwargs: FakeResponse(html)
        ),
    )

    results = BVAProvider().search("tinnitus", max_results=2)

    assert len(results) == 2


def test_bva_malformed_results_data_json(monkeypatch):
    html = '<html><script>{&quot;resultsData&quot;:{broken json</script></html>'
    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session",
        lambda: _FakeSession(
            lambda url, params=None, timeout=None, **kwargs: FakeResponse(html)
        ),
    )

    with pytest.raises(SearchError, match="No search results"):
        BVAProvider().search("tinnitus")


def test_rollup_skips_records_without_provider_and_bad_variants():
    from va_legal_agent.providers import rollup_search_telemetry

    assert rollup_search_telemetry([{"provider": "", "queries_issued": 5, "results": 3}]) == {}
    rolled = rollup_search_telemetry(
        [
            {
                "provider": "duckduckgo",
                "queries_issued": 1,
                "results": 1,
                "variants": {"v": "not-a-dict", "w": {"results": 1, "failures": 0}},
            }
        ]
    )
    assert rolled["duckduckgo"]["variants"] == {"w": {"results": 1, "failures": 0}}


def test_search_all_skips_provider_that_raises_on_instantiation(monkeypatch, caplog):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    def fake_get_provider(name):
        raise ValueError("Unknown search provider: 'duckduckgo'")

    monkeypatch.setattr("va_legal_agent.providers.get_provider", fake_get_provider)

    assert search_all("tinnitus") == []
    assert any("skipping" in r.message for r in caplog.records)


def test_search_all_appends_telemetry_on_early_cap_return(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            return [
                {"title": f"R{i}", "url": f"https://example.com/{i}", "snippet": ""}
                for i in range(3)
            ]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    telemetry: list[dict[str, object]] = []
    results = search_all("tinnitus", max_results=2, telemetry=telemetry)

    assert len(results) == 2  # capped at max_results
    assert len(telemetry) == 1  # record still appended on the early return
    assert telemetry[0]["results"] == 3
    assert telemetry[0]["queries_issued"] == 1


def test_courtlistener_sends_exact_request_args(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    captured: dict[str, object] = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers, timeout=timeout)
        return FakeResponse({"results": [_cl_result()]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    CourtListenerProvider().search("tinnitus", max_results=1)
    assert captured["url"] == CourtListenerProvider.SEARCH_URL
    assert captured["params"]["q"] == (  # type: ignore[index]
        "(tinnitus) AND court_id:(cavc OR cafc OR scotus)"
    )
    assert "court" not in captured["params"]  # type: ignore[operator]
    assert captured["params"]["format"] == "json"  # type: ignore[index]
    assert "page" not in captured["params"]  # type: ignore[operator]
    assert "User-Agent" in captured["headers"]  # type: ignore[operator]
    assert captured["timeout"] is not None

    # max_results is applied client-side; the search endpoint returns up to 20
    # per page regardless, so no page_size is forwarded.
    CourtListenerProvider().search("tinnitus")
    assert "page_size" not in captured["params"]  # type: ignore[operator]
    CourtListenerProvider().search("tinnitus", max_results=150)
    assert "page_size" not in captured["params"]  # type: ignore[operator]


def test_courtlistener_follows_cursor_for_page_above_one(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    calls: list[tuple[str, object]] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params))
        if len(calls) == 1:
            return FakeResponse(
                {"results": [_cl_result(absolute_url="/opinion/1/")], "next": "https://cl/next"}
            )
        return FakeResponse({"results": [_cl_result(absolute_url="/opinion/2/")]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    results = CourtListenerProvider().search("tinnitus", page=2)

    assert [r["url"] for r in results] == ["https://www.courtlistener.com/opinion/2/"]
    assert calls[1][0] == "https://cl/next"  # cursor URL followed verbatim, no params
    assert calls[1][1] is None


def test_courtlistener_page_beyond_last_returns_empty(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")

    # The first response has results but no ``next`` cursor: requesting page 2
    # must return [] (the cursor chain is exhausted), not crash.
    def fake_get(url, params=None, headers=None, timeout=None):
        return FakeResponse({"results": [_cl_result()]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    assert CourtListenerProvider().search("tinnitus", page=2) == []


def test_courtlistener_maps_dict_form_citation(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    # The mapper tolerates a cluster-style dict-form citation entry too, not
    # just the string list the search endpoint normally returns.
    items = [_cl_result(citation=[{"cite": "35 Vet.App. 123"}])]
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse({"results": items}),
    )

    results = CourtListenerProvider().search("tinnitus")

    assert results[0]["citation"] == "35 Vet.App. 123"


def test_courtlistener_dict_citation_missing_cite_falls_back(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    # A dict-form citation without the "cite" key must degrade to "", not to
    # the string "None" or a sentinel.
    items = [_cl_result(citation=[{"reporter": "x"}])]
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse({"results": items}),
    )

    results = CourtListenerProvider().search("tinnitus")

    assert results[0]["citation"] == ""


def test_courtlistener_search_missing_results_key_raises(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    # A 200 response that omits the "results" key entirely must degrade to
    # "no results", not crash iterating None.
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse({"count": 0}),
    )

    with pytest.raises(SearchError, match="No search results"):
        CourtListenerProvider().search("tinnitus")


def test_courtlistener_search_response_less_exception(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    # A transport-level failure (no .response attribute) must surface as a
    # SearchError, not an AttributeError from the 401-detection guard.
    def fake_get(url, params=None, headers=None, timeout=None):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError, match="search failed"):
        CourtListenerProvider().search("tinnitus")


def test_courtlistener_default_max_results_caps_client_side(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    items = [_cl_result(absolute_url=f"/opinion/{i}/") for i in range(12)]
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse({"results": items}),
    )

    results = CourtListenerProvider().search("tinnitus")

    assert len(results) == 10  # default max_results is applied client-side


def test_courtlistener_fallback_fields_and_unknown_court(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    items = [
        {  # everything missing: all fallbacks and the unknown-court mapping
            "absolute_url": "/opinion/9/",
            "court_id": "not-a-court",
            "citation": [],
        },
        {  # citation list present but with an unparseable entry
            "absolute_url": "/opinion/10/",
            "court_id": "cavc",
            "citation": [123],
        },
    ]
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse({"results": items}),
    )

    results = CourtListenerProvider().search("tinnitus")

    assert results[0]["title"] == "Untitled case"
    assert results[0]["court"] == COURT_UNKNOWN
    assert results[0]["citation"] == ""
    assert results[0]["decision_date"] == ""
    assert results[0]["docket"] == ""
    assert results[0]["judge"] == ""
    assert results[0]["snippet"] == ""
    assert results[1]["citation"] == ""


def test_courtlistener_continues_past_url_less_item(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    items = [
        {"caseName": "No URL", "court_id": "cavc"},  # no absolute_url -> skipped
        _cl_result(),
    ]
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse({"results": items}),
    )

    results = CourtListenerProvider().search("tinnitus")

    assert len(results) == 1
    assert results[0]["title"] == "Smith v. McDonough"


def test_courtlistener_401_with_token_is_generic_error(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_KEY", "secret-token")

    def fake_get(url, params=None, headers=None, timeout=None):
        raise requests.HTTPError("401", response=FakeResponse({}, status_code=401))

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError) as exc:
        CourtListenerProvider().search("tinnitus")

    assert "COURTLISTENER_API_KEY" not in str(exc.value)


def _usage_payload(daily_used=0, daily_limit=125, daily_reset="2026-08-18T05:12:28+00:00"):
    """A realistic api-usage payload: one row per scope/rate pair."""
    return {
        "current_usage": [
            {
                "scope": "user",
                "rate": "125/day",
                "used": daily_used,
                "limit": daily_limit,
                "remaining": max(daily_limit - daily_used, 0),
                "window_seconds": 86400,
                "reset_at": daily_reset,
                "blocked": False,
            },
            {
                "scope": "user",
                "rate": "5/min",
                "used": 0,
                "limit": 5,
                "remaining": 5,
                "window_seconds": 60,
                "reset_at": None,
                "blocked": False,
            },
            {
                "scope": "user",
                "rate": "50/hour",
                "used": 28,
                "limit": 50,
                "remaining": 22,
                "window_seconds": 3600,
                "reset_at": None,
                "blocked": False,
            },
        ],
        "historical_usage": {"2026-08-17": 75, "total": 123},
        "membership": None,
    }


def test_fetch_courtlistener_usage_returns_payload(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_KEY", "secret-token")
    seen: list[str] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append(url)
        return FakeResponse(_usage_payload())

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    payload = fetch_courtlistener_usage()

    assert payload["membership"] is None
    assert len(payload["current_usage"]) == 3
    # The usage endpoint URL is the one queried (never a None/different host).
    assert seen == [USAGE_URL]


def test_fetch_courtlistener_usage_without_token_gets_actionable_hint(monkeypatch):
    monkeypatch.delenv("COURTLISTENER_API_KEY", raising=False)

    def fake_get(url, params=None, headers=None, timeout=None):
        raise requests.HTTPError("401 Unauthorized", response=FakeResponse({}, status_code=401))

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError) as exc:
        fetch_courtlistener_usage()
    # The actionable no-token hint is re-raised verbatim, not wrapped by the
    # usage wrapper (the wrapper must not obscure the "set the key" message).
    assert "COURTLISTENER_API_KEY" in str(exc.value)
    assert "Could not check CourtListener API usage" not in str(exc.value)


def test_fetch_courtlistener_usage_wraps_other_failures(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_KEY", "secret-token")

    def fake_get(url, params=None, headers=None, timeout=None):
        raise requests.HTTPError("500", response=FakeResponse({}, status_code=500))

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError, match="Could not check CourtListener API usage"):
        fetch_courtlistener_usage()


def test_courtlistener_daily_budget_picks_user_day_row():
    budget = courtlistener_daily_budget(_usage_payload(daily_used=63))

    assert budget["used"] == 63
    assert budget["limit"] == 125
    assert budget["remaining"] == 62
    assert budget["reset_at"] == "2026-08-18T05:12:28+00:00"


def test_courtlistener_daily_budget_skips_non_user_and_non_dict_rows():
    # The payload can mix non-dict entries (defensive skip) and other scopes
    # (e.g. the citation-lookup or api-usage rows); only the user /day row
    # counts as the daily budget.
    payload = {
        "current_usage": [
            "not-a-dict",
            {"scope": "citations", "rate": "60/min", "used": 12, "limit": 60},
            {"scope": "user", "rate": "5/min", "used": 2, "limit": 5},
            {"scope": "user", "rate": "125/day", "used": 63, "limit": 125,
             "remaining": 62, "reset_at": "2026-08-18T05:12:28+00:00"},
        ]
    }

    budget = courtlistener_daily_budget(payload)

    assert budget["used"] == 63
    assert budget["remaining"] == 62


def test_courtlistener_daily_budget_missing_rows_raises():
    with pytest.raises(SearchError) as exc:
        courtlistener_daily_budget({"current_usage": []})
    assert str(exc.value) == (
        "Could not find the CourtListener daily request budget in the api-usage response."
    )
    with pytest.raises(SearchError) as exc:
        courtlistener_daily_budget({})
    assert str(exc.value) == "CourtListener api-usage response had no current_usage list."


def test_courtlistener_daily_budget_missing_keys_default_to_zero():
    # A day row that omits the counters degrades to 0/0/0 rather than to a
    # wrong non-zero figure (a `1` fallback would overstate usage/limits and
    # abort a healthy run, or understate remaining headroom).
    payload = {
        "current_usage": [
            {"scope": "user", "rate": "125/day", "reset_at": "2026-08-18T05:12:28+00:00"}
        ]
    }

    budget = courtlistener_daily_budget(payload)

    assert budget == {
        "used": 0,
        "limit": 0,
        "remaining": 0,
        "reset_at": "2026-08-18T05:12:28+00:00",
    }


def test_check_courtlistener_daily_budget_aborts_when_exhausted(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=125),  # 0 remaining
    )

    with pytest.raises(SearchError) as exc:
        check_courtlistener_daily_budget(8)

    message = str(exc.value)
    assert "0 remaining" in message
    assert "need 8" in message
    assert "used 125/125" in message
    assert "2026-08-18T05:12:28+00:00" in message
    # Exact tails (not substrings) so message-wrapping/uppercasing mutants die.
    assert message.endswith("COURTLISTENER_USAGE_GUARD=0.")
    assert "today, or disable this guard" in message


def test_check_courtlistener_daily_budget_abort_reports_unknown_reset(monkeypatch):
    # When the API omits reset_at, the message says so instead of a bare None.
    payload = _usage_payload(daily_used=125, daily_reset=None)
    monkeypatch.setattr(
        "va_legal_agent.providers.fetch_courtlistener_usage", lambda: payload
    )

    with pytest.raises(SearchError) as exc:
        check_courtlistener_daily_budget(1)

    # The exact phrase (not just "unknown time") so XX-wrapping or
    # uppercasing the fallback dies.
    assert "resets at unknown time." in str(exc.value)


def test_check_courtlistener_daily_budget_passes_with_headroom(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=50),  # 75 remaining
    )

    budget = check_courtlistener_daily_budget(8)

    assert budget["remaining"] == 75


def test_check_courtlistener_daily_budget_boundary_remaining_equals_need(monkeypatch):
    # remaining == need is enough (the guard is about covering the run, not
    # leaving spare quota); a would-be off-by-one abort is pinned here.
    monkeypatch.setattr(
        "va_legal_agent.providers.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=117),  # 8 remaining
    )

    budget = check_courtlistener_daily_budget(8)

    assert budget["remaining"] == 8


def test_courtlistener_minute_budget_picks_user_min_row():
    budget = courtlistener_minute_budget(_usage_payload())

    assert budget["used"] == 0
    assert budget["limit"] == 5
    assert budget["remaining"] == 5


def test_courtlistener_minute_budget_returns_empty_when_absent():
    # A payload with no minute row (e.g. a different tier) returns {} instead
    # of raising — higher tiers may have wider windows we cannot read.
    payload = {
        "current_usage": [
            {"scope": "user", "rate": "125/day", "used": 10, "limit": 125, "remaining": 115},
        ]
    }
    assert courtlistener_minute_budget(payload) == {}


def test_check_courtlistener_minute_budget_aborts_when_exhausted(monkeypatch):
    # Minute window: 5 used of 5, 0 remaining — guard must abort.
    payload = _usage_payload()
    payload["current_usage"][1]["used"] = 5
    payload["current_usage"][1]["remaining"] = 0
    monkeypatch.setattr(
        "va_legal_agent.providers.fetch_courtlistener_usage", lambda: payload
    )

    with pytest.raises(SearchError) as exc:
        check_courtlistener_minute_budget(3)

    message = str(exc.value)
    assert "0 remaining" in message
    assert "need 3" in message
    assert "used 5/5" in message
    assert "SEARCH_DELAY_SECONDS" in message
    assert message.endswith("COURTLISTENER_USAGE_GUARD=0.")


def test_check_courtlistener_minute_budget_passes_with_headroom(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers.fetch_courtlistener_usage",
        lambda: _usage_payload(),  # 5 remaining
    )

    budget = check_courtlistener_minute_budget(3)

    assert budget["remaining"] == 5


def test_check_courtlistener_minute_budget_skips_when_no_minute_row(monkeypatch):
    payload = {
        "current_usage": [
            {"scope": "user", "rate": "125/day", "used": 10, "limit": 125, "remaining": 115},
        ]
    }
    monkeypatch.setattr(
        "va_legal_agent.providers.fetch_courtlistener_usage", lambda: payload
    )

    budget = check_courtlistener_minute_budget(6)

    assert budget == {}  # skipped, not aborted


def test_check_courtlistener_minute_budget_boundary_remaining_equals_need(monkeypatch):
    # remaining == need is enough; off-by-one must not abort.
    payload = _usage_payload()
    payload["current_usage"][1]["used"] = 2
    payload["current_usage"][1]["remaining"] = 3
    monkeypatch.setattr(
        "va_legal_agent.providers.fetch_courtlistener_usage", lambda: payload
    )

    budget = check_courtlistener_minute_budget(3)

    assert budget["remaining"] == 3


def test_parse_retry_after_forms():
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after("  120 ") == 120.0
    assert _parse_retry_after("garbage") is None
    # A past HTTP-date (aware and naive forms) clamps to zero.
    assert _parse_retry_after("Thu, 01 Jan 1970 00:00:00 GMT") == 0.0
    assert _parse_retry_after("Thu, 01 Jan 1970 00:00:00") == 0.0
    # A near-future HTTP-date yields its delta seconds.
    future = email.utils.formatdate(time.time() + 60, usegmt=True)
    assert 55 <= _parse_retry_after(future) <= 61


def test_courtlistener_retries_429_with_retry_after_then_succeeds(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "2")
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.providers.time.sleep", sleeps.append)
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse({}, status_code=429, headers={"Retry-After": "5"})
        return FakeResponse({"results": [_cl_result()]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    results = CourtListenerProvider().search("tinnitus")

    assert calls["n"] == 2  # one throttle, then success
    assert sleeps == [5.0]  # Retry-After (5s) beats the exponential floor
    assert results[0]["title"] == "Smith v. McDonough"


def test_courtlistener_retries_429_without_retry_after_header(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "1")
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.providers.time.sleep", sleeps.append)
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse({}, status_code=429)  # no Retry-After header
        return FakeResponse({"results": [_cl_result()]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    results = CourtListenerProvider().search("tinnitus")

    assert calls["n"] == 2
    assert len(sleeps) == 1
    assert 0.75 <= sleeps[0] <= 1.25  # exponential floor with jitter
    assert results[0]["title"] == "Smith v. McDonough"


def test_courtlistener_retries_response_less_error_then_succeeds(monkeypatch):
    # A transient ConnectionError has no .response attribute, so the Retry-After
    # lookup must fall back to the exponential delay instead of dereferencing
    # None. This pins the None-response path of _courtlistener_retry_delay.
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "1")
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.providers.time.sleep", sleeps.append)
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("boom")
        return FakeResponse({"results": [_cl_result()]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    results = CourtListenerProvider().search("tinnitus")

    assert calls["n"] == 2
    assert len(sleeps) == 1
    assert 0.75 <= sleeps[0] <= 1.25  # no response, so no Retry-After
    assert results[0]["title"] == "Smith v. McDonough"


def test_courtlistener_exhausts_retries_and_raises_last_error(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "2")
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.providers.time.sleep", sleeps.append)
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        return FakeResponse({}, status_code=429, headers={"Retry-After": "1"})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError, match="search failed"):
        CourtListenerProvider().search("tinnitus")

    assert calls["n"] == 3  # initial attempt + SEARCH_RETRY_ATTEMPTS retries
    assert len(sleeps) == 2


def test_courtlistener_retry_after_capped_at_backoff_max(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("SEARCH_BACKOFF_MAX_SECONDS", "3")
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.providers.time.sleep", sleeps.append)
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse({}, status_code=429, headers={"Retry-After": "999"})
        return FakeResponse({"results": [_cl_result()]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    CourtListenerProvider().search("tinnitus")

    assert calls["n"] == 2
    assert sleeps == [3.0]  # server asked 999s; capped at the configured max


def test_courtlistener_retry_after_http_date_is_honored(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("SEARCH_BACKOFF_MAX_SECONDS", "60")
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.providers.time.sleep", sleeps.append)
    calls = {"n": 0}
    future = email.utils.formatdate(time.time() + 30, usegmt=True)

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse({}, status_code=429, headers={"Retry-After": future})
        return FakeResponse({"results": [_cl_result()]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    CourtListenerProvider().search("tinnitus")

    assert calls["n"] == 2
    assert 25 <= sleeps[0] <= 31  # ~30s from the HTTP-date


def test_courtlistener_throttles_every_attempt(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "1")
    monkeypatch.setattr("va_legal_agent.providers.time.sleep", lambda s: None)
    throttles: list[int] = []
    throttled_providers: list[str | None] = []

    def record_throttle(provider=None):
        throttles.append(1)
        throttled_providers.append(provider)

    monkeypatch.setattr("va_legal_agent.providers._throttle", record_throttle)
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse({}, status_code=429, headers={"Retry-After": "1"})
        return FakeResponse({"results": [_cl_result()]})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    CourtListenerProvider().search("tinnitus")

    assert calls["n"] == 2
    assert throttles == [1, 1]  # paced before each of the two attempts
    assert throttled_providers == ["courtlistener", "courtlistener"]


def test_bva_provider_sends_exact_request_args(monkeypatch):
    captured: dict[str, object] = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        captured.update(url=url, params=params, headers=headers, timeout=timeout)
        return FakeResponse(BVA_HTML)

    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session", lambda: _FakeSession(fake_get)
    )

    BVAProvider().search("tinnitus")  # default page=1
    assert captured["url"] == BVAProvider.SEARCH_URL
    assert captured["params"]["affiliate"] == "bvadecisions"  # type: ignore[index]
    assert captured["params"]["query"] == "tinnitus"  # type: ignore[index]
    assert "page" not in captured["params"]  # type: ignore[operator]
    # The session impersonation supplies the browser User-Agent; the request
    # must not override it with the app's own bot-identifying UA.
    assert captured["headers"] is None
    assert captured["timeout"] is not None

    BVAProvider().search("tinnitus", page=2)
    assert captured["params"]["page"] == 2  # type: ignore[index]


def test_bva_provider_throttles_before_request(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session",
        lambda: _FakeSession(lambda url, **kwargs: FakeResponse(BVA_HTML)),
    )
    throttled_providers: list[str | None] = []
    monkeypatch.setattr(
        "va_legal_agent.providers._throttle",
        lambda provider=None: throttled_providers.append(provider),
    )

    BVAProvider().search("tinnitus")

    assert throttled_providers == ["bva"]


def test_bva_provider_detects_202_challenge_without_anomaly_text(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session",
        lambda: _FakeSession(
            lambda url, params=None, timeout=None, **kwargs: FakeResponse(
                "<html>slow down</html>", status_code=202
            )
        ),
    )

    with pytest.raises(SearchError) as exc:
        BVAProvider().search("tinnitus")
    assert str(exc.value) == (
        "BVA search returned a rate-limit/anomaly challenge page for query: tinnitus. "
        "Slow down requests or raise SEARCH_DELAY_SECONDS."
    )


def test_bva_provider_detects_mixed_case_anomaly_text(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session",
        lambda: _FakeSession(
            lambda url, params=None, timeout=None, **kwargs: FakeResponse(
                "<html>Anomaly challenge</html>", status_code=200
            )
        ),
    )

    with pytest.raises(SearchError, match="rate-limit/anomaly"):
        BVAProvider().search("tinnitus")


def test_bva_provider_sanitizes_descriptions_and_fallbacks(monkeypatch):
    items = [
        {
            "url": "https://www.va.gov/vetapp25/Files1/A25000001.txt",
            "title": "A25000001.txt",
            "description": "<strong>Lost</strong> hearing",
        },
        {
            "url": "https://www.va.gov/vetapp25/Files1/A25000002.txt",
            "title": "A25000002.txt",
        },
        {
            "url": "https://www.va.gov/vetapp25/Files1/A25000003.txt",
            "title": "",
        },
    ]
    html = f'<html><script>"resultsData":{json.dumps({"results": items})}</script></html>'
    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session",
        lambda: _FakeSession(
            lambda url, params=None, timeout=None, **kwargs: FakeResponse(html)
        ),
    )

    results = BVAProvider().search("tinnitus", max_results=10)

    assert results[0]["snippet"] == "Lost hearing"  # tags stripped, text kept
    assert results[0]["title"] == "A25000001"
    assert results[1]["snippet"] == ""  # no description key -> empty snippet
    assert results[2]["title"] == "Board of Veterans' Appeals decision"


def test_bva_parse_results_data_edge_cases():
    key = BVAProvider._RESULTS_KEY
    payload = '{"results": [{"url": "u", "title": "t"}]}'
    # Key at index 0 is still parsed (start == 0 is a valid position).
    assert len(BVAProvider._parse_results_data(f'{key}{payload}')) == 1
    # Leading whitespace after the key is tolerated (lstrip before decode).
    assert len(BVAProvider._parse_results_data(f'{key} {payload}')) == 1
    # First occurrence wins; trailing junk after the first object is ignored.
    parsed = BVAProvider._parse_results_data(f'{key}{payload} trailing {key}not-json')
    assert len(parsed) == 1
    # A resultsData object without a "results" key yields an empty list.
    assert BVAProvider._parse_results_data(f'{key}{{"total": 3}}') == []
    # A non-object resultsData payload (list/scalar) degrades to empty too.
    assert BVAProvider._parse_results_data(f'{key}[1, 2, 3]') == []
    assert BVAProvider._parse_results_data(f'{key}"just a string"') == []


def test_resolve_search_providers_splits_comma_list():
    assert resolve_search_providers("duckduckgo,courtlistener") == ["duckduckgo", "courtlistener"]


def test_search_all_counts_failures_across_pages(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "2")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            raise SearchError("provider down")

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    telemetry: list[dict[str, object]] = []
    with pytest.raises(SearchError):
        search_all("tinnitus", telemetry=telemetry)

    assert telemetry[0]["failures"] == 2  # both pages attempted, both failed
    assert telemetry[0]["variants"]["tinnitus"]["failures"] == 2  # type: ignore[index]


def test_search_all_exhausts_every_query_and_propagates_last_error(monkeypatch):
    """When every query x page fails, the LAST error propagates.

    Mirrors the search-layer exhaustion contract: no early bail-out (every
    variant is attempted on every page), the raised error is the last one
    (errors[-1]), and the provider-level failure count reconciles with the
    per-variant breakdown across pages.
    """
    from va_legal_agent.queries import derive_variants

    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "2")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "2")
    received: list[str] = []

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            received.append(query)
            raise SearchError(f"down for: {query}")

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    telemetry: list[dict[str, object]] = []
    with pytest.raises(SearchError) as excinfo:
        search_all("rating", telemetry=telemetry)

    # Exhaustion: every variant is attempted on every page - no early return.
    variants = derive_variants("rating", limit=2)
    assert len(received) == len(variants) * 2
    # The error that propagates is the LAST one, not the first.
    assert str(excinfo.value) == f"down for: {received[-1]}"
    assert str(excinfo.value) != f"down for: {received[0]}"
    # Per-page failure counting: provider-level failures == every attempt, and
    # the per-variant breakdown reconciles with it.
    assert telemetry[0]["failures"] == len(received)
    variants_stats = telemetry[0]["variants"]
    assert isinstance(variants_stats, dict)
    assert sum(v["failures"] for v in variants_stats.values()) == len(received)
    assert all(v["failures"] == 2 for v in variants_stats.values())  # 2 pages each


def test_search_all_aborts_when_deadline_passed(monkeypatch):
    """search_all stops starting new provider calls once the deadline is crossed."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "2")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "2")
    clock = {"now": 100.0}
    calls = {"n": 0}
    monkeypatch.setattr("va_legal_agent.providers.time.monotonic", lambda: clock["now"])

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            calls["n"] += 1
            clock["now"] = 200.0  # the first provider call burns the whole budget
            raise SearchError("down")

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    with pytest.raises(SearchError, match="wall-time budget"):
        search_all("tinnitus", deadline=clock["now"] + 1.0)

    # No further provider calls after the deadline: the remaining variants/pages
    # of the query are abandoned instead of running to exhaustion.
    assert calls["n"] == 1


def test_search_all_aborts_exactly_at_deadline(monkeypatch):
    """The deadline check fires when the clock is exactly at the deadline."""
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "1")
    monkeypatch.setattr("va_legal_agent.providers.time.monotonic", lambda: 100.0)
    calls = {"n": 0}

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            calls["n"] += 1
            return [{"title": "t", "url": "https://example.com/t", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    with pytest.raises(SearchError, match="wall-time budget"):
        search_all("tinnitus", deadline=100.0)  # deadline == now

    assert calls["n"] == 0  # >= deadline aborts before the first provider call


def test_search_all_default_max_results(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    seen: dict[str, int] = {}

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            seen["max_results"] = max_results
            return [{"title": "x", "url": "https://example.com/x", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    search_all("tinnitus")

    assert seen["max_results"] == 10


def test_search_all_continues_past_instantiation_failure(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo,bva")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")

    class _FakeBVA:
        name = "bva"

        def search(self, query, max_results=10, page=1):
            return [{"title": "t", "url": "https://example.com/t", "snippet": ""}]

    def fake_get_provider(name):
        if name == "duckduckgo":
            raise ValueError("Unknown search provider: 'duckduckgo'")
        return _FakeBVA()

    monkeypatch.setattr("va_legal_agent.providers.get_provider", fake_get_provider)

    results = search_all("tinnitus")

    assert len(results) == 1  # the failing provider is skipped, bva still runs


def test_rollup_skips_providerless_records_and_defaults_counts():
    records = [
        {"queries_issued": 5},  # no provider key -> skipped entirely
        {"provider": "bva", "queries_issued": 2, "results": 0},
        {"provider": "bva", "queries_issued": 3},
    ]

    rolled = rollup_search_telemetry(records)

    assert set(rolled) == {"bva"}
    assert rolled["bva"]["queries_issued"] == 5
    assert rolled["bva"]["results"] == 0  # explicit 0 stays 0; missing key adds 0
    assert rolled["bva"]["deduped"] == 0
    assert rolled["bva"]["failures"] == 0


def test_rollup_accumulates_variant_stats_across_records():
    records = [
        {"provider": "bva", "variants": {"q1": {"results": 2, "failures": 1}}},
        {"provider": "bva", "variants": {"q1": {"results": 3, "failures": 1}}},
        {"provider": "bva", "variants": {"q2": {"failures": 2}}},
        {"provider": "bva", "variants": {"q3": {"results": 4}}},
    ]

    rolled = rollup_search_telemetry(records)
    variants = rolled["bva"]["variants"]  # type: ignore[index]

    assert variants["q1"]["results"] == 5  # accumulated across records
    assert variants["q1"]["failures"] == 2  # accumulated, not overwritten
    assert variants["q2"]["results"] == 0  # missing results adds 0
    assert variants["q2"]["failures"] == 2
    assert variants["q3"]["results"] == 4
    assert variants["q3"]["failures"] == 0  # missing failures adds 0


def test_validate_search_providers_warns_exact_message(monkeypatch, caplog):
    monkeypatch.setenv("SEARCH_PROVIDERS", "google,duckduckgo")

    assert validate_search_providers() == ["duckduckgo"]

    expected = (
        "Unknown search provider in SEARCH_PROVIDERS: 'google'; available: bva, "
        "bvalocal, bvasitemap, courtlistener, duckduckgo. Skipping it."
    )
    assert any(r.message == expected for r in caplog.records)


def test_bva_provider_caps_default_max_results(monkeypatch):
    items = [
        {"url": f"https://www.va.gov/vetapp25/Files6/A{i}.txt", "title": f"A{i}.txt"}
        for i in range(12)
    ]
    html = f'<html><script>"resultsData":{json.dumps({"results": items})}</script></html>'
    monkeypatch.setattr(
        "va_legal_agent.providers._get_bva_session",
        lambda: _FakeSession(
            lambda url, params=None, timeout=None, **kwargs: FakeResponse(html)
        ),
    )

    results = BVAProvider().search("tinnitus")  # default max_results=10

    assert len(results) == 10


def test_search_all_forwards_custom_max_results(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "0")
    seen: dict[str, int] = {}

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            seen["max_results"] = max_results
            return [{"title": "x", "url": "https://example.com/x", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    search_all("tinnitus", max_results=3)

    assert seen["max_results"] == 3


def test_extract_courtlistener_opinion_id():
    from va_legal_agent.providers import extract_courtlistener_opinion_id

    assert extract_courtlistener_opinion_id("https://www.courtlistener.com/opinion/12345/x/") == 12345
    assert extract_courtlistener_opinion_id("https://example.com/opinion/999/") == 999
    assert extract_courtlistener_opinion_id("https://example.com/not-an-opinion") is None
    assert extract_courtlistener_opinion_id("") is None
    assert extract_courtlistener_opinion_id(None) is None


def test_courtlistener_get_opinion_success(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_KEY", "tok-123")
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "33")
    captured: list[tuple[str, object, object, object]] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.append((url, params, headers, timeout))
        if url == CourtListenerProvider.API_URL + "12345/":
            return FakeResponse(_cl_opinion_detail())
        if url == "https://www.courtlistener.com/api/rest/v4/clusters/98765/":
            return FakeResponse(_cl_cluster_detail())
        if url == "https://www.courtlistener.com/api/rest/v4/dockets/55555/":
            return FakeResponse(_cl_docket_detail())
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    result = CourtListenerProvider()._get_opinion(12345)

    # opinion detail -> cluster -> docket, each with the auth header + timeout.
    assert [c[0] for c in captured] == [
        CourtListenerProvider.API_URL + "12345/",
        "https://www.courtlistener.com/api/rest/v4/clusters/98765/",
        "https://www.courtlistener.com/api/rest/v4/dockets/55555/",
    ]
    assert all(c[1] is None for c in captured)  # no query params on any fetch
    assert all(c[2]["Authorization"] == "Token tok-123" for c in captured)  # type: ignore[index]
    assert all(c[3] == 33 for c in captured)
    assert result["title"] == "Smith v. McDonough"
    assert result["url"] == "https://www.courtlistener.com/opinion/12345/smith-v-mcdonough/"
    assert result["court"] == "Court of Appeals for Veterans Claims"
    assert result["citation"] == "35 Vet.App. 123"
    assert result["decision_date"] == "2023-05-01"
    assert result["docket"] == "19-4433"
    assert result["judge"] == "Judge Mary J. Smith"
    assert result["snippet"] == ""
    assert result["courtlistener_opinion_id"] == "12345"


def test_courtlistener_get_opinion_sparse_fallbacks(monkeypatch):
    # Opinion/cluster/docket with everything missing: every fallback fires and
    # no fetch is attempted for absent links.
    calls: list[str] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if url == CourtListenerProvider.API_URL + "12345/":
            return FakeResponse({"absolute_url": "/opinion/12345/"})  # no cluster link
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    result = CourtListenerProvider()._get_opinion(12345)

    assert calls == [CourtListenerProvider.API_URL + "12345/"]
    assert result["title"] == "Untitled case"
    assert result["court"] == COURT_UNKNOWN
    assert result["citation"] == ""
    assert result["decision_date"] == ""
    assert result["docket"] == ""
    assert result["judge"] == ""
    assert result["snippet"] == ""
    assert result["courtlistener_opinion_id"] == "12345"


def test_courtlistener_get_opinion_missing_cite_in_cluster(monkeypatch):
    # Cluster citations present but lacking the "cite" key -> empty citation,
    # not the string "None".
    def fake_get(url, params=None, headers=None, timeout=None):
        if url == CourtListenerProvider.API_URL + "12345/":
            return FakeResponse(_cl_opinion_detail())
        if url == "https://www.courtlistener.com/api/rest/v4/clusters/98765/":
            return FakeResponse(_cl_cluster_detail(citations=[{"reporter": "x"}]))
        if url == "https://www.courtlistener.com/api/rest/v4/dockets/55555/":
            return FakeResponse(_cl_docket_detail())
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    result = CourtListenerProvider()._get_opinion(12345)

    assert result["citation"] == ""


def test_courtlistener_get_opinion_http_error(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse({}, status_code=500),
    )

    with pytest.raises(SearchError, match="opinion 12345 fetch failed"):
        CourtListenerProvider()._get_opinion(12345)


def test_courtlistener_cited_opinions_maps_authorities(monkeypatch):
    """The `citing` relation maps cited_opinion rows and fetches each once."""
    fetched: list[int] = []
    rows = [
        {"cited_opinion": "https://www.courtlistener.com/opinion/100/x/"},
        {"cited_opinion": "https://www.courtlistener.com/opinion/100/x/"},  # dup id
        {"cited_opinion": "https://www.courtlistener.com/opinion/101/y/"},
        {"cited_opinion": "no-opinion-here"},  # unparseable -> skipped
    ]

    def fake_get(url, params=None, headers=None, timeout=None):
        if url == CourtListenerProvider.CITATIONS_URL:
            assert params["citing_opinion"] == 12345
            return FakeResponse({"results": rows})
        opinion_id = int(url.rstrip("/").split("/")[-1])
        fetched.append(opinion_id)
        return FakeResponse(_cl_result(absolute_url=f"/opinion/{opinion_id}/"))

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    results = CourtListenerProvider().cited_opinions(12345, max_results=10)

    assert fetched == [100, 101]  # deduped, unparseable skipped
    assert [r["url"] for r in results] == [
        "https://www.courtlistener.com/opinion/100/",
        "https://www.courtlistener.com/opinion/101/",
    ]


def test_courtlistener_citing_opinions_maps_forward_citations(monkeypatch):
    """The `cited` relation maps citing_opinion rows (opinions citing this one)."""
    rows = [{"citing_opinion": "https://www.courtlistener.com/opinion/200/x/"}]

    def fake_get(url, params=None, headers=None, timeout=None):
        if url == CourtListenerProvider.CITATIONS_URL:
            assert params["cited_opinion"] == 12345
            return FakeResponse({"results": rows})
        if url == "https://www.courtlistener.com/api/rest/v4/opinions/200/":
            return FakeResponse(
                _cl_opinion_detail(absolute_url="/opinion/200/")
            )
        if url == "https://www.courtlistener.com/api/rest/v4/clusters/98765/":
            return FakeResponse(_cl_cluster_detail())
        if url == "https://www.courtlistener.com/api/rest/v4/dockets/55555/":
            return FakeResponse(_cl_docket_detail())
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    results = CourtListenerProvider().citing_opinions(12345, max_results=10)

    assert [r["url"] for r in results] == ["https://www.courtlistener.com/opinion/200/"]
    assert results[0]["title"] == "Smith v. McDonough"
    assert results[0]["citation"] == "35 Vet.App. 123"


def test_courtlistener_related_opinions_http_error(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse({}, status_code=500),
    )

    with pytest.raises(SearchError, match="citation traversal \\(citing\\) failed"):
        CourtListenerProvider().cited_opinions(12345)


def test_courtlistener_related_opinions_no_results(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse({"results": []}),
    )

    with pytest.raises(SearchError, match="No citing opinions found for opinion 12345"):
        CourtListenerProvider().cited_opinions(12345)


def test_courtlistener_related_opinions_request_args(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_KEY", "tok-123")
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "33")
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(params=params, headers=headers, timeout=timeout)
        return FakeResponse({"results": []})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError):
        CourtListenerProvider().cited_opinions(12345, max_results=1)

    # Exact params: the relation key, the lower-clamped page_size, and format;
    # plus the forwarded auth header and timeout.
    assert captured["params"] == {"citing_opinion": 12345, "page_size": 1, "format": "json"}
    assert captured["headers"]["Authorization"] == "Token tok-123"
    assert captured["timeout"] == 33


def test_courtlistener_related_opinions_missing_results_key(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse({}),
    )

    with pytest.raises(SearchError, match="No citing opinions found"):
        CourtListenerProvider().cited_opinions(12345)


def test_citing_and_cited_opinions_default_max_results(monkeypatch):
    calls = []

    def fake_related(self, opinion_id, relation, max_results):
        calls.append((opinion_id, relation, max_results))

    monkeypatch.setattr(
        "va_legal_agent.providers.CourtListenerProvider._related_opinions", fake_related
    )

    CourtListenerProvider().citing_opinions(12345)
    CourtListenerProvider().cited_opinions(12345)

    assert calls == [(12345, "cited", 10), (12345, "citing", 10)]


def test_courtlistener_related_opinions_caps_at_max_results(monkeypatch):
    rows = [
        {"cited_opinion": f"https://www.courtlistener.com/opinion/{i}/"}
        for i in range(5)
    ]

    def fake_get(url, params=None, headers=None, timeout=None):
        if url == CourtListenerProvider.CITATIONS_URL:
            return FakeResponse({"results": rows})
        opinion_id = int(url.rstrip("/").split("/")[-1])
        if url == f"https://www.courtlistener.com/api/rest/v4/opinions/{opinion_id}/":
            return FakeResponse(_cl_opinion_detail(absolute_url=f"/opinion/{opinion_id}/"))
        if url == "https://www.courtlistener.com/api/rest/v4/clusters/98765/":
            return FakeResponse(_cl_cluster_detail())
        if url == "https://www.courtlistener.com/api/rest/v4/dockets/55555/":
            return FakeResponse(_cl_docket_detail())
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    results = CourtListenerProvider().cited_opinions(12345, max_results=2)

    assert len(results) == 2


def test_courtlistener_related_opinions_clamps_page_size(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return FakeResponse({"results": []})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError):
        CourtListenerProvider().cited_opinions(12345, max_results=150)

    assert captured["params"]["page_size"] == 100  # capped, not forwarded as 150


def test_courtlistener_related_opinions_skips_detail_without_url(monkeypatch):
    rows = [
        {"cited_opinion": "https://www.courtlistener.com/opinion/100/"},
        {"cited_opinion": "https://www.courtlistener.com/opinion/101/"},
    ]

    def fake_get(url, params=None, headers=None, timeout=None):
        if url == CourtListenerProvider.CITATIONS_URL:
            return FakeResponse({"results": rows})
        opinion_id = int(url.rstrip("/").split("/")[-1])
        if url == f"https://www.courtlistener.com/api/rest/v4/opinions/{opinion_id}/":
            absolute = "" if opinion_id == 100 else f"/opinion/{opinion_id}/"
            return FakeResponse(_cl_opinion_detail(absolute_url=absolute))
        if url == "https://www.courtlistener.com/api/rest/v4/clusters/98765/":
            return FakeResponse(_cl_cluster_detail())
        if url == "https://www.courtlistener.com/api/rest/v4/dockets/55555/":
            return FakeResponse(_cl_docket_detail())
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    results = CourtListenerProvider().cited_opinions(12345, max_results=10)

    assert [r["url"] for r in results] == ["https://www.courtlistener.com/opinion/101/"]


def test_traverse_citations_skips_non_courtlistener_urls(monkeypatch):
    from va_legal_agent.providers import traverse_citations

    calls = {"n": 0}

    def related(self, opinion_id, max_results=10):
        calls["n"] += 1
        return []

    monkeypatch.setattr("va_legal_agent.providers.CourtListenerProvider.cited_opinions", related)
    monkeypatch.setattr("va_legal_agent.providers.CourtListenerProvider.citing_opinions", related)

    result = traverse_citations(
        ["https://example.com/not-cl", "https://www.courtlistener.com/opinion/1/"]
    )

    assert result == []
    assert calls["n"] == 2  # only the one CourtListener url is traversed (cited + citing)


def test_traverse_citations_merges_dedupes_and_forwards_args(monkeypatch):
    from va_legal_agent.providers import traverse_citations

    calls: list[tuple[int, int]] = []

    def cited(self, opinion_id, max_results=10):
        calls.append((opinion_id, max_results))
        return [
            {"title": "A", "url": "https://www.courtlistener.com/opinion/100/"},
            {"title": "NoUrl", "no_url": True},  # url-less -> skipped
            {"title": "Dup", "url": "https://www.courtlistener.com/opinion/101/"},
        ]

    def citing(self, opinion_id, max_results=10):
        calls.append((opinion_id, max_results))
        return [
            {"title": "B", "url": "https://www.courtlistener.com/opinion/101/"},  # dup across relations
            {"title": "C", "url": "https://www.courtlistener.com/opinion/102/"},
        ]

    monkeypatch.setattr("va_legal_agent.providers.CourtListenerProvider.cited_opinions", cited)
    monkeypatch.setattr("va_legal_agent.providers.CourtListenerProvider.citing_opinions", citing)

    result = traverse_citations(["https://www.courtlistener.com/opinion/1/"])

    # Both relations are called with the extracted id and the default max_results.
    assert calls == [(1, 10), (1, 10)]

    # A non-default max_results must be forwarded to both relations.
    calls.clear()
    traverse_citations(["https://www.courtlistener.com/opinion/1/"], max_results=4)
    assert calls == [(1, 4), (1, 4)]
    # The url-less result is skipped; the duplicate across relations is deduped.
    assert [r["url"] for r in result] == [
        "https://www.courtlistener.com/opinion/100/",
        "https://www.courtlistener.com/opinion/101/",
        "https://www.courtlistener.com/opinion/102/",
    ]


def test_traverse_citations_skips_failed_node(monkeypatch, caplog):
    from va_legal_agent.providers import traverse_citations

    def cited(self, opinion_id, max_results=10):
        raise SearchError("rate limited")

    def citing(self, opinion_id, max_results=10):
        return [{"title": "B", "url": "https://www.courtlistener.com/opinion/102/"}]

    monkeypatch.setattr("va_legal_agent.providers.CourtListenerProvider.cited_opinions", cited)
    monkeypatch.setattr("va_legal_agent.providers.CourtListenerProvider.citing_opinions", citing)

    result = traverse_citations(["https://www.courtlistener.com/opinion/1/"])

    assert [r["url"] for r in result] == ["https://www.courtlistener.com/opinion/102/"]
    assert any(
        r.getMessage() == "Citation traversal skipped opinion 1: rate limited"
        for r in caplog.records
    )


# ── BVA local-index provider ────────────────────────────────────────────


class TestBvaLocalIndexPaths:
    def test_returns_corpus_and_manifest(self):
        c, m = _bva_local_index_paths("/tmp/bva_idx")
        assert c.endswith("/corpus.txt")
        assert m.endswith("/manifest.json")
        assert c.startswith("/tmp/bva_idx")

    def test_relative_path(self):
        c, m = _bva_local_index_paths("idx")
        assert c == "idx/corpus.txt"
        assert m == "idx/manifest.json"


class TestBvaLocalSearchCorpus:
    def test_no_tokens_returns_empty(self):
        assert _bva_local_search_corpus("any text", [], [], 10) == []

    def test_single_token_non_match(self, tmp_path):
        manifest = [{"url": "u", "title": "t", "lastmod": "", "start": 0, "end": 4}]
        assert _bva_local_search_corpus("hello", manifest, ["zzz"], 10) == []

    def test_single_token_match(self, tmp_path):
        text = "HELLO tinnitus world"
        manifest = [{"url": "u", "title": "t", "lastmod": "", "start": 0, "end": len(text)}]
        results = _bva_local_search_corpus(text, manifest, ["tinnitus"], 10)
        assert len(results) == 1
        assert results[0]["url"] == "u"
        assert "tinnitus" in results[0]["snippet"].lower()

    def test_all_tokens_must_match(self, tmp_path):
        text = "service connection for hearing loss"
        manifest = [{"url": "u", "title": "t", "lastmod": "", "start": 0, "end": len(text)}]
        # "tinnitus" is not present, so the AND fails even though "hearing" is.
        assert _bva_local_search_corpus(text, manifest, ["hearing", "tinnitus"], 10) == []
        # Both "hearing" and "loss" are present.
        results = _bva_local_search_corpus(text, manifest, ["hearing", "loss"], 10)
        assert len(results) == 1

    def test_multiple_decisions_in_corpus(self, tmp_path):
        text = "A: tinnitus granted.\n\x00\nB: hearing loss denied.\n\x00\nC: tinnitus remanded."
        manifest = [
            {"url": "a", "title": "A", "lastmod": "", "start": 0, "end": 21},
            {"url": "b", "title": "B", "lastmod": "", "start": 21, "end": 44},
            {"url": "c", "title": "C", "lastmod": "", "start": 44, "end": len(text)},
        ]
        results = _bva_local_search_corpus(text, manifest, ["tinnitus"], 10)
        assert [r["url"] for r in results] == ["a", "c"]

    def test_max_results_truncates(self, tmp_path):
        text = "tinnitus A.\n\x00\ntinnitus B.\n\x00\ntinnitus C."
        manifest = [
            {"url": "a", "title": "A", "lastmod": "", "start": 0, "end": 13},
            {"url": "b", "title": "B", "lastmod": "", "start": 13, "end": 26},
            {"url": "c", "title": "C", "lastmod": "", "start": 26, "end": 39},
        ]
        results = _bva_local_search_corpus(text, manifest, ["tinnitus"], 2)
        assert len(results) == 2

    def test_snippet_falls_back_when_token_absent(self):
        """Snippet uses the first token, but when none match it falls back."""
        text = "no match for any token at all here"
        manifest = [{"url": "u", "title": "t", "lastmod": "", "start": 0, "end": len(text)}]
        results = _bva_local_search_corpus(text, manifest, ["zzz"], 10)
        assert results == []  # no match at all

    def test_snippet_never_leaks_separator_null_byte(self):
        """A token near the body end must not leak the separator's \\x00 into the snippet."""
        body = ("x" * 100) + " tinnitus granted"
        text = body + "\n\x00\n"
        manifest = [
            {"url": "u", "title": "t", "lastmod": "", "start": 0, "end": len(text)}
        ]
        results = _bva_local_search_corpus(text, manifest, ["tinnitus"], 10)
        assert len(results) == 1
        assert "\x00" not in results[0]["snippet"]
        assert "tinnitus" in results[0]["snippet"]


class TestBvaLocalBuild:
    def test_build_and_load_roundtrip(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        # Mock the download so no real HTTP is made.
        fetches: list[tuple[str, str]] = []

        def fake_fetch(url: str) -> str:
            fetches.append((url, "body"))
            # Simulate two real-looking decision files.
            body_map = {
                "https://www.va.gov/vetapp26/Files4/26000001.txt": "The Board granted service connection for tinnitus.",
                "https://www.va.gov/vetapp26/Files4/26000002.txt": "Hearing loss denied. No nexus shown.",
                "https://www.va.gov/vetapp26/Files4/26000003.txt": "  ",  # blank body → skipped
            }
            return body_map.get(url, "fallback for " + url)

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )

        leaf = [
            ("https://www.va.gov/vetapp26/Files4/26000001.txt", "2026-08-01"),
            ("https://www.va.gov/vetapp26/Files4/26000002.txt", "2026-07-15"),
            ("https://www.va.gov/vetapp26/Files4/26000003.txt", "2026-07-01"),
        ]
        corpus_path, manifest = _bva_local_build(str(tmp_path), leaf, 0)

        assert len(manifest) == 2  # blank skipped
        assert manifest[0]["title"] == "26000001"
        assert manifest[1]["title"] == "26000002"
        assert os.path.exists(corpus_path)

        # Load it back and verify.
        loaded_corpus, loaded_manifest = _bva_local_load(str(tmp_path))
        assert loaded_corpus == corpus_path
        assert len(loaded_manifest) == 2

    def test_max_files_caps_downloads(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")

        def fake_fetch(url: str) -> str:
            return "tinnitus body"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )
        leaf = [(f"https://va.gov/vetapp26/Files1/{i:08d}.txt", "2026-08-01") for i in range(5)]
        _, manifest = _bva_local_build(str(tmp_path), leaf, 3)
        assert len(manifest) == 3

    def test_max_files_one_downloads_single_file(self, monkeypatch, tmp_path):
        """max_files=1 downloads only the newest file (not the whole leaf)."""
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")

        def fake_fetch(url: str) -> str:
            return "tinnitus body"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )
        leaf = [(f"https://va.gov/vetapp26/Files1/{i:08d}.txt", "2026-08-01") for i in range(3)]
        _, manifest = _bva_local_build(str(tmp_path), leaf, 1)
        assert len(manifest) == 1

    def test_download_failure_is_skipped(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")

        def fake_fetch(url: str) -> str:
            if "00002" in url:
                raise SearchError("rate limited")
            return "tinnitus granted"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )
        # The failure is in the MIDDLE so a `continue` (skip and keep going)
        # is distinguished from a `break` (abort the whole download): the
        # third file must still be downloaded.
        leaf = [
            ("https://va.gov/vetapp26/Files4/26000001.txt", "2026-08-01"),
            ("https://va.gov/vetapp26/Files4/26000002.txt", "2026-07-15"),
            ("https://va.gov/vetapp26/Files4/26000003.txt", "2026-07-01"),
        ]
        _, manifest = _bva_local_build(str(tmp_path), leaf, 0)
        assert len(manifest) == 2  # 00001 and 00003; 00002 failed and was skipped
        assert any(
            "download failed" in r.getMessage() for r in caplog.records
        )

    def test_txt_extension_stripped_from_title(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")

        def fake_fetch(url: str) -> str:
            return "body"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )
        leaf = [("https://va.gov/vetapp26/Files1/26_00_123.txt", "2026-08-01")]
        _, manifest = _bva_local_build(str(tmp_path), leaf, 0)
        assert manifest[0]["title"] == "26_00_123"

    def test_interrupted_build_leaves_old_index_intact(self, monkeypatch, tmp_path):
        """A hard failure mid-download must not corrupt the committed index.

        Build once, then start a rebuild whose second fetch raises a hard
        (non-SearchError) exception — the loop only skips SearchError, so the
        exception aborts the build before the temp files are swapped in. The
        previously committed corpus/manifest must be byte-for-byte intact.
        """
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")

        # First build: one decision, committed.
        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text",
            lambda url: "OLD tinnitus body",
        )
        leaf = [
            ("https://va.gov/vetapp26/Files4/26000001.txt", "2026-08-01"),
        ]
        _bva_local_build(str(tmp_path), leaf, 0)
        old_corpus = (tmp_path / "corpus.txt").read_text()
        old_manifest = (tmp_path / "manifest.json").read_text()
        old_meta = (tmp_path / "index.meta.json").read_text()

        # Rebuild that crashes on the second file.
        calls: list[str] = []

        def crashing_fetch(url: str) -> str:
            calls.append(url)
            if len(calls) > 1:
                raise RuntimeError("simulated hard crash")
            return "NEW tinnitus body"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", crashing_fetch
        )
        leaf2 = [
            ("https://va.gov/vetapp26/Files4/26000001.txt", "2026-09-01"),
            ("https://va.gov/vetapp26/Files4/26000002.txt", "2026-09-02"),
        ]
        with pytest.raises(RuntimeError, match="simulated hard crash"):
            _bva_local_build(str(tmp_path), leaf2, 0)

        # The committed files are unchanged (only .tmp scratch was touched).
        assert (tmp_path / "corpus.txt").read_text() == old_corpus
        assert (tmp_path / "manifest.json").read_text() == old_manifest
        assert (tmp_path / "index.meta.json").read_text() == old_meta


class TestBvaLocalIndexProvider:
    def test_disabled_when_dir_empty(self, monkeypatch):
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", "")
        with pytest.raises(SearchError, match="disabled"):
            BvaLocalIndexProvider().search("tinnitus")

    def test_error_when_sitemap_unavailable(self, monkeypatch):
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", "/tmp/nonexistent_bva_idx")
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")

        def empty_index():
            raise SearchError("sitemap down")

        monkeypatch.setattr(
            "va_legal_agent.providers._bva_sitemap_index", empty_index
        )
        with pytest.raises(SearchError, match="sitemap"):
            BvaLocalIndexProvider().search("tinnitus")

    def test_builds_on_first_query(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", str(tmp_path))
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [
                ("https://va.gov/vetapp26/Files4/26000100.txt", "2026-08-01"),
                ("https://va.gov/vetapp26/Files4/26000101.txt", "2026-08-02"),
            ],
        )

        def fake_fetch(url: str) -> str:
            return "The Board granted service connection for tinnitus."

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )

        results = BvaLocalIndexProvider().search("tinnitus")
        assert len(results) == 2
        from va_legal_agent.topics import COURT_BVA  # noqa: F811

        assert all(r["court"] == COURT_BVA for r in results)
        # Second call (index already built) must hit the disk, not re-download.
        results2 = BvaLocalIndexProvider().search("tinnitus", max_results=1)
        assert len(results2) == 1

    def test_raises_on_no_results(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", str(tmp_path))
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [
                ("https://va.gov/vetapp26/Files4/26000100.txt", "2026-08-01"),
            ],
        )

        def fake_fetch(url: str) -> str:
            return "hearing loss denied"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )

        with pytest.raises(SearchError, match="No search results"):
            BvaLocalIndexProvider().search("tinnitus")

    def test_query_minimization(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", str(tmp_path))
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [
                ("https://va.gov/vetapp26/Files4/26000100.txt", "2026-08-01"),
            ],
        )

        def fake_fetch(url: str) -> str:
            return "service connection tinnitus denied"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )
        # Full query with "for" stopword — minimized to the issue phrase.
        results = BvaLocalIndexProvider().search("service connection for tinnitus")
        assert len(results) == 1

    def test_adapt_query_strips_site(self, monkeypatch):
        from va_legal_agent.queries import adapt_query_for_provider

        result = adapt_query_for_provider('site:va.gov tinnitus', 'bvalocal')
        assert 'site:' not in result

    def test_empty_issue_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", str(tmp_path))
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [
                ("https://va.gov/vetapp26/Files4/26000100.txt", "2026-08-01"),
            ],
        )

        def fake_fetch(url: str) -> str:
            return "some text"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )
        # "for" is a stopword, so after minimization we get empty tokens.
        with pytest.raises(SearchError, match="No search results"):
            BvaLocalIndexProvider().search("for")

    def test_empty_issue_falls_back_to_site_stripped(self, monkeypatch, tmp_path):
        """When minimization produces nothing, fall back to site-stripped query."""
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", str(tmp_path))
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [
                ("https://va.gov/vetapp26/Files4/26000100.txt", "2026-08-01"),
            ],
        )

        def fake_fetch(url: str) -> str:
            return "service connection tinnitus granted"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )
        # After minimization + strip_site_prefixes, we get "tinnitus".
        results = BvaLocalIndexProvider().search("site:bva.va.gov tinnitus")
        assert len(results) == 1

    def test_empty_leaf_raises(self, monkeypatch, tmp_path):
        """When the sitemap index is empty, search raises."""
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", str(tmp_path))
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [],
        )
        with pytest.raises(SearchError, match="No BVA decisions indexed"):
            BvaLocalIndexProvider().search("tinnitus")

    def test_non_txt_extension_preserves_title_as_is(self, monkeypatch, tmp_path):
        """URLs without a .txt extension keep their basename as the title."""
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")

        def fake_fetch(url: str) -> str:
            return "tinnitus granted"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )
        leaf = [
            ("https://va.gov/vetapp26/Files1/decision", "2026-08-01"),
        ]
        _, manifest = _bva_local_build(str(tmp_path), leaf, 0)
        assert manifest[0]["title"] == "decision"

    def test_minimize_returns_empty_falls_back_to_strip(self, monkeypatch, tmp_path):
        """When _minimize_bva_query returns empty but strip_site_prefixes doesn't."""
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", str(tmp_path))
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [
                ("https://va.gov/vetapp26/Files4/26000100.txt", "2026-08-01"),
            ],
        )

        def fake_fetch(url: str) -> str:
            return "1110 tinnitus granted"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )
        # "1110" is a statute phrase (skipped by minimize), leading text is
        # empty → minimize returns "", but strip_site_prefixes returns the
        # full query which tokenizes to ['1110', 'tinnitus'].
        results = BvaLocalIndexProvider().search('"1110" tinnitus')
        assert len(results) == 1


class TestBvaLocalMeta:
    def test_meta_path_joins_correctly(self):
        assert _bva_local_meta_path("/tmp/idx") == "/tmp/idx/index.meta.json"

    def test_write_meta_creates_file(self, tmp_path):
        _bva_local_write_meta(str(tmp_path), "2026-08-15")
        meta_path = _bva_local_meta_path(str(tmp_path))
        assert os.path.exists(meta_path)
        with open(meta_path) as fh:
            meta = json.load(fh)
        assert "build_time" in meta
        assert meta["most_recent_lastmod"] == "2026-08-15"

    def test_build_writes_meta(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")

        def fake_fetch(url: str) -> str:
            return "body"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )
        leaf = [
            ("https://va.gov/vetapp26/Files4/26000001.txt", "2026-08-15"),
            ("https://va.gov/vetapp26/Files4/26000002.txt", "2026-08-10"),
        ]
        _bva_local_build(str(tmp_path), leaf, 0)
        meta_path = _bva_local_meta_path(str(tmp_path))
        assert os.path.exists(meta_path)
        with open(meta_path) as fh:
            meta = json.load(fh)
        assert meta["most_recent_lastmod"] == "2026-08-15"


class TestBvaLocalNeedsRebuild:
    def test_true_when_meta_missing(self, tmp_path):
        assert _bva_local_needs_rebuild(str(tmp_path), 24) is True

    def test_missing_build_time_falls_through_to_sitemap_check(
        self, monkeypatch, tmp_path
    ):
        """Meta without a build_time can't be aged, so it compares lastmods."""
        meta = {"most_recent_lastmod": "2026-08-15"}
        meta_path = _bva_local_meta_path(str(tmp_path))
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [
                ("https://va.gov/vetapp26/Files4/26000099.txt", "2026-09-01"),
            ],
        )
        assert _bva_local_needs_rebuild(str(tmp_path), 24) is True

    def test_false_when_max_age_disabled(self, monkeypatch, tmp_path):
        """max_age_hours=0 disables auto-rebuild and never touches the sitemap."""
        _bva_local_write_meta(str(tmp_path), "2026-08-15")
        sitemap_calls = []
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: sitemap_calls.append(1) or [],
        )
        assert (
            _bva_local_needs_rebuild(str(tmp_path), 0) is False
        )
        assert sitemap_calls == []  # disabled: no sitemap fetch at all

    def test_old_index_but_sitemap_no_newer_content(self, monkeypatch, tmp_path):
        """Age exceeded but live sitemap has no newer lastmod → don't rebuild."""
        meta = {
            "build_time": "2000-01-01T00:00:00+00:00",
            "most_recent_lastmod": "2026-08-15",
        }
        meta_path = _bva_local_meta_path(str(tmp_path))
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [
                ("https://va.gov/vetapp26/Files4/26000001.txt", "2026-08-15"),
            ],
        )
        assert _bva_local_needs_rebuild(str(tmp_path), 1) is False

    def test_true_when_age_exceeds_threshold(self, monkeypatch, tmp_path):
        """A very old index triggers an age-based rebuild request."""
        old_time = "2000-01-01T00:00:00+00:00"
        meta = {"build_time": old_time, "most_recent_lastmod": "2000-01-01"}
        meta_path = _bva_local_meta_path(str(tmp_path))
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        # The live sitemap has a newer lastmod.
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [
                ("https://va.gov/vetapp26/Files4/26000001.txt", "2026-08-15"),
            ],
        )
        # Even age=1 hour should trigger and find newer content.
        assert _bva_local_needs_rebuild(str(tmp_path), 1) is True

    def test_fresh_index_within_age_threshold(
        self, monkeypatch, tmp_path
    ):
        """A just-built index returns False without fetching the sitemap."""
        _bva_local_write_meta(str(tmp_path), "2026-08-15")
        sitemap_calls = []
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: sitemap_calls.append(1) or [],
        )
        assert _bva_local_needs_rebuild(str(tmp_path), 24) is False
        assert sitemap_calls == []  # sitemap was never fetched

    def test_true_when_sitemap_has_newer_lastmod(self, monkeypatch, tmp_path):
        """When the age threshold is passed, a newer sitemap triggers rebuild."""
        # Write old meta so the age threshold is exceeded.
        meta = {
            "build_time": "2000-01-01T00:00:00+00:00",
            "most_recent_lastmod": "2026-08-15",
        }
        meta_path = _bva_local_meta_path(str(tmp_path))
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [
                ("https://va.gov/vetapp26/Files4/26000099.txt", "2026-09-01"),
            ],
        )
        assert _bva_local_needs_rebuild(str(tmp_path), 24) is True

    def test_false_when_sitemap_fetch_fails(self, monkeypatch, tmp_path):
        """When the sitemap is down, keep the existing index."""
        meta = {
            "build_time": "2000-01-01T00:00:00+00:00",
            "most_recent_lastmod": "2026-08-15",
        }
        meta_path = _bva_local_meta_path(str(tmp_path))
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)

        def failing_leaf(self):
            raise SearchError("sitemap down")

        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            failing_leaf,
        )
        # Age exceeded + sitemap fails → return False (keep existing index).
        assert _bva_local_needs_rebuild(str(tmp_path), 1) is False

    def test_no_lastmod_returns_false_when_age_exceeded(self, monkeypatch, tmp_path):
        """Age exceeded but no stored_lastmod to compare → don't rebuild.

        The sitemap must never be fetched: without a stored lastmod there is
        nothing to compare against, so the decision is made from the meta file
        alone. Mocking ``_most_recent_leaf`` and asserting it is never called
        both makes this hermetic (no va.gov network call) and pins the
        short-circuit — a mutant that flips the guard would invoke the mocked
        sitemap and fail this test.
        """
        meta = {"build_time": "2000-01-01T00:00:00+00:00"}
        meta_path = _bva_local_meta_path(str(tmp_path))
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        sitemap_calls = []
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: sitemap_calls.append(1) or [],
        )
        # stored_lastmod missing → falls through to return False without
        # touching the live sitemap.
        assert _bva_local_needs_rebuild(str(tmp_path), 1) is False
        assert sitemap_calls == []  # sitemap was never fetched

    def test_meta_without_lastmod_stays_fresh(self, monkeypatch, tmp_path):
        """Meta missing most_recent_lastmod is not stale when age is within threshold."""
        meta = {"build_time": datetime.now(timezone.utc).isoformat()}
        meta_path = _bva_local_meta_path(str(tmp_path))
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        # No lastmod → no live comparison → age check short-circuits to False.
        assert _bva_local_needs_rebuild(str(tmp_path), 24) is False

    def test_true_when_meta_json_corrupt(self, tmp_path):
        meta_path = _bva_local_meta_path(str(tmp_path))
        with open(meta_path, "w") as fh:
            fh.write("not json")
        assert _bva_local_needs_rebuild(str(tmp_path), 24) is True

    def test_true_when_build_time_unparseable(self, tmp_path):
        meta = {"build_time": "garbage", "most_recent_lastmod": "2026-08-15"}
        meta_path = _bva_local_meta_path(str(tmp_path))
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        assert _bva_local_needs_rebuild(str(tmp_path), 1) is True


class TestBvaLocalIndexAutoRebuild:
    def test_rebuilds_when_stale(self, monkeypatch, tmp_path):
        """Search triggers a rebuild when the meta reports the index is old."""
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", str(tmp_path))
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_MAX_AGE_HOURS", "1")

        # Pre-populate an old index.
        _bva_local_write_meta(str(tmp_path), "2026-01-01")
        # Monkeypatch needs_rebuild to return True (age exceeded for any realistic clock).
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_MAX_AGE_HOURS", "0")  # disable age to test the sitemap path
        monkeypatch.setattr(
            "va_legal_agent.providers._bva_local_needs_rebuild",
            lambda d, h: True,
        )

        def fake_fetch(url: str) -> str:
            return "tinnitus service connection granted"

        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text", fake_fetch
        )
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [
                ("https://va.gov/vetapp26/Files4/26000100.txt", "2026-09-01"),
            ],
        )

        results = BvaLocalIndexProvider().search("tinnitus")
        assert len(results) == 1

    def test_uses_stale_copy_when_sitemap_down(self, monkeypatch, tmp_path):
        """When the sitemap is down, the stale index still serves results."""
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", str(tmp_path))

        # Pre-populate a valid index.
        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text",
            lambda url: "tinnitus service connection granted",
        )
        leaf = [
            ("https://va.gov/vetapp26/Files4/26000100.txt", "2026-08-01"),
        ]
        _bva_local_build(str(tmp_path), leaf, 0)

        # Now make needs_rebuild try to fetch the sitemap, but it fails.
        monkeypatch.setattr(
            "va_legal_agent.providers._bva_local_needs_rebuild",
            lambda d, h: True,
        )
        def failing_leaf(self):
            raise SearchError("sitemap down")

        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            failing_leaf,
        )

        # The existing index on disk should still serve results.
        results = BvaLocalIndexProvider().search("tinnitus")
        assert len(results) == 1

    def test_empty_leaf_with_existing_index_uses_stale_copy(self, monkeypatch, tmp_path):
        """When rebuild is needed but sitemap returns empty leaf, use stale."""
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", str(tmp_path))

        # Pre-populate a valid index.
        monkeypatch.setattr(
            "va_legal_agent.providers._fetch_bva_decision_text",
            lambda url: "tinnitus granted",
        )
        leaf = [
            ("https://va.gov/vetapp26/Files4/26000100.txt", "2026-08-01"),
        ]
        _bva_local_build(str(tmp_path), leaf, 0)

        # Now make needs_rebuild return True, but the sitemap is empty.
        monkeypatch.setattr(
            "va_legal_agent.providers._bva_local_needs_rebuild",
            lambda d, h: True,
        )
        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            lambda self: [],
        )

        results = BvaLocalIndexProvider().search("tinnitus")
        assert len(results) == 1

    def test_no_rebuild_when_fresh(self, monkeypatch, tmp_path):
        """A fresh index is reused without re-downloading."""
        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", str(tmp_path))
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_MAX_AGE_HOURS", "9999")

        # Write a valid meta
        _bva_local_write_meta(str(tmp_path), "2026-08-15")
        # Write matching corpus/manifest
        corpus_path, manifest_path = _bva_local_index_paths(str(tmp_path))
        with open(corpus_path, "w") as fh:
            fh.write("tinnitus granted.\n\x00\n")
        with open(manifest_path, "w") as fh:
            json.dump(
                [{"url": "u", "title": "t", "lastmod": "2026-08-15", "start": 0, "end": 21}],
                fh,
            )

        # Mock the sitemap to return newer content — but it shouldn't be
        # called because age is under the max.
        sitemap_calls = []

        def spy_leaf(self):
            sitemap_calls.append(1)
            return []

        monkeypatch.setattr(
            "va_legal_agent.providers.BvaSitemapProvider._most_recent_leaf",
            spy_leaf,
        )

        results = BvaLocalIndexProvider().search("tinnitus")
        assert len(results) == 1
        assert sitemap_calls == []  # never checked the sitemap!


class TestBvaLocalConcurrency:
    def test_concurrent_cold_start_builds_once(self, monkeypatch, tmp_path):
        """Fan-out threads racing a cold index trigger exactly one build.

        The agent fans queries out across a worker pool, so several threads
        can reach ``BvaLocalIndexProvider.search`` at once on a first-use
        index. The process lock must serialize the build so the corpus file
        is written once (not truncated by concurrent ``"w"`` opens) and only
        one download happens.
        """
        import va_legal_agent.providers as prov

        monkeypatch.setenv("SEARCH_PROVIDERS", "bvalocal")
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_DIR", str(tmp_path))
        monkeypatch.setenv("SEARCH_BVA_LOCAL_INDEX_MAX_AGE_HOURS", "0")
        monkeypatch.setattr(
            prov.BvaSitemapProvider,
            "_most_recent_leaf",
            lambda self: [
                ("https://va.gov/vetapp26/Files4/26000100.txt", "2026-08-01"),
            ],
        )
        monkeypatch.setattr(
            prov,
            "_fetch_bva_decision_text",
            lambda url: "tinnitus service connection granted",
        )

        build_calls: list[int] = []
        real_build = prov._bva_local_build

        def counting_build(directory, leaf, max_files):
            build_calls.append(1)
            return real_build(directory, leaf, max_files)

        monkeypatch.setattr(prov, "_bva_local_build", counting_build)

        results: list[list[dict[str, str]]] = []
        errors: list[Exception] = []

        def run():
            try:
                results.append(prov.BvaLocalIndexProvider().search("tinnitus"))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert build_calls == [1]  # exactly one build despite 8 concurrent cold starts
        assert len(results) == 8
        assert all(len(r) == 1 for r in results)
