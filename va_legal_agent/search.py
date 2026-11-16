from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Iterator
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import (
    ConnectionError as CffiConnectionError,
    RequestException as CffiRequestException,
    Timeout as CffiTimeout,
)

from .config import get_settings

logger = logging.getLogger(__name__)


# Stable machine-readable failure categories used by audit rows. Keep these
# values aligned with SOURCE_OPINION_FETCH_ERROR_KINDS in models.
SEARCH_ERROR_KINDS = frozenset(
    {
        "rate_limit",
        "authentication",
        "timeout",
        "malformed_response",
        "other",
    }
)
_RETRYABLE_SEARCH_ERROR_KINDS = frozenset({"rate_limit", "timeout"})
_MAX_SEARCH_ERROR_CAUSES = 4
_SEARCH_ERROR_MESSAGE_MARKERS = (
    (
        "malformed_response",
        (
            "invalid json",
            "malformed",
            "unexpected response",
            "not a collection",
            "response was not",
        ),
    ),
    (
        "rate_limit",
        (
            "rate limit",
            "rate-limit",
            "throttl",
            "too many requests",
        ),
    ),
    ("timeout", ("timeout", "timed out", "time out")),
    (
        "authentication",
        ("unauthorized", "authentication", "api token", "forbidden"),
    ),
)
_SEARCH_ERROR_HTTP_STATUS_MARKER = re.compile(
    r"\b(?:http(?: status)?|status(?: code)?)\s*[:=]?\s*(401|403|429)\b",
    re.IGNORECASE,
)
_SEARCH_ERROR_HTTP_STATUS_KINDS = {
    "401": "authentication",
    "403": "authentication",
    "429": "rate_limit",
}


class SearchError(RuntimeError):
    """Provider failure with an optional stable kind and retryability hint.

    The cause is still preserved for logging, but callers that build audit
    rows must not have to parse human-readable request messages to distinguish
    authentication, throttling, timeout, and malformed-response failures.
    """

    def __init__(
        self,
        message: str = "",
        *,
        kind: str = "",
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


def _search_error_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield an exception and up to three explicitly wrapped causes."""
    current: BaseException | None = exc
    for _ in range(_MAX_SEARCH_ERROR_CAUSES):
        if current is None:
            break
        yield current
        current = current.__cause__


def _search_error_status(exc: BaseException) -> int | None:
    """Find an HTTP status on an exception or one of its wrapped causes."""
    for current in _search_error_chain(exc):
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _search_error_transport_kind(exc: BaseException) -> str:
    """Classify a transport failure on an exception or its wrapped causes."""
    for current in _search_error_chain(exc):
        if isinstance(current, (requests.Timeout, CffiTimeout)):
            return "timeout"
        if isinstance(current, (requests.ConnectionError, CffiConnectionError)):
            return "connection"
    return ""


def _search_error_structured_kind(exc: BaseException) -> str | None:
    """Return the nearest valid structured kind in the exception chain."""
    for current in _search_error_chain(exc):
        kind = getattr(current, "kind", "")
        if kind in SEARCH_ERROR_KINDS:
            return kind
    return None


def _search_error_retryable(exc: BaseException) -> bool | None:
    """Return the nearest explicit retryability hint in the exception chain."""
    for current in _search_error_chain(exc):
        retryable = getattr(current, "retryable", None)
        if retryable is not None:
            return bool(retryable)
    return None


def _search_error_message_kind(exc: BaseException) -> str:
    """Return the nearest message-derived kind in the exception chain."""
    for current in _search_error_chain(exc):
        message = str(current).casefold()
        for kind, markers in _SEARCH_ERROR_MESSAGE_MARKERS:
            if any(marker in message for marker in markers):
                return kind
        status_match = _SEARCH_ERROR_HTTP_STATUS_MARKER.search(message)
        if status_match:
            return _SEARCH_ERROR_HTTP_STATUS_KINDS[status_match.group(1)]
    return ""


def classify_search_error(exc: BaseException) -> tuple[str, bool]:
    """Return a stable ``(kind, retryable)`` pair for a provider failure.

    Structured values on the outer ``SearchError`` win. Otherwise the nearest
    valid structured kind in the wrapped-cause chain is used, followed by HTTP
    status, transport type, and the nearest message marker. Explicit
    retryability follows the same outer-to-cause order and overrides fallback
    results.
    """
    explicit_kind = getattr(exc, "kind", "")
    explicit_retryable = getattr(exc, "retryable", None)
    if explicit_kind in SEARCH_ERROR_KINDS:
        if explicit_retryable is None:
            explicit_retryable = explicit_kind in _RETRYABLE_SEARCH_ERROR_KINDS
        return explicit_kind, bool(explicit_retryable)

    inherited_kind = _search_error_structured_kind(exc)
    if inherited_kind is not None:
        retryable = _search_error_retryable(exc)
        if retryable is None:
            retryable = inherited_kind in _RETRYABLE_SEARCH_ERROR_KINDS
        return inherited_kind, retryable

    status = _search_error_status(exc)
    transport_kind = _search_error_transport_kind(exc)
    message_kind = _search_error_message_kind(exc)
    kind = "other"
    retryable = False
    if status in {401, 403}:
        kind = "authentication"
    elif status == 429:
        kind = "rate_limit"
        retryable = True
    elif status in {408, 504} or transport_kind == "timeout":
        kind = "timeout"
        retryable = True
    elif message_kind:
        kind = message_kind
        retryable = kind in _RETRYABLE_SEARCH_ERROR_KINDS
    elif status is not None and 500 <= status <= 599:
        retryable = True
    elif transport_kind == "connection":
        retryable = True
    retryable_override = _search_error_retryable(exc)
    if retryable_override is not None:
        retryable = retryable_override
    return kind, retryable


# HTTP statuses that indicate transient provider throttling or server trouble
# and are worth retrying with a polite backoff.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _retry_delay(attempt: int) -> float:
    """Exponential backoff with jitter so concurrent retries don't pile up."""
    settings = get_settings()
    delay = settings.search_backoff_base_seconds * (2 ** attempt)
    return min(delay * random.uniform(0.75, 1.25), settings.search_backoff_max_seconds)


def _is_challenge(response: "requests.Response") -> bool:
    """True when DuckDuckGo served a structural rate-limit challenge.

    A challenge is identified by a 202 status, an ``/anomaly`` redirect, a
    DuckDuckGo anomaly title, or a challenge/captcha container. Plain result
    text mentioning "anomaly" is not a challenge; treating it as one discards
    valid results for queries about anomaly detection.
    """
    if response.status_code == 202:
        return True

    final_url = getattr(response, "url", "") or ""
    if "anomaly" in urlparse(str(final_url)).path.lower():
        return True

    soup = BeautifulSoup(response.text or "", "html.parser")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if "duckduckgo" in title and "anomaly" in title:
        return True

    challenge_selectors = (
        ".anomaly-modal",
        "[class*='anomaly-modal']",
        "#anomaly-modal",
        "form[action*='anomaly']",
        ".challenge-form",
        "#challenge-form",
        ".captcha",
        "[class*='captcha']",
        "iframe[src*='captcha']",
    )
    return soup.select_one(", ".join(challenge_selectors)) is not None


def _is_transient_error(exc: Exception) -> bool:
    """True for retryable network errors and throttle/server status codes.

    Covers both the plain ``requests`` client (CourtListener) and the
    curl_cffi browser-impersonating client (DuckDuckGo), whose exception
    hierarchies share the same shape but are unrelated classes.
    """
    if isinstance(
        exc,
        (
            requests.ConnectionError,
            requests.Timeout,
            CffiConnectionError,
            CffiTimeout,
        ),
    ):
        return True
    # Both exception families always set ``.response`` (defaulting to None for
    # non-HTTP errors), so the direct attribute access is safe; only the
    # ``status_code`` lookup needs a None default for ``.response is None``.
    return getattr(exc.response, "status_code", None) in _RETRYABLE_STATUSES


# Global pacing: enforces SEARCH_MIN_INTERVAL_SECONDS between actual HTTP
# requests across all concurrent search workers, so the thread pool can't
# burst DuckDuckGo.
_interval_lock = threading.Lock()
_last_request_monotonic: float | None = None
# Per-provider pacing: the last request time for each backend with an
# SEARCH_MAX_RPM_BY_PROVIDER budget, so each provider independently respects
# its own requests-per-minute cap without slowing the others.
_last_request_by_provider: dict[str, float] = {}


def _throttle(provider: str | None = None) -> None:
    """Sleep (if needed) so requests stay within pacing limits.

    Two independent limits are enforced under the same lock so concurrent
    workers can't race past them:

    * the global ``SEARCH_MIN_INTERVAL_SECONDS`` — the minimum gap between
      ANY two requests, regardless of provider;
    * the per-provider ``SEARCH_MAX_RPM_BY_PROVIDER`` budget — at most N
      requests per minute for *provider* (60/N seconds apart). Providers
      without an entry have no budget.
    """
    settings = get_settings()
    global_interval = settings.search_min_interval_seconds
    rpm = settings.search_max_rpm_by_provider.get(provider or "", 0)
    provider_interval = 60.0 / rpm if rpm > 0 else 0.0
    if global_interval <= 0 and provider_interval <= 0:
        return
    global _last_request_monotonic, _last_request_by_provider
    with _interval_lock:
        now = time.monotonic()
        if global_interval > 0 and _last_request_monotonic is not None:
            wait = global_interval - (now - _last_request_monotonic)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        if provider is not None and provider_interval > 0:
            last = _last_request_by_provider.get(provider)
            if last is not None:
                wait = provider_interval - (now - last)
                if wait > 0:
                    time.sleep(wait)
                    now = time.monotonic()
            _last_request_by_provider[provider] = now
        if global_interval > 0:
            _last_request_monotonic = now


def http_proxy_kwargs() -> dict[str, dict[str, str]]:
    """Return ``{"proxies": {...}}`` for ``SEARCH_HTTP_PROXY``, else ``{}``.

    Spread into a request call (``**http_proxy_kwargs()``) so a configured
    residential proxy is applied to both http and https, while an unset proxy
    leaves the call signature unchanged (no ``proxies`` kwarg at all, so the
    direct-connect path and its tests stay identical). ``requests`` and
    ``curl_cffi`` both accept the same ``{"http": ..., "https": ...}`` form.
    """
    proxy = get_settings().search_http_proxy.strip()
    if not proxy:
        return {}
    return {"proxies": {"http": proxy, "https": proxy}}


def build_duckduckgo_url(query: str, page: int = 1, exclude_terms: str = "") -> str:
    """Build a DuckDuckGo HTML search URL with optional ``-term`` exclusion operators.

    When *exclude_terms* is non-empty, each comma-separated term longer than
    one character is appended as a ``-term`` operator to the query, so
    DuckDuckGo pre-filters results server-side. The post-fetch
    ``_filter_excluded_terms`` gate in providers.py provides a second fence.
    """
    q = query
    tokens = [t.strip() for t in (exclude_terms or "").split(",") if len(t.strip()) >= 2]
    if tokens:
        q = q + " " + " ".join("-" + t for t in tokens)
    params = {
        "q": q,
        "kl": "us-en",
        "s": str((page - 1) * 10),
    }
    return "https://duckduckgo.com/html/?" + urlencode(params)


def _is_http_url(url: str) -> bool:
    """Return True only for an absolute HTTP(S) URL with a non-empty host."""
    parsed = urlparse(url)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def extract_url_from_duckduckgo(link: str) -> str:
    """Return the real destination URL hidden inside a DuckDuckGo ``/l/?uddg=`` redirect.

    Handles the link forms returned by the HTML endpoint, e.g.
    ``//duckduckgo.com/l/?uddg=<percent-encoded-url>&rut=...``,
    ``/l/?uddg=...``, and ``https://duckduckgo.com/l/?uddg=...``.
    A decoded destination is returned only when it is an absolute HTTP(S) URL;
    ``javascript:``, ``data:``, ``file:`` and other schemes return an empty
    string so the caller drops the result. Non-redirect links are returned
    unchanged.
    """
    parsed = urlparse(link)
    is_ddg_redirect = parsed.path == "/l/" and (
        parsed.netloc == "" or parsed.netloc.endswith("duckduckgo.com")
    )
    if is_ddg_redirect:
        params = parse_qs(parsed.query)
        if "uddg" in params:
            destination = params["uddg"][0].strip()
            return destination if _is_http_url(destination) else ""
    return link


def _parse_results(
    response: "requests.Response", query: str, max_results: int
) -> list[dict[str, str]]:
    """Extract and dedupe result links from a DuckDuckGo HTML page."""
    soup = BeautifulSoup(response.text, "html.parser")
    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for idx, item in enumerate(soup.select(".result")):
        if idx >= max_results * 3:
            break
        # DuckDuckGo renders result titles as a.result__a. Prefer that class
        # explicitly; some providers/versions use a.result-link, and the bare
        # anchor is a last-resort fallback (the first <a> in a block can be a
        # favicon, snippet, or unrelated link).
        title_el = (
            item.select_one("a.result__a")
            or item.select_one("a.result-link")
            or item.select_one("a")
        )
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
    # A parsed page with no result blocks is a legitimate zero-hit query, not
    # a provider failure: search_all records zero results and reserves
    # SearchError for transport, challenge, and parse failures.
    return results


class DuckDuckGoProvider:
    """Search provider backed by DuckDuckGo's HTML endpoint.

    DuckDuckGo rate-limits unauthenticated scraping. A 202/challenge page, a
    429, or a 5xx triggers an exponential backoff with jitter (rather than an
    immediate failure), so occasional throttling doesn't abort a research run.
    """

    name = "duckduckgo"

    def search(self, query: str, max_results: int = 10, page: int = 1) -> list[dict[str, str]]:
        settings = get_settings()
        url = build_duckduckgo_url(query, page=page)
        timeout = settings.request_timeout_seconds
        retries = settings.search_retry_attempts

        last_error: Exception = SearchError(f"Search failed for query: {query}")
        for attempt in range(retries + 1):
            if attempt > 0:
                # Back off before retrying so the provider isn't hammered. The
                # loop deliberately has no break so it always exhausts its
                # range and falls through to the raise below.
                logger.warning(
                    "DuckDuckGo throttled query %r (retry %d/%d); backing off.",
                    query, attempt, retries,
                )
                time.sleep(_retry_delay(attempt - 1))
            _throttle(self.name)
            try:
                # DuckDuckGo's html endpoint challenges the plain ``requests``
                # TLS fingerprint (403/202 anomaly page) and its bot-detection
                # keys on the User-Agent. Impersonating a real Chrome handshake
                # via curl_cffi passes both; the impersonation supplies a
                # browser User-Agent, so do not override it with the app's own
                # bot-identifying UA.
                response = cffi_requests.get(
                    url,
                    impersonate="chrome",
                    timeout=timeout,
                    **http_proxy_kwargs(),
                )
                response.raise_for_status()
            except CffiRequestException as exc:
                if not _is_transient_error(exc):
                    raise SearchError(f"Search request failed for query: {query}: {exc}") from exc
                last_error = exc
            else:
                if _is_challenge(response):
                    last_error = SearchError(
                        f"DuckDuckGo returned a rate-limit/anomaly challenge page for query: {query}. "
                        "Slow down requests or raise SEARCH_DELAY_SECONDS."
                    )
                else:
                    return _parse_results(response, query, max_results)

        if isinstance(last_error, SearchError):
            raise last_error
        raise SearchError(
            f"DuckDuckGo was repeatedly unavailable for query: {query}. Last error: {last_error}"
        ) from last_error


def search_web(query: str, max_results: int = 10) -> list[dict[str, str]]:
    """Search DuckDuckGo's HTML endpoint (page 1), retrying throttled responses.

    Backward-compatible wrapper around :class:`DuckDuckGoProvider` for code and
    tests that call the search entry point directly; the research pipeline uses
    ``va_legal_agent.providers.search_all`` instead.
    """
    return DuckDuckGoProvider().search(query, max_results)
