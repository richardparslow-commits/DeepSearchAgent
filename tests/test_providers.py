"""Tests for the search provider abstraction (va_legal_agent.providers)."""

import json
import time

import pytest
import requests

from va_legal_agent.providers import (
    BVAProvider,
    CourtListenerProvider,
    DuckDuckGoProvider,
    SearchError,
    get_provider,
    resolve_search_providers,
    rollup_search_telemetry,
    search_all,
    validate_search_providers,
)
from va_legal_agent.topics import COURT_UNKNOWN


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


def test_get_provider_known_names():
    assert isinstance(get_provider("duckduckgo"), DuckDuckGoProvider)
    assert isinstance(get_provider("courtlistener"), CourtListenerProvider)
    assert isinstance(get_provider("bva"), BVAProvider)


def test_get_provider_unknown_name():
    with pytest.raises(ValueError, match="Unknown search provider"):
        get_provider("google")


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
    item = {
        "case_name": "Smith v. McDonough",
        "absolute_url": "/opinion/12345/smith-v-mcdonough/",
        "court": "cavc",
        "date_filed": "2023-05-01",
        "docket_number": "19-4433",
        "judges": "Judge Mary J. Smith",
        "citations": [{"cite": "35 Vet.App. 123"}],
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
    assert captured["params"]["court"] == ["cavc", "cafc", "scotus"]


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


def test_derive_variants_unmatched_issue_adds_statutes_only():
    from va_legal_agent.queries import derive_variants

    # An issue matching no topic keyword still gets statute-fragment variants.
    variants = derive_variants('site:bva.va.gov "tinnitus"', limit=2)

    assert len(variants) == 3
    assert all("5107" in v or "7104" in v for v in variants[1:])


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
        "va_legal_agent.queries.STATUTE_HINTS", (("5107", "hint one"), ("5107", "hint two"))
    )

    phrases = _expansion_phrases("rating")

    assert phrases.count("5107") == 1


def test_derive_variants_default_limit_bounds_variant_count():
    from va_legal_agent.queries import derive_variants

    # Default limit is 3: base query plus at most three variants. A rating issue
    # yields well over three expansion phrases, so the count is pinned by limit.
    variants = derive_variants('site:bva.va.gov "rating" "Compensation"')

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

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResponse(BVA_HTML)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

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

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return FakeResponse(BVA_HTML)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    BVAProvider().search("site:bva.va.gov tinnitus")

    assert "site:" not in captured["params"]["query"]


def test_bva_provider_raises_on_challenge_page(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse(
            "<html>anomaly challenge</html>", status_code=202
        ),
    )

    with pytest.raises(SearchError, match="rate-limit/anomaly"):
        BVAProvider().search("tinnitus")


def test_bva_provider_raises_on_http_error(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return FakeResponse({}, status_code=500)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    with pytest.raises(SearchError, match="BVA"):
        BVAProvider().search("tinnitus")


def test_bva_provider_raises_when_no_results(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse("<html></html>"),
    )

    with pytest.raises(SearchError, match="No search results"):
        BVAProvider().search("tinnitus")


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
    search_all("tinnitus", max_results=10, telemetry=telemetry)

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
    search_all("tinnitus", max_results=10, telemetry=telemetry)

    by_provider = {t["provider"]: t for t in telemetry}
    assert by_provider["duckduckgo"]["queries_issued"] == 1  # base query only
    assert by_provider["courtlistener"]["queries_issued"] == 3  # base + 2 overrides
    assert by_provider["bva"]["queries_issued"] == 4  # base + 3 global


def test_expansion_skips_statute_fragment_already_used_as_synonym(monkeypatch):
    from va_legal_agent.queries import _expansion_phrases

    # "evaluation" is both a rating-topic synonym and (via the patch) a
    # statute fragment; the fragment must be deduped against the synonym.
    monkeypatch.setattr("va_legal_agent.queries.STATUTE_HINTS", (("evaluation", "hint"),))

    phrases = _expansion_phrases("rating")

    assert "evaluation" in phrases
    assert phrases.count("evaluation") == 1


def test_derive_variants_with_no_phrases_returns_original_only(monkeypatch):
    from va_legal_agent.queries import derive_variants

    # No topic matches "tinnitus" and no statute fragments remain: the
    # expansion loop runs zero times.
    monkeypatch.setattr("va_legal_agent.queries.STATUTE_HINTS", ())

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
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "2")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "1")

    class _FakeDDG:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            if "5107" in query:
                raise SearchError("rate limited")
            return [{"title": "Hit", "url": f"https://example.com/{len(query)}", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.providers.get_provider", lambda name: _FakeDDG())

    telemetry: list[dict[str, object]] = []
    search_all("tinnitus", max_results=10, telemetry=telemetry)

    variants = telemetry[0]["variants"]
    assert isinstance(variants, dict)
    # Per-variant results/failures reconcile with the provider-level counters.
    assert sum(v["results"] for v in variants.values()) == telemetry[0]["results"]
    assert sum(v["failures"] for v in variants.values()) == telemetry[0]["failures"]
    # The 5107-anchored variant was throttled; the others surfaced cases.
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


def test_bva_sets_page_param_when_paged(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return FakeResponse(BVA_HTML)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

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
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse(html),
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
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse(html),
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
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse(html),
    )

    results = BVAProvider().search("tinnitus", max_results=2)

    assert len(results) == 2


def test_bva_malformed_results_data_json(monkeypatch):
    html = '<html><script>{&quot;resultsData&quot;:{broken json</script></html>'
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse(html),
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
    assert captured["url"] == CourtListenerProvider.API_URL
    assert captured["params"]["q"] == "tinnitus"  # type: ignore[index]
    assert captured["params"]["page_size"] == 1  # type: ignore[index]  # clamps to >= 1
    assert captured["params"]["page"] == 1  # type: ignore[index]
    assert captured["params"]["format"] == "json"  # type: ignore[index]
    assert "User-Agent" in captured["headers"]  # type: ignore[operator]
    assert captured["timeout"] is not None

    # Defaults: max_results=10, page=1; page_size caps at 100.
    CourtListenerProvider().search("tinnitus")
    assert captured["params"]["page_size"] == 10  # type: ignore[index]
    assert captured["params"]["page"] == 1  # type: ignore[index]
    CourtListenerProvider().search("tinnitus", max_results=150)
    assert captured["params"]["page_size"] == 100  # type: ignore[index]


def test_courtlistener_fallback_fields_and_unknown_court(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    items = [
        {  # everything missing: all fallbacks and the unknown-court mapping
            "absolute_url": "/opinion/9/",
            "court": "not-a-court",
            "citations": [],
        },
        {  # citation dict present but lacking the "cite" key
            "absolute_url": "/opinion/10/",
            "court": "cavc",
            "citations": [{"reporter": "x"}],
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
        {"case_name": "No URL", "court": "cavc"},  # no absolute_url -> skipped
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


def test_bva_provider_sends_exact_request_args(monkeypatch):
    captured: dict[str, object] = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers, timeout=timeout)
        return FakeResponse(BVA_HTML)

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    BVAProvider().search("tinnitus")  # default page=1
    assert captured["url"] == BVAProvider.SEARCH_URL
    assert captured["params"]["affiliate"] == "bvadecisions"  # type: ignore[index]
    assert captured["params"]["query"] == "tinnitus"  # type: ignore[index]
    assert "page" not in captured["params"]  # type: ignore[operator]
    assert "User-Agent" in captured["headers"]  # type: ignore[operator]
    assert captured["timeout"] is not None

    BVAProvider().search("tinnitus", page=2)
    assert captured["params"]["page"] == 2  # type: ignore[index]


def test_bva_provider_detects_202_challenge_without_anomaly_text(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse(
            "<html>slow down</html>", status_code=202
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
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse(
            "<html>Anomaly challenge</html>", status_code=200
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
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse(html),
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
        search_all("tinnitus", telemetry=telemetry)

    # Exhaustion: every variant is attempted on every page - no early return.
    variants = derive_variants("tinnitus", limit=2)
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
        "courtlistener, duckduckgo. Skipping it."
    )
    assert any(r.message == expected for r in caplog.records)


def test_bva_provider_caps_default_max_results(monkeypatch):
    items = [
        {"url": f"https://www.va.gov/vetapp25/Files6/A{i}.txt", "title": f"A{i}.txt"}
        for i in range(12)
    ]
    html = f'<html><script>"resultsData":{json.dumps({"results": items})}</script></html>'
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda url, params=None, headers=None, timeout=None: FakeResponse(html),
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
