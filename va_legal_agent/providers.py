"""Search provider abstraction: DuckDuckGo (default), CourtListener, BVA, BVA sitemap, and BVA local index.

The research pipeline calls :func:`search_all`, which runs every configured
provider (``SEARCH_PROVIDERS``) across up to ``SEARCH_PAGES_PER_QUERY`` pages
per query and merges the results, deduping by canonical URL.

Each provider returns result dicts with the standard keys ``title``/``url``/
``snippet`` plus optional structured fields (``court``, ``citation``,
``decision_date``, ``docket``, ``judge``) that the agent layer carries into
``CaseRecord`` directly, avoiding a re-fetch.
"""

from __future__ import annotations

import email.utils
import html as html_module
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Protocol

import requests
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException as CffiRequestException

from .config import get_settings
from .fetch import extract_holding_sentences
from .queries import adapt_query_for_provider, derive_variants, strip_site_prefixes
from .search import (
    DuckDuckGoProvider,
    SearchError,
    _is_transient_error,
    _retry_delay,
    _throttle,
    http_proxy_kwargs,
)
from .topics import (
    COURT_BVA,
    COURT_CAVC,
    COURT_FEDERAL_CIRCUIT,
    COURT_SUPREME,
    COURT_UNKNOWN,
)

logger = logging.getLogger(__name__)


class SearchProvider(Protocol):
    """A search backend returning result dicts for a query."""

    name: str

    def search(self, query: str, max_results: int = 10, page: int = 1) -> list[dict[str, str]]:
        """Return up to *max_results* results for *query* on the given *page*."""


# CourtListener court id -> canonical court name (topics.COURT_*).
_CL_COURT_NAMES = {
    "cavc": COURT_CAVC,
    "cafc": COURT_FEDERAL_CIRCUIT,
    "scotus": COURT_SUPREME,
}


def _courtlistener_query(query: str) -> str:
    """Wrap *query* with the multi-court filter as a fielded ``q`` clause.

    The search engine only honors the LAST repeated ``court`` GET parameter
    (``court=cavc&court=cafc&court=scotus`` silently filters to ``scotus``
    alone), so the court filter must live inside ``q`` instead, as
    ``court_id:(cavc OR cafc OR scotus)``. An empty query (e.g. nothing but a
    stripped ``site:`` token) degrades to the bare court clause.
    """
    clause = "court_id:(" + " OR ".join(_CL_COURT_NAMES) + ")"
    return f"({query}) AND {clause}" if query else clause

# A CourtListener opinion URL carries its numeric id as either the frontend
# form (".../opinion/12345/", produced by _map_courtlistener_opinion) or the
# API form (".../api/rest/v4/opinions/12345/", the opinions-cited rows);
# match both so traversal works end to end.
_OPINION_ID_PATTERN = re.compile(r"/opinions?/(\d+)")


def _map_courtlistener_opinion(item: dict) -> dict[str, str] | None:
    """Map one CourtListener /search/ result (camelCase fields) to a result dict.

    The v4 search endpoint returns camelCase keys (``caseName``, ``court_id``,
    ``dateFiled``, ``docketNumber``, ``judge``) and a ``citation`` list of
    parallel-citation strings; the older opinions endpoint shape (snake_case
    fields on the opinion object itself) does not exist on the live API.
    """
    case_name = item.get("caseName") or "Untitled case"
    absolute_url = item.get("absolute_url") or ""
    url = "https://www.courtlistener.com" + absolute_url if absolute_url else ""
    if not url:
        return None
    citations = item.get("citation") or []
    citation = ""
    if citations:
        first = citations[0]
        if isinstance(first, str):
            citation = first
        elif isinstance(first, dict):
            citation = str(first.get("cite", ""))
    # The opinion text lives on the nested opinions[].snippet field (the search
    # endpoint carries up to 500 characters there); without it the LLM and
    # holding extraction get empty text. Sane into the standard snippet slot.
    opinions = item.get("opinions") or []
    snippet = ""
    opinion_id = ""
    if opinions and isinstance(opinions[0], dict):
        snippet = str(opinions[0].get("snippet") or "").strip()
        raw_id = opinions[0].get("id")
        opinion_id = str(raw_id) if raw_id is not None else ""
    return {
        "title": case_name,
        "url": url,
        "snippet": snippet,
        "court": _CL_COURT_NAMES.get(item.get("court_id"), COURT_UNKNOWN),
        "citation": citation,
        "decision_date": item.get("dateFiled") or "",
        "docket": item.get("docketNumber") or "",
        "judge": item.get("judge") or "",
        "courtlistener_opinion_id": opinion_id,
    }


def extract_courtlistener_opinion_id(url: str) -> int | None:
    """Return the numeric opinion id from a CourtListener opinion URL, if any."""
    match = _OPINION_ID_PATTERN.search(url or "")
    return int(match.group(1)) if match else None


# Court-header boilerplate markers — when these dominate the snippet and no
# sentence-ending punctuation appears, the snippet is just the opinion's
# header (court name, docket number, caption), not substantive text.
_HEADER_MARKERS = (
    "united states court of appeals",
    "court of appeals for veterans claims",
    "supreme court of the united states",
    "no.",  # docket number line
    "case:",  # pagination line
    "document:",  # pagination line
    "note: this disposition",
    "before:",  # panel listing
)


def _is_header_snippet(snippet: str) -> bool:
    """True when *snippet* is court-header boilerplate, not holding text.

    The search endpoint returns a 500-char excerpt from the *beginning* of
    the opinion text, which is always the court header (name, docket,
    caption, panel). A snippet with no sentence-ending punctuation (``.?!``)
    in the first 500 chars — or one where every line is a header marker —
    carries no usable text for relevance scoring or holding extraction.
    """
    if not snippet:
        return True
    lowered = snippet.lower()
    # Has real sentences → not a header snippet.
    if any(ch in lowered[:500] for ch in (".", "?", "!")):
        # But a snippet can have header punctuation (docket "No.", "Case:")
        # without real sentences. Check that at least one line isn't a marker.
        lines = [line.strip().lower() for line in snippet.splitlines() if line.strip()]
        non_header = [line for line in lines if not any(m in line for m in _HEADER_MARKERS)]
        return len(non_header) == 0
    return True


def _extract_holding_excerpt(body: str, max_chars: int = 500) -> str:
    """Extract a holding-centered excerpt from the full opinion body.

    Returns up to *max_chars* centered on the first holding sentence (a
    sentence matching the holding pattern from :func:`extract_holding_sentences`).
    Falls back to the first substantive paragraph (skipping headers) when no
    explicit holding is found. Returns ``''`` when the body is empty.
    """
    if not body or not body.strip():
        return ""
    # Reuse the fetch layer's holding extraction to find the first holding.
    holdings = extract_holding_sentences(body)
    if holdings:
        # Find the holding's position in the body to center the excerpt.
        first = holdings[0]
        pos = body.find(first)
        if pos < 0:
            return first[:max_chars]
        start = max(0, pos - 50)  # a little context before the holding
        end = min(len(body), start + max_chars)
        excerpt = body[start:end].strip()
        # Clean to a single line so the snippet slot stays compact.
        return re.sub(r"\s+", " ", excerpt)
    # No explicit holding: find the first substantive paragraph (skip lines
    # that are pure headers, pagination, or panel listings).
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    for para in paragraphs:
        lowered = para.lower()
        if any(m in lowered for m in _HEADER_MARKERS) or len(para.split()) < 6:
            continue
        return re.sub(r"\s+", " ", para)[:max_chars]
    return ""

def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header into seconds, or None if absent/invalid.

    The header is either a delta-seconds integer or an HTTP-date; the date
    form is converted to seconds-from-now (clamped at zero for past dates).
    """
    if not value:
        return None
    stripped = value.strip()
    if stripped.isdigit():
        return float(stripped)
    try:
        target = email.utils.parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delay = (target - datetime.now(timezone.utc)).total_seconds()
    return max(delay, 0.0)


def _courtlistener_retry_delay(
    attempt: int, exc: requests.RequestException | None
) -> float:
    """Backoff for a throttled CourtListener request.

    Starts from the shared exponential-with-jitter delay and honors the
    server's ``Retry-After`` header when present, capped at
    ``SEARCH_BACKOFF_MAX_SECONDS`` so a run stays bounded even when the server
    asks for a long wait. Never sleeps less than the exponential floor, so
    retries stay polite without a ``Retry-After`` hint too.
    """
    settings = get_settings()
    base = _retry_delay(attempt)
    retry_after = _parse_retry_after(
        getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After")
    )
    if retry_after is None:
        return base
    return max(base, min(retry_after, settings.search_backoff_max_seconds))


class CourtListenerProvider:
    """Structured case search over CourtListener's REST API.

    CourtListener aggregates federal court opinions with rich metadata
    (citation, decision date, docket, judges), so the results carry those
    fields directly instead of relying on HTML enrichment. The court filter
    targets CAVC, the Federal Circuit, and SCOTUS; the Board (BVA) is not on
    CourtListener, so those queries fall back to DuckDuckGo.

    Beyond search, the provider traverses the citation graph: the
    ``opinions-cited`` endpoint yields the opinions a decision cites (its
    authorities) and the later opinions that cite it (forward citations),
    which the research loop follows for multi-hop recall.

    CourtListener's v4 API now requires a token: anonymous requests get a 401.
    Set ``COURTLISTENER_API_KEY`` (a free account token) or this provider will
    fail every query.
    """

    name = "courtlistener"
    # Full-text search lives on /search/ (the opinions list endpoint rejects
    # ``q``/``court`` as unknown filter params). Search results carry the
    # metadata inline (camelCase keys), so no follow-up fetches are needed.
    SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"
    API_URL = "https://www.courtlistener.com/api/rest/v4/opinions/"
    CITATIONS_URL = "https://www.courtlistener.com/api/rest/v4/opinions-cited/"

    def _headers(self) -> dict[str, str]:
        settings = get_settings()
        headers = {"User-Agent": settings.user_agent}
        if settings.courtlistener_api_key:
            headers["Authorization"] = f"Token {settings.courtlistener_api_key}"
        return headers

    def _get_json(self, url: str, params: dict[str, object] | None = None) -> dict:
        """GET *url* with pacing and throttle-aware retries; return parsed JSON.

        Every attempt is paced by the global ``SEARCH_MIN_INTERVAL_SECONDS``
        throttle so concurrent workers cannot burst CourtListener, and a
        transient throttle (429/5xx, connection/timeout errors) is retried up
        to ``SEARCH_RETRY_ATTEMPTS`` times with a backoff that honors the
        server's ``Retry-After`` header (capped at
        ``SEARCH_BACKOFF_MAX_SECONDS``). A 401 without a token raises a hint to
        set ``COURTLISTENER_API_KEY``; any other non-transient error raises
        immediately.
        """
        settings = get_settings()
        retries = settings.search_retry_attempts
        last_error: requests.RequestException | None = None
        for attempt in range(retries + 1):
            if attempt > 0:
                delay = _courtlistener_retry_delay(attempt - 1, last_error)
                logger.warning(
                    "CourtListener throttled %s (retry %d/%d); backing off %.1fs.",
                    url, attempt, retries, delay,
                )
                time.sleep(delay)
            _throttle(self.name)
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=settings.request_timeout_seconds,
                    **http_proxy_kwargs(),
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                if not _is_transient_error(exc):
                    if (
                        not settings.courtlistener_api_key
                        and getattr(exc, "response", None) is not None
                        and getattr(exc.response, "status_code", None) == 401
                    ):
                        raise SearchError(
                            "CourtListener requires an API token (401 Unauthorized); "
                            "set COURTLISTENER_API_KEY."
                        ) from exc
                    raise SearchError(
                        f"CourtListener request failed: {url}: {exc}"
                    ) from exc
                last_error = exc
                continue
            return response.json()
        raise SearchError(
            f"CourtListener was repeatedly unavailable for {url}: {last_error}"
        ) from last_error

    def search(self, query: str, max_results: int = 10, page: int = 1) -> list[dict[str, str]]:
        # CourtListener has its own court filter; site: tokens are noise there.
        query = strip_site_prefixes(query)
        # The search endpoint returns up to 20 results per page regardless of
        # page_size and paginates with an opaque cursor in ``next``, so a
        # numeric page N means: follow the cursor chain N-1 times, then read
        # that page's results.
        params: dict[str, object] = {
            "q": _courtlistener_query(query),
            "format": "json",
        }
        try:
            data = self._get_json(self.SEARCH_URL, params)
            for _ in range(max(page, 1) - 1):
                next_url = data.get("next")
                if not next_url:
                    return []
                data = self._get_json(str(next_url))
        except SearchError as exc:
            if "COURTLISTENER_API_KEY" in str(exc):
                raise  # already the actionable no-token hint
            raise SearchError(
                f"CourtListener search failed for query: {query}: {exc}"
            ) from exc

        results: list[dict[str, str]] = []
        for item in data.get("results", []):
            mapped = _map_courtlistener_opinion(item)
            if mapped is None:
                continue
            results.append(mapped)
            if len(results) >= max_results:
                break
        if not results:
            raise SearchError(f"No search results returned for query: {query}")
        # Enrich header-only snippets with a holding-centered excerpt from
        # the opinion body so relevance scoring and the interpretation layer
        # see real text, not court boilerplate. The search endpoint's snippet
        # starts at the beginning of the text (always a header), so a result
        # that discusses the issue deeply in the body but not the header gets
        # a relevance score of 0 and is truncated out of results. Only
        # opinion-bearing results (those with an opinion id) are enriched,
        # and only when the snippet looks like a header (no sentence-ending
        # punctuation in the first 500 chars).
        for result in results:
            snippet = result.get("snippet") or ""
            opinion_id = result.get("courtlistener_opinion_id") or ""
            if not opinion_id or not _is_header_snippet(snippet):
                continue
            result["snippet"] = self._enrich_header_snippet(opinion_id, snippet)
        return results

    def _enrich_header_snippet(self, opinion_id: str, header: str) -> str:
        """Replace a header-only snippet with a holding excerpt from the body.

        Best-effort: returns *header* unchanged when the opinion detail fetch
        fails or yields no usable excerpt, so the search result keeps its
        header snippet instead of raising.
        """
        try:
            body = self.fetch_opinion_text(int(opinion_id))
            excerpt = _extract_holding_excerpt(body)
            return excerpt if excerpt else header
        except (SearchError, ValueError):
            return header

    def _get_opinion(self, opinion_id: int) -> dict[str, str] | None:
        """Fetch one opinion (plus its cluster and docket) into a result dict.

        The opinions detail endpoint carries only the opinion text and links;
        the case name, citations, decision date, and judges live on the
        opinion's *cluster*, and the docket number/court on its *docket*, so
        all three are fetched to produce the same structured fields the search
        endpoint returns in a single call.
        """
        try:
            opinion = self._get_json(f"{self.API_URL}{opinion_id}/")
            cluster_url = opinion.get("cluster")
            cluster = self._get_json(str(cluster_url)) if cluster_url else {}
            docket_url = cluster.get("docket")
            docket = self._get_json(str(docket_url)) if docket_url else {}
        except SearchError as exc:
            raise SearchError(
                f"CourtListener opinion {opinion_id} fetch failed: {exc}"
            ) from exc

        absolute_url = opinion.get("absolute_url") or ""
        url = "https://www.courtlistener.com" + absolute_url if absolute_url else ""
        if not url:
            return None
        citations = cluster.get("citations") or []
        citation = citations[0].get("cite", "") if citations else ""
        return {
            "title": cluster.get("case_name") or "Untitled case",
            "url": url,
            "snippet": "",
            "court": _CL_COURT_NAMES.get(docket.get("court_id"), COURT_UNKNOWN),
            "citation": citation,
            "decision_date": cluster.get("date_filed") or "",
            "docket": docket.get("docket_number") or "",
            "judge": cluster.get("judges") or "",
            # Carry the opinion id so enrichment/deep-read can pull full text
            # from the detail endpoint instead of the WAF-challenged frontend.
            "courtlistener_opinion_id": str(opinion_id),
        }

    def fetch_opinion_text(self, opinion_id: int) -> str:
        """Return the full opinion body for *opinion_id* from the detail endpoint.

        The frontend ``/opinion/<cluster>/`` page is AWS-WAF-challenged for
        every non-browser client (including curl_cffi), so deep-read cannot
        scrape it. The REST opinion detail endpoint instead carries the body
        in ``plain_text`` or one of the HTML variants; prefer the plain text,
        else strip markup from the richest HTML field so the chunked
        map-reduce can digest real holdings instead of empty snippets.
        """
        opinion = self._get_json(f"{self.API_URL}{opinion_id}/")
        plain = str(opinion.get("plain_text") or "").strip()
        if plain:
            return plain
        for key in ("html_with_citations", "html", "html_columbia", "html_lawbox", "html_anon_2020"):
            html_body = str(opinion.get(key) or "")
            if not html_body.strip():
                continue
            stripped = re.sub(r"<[^>]+>", " ", html_body)
            return re.sub(r"\s+", " ", stripped).strip()
        return ""

    def _related_opinions(
        self, opinion_id: int, relation: str, max_results: int
    ) -> list[dict[str, str]]:
        """Traverse one citation relation for *opinion_id*.

        ``relation`` is ``citing`` (opinions this decision cites) or ``cited``
        (opinions citing this decision); each relationship row links the other
        opinion, whose detail is fetched for its metadata.
        """
        params = {
            f"{relation}_opinion": opinion_id,
            "page_size": min(max(max_results, 1), 100),
            "format": "json",
        }
        try:
            data = self._get_json(self.CITATIONS_URL, params)
        except SearchError as exc:
            raise SearchError(
                f"CourtListener citation traversal ({relation}) failed for "
                f"opinion {opinion_id}: {exc}"
            ) from exc

        other_key = "cited_opinion" if relation == "citing" else "citing_opinion"
        results: list[dict[str, str]] = []
        seen: set[int] = set()
        for row in data.get("results", []):
            other_id = extract_courtlistener_opinion_id(row.get(other_key) or "")
            if other_id is None or other_id in seen:
                continue
            seen.add(other_id)
            mapped = self._get_opinion(other_id)
            if mapped is not None:
                results.append(mapped)
            if len(results) >= max_results:
                break
        if not results:
            raise SearchError(f"No {relation} opinions found for opinion {opinion_id}")
        return results

    def citing_opinions(self, opinion_id: int, max_results: int = 10) -> list[dict[str, str]]:
        """Return opinions citing *opinion_id* (forward citations)."""
        return self._related_opinions(opinion_id, "cited", max_results)

    def cited_opinions(self, opinion_id: int, max_results: int = 10) -> list[dict[str, str]]:
        """Return opinions *opinion_id* cites (its authorities)."""
        return self._related_opinions(opinion_id, "citing", max_results)


USAGE_URL = "https://www.courtlistener.com/api/rest/v4/api-usage/"


def fetch_courtlistener_usage() -> dict[str, object]:
    """Query CourtListener's API-usage endpoint and return its payload.

    The endpoint reports live used/remaining for every rate window (minute,
    hour, day) and has its *own* throttle (10/min, 120/hour), so calling it
    does not consume the search budget — which is exactly why it can be used
    to pace a run before hitting the wall. It requires authentication
    (``COURTLISTENER_API_KEY``); anonymous requests get a 401 and raise the
    same actionable hint as the search endpoint.
    """
    provider = CourtListenerProvider()
    try:
        return provider._get_json(USAGE_URL)
    except SearchError as exc:
        if "COURTLISTENER_API_KEY" in str(exc):
            raise  # already the actionable no-token hint
        raise SearchError(
            f"Could not check CourtListener API usage: {exc}"
        ) from exc


_DAILY_RATE_PATTERN = re.compile(r"^\d+/day$")
_MINUTE_RATE_PATTERN = re.compile(r"^\d+/min$")


def courtlistener_daily_budget(usage: dict[str, object]) -> dict[str, object]:
    """Extract the user-scope daily-window row from an api-usage payload.

    The response lists one row per scope/rate pair (e.g. ``5/min``,
    ``50/hour``, ``125/day``); the daily row is the user-scope entry whose
    rate ends in ``/day``, regardless of the exact limit (membership accounts
    have higher limits). A missing day row means the payload shape changed,
    which should raise rather than silently pass a wrong budget.
    """
    return _extract_rate_row(usage, _DAILY_RATE_PATTERN, "daily")


def courtlistener_minute_budget(usage: dict[str, object]) -> dict[str, object]:
    """Extract the user-scope per-minute row from an api-usage payload.

    Mirrors :func:`courtlistener_daily_budget` for the ``N/min`` window.
    Returns the budget dict when found, or ``None`` when the payload has no
    minute row (e.g. a different API version or a non-standard scope).
    """
    try:
        return _extract_rate_row(usage, _MINUTE_RATE_PATTERN, "per-minute")
    except SearchError:
        return {}  # absent minute row is non-fatal; callers check remaining


def _extract_rate_row(
    usage: dict[str, object], pattern: re.Pattern[str], label: str  # type: ignore[type-arg]
) -> dict[str, object]:
    """Pull a single rate-window row from an api-usage payload.

    Shared helper for the daily and per-minute budget extractors.
    """
    rows = usage.get("current_usage")
    if not isinstance(rows, list):
        raise SearchError("CourtListener api-usage response had no current_usage list.")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("scope") != "user":
            continue
        rate = str(row.get("rate", ""))
        if pattern.match(rate):
            return {
                "used": int(row.get("used", 0) or 0),
                "limit": int(row.get("limit", 0) or 0),
                "remaining": int(row.get("remaining", 0) or 0),
                "reset_at": row.get("reset_at"),
            }
    raise SearchError(
        f"Could not find the CourtListener {label} request budget in the api-usage response."
    )


def check_courtlistener_daily_budget(min_remaining: int) -> dict[str, object]:
    """Abort when CourtListener's daily budget can't cover the planned run.

    Fetches live usage, then raises :class:`SearchError` — with the numbers
    and the window reset time — when fewer than *min_remaining* daily requests
    remain. Returns the budget dict (``used``/``limit``/``remaining``/
    ``reset_at``) when there is enough headroom, so callers can surface it.
    """
    budget = courtlistener_daily_budget(fetch_courtlistener_usage())
    remaining = int(budget["remaining"])
    if remaining < min_remaining:
        reset = budget.get("reset_at") or "unknown time"
        raise SearchError(
            f"CourtListener daily request budget too low: {remaining} remaining, "
            f"need {min_remaining} for this run (used {budget['used']}/{budget['limit']}). "
            f"Daily window resets at {reset}. Wait for the reset, run fewer issues "
            "today, or disable this guard with COURTLISTENER_USAGE_GUARD=0."
        )
    return budget


def check_courtlistener_minute_budget(min_remaining: int) -> dict[str, object]:
    """Abort when CourtListener's per-minute budget can't cover the planned run.

    The free tier allows 5 requests per minute; deep-read opinion-detail
    fetches each burn one of those, so a run that plans to fetch more
    opinion details than the window allows will grind through 429s with
    60-second backoffs. This guard pre-flights the live minute window and
    aborts with the reset time when headroom is insufficient.

    Returns the minute-window budget dict on success. When the api-usage
    payload has no minute row (non-standard tier), the check is skipped
    (returns an empty dict) rather than aborting — higher tiers may have
    wider minute windows that we cannot read.
    """
    budget = courtlistener_minute_budget(fetch_courtlistener_usage())
    if not budget:
        return budget  # no minute row; skip
    remaining = int(budget["remaining"])
    if remaining < min_remaining:
        reset = budget.get("reset_at") or "the next minute window"
        raise SearchError(
            f"CourtListener per-minute budget too low: {remaining} remaining, "
            f"need {min_remaining} for this run (used {budget['used']}/{budget['limit']}). "
            f"Minute window resets at {reset}. Slow down with SEARCH_DELAY_SECONDS, "
            "reduce deep-read cases, or disable this guard with COURTLISTENER_USAGE_GUARD=0."
        )
    return budget


def traverse_citations(urls: list[str], max_results: int = 10) -> list[dict[str, str]]:
    """Follow one hop of the CourtListener citation graph from *urls*.

    For each CourtListener opinion URL, fetch the opinions citing it and the
    opinions it cites, merging the newly discovered opinions (deduped by URL)
    as standard result dicts. Non-CourtListener URLs are skipped; a per-opinion
    traversal failure is logged and skipped so one bad node does not abort the
    trail.
    """
    provider = CourtListenerProvider()
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for url in urls:
        opinion_id = extract_courtlistener_opinion_id(url)
        if opinion_id is None:
            continue
        for related in (provider.cited_opinions, provider.citing_opinions):
            try:
                for result in related(opinion_id, max_results):
                    result_url = result.get("url", "")
                    if result_url and result_url not in seen:
                        seen.add(result_url)
                        merged.append(result)
            except SearchError as exc:
                logger.warning("Citation traversal skipped opinion %s: %s", opinion_id, exc)
    return merged


_bva_session: cffi_requests.Session | None = None


def _get_bva_session() -> cffi_requests.Session:
    """Return the process-wide BVA session, creating it lazily.

    search.usa.gov issues ``AWSALB``/``AWSALBCORS`` session cookies on the
    first request. A fresh connection per query never carries them back, so a
    multi-query run looks like a stream of cookie-less bots and trips the WAF's
    challenge mid-run. Reusing one ``curl_cffi`` Session keeps the impersonated
    Chrome handshake *and* the cookie jar across the whole run, so it reads as
    a single browser session instead of a burst of anonymous clients.
    """
    global _bva_session
    if _bva_session is None:
        _bva_session = cffi_requests.Session(impersonate="chrome")
    return _bva_session


# The WAF in front of search.usa.gov's Board index challenges *long*
# court-recall queries (``site:bva.va.gov "issue" "ClaimType" veterans
# compensation``) but accepts the short issue phrase on its own -- verified
# live: ``service connection for tinnitus`` passes while
# ``service connection for tinnitus veterans compensation decision`` returns a
# 202 challenge. The Board index ranks decisions on the issue phrase alone, so
# the provider reduces every query to that phrase instead of sending the full
# recall string. A double-quoted phrase, or the quoted (or unquoted) suffix
# ``veterans compensation decision``, is the recall boilerplate we shed.
_BVA_QUOTED_PHRASE = re.compile(r'"([^"]+)"')
# Quoted statute/regulation fragments (``"1110"``, ``"3.303"``, or the long
# ``"38 U.S.C. 1110"`` form) anchor the statute-anchored queries and are not
# the issue; skip them so the issue phrase is selected instead.
_BVA_STATUTE_PHRASE = re.compile(
    r"^(?:\d+(?:\.\d+)*$|\d+\s+(?:U\.S\.C\.|C\.F\.R\.))", re.IGNORECASE
)
# The four unquoted broad-recall queries append a fixed boilerplate suffix
# after the issue (see planning._broad_queries): `` veterans compensation
# decision``, `` veterans benefits court/law``, or `` service connection
# veterans law``. Strip the known suffix (longest first) so only the issue
# remains; the word-level stoplist below is a backstop for anything else.
_BVA_BOILERPLATE_SUFFIXES = (
    "service connection veterans law",
    "veterans compensation decision",
    "veterans benefits court",
    "veterans benefits law",
    "veterans compensation",
    "veterans benefits",
)
# Trailing recall-boilerplate words dropped as a backstop from unquoted
# queries (``... tinnitus veterans compensation`` -> ``... tinnitus``).
_BVA_BOILERPLATE = frozenset(
    {
        "appeals",
        "benefit",
        "benefits",
        "board",
        "case",
        "cases",
        "compensation",
        "court",
        "decision",
        "decisions",
        "law",
        "veteran",
        "veterans",
    }
)


def _minimize_bva_query(query: str) -> str:
    """Reduce a court-recall query to the issue phrase the Board index needs.

    ``build_case_queries`` emits broad recalls such as
    ``site:bva.va.gov "service connection for tinnitus" "Compensation" veterans
    compensation`` and statute-anchored queries such as
    ``"1110" "tinnitus" veterans compensation``. search.usa.gov's WAF
    challenges those long queries but accepts the short issue phrase, and the
    Board index ranks decisions on that phrase alone, so nothing is lost by
    sending only the phrase.

    When the query *begins* with a quoted phrase, the first quoted phrase that
    is not a statute/regulation fragment is the issue (broad recalls put it
    first; statute queries put the fragment first and are skipped to the
    following phrase). An unquoted query is the issue followed by the
    boilerplate suffix, so the leading text before the first quote is taken
    and its trailing recall boilerplate is shed instead.
    """
    text = strip_site_prefixes(query).replace("\u201c", '"').replace("\u201d", '"')
    if text.lstrip().startswith('"'):
        for match in _BVA_QUOTED_PHRASE.finditer(text):
            phrase = match.group(1).strip()
            if phrase and not _BVA_STATUTE_PHRASE.match(phrase):
                return phrase
    # No leading quoted phrase (or none that names the issue): the issue is the
    # leading text before the first quote. Shed the known boilerplate suffix,
    # then any residual trailing boilerplate words, never emptying the query.
    leading = text.split('"', 1)[0]
    lower = leading.lower()
    for suffix in _BVA_BOILERPLATE_SUFFIXES:
        if lower.endswith(suffix):
            candidate = leading[: -len(suffix)].rstrip()
            if candidate:  # keep at least the issue; never strip the whole string
                leading = candidate
            break
    words = leading.split()
    while len(words) > 1 and words[-1].lower() in _BVA_BOILERPLATE:
        words.pop()
    return " ".join(words)


class BVAProvider:
    """Board of Veterans' Appeals decisions via search.usa.gov.

    The BVA publishes its decisions as plain-text files on va.gov, indexed by
    search.usa.gov under the ``bvadecisions`` affiliate. The HTML search page
    embeds the result set as JSON (``resultsData``), so we parse that instead
    of scraping markup. Each result links to a ``.txt`` decision file, which
    the fetch layer handles directly.

    search.usa.gov rate-limits anonymous requests aggressively (HTTP 202
    challenge pages), so failures surface as :class:`SearchError` and the
    caller's retry/merge logic treats them like DuckDuckGo throttling.
    """

    name = "bva"
    SEARCH_URL = "https://search.usa.gov/search"
    AFFILIATE = "bvadecisions"
    _RESULTS_KEY = '"resultsData":'

    @staticmethod
    def _parse_results_data(html_text: str) -> list[dict[str, object]]:
        """Extract the ``resultsData`` JSON embedded in a search.usa.gov page.

        The page HTML-escapes the JSON (``&quot;`` for quotes), so we unescape
        first, then locate the ``resultsData`` key and decode the object that
        follows it (robust to the surrounding script markup).
        """
        decoded = html_module.unescape(html_text)
        start = decoded.find(BVAProvider._RESULTS_KEY)
        if start < 0:
            return []
        try:
            data, _ = json.JSONDecoder().raw_decode(decoded[start + len(BVAProvider._RESULTS_KEY):].lstrip())
        except json.JSONDecodeError:
            logger.warning("Could not parse BVA resultsData JSON")
            return []
        # ``resultsData`` is normally an object, but the embedded JSON can be
        # any value (e.g. a list or scalar); guard before calling ``.get`` so a
        # non-object payload degrades to an empty result instead of raising.
        if not isinstance(data, dict):
            return []
        results = data.get("results", [])
        return results if isinstance(results, list) else []

    def search(self, query: str, max_results: int = 10, page: int = 1) -> list[dict[str, str]]:
        settings = get_settings()
        # Reduce the court-recall query to its issue phrase. The WAF in front
        # of search.usa.gov challenges long, quote-bearing, boilerplate-laden
        # research queries while accepting the short issue phrase (verified
        # live); the Board index ranks decisions on that phrase alone, so the
        # recall string's site:/quote/claim-type/statute-anchor noise is
        # dropped rather than sent. See :func:`_minimize_bva_query`.
        query = _minimize_bva_query(query)
        params = {"affiliate": self.AFFILIATE, "query": query}
        if page > 1:
            params["page"] = page
        _throttle(self.name)
        try:
            # search.usa.gov sits behind AWS WAF, which challenges the TLS
            # fingerprint of the plain ``requests`` client (HTTP 202 with an
            # empty body). A reused curl_cffi Session impersonates a real
            # Chrome handshake and carries the AWSALB session cookies between
            # queries; the impersonation also supplies a browser User-Agent, so
            # do not override it with the app's own UA.
            response = _get_bva_session().get(
                self.SEARCH_URL,
                params=params,
                timeout=settings.request_timeout_seconds,
                **http_proxy_kwargs(),
            )
            response.raise_for_status()
        except CffiRequestException as exc:
            raise SearchError(f"BVA search failed for query: {query}: {exc}") from exc

        if response.status_code == 202 or "anomaly" in response.text.lower():
            raise SearchError(
                f"BVA search returned a rate-limit/anomaly challenge page for query: {query}. "
                "Slow down requests or raise SEARCH_DELAY_SECONDS."
            )

        results: list[dict[str, str]] = []
        for item in self._parse_results_data(response.text):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not url:
                continue
            title = str(item.get("title") or "")
            # Titles look like "A25049742.txt"; surface the citation, not the extension.
            if title.lower().endswith(".txt"):
                title = title[:-4]
            description = str(item.get("description") or "")
            # Strip the <strong> highlight tags search.usa.gov injects.
            snippet = re.sub(r"<[^>]+>", "", description).strip()
            results.append(
                {
                    "title": title or "Board of Veterans' Appeals decision",
                    "url": url,
                    "snippet": snippet,
                    "court": COURT_BVA,
                    "citation": title,
                }
            )
            if len(results) >= max_results:
                break
        if not results:
            raise SearchError(f"No search results returned for query: {query}")
        return results


_SITEMAP_INDEX_URL = "https://www.va.gov/sitemap_bva.xml"
# Process-wide caches: the sitemap index and each year's leaf are fetched once
# per run (they are stable within a run), and decision bodies are cached per
# URL so repeated queries over the same recent window don't re-fetch files.
_bva_sitemap_cache: dict[str, object] = {}
# Stopwords dropped when tokenizing the issue phrase for decision matching;
# the remaining significant tokens must all appear in a decision's text.
_BVA_MATCH_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "in", "is", "it", "of", "on", "or", "that", "the", "to", "was",
        "were", "with",
    }
)


def _parse_bva_sitemap_index(xml_text: str) -> list[tuple[str, str]]:
    """Parse ``sitemap_bva.xml`` into ``[(year_code, leaf_url), ...]``.

    Each ``<sitemap><loc>https://www.va.gov/vetapp26/sitemap.xml</loc>``
    entry maps a two-digit year code (``26``) to its leaf sitemap URL.
    """
    index: list[tuple[str, str]] = []
    for loc in re.findall(r"<loc>([^<]+)</loc>", xml_text):
        match = re.search(r"/vetapp(\d{2})/sitemap\.xml$", loc)
        if match:
            index.append((match.group(1), loc))
    return index


def _parse_bva_leaf_sitemap(xml_text: str) -> list[tuple[str, str]]:
    """Parse a year leaf sitemap into ``[(decision_url, lastmod), ...]``.

    Each ``<url><loc>...txt</loc><lastmod>YYYY-MM-DD</lastmod>`` entry is a
    single Board decision file. The list is sorted newest-first by lastmod
    (ties broken by URL descending, which mirrors the sitemap's own order
    within a month) so the provider scans the most recent decisions first.
    """
    entries = re.findall(r"<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", xml_text)
    return sorted(entries, key=lambda pair: (pair[1], pair[0]), reverse=True)


def _bva_sitemap_index() -> list[tuple[str, str]]:
    """Fetch and cache the BVA sitemap index (once per process).

    Raises :class:`SearchError` on network failure, so the caller can surface
    a clean provider error instead of a bare request exception.
    """
    cached = _bva_sitemap_cache.get("index")
    if cached is not None:
        return cached  # type: ignore[return-value]
    settings = get_settings()
    try:
        response = requests.get(
            _SITEMAP_INDEX_URL,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.request_timeout_seconds,
            **http_proxy_kwargs(),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SearchError(f"BVA sitemap index fetch failed: {exc}") from exc
    index = _parse_bva_sitemap_index(response.text)
    _bva_sitemap_cache["index"] = index
    return index


def _bva_leaf_sitemap(year_code: str) -> list[tuple[str, str]]:
    """Fetch and cache one year's leaf sitemap (once per process per year)."""
    cache_key = f"leaf:{year_code}"
    cached = _bva_sitemap_cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    index = _bva_sitemap_index()
    url = next((leaf_url for code, leaf_url in index if code == year_code), None)
    if url is None:
        return []
    settings = get_settings()
    try:
        response = requests.get(
            url,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.request_timeout_seconds,
            **http_proxy_kwargs(),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SearchError(f"BVA leaf sitemap fetch failed for {url}: {exc}") from exc
    leaf = _parse_bva_leaf_sitemap(response.text)
    _bva_sitemap_cache[cache_key] = leaf
    return leaf


def _fetch_bva_decision_text(url: str) -> str:
    """Fetch one Board decision body, cached per URL across the process.

    Decision files on va.gov are WAF-free plain text; the app's fetch layer
    reads them the same way. Raises :class:`SearchError` on network failure.
    """
    cached = _bva_sitemap_cache.get(f"text:{url}")
    if cached is not None:
        return str(cached)
    settings = get_settings()
    try:
        response = requests.get(
            url,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.request_timeout_seconds,
            **http_proxy_kwargs(),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SearchError(f"BVA decision fetch failed for {url}: {exc}") from exc
    _bva_sitemap_cache[f"text:{url}"] = response.text
    return response.text


def _bva_issue_tokens(issue: str) -> list[str]:
    """Significant, lowercase tokens of the minimized issue phrase."""
    return [
        word
        for word in re.findall(r"[a-z0-9]+", issue.lower())
        if word not in _BVA_MATCH_STOPWORDS
    ]


def _bva_text_matches(text: str, issue: str) -> bool:
    """True when every significant issue token appears in the decision text."""
    tokens = _bva_issue_tokens(issue)
    if not tokens:
        return False
    lowered = text.lower()
    return all(token in lowered for token in tokens)


def _bva_snippet(text: str, issue: str) -> str:
    """A short excerpt of the decision around the first significant issue token."""
    tokens = _bva_issue_tokens(issue)
    if tokens:
        idx = text.lower().find(tokens[0])
        if idx >= 0:
            start = max(0, idx - 120)
            return " ".join(text[start : idx + 300].split())[:280]
    return " ".join(text.split())[:280]


class BvaSitemapProvider:
    """Board of Veterans' Appeals decisions straight from the va.gov sitemap.

    BVA publishes every decision as a plain-text ``.txt`` file under
    ``https://www.va.gov/vetappYY/FilesN/``. The official sitemap index
    (``sitemap_bva.xml``, the ``Described By`` target of the data.va.gov
    catalog entry) enumerates those files by year, WAF-free — no
    search.usa.gov, no challenge pages. This provider fetches the index and
    the current year's leaf sitemap once per run, scans the most recent
    ``SEARCH_BVA_SITEMAP_SCAN_LIMIT`` decision files for the issue phrase,
    and returns matching decisions.

    It complements :class:`BVAProvider` (the full-corpus search.usa.gov
    index): the sitemap path is reliable and unblocked but covers only the
    most recent decisions of the current year, so it is best used alongside
    the search index rather than as a complete replacement.
    """

    name = "bvasitemap"
    INDEX_URL = _SITEMAP_INDEX_URL

    @staticmethod
    def _current_year_code() -> str:
        return f"{datetime.now(timezone.utc).year % 100:02d}"

    def _most_recent_leaf(self) -> list[tuple[str, str]]:
        """The current year's leaf sitemap, newest-first.

        Early in a year the current year's leaf may not exist yet; fall back
        to the most recent year present in the index. Returns ``[]`` only
        when the index itself is empty.
        """
        year_code = self._current_year_code()
        leaf = _bva_leaf_sitemap(year_code)
        if leaf:
            return leaf
        index = _bva_sitemap_index()
        if not index:
            return []
        # Year codes are the last two digits of the decision year (vetapp92 is
        # 1992, vetapp26 is 2026). Codes 92-99 are therefore 19xx and codes
        # 00-91 are 20xx; comparing the two-digit code itself (numerically or
        # lexicographically) would pick 99 (1999) over 26 (2026), so map each
        # code to its full year before taking the max.
        def _full_year(code: str) -> int:
            two_digit = int(code)
            return 1900 + two_digit if two_digit >= 92 else 2000 + two_digit

        newest_year = max(index, key=lambda pair: _full_year(pair[0]))[0]
        return _bva_leaf_sitemap(newest_year)

    def search(self, query: str, max_results: int = 10, page: int = 1) -> list[dict[str, str]]:
        settings = get_settings()
        issue = _minimize_bva_query(query).strip()
        if not issue:
            issue = strip_site_prefixes(query).strip()
        leaf = self._most_recent_leaf()
        if not leaf:
            raise SearchError("No BVA decisions indexed; the va.gov sitemap is unavailable.")
        scan_limit = max(settings.search_bva_sitemap_scan_limit, 1)
        window = leaf[(page - 1) * scan_limit : page * scan_limit]
        results: list[dict[str, str]] = []
        for url, _lastmod in window:
            _throttle(self.name)
            try:
                text = _fetch_bva_decision_text(url)
            except SearchError as exc:
                logger.warning("BVA sitemap decision fetch failed for %s: %s", url, exc)
                continue
            if not _bva_text_matches(text, issue):
                continue
            title = url.rsplit("/", 1)[-1]
            if title.lower().endswith(".txt"):
                title = title[:-4]
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": _bva_snippet(text, issue),
                    "court": COURT_BVA,
                    "citation": title,
                }
            )
            if len(results) >= max_results:
                break
        if not results:
            raise SearchError(f"No search results returned for query: {query}")
        return results


_BVA_LOCAL_CORPUS = "corpus.txt"
_BVA_LOCAL_MANIFEST = "manifest.json"
_BVA_LOCAL_META = "index.meta.json"
# Byte separator written between decision bodies in corpus.txt so a
# manifest row's [start, end) range can never straddle two decisions.
_BVA_LOCAL_SEPARATOR = "\n\x00\n"
# Serializes index build/load so the agent's worker-pool fan-out can't race
# concurrent ``corpus.txt`` writes (build truncates with "w" while another
# thread reads) or trigger several redundant first-use downloads.
_bva_local_lock = threading.Lock()


def _bva_local_index_paths(directory: str) -> tuple[str, str]:
    """Return the (corpus, manifest) file paths for a local index directory."""
    return (
        os.path.join(directory, _BVA_LOCAL_CORPUS),
        os.path.join(directory, _BVA_LOCAL_MANIFEST),
    )


def _bva_local_meta_path(directory: str) -> str:
    """Return the meta file path for a local index directory."""
    return os.path.join(directory, _BVA_LOCAL_META)


def _bva_local_write_meta(
    directory: str, most_recent_lastmod: str
) -> None:
    """Write the build-time metadata after a successful build."""
    meta = {
        "build_time": datetime.now(timezone.utc).isoformat(),
        "most_recent_lastmod": most_recent_lastmod,
    }
    with open(_bva_local_meta_path(directory), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)


def _bva_local_needs_rebuild(
    directory: str, max_age_hours: int
) -> bool:
    """True when the local index is stale and should be rebuilt.

    ``max_age_hours <= 0`` disables auto-rebuild entirely: the index is built
    once on first use and never refreshed, so this returns ``False`` without
    touching the sitemap (the caller still builds when the index is *missing*,
    which is the "first use"). Otherwise:

    Returns ``True`` when:
    - The meta file is absent (first build was before meta was introduced),
    - The index is older than *max_age_hours* and a live sitemap check
      finds newer content,
    - The live sitemap's most recent lastmod is newer than the stored one.

    Returns ``False`` when the index is still fresh or the sitemap fetch
    fails (the existing index is better than nothing).
    """
    if max_age_hours <= 0:
        return False  # auto-rebuild disabled — never refresh
    meta_path = _bva_local_meta_path(directory)
    if not os.path.exists(meta_path):
        return True
    try:
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return True
    build_time_str = meta.get("BUILD_TIME", "")
    if build_time_str:
        try:
            build_time = datetime.fromisoformat(build_time_str)
        except ValueError:
            return True
        age = (datetime.now(timezone.utc) - build_time).total_seconds()
        if age <= max_age_hours * 3600:
            return False  # fresh — don't even fetch the sitemap
    # Age exceeded: check the live sitemap for newer content before deciding
    # whether to rebuild.
    stored_lastmod = meta.get("most_recent_lastmod", "")
    if stored_lastmod:
        try:
            leaf = BvaSitemapProvider()._most_recent_leaf()
        except SearchError:
            return False  # sitemap down; keep what we have
        if leaf and leaf[0][1] > stored_lastmod:
            return True
    # Stale by age but the sitemap has no newer content (or no lastmod in
    # meta to compare against): don't rebuild needlessly.
    return False


def _bva_local_build(
    directory: str,
    leaf: list[tuple[str, str]],
    max_files: int,
) -> tuple[str, list[dict[str, object]]]:
    """Download decision files into ``corpus.txt`` + byte-offset manifest.

    Concatenates each body separated by a ``\\n\\x00\\n`` sentinel so the
    manifest's ``[start, end)`` byte ranges never straddle a decision.
    Returns ``(corpus_path, manifest_rows)`` where each row is
    ``{"url": str, "title": str, "lastmod": str, "start": int, "end": int}``.

    The three files are written to ``.tmp`` siblings and committed with
    ``os.replace`` only after every temp is fully written. A full-corpus
    build takes minutes-to-hours, so a hard interruption (Ctrl-C, crash,
    power loss) mid-download must leave the previous index intact rather than
    a truncated ``corpus.txt`` paired with a stale manifest — which the
    freshness check would otherwise accept and silently serve wrong results.
    """
    os.makedirs(directory, exist_ok=True)
    corpus_path, manifest_path = _bva_local_index_paths(directory)
    meta_path = _bva_local_meta_path(directory)
    corpus_tmp = corpus_path + ".tmp"
    manifest_tmp = manifest_path + ".tmp"
    meta_tmp = meta_path + ".tmp"
    manifest: list[dict[str, object]] = []
    offset = 0
    window = leaf if max_files <= 0 else leaf[:max_files]
    with open(corpus_tmp, "w", encoding="utf-8") as corpus:
        for url, lastmod in window:
            _throttle("bvalocal")
            try:
                text = _fetch_bva_decision_text(url)
            except SearchError as exc:
                logger.warning(
                    "BVA local-index download failed for %s: %s", url, exc
                )
                continue
            if not text.strip():
                continue
            title = url.rsplit("/", 1)[-1]
            if title.lower().endswith(".txt"):
                title = title[:-4]
            start = offset
            corpus.write(text)
            corpus.write(_BVA_LOCAL_SEPARATOR)
            end = start + len(text) + len(_BVA_LOCAL_SEPARATOR)
            manifest.append(
                {
                    "url": url,
                    "title": title,
                    "lastmod": lastmod,
                    "start": start,
                    "end": end,
                }
            )
            offset = end
    with open(manifest_tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    most_recent = leaf[0][1] if leaf else ""
    with open(meta_tmp, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "build_time": datetime.now(timezone.utc).isoformat(),
                "most_recent_lastmod": most_recent,
            },
            fh,
        )
    # Commit: atomically swap the real files in only after all temps are done.
    os.replace(corpus_tmp, corpus_path)
    os.replace(manifest_tmp, manifest_path)
    os.replace(meta_tmp, meta_path)
    return corpus_path, manifest


def _bva_local_load(directory: str) -> tuple[str, list[dict[str, object]]]:
    """Read the local index from disk."""
    corpus_path, manifest_path = _bva_local_index_paths(directory)
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    return corpus_path, manifest


def _bva_local_search_corpus(
    corpus_text: str,
    manifest: list[dict[str, object]],
    tokens: list[str],
    max_results: int,
) -> list[dict[str, str]]:
    """Grep the in-memory corpus for decisions containing every token.

    Tokens are already lowercased; the search lowercases the corpus once
    and does contain-checks in O(manifest) time.
    """
    if not tokens:
        return []
    results: list[dict[str, str]] = []
    for row in manifest:
        start = int(row["start"])
        end = int(row["end"])
        text = corpus_text[start:end]
        # Strip the inter-decision separator so matching and the snippet see
        # the clean body (the separator's \x00 would otherwise leak into a
        # snippet window that reaches the end of the decision).
        if text.endswith(_BVA_LOCAL_SEPARATOR):
            text = text[: -len(_BVA_LOCAL_SEPARATOR)]
        body_lower = text.lower()
        if all(token in body_lower for token in tokens):
            results.append(
                {
                    "title": str(row["title"]),
                    "url": str(row["url"]),
                    "snippet": _bva_snippet(text, " ".join(tokens)),
                    "court": COURT_BVA,
                    "citation": str(row["title"]),
                }
            )
            if len(results) >= max_results:
                break
    return results


class BvaLocalIndexProvider:
    """Sub-second, WAF-free BVA search from an on-disk local index.

    On first use it downloads every decision file enumerated by the
    current year's leaf sitemap (optionally capped by
    ``SEARCH_BVA_LOCAL_INDEX_MAX_FILES``), concatenates their bodies
    into one ``corpus.txt`` with a byte-offset ``manifest.json``, and
    then serves every later query by reading the corpus once into
    memory — no per-query HTTP at all.

    Subsequent queries are instantaneous (sub-second even for 40k+
    decisions).  Set ``SEARCH_BVA_LOCAL_INDEX_DIR`` to an empty string
    to disable this provider.
    """

    name = "bvalocal"

    def search(
        self, query: str, max_results: int = 10, page: int = 1
    ) -> list[dict[str, str]]:
        # ``page`` is accepted for the provider protocol but ignored here: the
        # local index is a whole-corpus grep (no pagination window), so every
        # page scans the same corpus and returns the top *max_results* matches.
        settings = get_settings()
        directory = settings.search_bva_local_index_dir
        if not directory:
            raise SearchError(
                "bvalocal provider is disabled "
                "(SEARCH_BVA_LOCAL_INDEX_DIR is empty)."
            )
        issue = _minimize_bva_query(query).strip()
        if not issue:
            issue = strip_site_prefixes(query).strip()
        tokens = _bva_issue_tokens(issue)
        corpus_path, manifest_path = _bva_local_index_paths(directory)
        # The agent fans queries out across a worker pool, so several threads
        # can reach here at once. Build/load is serialized under the process
        # lock so a first-use build (which truncates corpus.txt with "w") can
        # never race a concurrent read, and so only one thread performs the
        # download. Double-check the stale/missing condition inside the lock:
        # the first thread may have already built it while the others queued.
        with _bva_local_lock:
            missing = not os.path.exists(manifest_path) or not os.path.exists(
                corpus_path
            )
            if missing or _bva_local_needs_rebuild(
                directory, settings.search_bva_local_index_max_age_hours
            ):
                try:
                    leaf = BvaSitemapProvider()._most_recent_leaf()
                except SearchError:
                    if missing:
                        raise SearchError(
                            "No BVA decisions indexed; the va.gov "
                            "sitemap is unavailable."
                        ) from None
                    # Index exists but sitemap is down — use the stale copy.
                else:
                    if not leaf:
                        if missing:
                            raise SearchError(
                                "No BVA decisions indexed; the va.gov "
                                "sitemap is unavailable."
                            )
                    else:
                        _bva_local_build(
                            directory,
                            leaf,
                            settings.search_bva_local_index_max_files,
                        )
            with open(corpus_path, encoding="utf-8") as fh:
                corpus_text = fh.read()
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        results = _bva_local_search_corpus(
            corpus_text, manifest, tokens, max_results
        )
        if not results:
            raise SearchError(
                f"No search results returned for query: {query}"
            )
        return results


_PROVIDERS: dict[str, type[SearchProvider]] = {
    "duckduckgo": DuckDuckGoProvider,
    "courtlistener": CourtListenerProvider,
    "bva": BVAProvider,
    "bvasitemap": BvaSitemapProvider,
    "bvalocal": BvaLocalIndexProvider,
}


def get_provider(name: str) -> SearchProvider:
    """Instantiate the named provider, raising for unknown names."""
    try:
        return _PROVIDERS[name]()
    except KeyError as exc:
        raise ValueError(f"Unknown search provider: {name!r}") from exc


def resolve_search_providers(raw: str | None = None) -> list[str]:
    """Return the provider names that will actually run, skipping unknown ones.

    Does not log; use :func:`validate_search_providers` when a warning is
    wanted. An empty list falls back to ``duckduckgo``; a list of only unknown
    names yields no providers.
    """
    if raw is None:
        raw = get_settings().search_providers
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        return ["duckduckgo"]
    return [name for name in names if name in _PROVIDERS]


def validate_search_providers(raw: str | None = None) -> list[str]:
    """Resolve the ``SEARCH_PROVIDERS`` list, warning about unknown names.

    Returns the names that are actually registered; typo'd entries are logged
    (not fatal) and skipped. An empty list falls back to ``duckduckgo``.
    Call at startup — including ``--show-config`` runs — so misconfigurations
    surface immediately, and from :func:`search_all` for library users.
    """
    if raw is None:
        raw = get_settings().search_providers
    names = [n.strip() for n in raw.split(",") if n.strip()]
    for name in names:
        if name not in _PROVIDERS:
            logger.warning(
                "Unknown search provider in SEARCH_PROVIDERS: %r; available: %s. "
                "Skipping it.",
                name,
                ", ".join(sorted(_PROVIDERS)),
            )
    return resolve_search_providers(raw)


_TELEMETRY_KEYS = ("queries_issued", "results", "deduped", "failures")


def rollup_search_telemetry(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    """Sum per-provider telemetry records into a ``{provider: {stat: count}}`` dict.

    Shared by the batch summary and the analysis output so both report the
    same recall picture. Each row carries the aggregate counters plus a
    ``variants`` dict mapping each expanded query to its own
    ``{results, failures}`` counts, merged across records by variant text.
    Records with unknown providers are skipped.
    """
    rolled: dict[str, dict[str, object]] = {}
    for record in records:
        provider = str(record.get("provider", ""))
        if not provider:
            continue
        row = rolled.setdefault(provider, {key: 0 for key in _TELEMETRY_KEYS})
        row.setdefault("variants", {})
        for key in _TELEMETRY_KEYS:
            row[key] = int(row[key]) + int(record.get(key, 0) or 0)
        variants = record.get("variants")
        if not isinstance(variants, dict):
            continue
        # row["variants"] is always the dict setdefault inserted above: a fresh
        # row is created without the key and gets {} here, and an existing row's
        # "variants" was set the same way. An isinstance guard is therefore
        # provably unreachable, so it is intentionally omitted (a broken
        # invariant should raise rather than silently skip).
        target = row["variants"]
        for variant, vstat in variants.items():
            if not isinstance(vstat, dict):
                continue
            vrow = target.setdefault(str(variant), {"results": 0, "failures": 0})
            vrow["results"] += int(vstat.get("results", 0) or 0)
            vrow["failures"] += int(vstat.get("failures", 0) or 0)
    return rolled


def recall_flags(telemetry: dict[str, dict[str, object]]) -> list[str]:
    """Flag low-recall / failing providers from rolled-up search telemetry.

    Emits one flag per provider that either had failed query attempts or
    returned nothing despite issuing queries. Used both in the analysis output
    (``search_flags``) and the text report's Gaps block, so the JSON and the
    human-readable report always agree.
    """
    flags: list[str] = []
    for provider, row in sorted(telemetry.items()):
        queries = int(row.get("queries_issued", 0) or 0)
        results = int(row.get("results", 0) or 0)
        failures = int(row.get("failures", 0) or 0)
        if failures > 0:
            flags.append(
                f"Search provider {provider} had {failures} failed query attempt(s); "
                "results may be incomplete."
            )
        elif queries > 0 and results == 0:
            flags.append(
                f"Search provider {provider} returned no results across {queries} queries; "
                "consider broadening the issue phrasing."
            )
    return flags


def _filter_excluded_terms(
    results: list[dict[str, str]], exclude_terms: str
) -> list[dict[str, str]]:
    """Remove results whose title or snippet contains any excluded term.

    Matching is case-insensitive and whole-word (\b-bounded). An empty
    *exclude_terms* string (or one that produces no tokens longer than one
    character) returns *results* unchanged.
    """
    tokens = [t.strip() for t in (exclude_terms or "").split(",") if len(t.strip()) >= 2]
    if not tokens:
        return results
    # Build a single regex — it is cheaper to scan each result once.
    pattern = re.compile("|".join(re.escape(t) for t in tokens), re.IGNORECASE)
    return [
        r
        for r in results
        if not pattern.search(r.get("title", ""))
        and not pattern.search(r.get("snippet", ""))
    ]


def search_all(
    query: str,
    max_results: int = 10,
    telemetry: list[dict[str, object]] | None = None,
    deadline: float | None = None,
) -> list[dict[str, str]]:
    """Run *query* across all configured providers, pages, and variants, merging results.

    Providers are taken from ``SEARCH_PROVIDERS`` (comma-separated names,
    default ``duckduckgo``). The query is expanded into up to
    ``SEARCH_QUERY_VARIANTS`` variants (see :func:`derive_variants`), with
    ``SEARCH_QUERY_VARIANTS_BY_PROVIDER`` overriding the limit per provider
    (e.g. ``bva=0`` disables expansion on a throttling backend); each variant
    runs up to ``SEARCH_PAGES_PER_QUERY`` pages (overridable per provider via
    ``SEARCH_PAGES_PER_QUERY_BY_PROVIDER``) and is adapted for the provider
    (e.g. ``site:`` tokens stripped for CourtListener). Results are deduped by
    canonical URL and capped at *max_results*.

    When *telemetry* (a list) is given, one record per provider is appended
    with ``provider``, ``queries_issued`` (successful search calls; failures
    are counted separately), ``results``
    (raw results returned), ``deduped`` (dropped as URL duplicates),
    ``failures`` (search attempts that raised), and ``variants`` — a dict
    mapping each expanded query to its own ``{results, failures}`` counts so
    callers can see which variant phrasings actually surfaced cases.

    *deadline* is a ``time.monotonic()`` timestamp; once the wall clock passes
    it, the loop stops starting new provider calls and raises ``SearchError``
    (so a caller enforcing a wall-time budget can abandon the query's
    remaining variants/pages instead of letting the nested retry loops run to
    exhaustion). ``None`` disables the check.
    """
    settings = get_settings()
    provider_names = validate_search_providers(settings.search_providers)
    # A fully-unknown SEARCH_PROVIDERS list resolves to no providers (see
    # validate_search_providers); there is nothing to run, so return an empty
    # result rather than dividing by zero below. Callers treat an empty merge
    # as "no results", which surfaces as the usual no-cases error.
    if not provider_names:
        return []
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    errors: list[Exception] = []
    # Fair-share the result cap across providers so the first-listed provider
    # cannot fill *max_results* alone and starve the rest (e.g. courtlistener
    # returning a full page of 20 before bva ever runs). Each provider gets at
    # least one slot; the merged list is still capped at *max_results* overall.
    per_provider_budget = max(1, -(-max_results // len(provider_names)))
    for name in provider_names:
        try:
            provider = get_provider(name)
        except ValueError as exc:
            logger.warning("%s; skipping.", exc)
            continue
        pages = max(
            settings.search_pages_per_query_by_provider.get(
                name, settings.search_pages_per_query
            ),
            1,
        )
        limit = settings.search_query_variants_by_provider.get(
            name, settings.search_query_variants
        )
        variants = derive_variants(query, limit=limit)
        variant_stats: dict[str, dict[str, int]] = {}
        stats: dict[str, object] = {
            "provider": name,
            "queries_issued": 0,
            "results": 0,
            "deduped": 0,
            "failures": 0,
            "variants": variant_stats,
        }
        provider_remaining = per_provider_budget
        for variant in variants:
            adapted = adapt_query_for_provider(variant, name)
            vstat = variant_stats.setdefault(adapted, {"results": 0, "failures": 0})
            for page in range(1, pages + 1):
                if provider_remaining <= 0:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    raise SearchError(f"Search wall-time budget exhausted for query {query!r}")
                try:
                    results = provider.search(adapted, provider_remaining, page=page)
                except SearchError as exc:
                    logger.warning(
                        "Provider %s returned no results for query %r: %s", name, adapted, exc
                    )
                    errors.append(exc)
                    stats["failures"] = int(stats["failures"]) + 1
                    vstat["failures"] += 1
                    continue
                stats["queries_issued"] = int(stats["queries_issued"]) + 1
                # Filter out results whose title/snippet match an excluded term
                # before counting them so excluded noise doesn't consume slots.
                if settings.search_exclude_terms:
                    filtered = _filter_excluded_terms(results, settings.search_exclude_terms)
                    excluded_count = len(results) - len(filtered)
                    stats["deduped"] = int(stats["deduped"]) + excluded_count
                    results = filtered
                stats["results"] = int(stats["results"]) + len(results)
                vstat["results"] += len(results)
                for result in results:
                    url = result.get("url")
                    if url and url not in seen:
                        seen.add(url)
                        merged.append(result)
                        provider_remaining -= 1
                    else:
                        stats["deduped"] = int(stats["deduped"]) + 1
                    if len(merged) >= max_results:
                        if telemetry is not None:
                            telemetry.append(stats)
                        return merged
        if telemetry is not None:
            telemetry.append(stats)
    if not merged and errors:
        raise errors[-1]
    return merged
