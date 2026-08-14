from __future__ import annotations

import logging
import os
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class SearchError(RuntimeError):
    pass


def build_duckduckgo_url(query: str, page: int = 1) -> str:
    params = {
        "q": query,
        "kl": "us-en",
        "s": str((page - 1) * 10),
    }
    return "https://duckduckgo.com/html/?" + urlencode(params)


def extract_url_from_duckduckgo(link: str) -> str:
    """Return the real destination URL hidden inside a DuckDuckGo ``/l/?uddg=`` redirect.

    Handles the link forms returned by the HTML endpoint, e.g.
    ``//duckduckgo.com/l/?uddg=<percent-encoded-url>&rut=...``,
    ``/l/?uddg=...``, and ``https://duckduckgo.com/l/?uddg=...``.
    Non-redirect links are returned unchanged.
    """
    parsed = urlparse(link)
    is_ddg_redirect = parsed.path == "/l/" and (
        parsed.netloc == "" or parsed.netloc.endswith("duckduckgo.com")
    )
    if is_ddg_redirect:
        params = parse_qs(parsed.query)
        if "uddg" in params:
            return params["uddg"][0]
    return link


def search_web(query: str, max_results: int = 10) -> list[dict[str, str]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; VA-Legal-Agent/1.0; +https://example.com)"
    }
    url = build_duckduckgo_url(query)
    response = requests.get(url, headers=headers, timeout=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")))
    response.raise_for_status()
    if response.status_code == 202:
        raise SearchError(
            f"DuckDuckGo returned a rate-limit/challenge page for query: {query}. "
            "Slow down requests or raise SEARCH_DELAY_SECONDS."
        )

    soup = BeautifulSoup(response.text, "html.parser")
    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for idx, item in enumerate(soup.select(".result")):
        if idx >= max_results * 3:
            break
        title_el = item.select_one("a.result-link") or item.select_one("a")
        snippet_el = item.select_one(".result__snippet") or item.select_one(".snippet")
        title = title_el.get_text(" ", strip=True) if title_el else "Untitled result"
        link = title_el.get("href") if title_el else ""
        normalized = extract_url_from_duckduckgo(link)
        if not normalized or normalized in seen_urls:
            continue
        if urlparse(normalized).netloc.endswith("duckduckgo.com"):
            # Skip ad click-trackers (e.g. /y.js) and any leftover redirect links.
            continue
        seen_urls.add(normalized)
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        results.append({"title": title, "url": normalized, "snippet": snippet})
        if len(results) >= max_results:
            break
    if not results:
        if "anomaly" in response.text.lower():
            raise SearchError(
                f"DuckDuckGo returned an anomaly/challenge page instead of results for query: {query}. "
                "The provider is likely rate-limiting automated requests; slow down or raise SEARCH_DELAY_SECONDS."
            )
        raise SearchError(f"No search results returned for query: {query}")
    return results
