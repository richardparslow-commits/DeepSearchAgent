import pytest

from va_legal_agent.search import (
    SearchError,
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


def test_search_web_raises_when_no_results(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse("<html><body></body></html>"),
    )

    with pytest.raises(SearchError):
        search_web("query that returns nothing")


def test_search_web_detects_rate_limit_challenge_page(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse("<html>anomaly-modal challenge</html>", status_code=202),
    )

    with pytest.raises(SearchError, match="rate-limit"):
        search_web("any query")


def test_search_web_detects_anomaly_page_with_200_status(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.search.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse("<html>duckduckgo anomaly detected</html>"),
    )

    with pytest.raises(SearchError, match="anomaly"):
        search_web("any query")