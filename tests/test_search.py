import pytest
import requests

from va_legal_agent.search import (
    SearchError,
    DuckDuckGoProvider,
    _retry_delay,
    _throttle,
    build_duckduckgo_url,
    extract_url_from_duckduckgo,
    search_web,
)


def test_build_url_encodes_special_characters():
    url = build_duckduckgo_url('site:uscourts.cavc.gov "service connection" & ratings')
    query = url.split("?", 1)[1]
    # urlencode percent-encodes quotes, ampersands, and spaces so the URL stays valid.
    assert "%22service+connection%22" in query
    assert "%26" in query
    assert "kl=us-en" in query
    assert "s=0" in query


def test_build_url_offsets_page():
    assert "s=20" in build_duckduckgo_url("veterans", page=3)


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.uscourts.cavc.gov%2Fdocuments%2Fcase.pdf&rut=abc",
            "https://www.uscourts.cavc.gov/documents/case.pdf",
        ),
        (
            "/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=x",
            "https://example.com/a",
        ),
        (
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb",
            "https://example.com/b",
        ),
        (
            "https://www.uscourts.cavc.gov/opinions.htm",
            "https://www.uscourts.cavc.gov/opinions.htm",
        ),
    ],
)
def test_extract_url_from_duckduckgo(link, expected):
    assert extract_url_from_duckduckgo(link) == expected


def test_extract_url_redirect_without_uddg_returns_unchanged():
    # A /l/ redirect without a uddg parameter is not a real destination URL.
    assert (
        extract_url_from_duckduckgo("//duckduckgo.com/l/?rut=abc123") == "//duckduckgo.com/l/?rut=abc123"
    )


def test_extract_url_leaves_click_tracker_alone_even_with_uddg():
    # Only /l/ redirects are unwrapped; a y.js click-tracker carrying a uddg
    # param is not a real destination and must be left unchanged.
    assert (
        extract_url_from_duckduckgo("//duckduckgo.com/y.js?uddg=https%3A%2F%2Fexample.com%2Fx")
        == "//duckduckgo.com/y.js?uddg=https%3A%2F%2Fexample.com%2Fx"
    )


def test_is_challenge_detects_202_without_anomaly_text(monkeypatch):
    from va_legal_agent.search import _is_challenge

    assert _is_challenge(FakeResponse("<html>normal results</html>", status_code=202))
    assert not _is_challenge(FakeResponse("<html>normal results</html>", status_code=200))


def test_search_web_sends_expected_request(monkeypatch):
    captured: dict[str, object] = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse(SAMPLE_HTML)

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)

    search_web("tinnitus", max_results=5)

    assert str(captured["url"]).startswith("https://duckduckgo.com/html/?")
    assert "q=tinnitus" in str(captured["url"])
    assert "s=0" in str(captured["url"])
    assert captured["headers"] is not None
    assert "User-Agent" in captured["headers"]
    assert captured["timeout"] == 20


def test_parse_uses_result_link_fallback(monkeypatch):
    html = """
    <html><body>
    <div class="result">
      <a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Falt&rut=q">Alternative result</a>
      <a class="snippet">Plain snippet element.</a>
    </div>
    </body></html>
    """
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse(html),
    )

    results = search_web("tinnitus", max_results=5)

    assert len(results) == 1
    assert results[0]["title"] == "Alternative result"
    assert results[0]["url"] == "https://example.com/alt"
    assert results[0]["snippet"] == "Plain snippet element."


def test_parse_falls_back_to_plain_anchor(monkeypatch):
    html = """
    <html><body>
    <div class="result">
      <a href="https://example.com/plain">Plain anchor title</a>
    </div>
    </body></html>
    """
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse(html),
    )

    results = search_web("tinnitus", max_results=5)

    assert len(results) == 1
    assert results[0]["title"] == "Plain anchor title"
    assert results[0]["url"] == "https://example.com/plain"
    assert results[0]["snippet"] == ""


def test_parse_skips_result_block_without_any_link(monkeypatch):
    html = """
    <html><body>
    <div class="result">no links in this block</div>
    </body></html>
    """
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse(html),
    )

    with pytest.raises(SearchError, match="No search results"):
        search_web("tinnitus", max_results=5)


SAMPLE_HTML = """
<html><body>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.uscourts.cavc.gov%2Fdocuments%2FFountain.pdf&rut=a">Fountain v. McDonald</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.uscourts.cavc.gov%2Fdocuments%2FFountain.pdf&rut=a">CAVC decision on service connection.</a>
</div>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.uscourts.cavc.gov%2Fdocuments%2FFountain.pdf&rut=b">Duplicate result</a>
  <a class="result__snippet">Same destination URL should be deduped.</a>
</div>
<div class="result">
  <a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=vivtone.com&ad_provider=bing">Sponsored result</a>
  <a class="result__snippet">An ad click-tracker that should be skipped.</a>
</div>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fcafc.uscourts.gov%2Fopinions%2F2024-1234&rut=c">Second case</a>
  <a class="result__snippet">Federal Circuit snippet.</a>
</div>
</body></html>
"""


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None


def test_search_web_parses_unwraps_and_dedupes(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse(SAMPLE_HTML),
    )

    results = search_web("site:uscourts.cavc.gov tinnitus", max_results=5)

    assert len(results) == 2
    assert results[0]["title"] == "Fountain v. McDonald"
    assert results[0]["url"] == "https://www.uscourts.cavc.gov/documents/Fountain.pdf"
    assert "CAVC" in results[0]["snippet"]
    assert results[1]["url"] == "https://cafc.uscourts.gov/opinions/2024-1234"


def test_parse_prefers_result__a_over_first_anchor(monkeypatch):
    # A result block whose first <a> is a decoy (favicon/logo) must not win;
    # the actual result link carries class="result__a".
    html = """
    <html><body>
    <div class="result">
      <a class="result__icon" href="https://example.com/favicon.ico">icon</a>
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.uscourts.cavc.gov%2Fdocuments%2FReal.pdf&rut=z">Real v. Decoy</a>
      <a class="result__snippet">Service connection decision.</a>
    </div>
    </body></html>
    """
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse(html),
    )

    results = search_web("tinnitus", max_results=5)

    assert len(results) == 1
    assert results[0]["title"] == "Real v. Decoy"
    assert results[0]["url"] == "https://www.uscourts.cavc.gov/documents/Real.pdf"


def test_search_web_raises_when_no_results(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse("<html><body></body></html>"),
    )

    with pytest.raises(SearchError, match="query that returns nothing"):
        search_web("query that returns nothing")


def test_search_web_detects_rate_limit_challenge_page(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse("<html>anomaly-modal challenge</html>", status_code=202),
    )

    with pytest.raises(SearchError) as exc:
        search_web("any query")
    assert str(exc.value) == (
        "DuckDuckGo returned a rate-limit/anomaly challenge page for query: any query. "
        "Slow down requests or raise SEARCH_DELAY_SECONDS."
    )


def test_search_web_detects_anomaly_page_with_200_status(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse("<html>duckduckgo anomaly detected</html>"),
    )

    with pytest.raises(SearchError, match="anomaly"):
        search_web("any query")


def test_search_web_retries_challenge_with_exponential_backoff(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("SEARCH_BACKOFF_BASE_SECONDS", "1.0")
    monkeypatch.setattr("va_legal_agent.search.random.uniform", lambda lo, hi: 1.0)
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.search.time.sleep", sleeps.append)

    responses = iter(
        [
            FakeResponse("<html>anomaly challenge</html>", status_code=202),
            FakeResponse("<html>anomaly challenge</html>", status_code=202),
            FakeResponse(SAMPLE_HTML),
        ]
    )
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: next(responses),
    )

    results = search_web("tinnitus", max_results=5)

    assert len(results) == 2
    assert sleeps == [1.0, 2.0]


def test_search_web_gives_up_after_retries(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "1")
    monkeypatch.setattr("va_legal_agent.search.time.sleep", lambda seconds: None)
    calls: list[str] = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return FakeResponse("<html>anomaly</html>", status_code=202)

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)

    with pytest.raises(SearchError, match="rate-limit"):
        search_web("tinnitus")

    assert len(calls) == 2  # initial request plus one retry


def test_search_web_retries_on_429(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "1")
    monkeypatch.setattr("va_legal_agent.search.time.sleep", lambda seconds: None)
    responses = iter([FakeResponse("", status_code=429), FakeResponse(SAMPLE_HTML)])

    def fake_get(url, headers=None, timeout=None):
        response = next(responses)
        if response.status_code == 429:
            raise requests.HTTPError("429 Too Many Requests", response=response)
        return response

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)

    results = search_web("tinnitus", max_results=5)

    assert len(results) == 2


def test_search_web_retries_connection_error(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "1")
    monkeypatch.setattr("va_legal_agent.search.time.sleep", lambda seconds: None)
    responses = iter([requests.ConnectionError("network down"), FakeResponse(SAMPLE_HTML)])

    def fake_get(url, headers=None, timeout=None):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)

    results = search_web("tinnitus", max_results=5)

    assert len(results) == 2


def test_search_web_reports_unavailable_after_transient_errors(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "1")
    monkeypatch.setattr("va_legal_agent.search.time.sleep", lambda seconds: None)

    def fake_get(url, headers=None, timeout=None):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)

    with pytest.raises(SearchError, match="repeatedly unavailable"):
        search_web("tinnitus")


def test_search_web_caps_results_at_max_results(monkeypatch):
    blocks = "".join(
        f'<div class="result"><a class="result__a" href="https://example.com/{i}">Result {i}</a></div>'
        for i in range(3)
    )
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse(f"<html><body>{blocks}</body></html>"),
    )

    results = search_web("tinnitus", max_results=2)

    assert len(results) == 2


def test_search_web_skips_click_trackers_then_stops_at_scan_cap(monkeypatch):
    # Three click-tracker results (skipped), then a real one. With max_results=1
    # the scan cap (idx >= max_results * 3) trips before the real result is seen.
    blocks = (
        '<div class="result"><a class="result__a" href="//duckduckgo.com/y.js">Ad</a></div>' * 3
        + '<div class="result"><a class="result__a" href="https://example.com/real">Real</a></div>'
    )
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse(f"<html><body>{blocks}</body></html>"),
    )

    with pytest.raises(SearchError, match="No search results"):
        search_web("tinnitus", max_results=1)


def test_search_web_does_not_retry_fatal_status(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "3")
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.search.time.sleep", sleeps.append)

    def fake_get(url, headers=None, timeout=None):
        raise requests.HTTPError("403 Forbidden", response=FakeResponse("", status_code=403))

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)

    with pytest.raises(SearchError, match="Search request failed"):
        search_web("tinnitus")

    assert sleeps == []  # no backoff for non-retryable failures


def test_throttle_is_disabled_when_interval_unset(monkeypatch):
    monkeypatch.delenv("SEARCH_MIN_INTERVAL_SECONDS", raising=False)
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.search.time.sleep", sleeps.append)

    _throttle()

    assert sleeps == []


def test_throttle_spaces_requests_by_minimum_interval(monkeypatch):
    monkeypatch.setenv("SEARCH_MIN_INTERVAL_SECONDS", "0.5")
    monkeypatch.setattr("va_legal_agent.search._last_request_monotonic", None)
    clock = {"now": 100.0}
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.search.time.monotonic", lambda: clock["now"])

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr("va_legal_agent.search.time.sleep", fake_sleep)

    _throttle()  # first request records the timestamp without sleeping
    _throttle()  # immediate follow-up waits a full interval
    _throttle()  # still immediate, so it waits again

    assert sleeps == [0.5, 0.5]


def test_throttle_skips_wait_after_interval_elapses(monkeypatch):
    monkeypatch.setenv("SEARCH_MIN_INTERVAL_SECONDS", "0.5")
    monkeypatch.setattr("va_legal_agent.search._last_request_monotonic", None)
    clock = {"now": 100.0}
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.search.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("va_legal_agent.search.time.sleep", sleeps.append)

    _throttle()
    clock["now"] = 101.0  # a full second passes
    _throttle()

    assert sleeps == []


def test_global_pacing_holds_under_concurrent_requests(monkeypatch):
    """Concurrent workers must not burst the provider past the min interval."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("SEARCH_MIN_INTERVAL_SECONDS", "0.1")
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    clock = {"now": 100.0}
    timestamps: list[float] = []
    lock = threading.Lock()

    def fake_get(url, headers=None, timeout=None):
        with lock:
            timestamps.append(clock["now"])
        raise requests.ConnectionError("down")

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)
    monkeypatch.setattr("va_legal_agent.search.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "va_legal_agent.search.time.sleep", lambda sec: clock.__setitem__("now", clock["now"] + sec)
    )
    monkeypatch.setattr("va_legal_agent.search._last_request_monotonic", None)

    def worker(_):
        try:
            search_web("t", max_results=1)
        except SearchError:
            pass  # every call fails; we only measure request spacing

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(worker, range(8)))

    assert len(timestamps) == 8
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    # The simulated clock accumulates binary float drift; allow 1e-6 slack.
    assert all(gap >= 0.1 - 1e-6 for gap in gaps), f"gaps: {gaps}"


def test_retry_backoff_respects_max_seconds(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "5")
    monkeypatch.setenv("SEARCH_BACKOFF_BASE_SECONDS", "1.0")
    monkeypatch.setenv("SEARCH_BACKOFF_MAX_SECONDS", "2.5")
    monkeypatch.setattr("va_legal_agent.search.random.uniform", lambda lo, hi: 1.0)
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.search.time.sleep", sleeps.append)

    def fake_get(url, headers=None, timeout=None):
        raise requests.ConnectionError("down")

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)

    with pytest.raises(SearchError, match="repeatedly unavailable"):
        search_web("tinnitus")

    assert sleeps == [1.0, 2.0, 2.5, 2.5, 2.5]  # one backoff per retry, capped


def test_search_web_exhausts_retries_and_propagates_error(monkeypatch):
    """After SEARCH_RETRY_ATTEMPTS failures the error actually propagates.

    SEARCH_RETRY_ATTEMPTS is the number of *retries* on top of the initial
    attempt, so the provider makes retries + 1 HTTP requests before giving up.
    The raised error must be a SearchError (never the raw connection error
    leaking through) that surfaces the last failure, chained from it.
    """
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("SEARCH_BACKOFF_BASE_SECONDS", "1.0")
    monkeypatch.setenv("SEARCH_BACKOFF_MAX_SECONDS", "10.0")
    monkeypatch.setenv("SEARCH_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setattr("va_legal_agent.search.random.uniform", lambda lo, hi: 1.0)
    attempts: list[str] = []
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.search.time.sleep", sleeps.append)

    def fake_get(url, headers=None, timeout=None):
        attempts.append(url)
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)

    with pytest.raises(SearchError) as excinfo:
        search_web("tinnitus")

    # Exhaustion: exactly one initial attempt plus one per configured retry.
    assert len(attempts) == 4  # SEARCH_RETRY_ATTEMPTS + 1
    # The failure propagates as a SearchError (not the raw ConnectionError) ...
    assert type(excinfo.value) is SearchError
    # ... and it surfaces the last failure rather than a generic message.
    assert "repeatedly unavailable" in str(excinfo.value)
    assert "connection refused" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, requests.ConnectionError)
    # Backoff ran before each retry: base * 2**i, capped at the max.
    assert sleeps == [1.0, 2.0, 4.0]


def test_backoff_never_exceeds_max_with_jitter(monkeypatch):
    """With real jitter, sleeps stay within SEARCH_BACKOFF_MAX_SECONDS.

    Uncapped delays grow exponentially (4, 8, 16, 32, ...), so past the cap
    every backoff is pinned to exactly the max regardless of the jitter draw.
    """
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "6")
    monkeypatch.setenv("SEARCH_BACKOFF_BASE_SECONDS", "4.0")
    monkeypatch.setenv("SEARCH_BACKOFF_MAX_SECONDS", "10.0")
    monkeypatch.setenv("SEARCH_MIN_INTERVAL_SECONDS", "0")
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.search.time.sleep", sleeps.append)

    def fake_get(url, headers=None, timeout=None):
        raise requests.ConnectionError("down")  # real random.uniform left intact

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)

    with pytest.raises(SearchError):
        search_web("tinnitus")

    assert len(sleeps) == 6  # one backoff per retry
    assert max(sleeps) <= 10.0  # never exceeds the configured cap
    # 16, 32, 64, 128 (with 0.75-1.25 jitter) all exceed the cap -> pinned at it.
    assert sleeps[-4:] == [10.0, 10.0, 10.0, 10.0]


def test_search_web_throttles_before_each_request(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    order: list[str] = []
    monkeypatch.setattr("va_legal_agent.search._throttle", lambda: order.append("throttle"))

    def fake_get(url, headers=None, timeout=None):
        order.append("get")
        return FakeResponse(SAMPLE_HTML)

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)

    results = search_web("tinnitus", max_results=5)

    assert len(results) == 2
    assert order == ["throttle", "get"]


def test_retry_delay_jitter_scales_delay(monkeypatch):
    monkeypatch.setenv("SEARCH_BACKOFF_BASE_SECONDS", "2.0")
    monkeypatch.setenv("SEARCH_BACKOFF_MAX_SECONDS", "100.0")
    monkeypatch.setattr("va_legal_agent.search.random.uniform", lambda lo, hi: 0.75)

    assert _retry_delay(0) == 1.5  # 2.0 * 0.75 (not divided by the jitter)
    assert _retry_delay(1) == 3.0  # 4.0 * 0.75


def test_retry_delay_uses_documented_jitter_range(monkeypatch):
    monkeypatch.setenv("SEARCH_BACKOFF_BASE_SECONDS", "1.0")
    monkeypatch.setenv("SEARCH_BACKOFF_MAX_SECONDS", "100.0")
    ranges: list[tuple[float, float]] = []
    monkeypatch.setattr(
        "va_legal_agent.search.random.uniform",
        lambda lo, hi: ranges.append((lo, hi)) or 1.0,
    )

    _retry_delay(0)

    assert ranges == [(0.75, 1.25)]


def test_throttle_interval_zero_leaves_state_unset(monkeypatch):
    import va_legal_agent.search as search_module

    monkeypatch.setenv("SEARCH_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setattr("va_legal_agent.search._last_request_monotonic", None)
    monkeypatch.setattr("va_legal_agent.search.time.monotonic", lambda: 100.0)

    _throttle()

    assert search_module._last_request_monotonic is None  # early return, no state


def test_throttle_exact_interval_elapsed_skips_sleep(monkeypatch):
    monkeypatch.setenv("SEARCH_MIN_INTERVAL_SECONDS", "0.5")
    monkeypatch.setattr("va_legal_agent.search._last_request_monotonic", 99.5)
    monkeypatch.setattr("va_legal_agent.search.time.monotonic", lambda: 100.0)
    sleeps: list[float] = []
    monkeypatch.setattr("va_legal_agent.search.time.sleep", sleeps.append)

    _throttle()  # exactly one interval elapsed: wait == 0, nothing to sleep

    assert sleeps == []


def test_parse_prefers_result_link_over_decoy_anchor(monkeypatch):
    html = """
    <html><body>
    <div class="result">
      <a class="result__icon" href="https://example.com/favicon.ico">icon</a>
      <a class="result-link" href="https://example.com/real">Real link title</a>
    </div>
    </body></html>
    """
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse(html),
    )

    results = search_web("tinnitus", max_results=5)

    assert len(results) == 1
    assert results[0]["title"] == "Real link title"
    assert results[0]["url"] == "https://example.com/real"


def test_parse_collapses_multiline_title(monkeypatch):
    html = """
    <html><body>
    <div class="result">
      <a class="result__a" href="https://example.com/t">My
<b>Title</b></a>
    </div>
    </body></html>
    """
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse(html),
    )

    results = search_web("tinnitus", max_results=5)

    assert len(results) == 1
    assert results[0]["title"] == "My Title"  # newline collapsed, nodes joined


def test_parse_collapses_multiline_snippet(monkeypatch):
    html = """
    <html><body>
    <div class="result">
      <a class="result__a" href="https://example.com/t">Title</a>
      <div class="result__snippet">Line1
<b>Line2</b></div>
    </div>
    </body></html>
    """
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse(html),
    )

    results = search_web("tinnitus", max_results=5)

    assert len(results) == 1
    assert results[0]["snippet"] == "Line1 Line2"


def test_duckduckgo_search_default_max_results(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    monkeypatch.setenv("SEARCH_MIN_INTERVAL_SECONDS", "0")
    seen: dict[str, object] = {}

    def fake_parse(response, query, max_results):
        seen["max_results"] = max_results
        return [{"title": "t", "url": "https://example.com/t", "snippet": ""}]

    monkeypatch.setattr("va_legal_agent.search._parse_results", fake_parse)
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse("ok"),
    )

    DuckDuckGoProvider().search("tinnitus")

    assert seen["max_results"] == 10


def test_search_web_default_max_results(monkeypatch):
    seen: dict[str, object] = {}

    class _Provider:
        name = "duckduckgo"

        def search(self, query, max_results=10, page=1):
            seen["max_results"] = max_results
            return []

    monkeypatch.setattr("va_legal_agent.search.DuckDuckGoProvider", _Provider)

    search_web("tinnitus")

    assert seen["max_results"] == 10


def test_duckduckgo_search_passes_page_to_url(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    monkeypatch.setenv("SEARCH_MIN_INTERVAL_SECONDS", "0")
    captured: dict[str, object] = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return FakeResponse(SAMPLE_HTML)

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)

    DuckDuckGoProvider().search("tinnitus", page=2)

    assert "s=10" in captured["url"]  # page offset flows into the request URL


def test_search_web_reports_last_error_details(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("SEARCH_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setattr("va_legal_agent.search.time.sleep", lambda seconds: None)

    def fake_get(url, headers=None, timeout=None):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr("va_legal_agent.search.requests.get", fake_get)

    with pytest.raises(SearchError, match="network down"):
        search_web("tinnitus")
