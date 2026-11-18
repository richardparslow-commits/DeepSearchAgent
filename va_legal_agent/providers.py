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
import hashlib
import html as html_module
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

import requests
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException as CffiRequestException

from .config import get_settings
from .courtlistener_cache import CACHE_VERSION, CacheEntry, CourtListenerCache
from .fetch import extract_holding_sentences
from .queries import adapt_query_for_provider, derive_variants, strip_site_prefixes
from .search import (
    DuckDuckGoProvider,
    SearchError,
    _is_transient_error,
    _retry_delay,
    _throttle,
    classify_search_error,
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


def _response_digest(payload: object) -> str:
    """Return a stable digest of one raw provider response payload."""
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        serialized = repr(payload)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass
class ProviderResponseTrace:
    """Per-audit observation of cache use and raw response payloads."""

    cache_hits: int = 0
    cache_misses: int = 0
    cache_entry_age_seconds: int = 0
    response_digests: set[str] = field(default_factory=set)

    def record_cache_hit(self, age_seconds: int) -> None:
        self.cache_hits += 1
        self.cache_entry_age_seconds = max(
            self.cache_entry_age_seconds, max(0, age_seconds)
        )

    def record_cache_miss(self) -> None:
        self.cache_misses += 1

    def record_response(self, payload: object) -> None:
        self.response_digests.add(_response_digest(payload))

    @property
    def response_digest(self) -> str:
        """Aggregate unique response payloads into one deterministic digest."""
        if not self.response_digests:
            return ""
        material = "\n".join(sorted(self.response_digests))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


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
_CLUSTER_ID_PATTERN = re.compile(r"/clusters?/(\d+)")
_DOCKET_ID_PATTERN = re.compile(r"/dockets?/(\d+)")


def _positive_id(value: object) -> int | None:
    """Return a positive numeric id from an int, digit string, or URL."""
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit():
        parsed = int(text)
        return parsed if parsed > 0 else None
    match = _OPINION_ID_PATTERN.search(text)
    if not match:
        return None
    parsed = int(match.group(1))
    return parsed if parsed > 0 else None


def _cluster_citation_values(values: object) -> list[str]:
    """Return every parallel citation string in a cluster citation list."""
    if not isinstance(values, list):
        return []
    citations: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("cite") or value.get("citation") or ""
        text = str(value or "").strip()
        if text and text not in citations:
            citations.append(text)
    return citations


def _cluster_opinion_ids(cluster: dict) -> list[int]:
    """Return every valid sub-opinion id in a cluster, preserving order."""
    ids: list[int] = []
    for key in ("sub_opinions", "opinions"):
        values = cluster.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict):
                value = value.get("resource_uri") or value.get("url") or value.get("id")
            parsed = _positive_id(value)
            if parsed is not None and parsed not in ids:
                ids.append(parsed)
    return ids


def _cluster_identifier(cluster: dict, cluster_url: str = "") -> str:
    """Return a cluster id from the object or one of its URL fields."""
    raw_id = cluster.get("id")
    if raw_id is not None and str(raw_id).strip().isdigit():
        return str(int(str(raw_id).strip()))
    for value in (
        cluster.get("resource_uri"),
        cluster.get("url"),
        cluster.get("absolute_url"),
        cluster_url,
    ):
        match = _CLUSTER_ID_PATTERN.search(str(value or ""))
        if match:
            return match.group(1)
    return ""


def _id_from_url(value: object, pattern: re.Pattern[str]) -> int | None:
    """Return the positive numeric id in a CourtListener API URL."""
    match = pattern.search(str(value or ""))
    if not match:
        return None
    parsed = int(match.group(1))
    return parsed if parsed > 0 else None


def _courtlistener_public_url(value: object) -> str:
    """Return an absolute CourtListener page URL from a relative API URL."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    return "https://www.courtlistener.com/" + text.lstrip("/")


def _panel_names(value: object) -> str:
    """Normalize CourtListener panel names into one advisory display string.

    The API mixes display names with links to the people endpoint. Only names
    are usable for display here; judge links are deliberately skipped rather
    than exposing opaque numeric ids as if they were names.
    """
    if isinstance(value, str):
        values: list[object] = [value]
    elif isinstance(value, list):
        values = value
    else:
        return ""
    names: list[str] = []
    for entry in values:
        if isinstance(entry, dict):
            entry = entry.get("name") or entry.get("full_name") or ""
        text = str(entry or "").strip()
        if not text or text.startswith(("http://", "https://")) or "/people/" in text:
            continue
        if text not in names:
            names.append(text)
    return "; ".join(names)


def _docket_identifier(docket: dict) -> str:
    """Return a positive CourtListener docket id from an object or URL."""
    raw_id = docket.get("id")
    if raw_id is not None and str(raw_id).strip().isdigit():
        parsed = int(str(raw_id).strip())
        if parsed > 0:
            return str(parsed)
    for value in (
        docket.get("resource_uri"),
        docket.get("url"),
        docket.get("absolute_url"),
    ):
        parsed = _id_from_url(value, _DOCKET_ID_PATTERN)
        if parsed is not None:
            return str(parsed)
    return ""


def _docket_public_url(docket_id: str, case_name: str = "") -> str:
    """Build a public docket URL when the API omitted its absolute URL.

    CourtListener's docket route requires a slug segment even though the slug
    is not used to resolve the page. Deriving it from the case name gives a
    readable, stable fallback; the numeric id alone (``/docket/<id>/``) does
    not resolve. The generic ``docket`` slug is used when no name is known.
    """
    if not docket_id:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", str(case_name or "").lower()).strip("-")
    return (
        "https://www.courtlistener.com/docket/"
        f"{docket_id}/{slug or 'docket'}/"
    )


def _opinion_result(
    opinion: dict,
    cluster: dict,
    docket: dict,
    opinion_id: int,
    cluster_url: str = "",
) -> dict[str, object] | None:
    """Build the citator's normalized metadata from opinion/cluster/docket."""
    absolute_url = opinion.get("absolute_url") or ""
    url = "https://www.courtlistener.com" + absolute_url if absolute_url else ""
    if not url:
        return None
    citations = _cluster_citation_values(cluster.get("citations"))
    citation = citations[0] if citations else ""
    opinion_ids = _cluster_opinion_ids(cluster) or [opinion_id]
    docket_id = _docket_identifier(docket)
    case_name = str(cluster.get("case_name") or "")
    recap_url = _courtlistener_public_url(docket.get("absolute_url"))
    if not recap_url:
        recap_url = _docket_public_url(docket_id, case_name)
    return {
        "title": cluster.get("case_name") or "Untitled case",
        "url": url,
        "snippet": "",
        "court": _CL_COURT_NAMES.get(docket.get("court_id"), COURT_UNKNOWN),
        "citation": citation,
        "citations": citations,
        "cluster_id": _cluster_identifier(cluster, str(cluster_url or "")),
        "cluster_resolved": bool(cluster),
        "opinion_ids": opinion_ids,
        "decision_date": cluster.get("date_filed") or "",
        "docket": docket.get("docket_number") or "",
        "judge": cluster.get("judges") or "",
        "panel": _panel_names(cluster.get("panel"))
        or _panel_names(docket.get("panel_str")),
        "recap_docket_url": recap_url,
        "recap_docket_id": docket_id,
        "oral_argument_date": str(docket.get("date_argued") or ""),
        # The citator needs the individual opinion's role (lead, dissent,
        # etc.) and the cluster's precedential designation before it can
        # decide whether a treatment finding is authoritative.
        "opinion_type": str(opinion.get("type") or ""),
        "precedential_status": str(cluster.get("precedential_status") or ""),
        # Carry the opinion id so enrichment/deep-read can pull full text
        # from the detail endpoint instead of the WAF-challenged frontend.
        "courtlistener_opinion_id": str(opinion_id),
    }


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
    # Some responses carry a single citation string instead of a list;
    # indexing it directly would yield the first character ("1").
    if isinstance(citations, str):
        citations = [citations]
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
    docket_id = str(item.get("docket_id") or "").strip()
    if not docket_id.isdigit() or int(docket_id) <= 0:
        docket_id = ""
    return {
        "title": case_name,
        "url": url,
        "snippet": snippet,
        "court": _CL_COURT_NAMES.get(item.get("court_id"), COURT_UNKNOWN),
        "citation": citation,
        "decision_date": item.get("dateFiled") or "",
        "docket": item.get("docketNumber") or "",
        "judge": item.get("judge") or "",
        "panel": _panel_names(item.get("panel_names")),
        "recap_docket_url": _docket_public_url(docket_id, case_name),
        "recap_docket_id": docket_id,
        "oral_argument_url": "",
        "oral_argument_date": str(item.get("dateArgued") or ""),
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


def _minimize_courtlistener_query(query: str) -> str:
    """Adapt a recall query for CourtListener's full-text search engine.

    CourtListener's search engine matches quoted phrases literally, so a
    phrase-quoted issue (``"Service connection for Aid and Attendance"``)
    returns 0 results while the unquoted words match real opinions. Unlike
    :func:`_minimize_bva_query`, this adapter preserves statute/regulation
    fragments (``1110``, ``3.303``) because CourtListener's full-text index
    includes them and they add useful specificity.

    For a broad recall query such as
    ``site:uscourts.cavc.gov "tinnitus" "Compensation" veterans compensation``,
    the first non-statute quoted phrase is taken as the issue (unquoted) and
    remaining boilerplate is dropped, yielding ``tinnitus``.

    For a statute-anchored query such as
    ``"1110" "tinnitus" veterans compensation``, both the statute fragment and
    the issue phrase are kept, yielding ``1110 tinnitus`` instead of dropping
    the statute number entirely.
    """
    text = strip_site_prefixes(query).replace("\u201c", '"').replace("\u201d", '"')

    if text.lstrip().startswith('"'):
        phrases = _BVA_QUOTED_PHRASE.findall(text)
        parts: list[str] = []
        found_issue = False
        for phrase in phrases:
            phrase = phrase.strip()
            if not phrase:
                continue
            if _BVA_STATUTE_PHRASE.match(phrase):
                # Statute/regulation fragment: CourtListener's full-text
                # index includes these, so keep it for specificity.
                parts.append(phrase)
            elif not found_issue:
                # First non-statute quoted phrase is the issue. Insert it
                # unquoted so CourtListener tokenizes on individual words
                # rather than requiring an exact phrase match.
                parts.append(phrase)
                found_issue = True
            # Skip remaining quoted phrases (boilerplate: "Compensation",
            # "veterans compensation", etc.) — the court_id filter already
            # scopes to veterans courts.
        if parts:
            return " ".join(parts)

    # Fallback: unquoted query (or a query whose only quoted phrases are
    # statute fragments with nothing else). Shed the boilerplate suffix,
    # then any trailing recall-boilerplate words, never emptying the query.
    leading = text.split('"', 1)[0].strip()
    lower = leading.lower()
    for suffix in _BVA_BOILERPLATE_SUFFIXES:
        if lower.endswith(suffix):
            candidate = leading[: -len(suffix)].rstrip()
            if candidate:
                leading = candidate
            break
    words = leading.split()
    while len(words) > 1 and words[-1].lower() in _BVA_BOILERPLATE:
        words.pop()
    return " ".join(words)


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
    which the research loop follows for multi-hop recall. Citator ranking
    metadata can also be fetched in batches through the list endpoints, with
    per-opinion fallback when a collection response is unavailable.

    CourtListener's v4 API now requires a token: anonymous requests get a 401.
    Set ``COURTLISTENER_API_KEY`` (a free account token) or this provider will
    fail every query.
    """

    name = "courtlistener"
    api_version = "v4"
    cache_version = CACHE_VERSION
    BASE_URL = "https://www.courtlistener.com/api/rest/v4/"
    # Full-text search lives on /search/ (the opinions list endpoint rejects
    # ``q``/``court`` as unknown filter params). Search results carry the
    # metadata inline (camelCase keys), so no follow-up fetches are needed.
    SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"
    API_URL = "https://www.courtlistener.com/api/rest/v4/opinions/"
    CLUSTERS_URL = "https://www.courtlistener.com/api/rest/v4/clusters/"
    DOCKETS_URL = "https://www.courtlistener.com/api/rest/v4/dockets/"
    AUDIO_URL = "https://www.courtlistener.com/api/rest/v4/audio/"
    BATCH_SIZE = 100
    CITATIONS_URL = "https://www.courtlistener.com/api/rest/v4/opinions-cited/"
    CITATION_LOOKUP_URL = "https://www.courtlistener.com/api/rest/v4/citation-lookup/"
    # The endpoint accepts blocks up to roughly fifty pages (64,000 chars).
    CITATION_LOOKUP_MAX_CHARS = 64_000

    def __init__(self, cache: CourtListenerCache | None = None):
        self._cache = cache
        self._request_trace_local = threading.local()
        if cache is not None:
            return
        settings = get_settings()
        directory = (settings.courtlistener_cache_dir or "").strip()
        if not directory or settings.courtlistener_cache_ttl_seconds <= 0:
            return
        try:
            self._cache = CourtListenerCache(
                directory, settings.courtlistener_cache_ttl_seconds
            )
        except OSError as exc:
            logger.warning(
                "CourtListener cache unavailable at %s: %s", directory, exc
            )

    def _cache_target(
        self, url: str, params: dict[str, object] | None
    ) -> tuple[str, str, dict[str, object]] | None:
        """Classify a GET as one of the four cached response families.

        Detail paths are keyed by numeric entity id, while citation-edge pages
        are keyed by direction and opinion id plus their full query (page size
        and cursor). Collection endpoints are intentionally not cached here;
        they are batch operations rather than shared entity reads.
        """
        if self._cache is None:
            return None
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        path = parts.path.rstrip("/") + "/"
        effective: dict[str, object] = dict(
            parse_qsl(parts.query, keep_blank_values=True)
        )
        if params:
            effective.update(params)

        for base, kind in (
            (self.API_URL, "opinion"),
            (self.CLUSTERS_URL, "cluster"),
            (self.DOCKETS_URL, "docket"),
        ):
            prefix = urlsplit(base).path.rstrip("/") + "/"
            if not path.startswith(prefix):
                continue
            identifier = path[len(prefix) :].strip("/")
            if identifier.isdigit():
                return kind, identifier, effective

        citation_path = urlsplit(self.CITATIONS_URL).path.rstrip("/") + "/"
        if path == citation_path:
            identifier = "collection"
            for key in ("cited_opinion", "citing_opinion"):
                parsed = _positive_id(effective.get(key))
                if parsed is not None:
                    identifier = f"{key}:{parsed}"
                    break
            return "citation_edges", identifier, effective
        return None

    def invalidate_cache(
        self, *, kind: str | None = None, identifier: str | None = None
    ) -> int:
        """Invalidate matching cache entries and return how many were removed.

        No filters clears the whole cache. Otherwise ``kind`` and/or
        ``identifier`` narrow the deletion, using the same names stored in the
        cache records (``opinion``, ``cluster``, ``docket``, or
        ``citation_edges``).
        """
        if self._cache is None:
            return 0
        return self._cache.invalidate(kind=kind, identifier=identifier)

    def _headers(self) -> dict[str, str]:
        settings = get_settings()
        headers = {"User-Agent": settings.user_agent}
        if settings.courtlistener_api_key:
            headers["Authorization"] = f"Token {settings.courtlistener_api_key}"
        return headers

    def _active_traces(self) -> tuple[ProviderResponseTrace, ...]:
        stack = getattr(self._request_trace_local, "stack", ())
        return tuple(stack)

    @contextmanager
    def request_trace(self) -> Iterator[ProviderResponseTrace]:
        """Collect cache and response evidence for one bounded audit."""
        trace = ProviderResponseTrace()
        stack = getattr(self._request_trace_local, "stack", None)
        if stack is None:
            stack = []
            self._request_trace_local.stack = stack
        stack.append(trace)
        try:
            yield trace
        finally:
            for index, active in enumerate(stack):
                if active is trace:
                    del stack[index]
                    break

    def _record_cache_hit(self, entry: CacheEntry) -> None:
        for trace in self._active_traces():
            trace.record_cache_hit(entry.age_seconds)
            trace.record_response(entry.response)

    def _record_cache_miss(self) -> None:
        for trace in self._active_traces():
            trace.record_cache_miss()

    def _record_response(self, payload: object) -> None:
        for trace in self._active_traces():
            trace.record_response(payload)

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
        cache_target = self._cache_target(url, params)
        if cache_target is not None and self._cache is not None:
            entry = self._cache.get_entry(*cache_target)
            if entry is not None:
                self._record_cache_hit(entry)
                if not isinstance(entry.response, dict):
                    raise SearchError(
                        f"CourtListener cache entry for {url} is not a JSON object",
                        kind="malformed_response",
                        retryable=False,
                    )
                return entry.response
            self._record_cache_miss()
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
                            "set COURTLISTENER_API_KEY.",
                            kind="authentication",
                            retryable=False,
                        ) from exc
                    kind, retryable = classify_search_error(exc)
                    raise SearchError(
                        f"CourtListener request failed: {url}: {exc}",
                        kind=kind,
                        retryable=retryable,
                    ) from exc
                last_error = exc
                continue
            try:
                data = response.json()
            except ValueError as exc:
                raise SearchError(
                    f"CourtListener returned invalid JSON for {url}: {exc}",
                    kind="malformed_response",
                    retryable=False,
                ) from exc
            self._record_response(data)
            if cache_target is not None and self._cache is not None:
                try:
                    kind, identifier, cache_params = cache_target
                    self._cache.set(
                        kind,
                        identifier,
                        data,
                        source_url=url,
                        params=cache_params,
                    )
                except (OSError, TypeError, ValueError) as exc:
                    logger.warning(
                        "Could not write CourtListener cache entry for %s: %s",
                        url,
                        exc,
                    )
            if not isinstance(data, dict):
                raise SearchError(
                    f"CourtListener returned a non-object JSON payload for {url}",
                    kind="malformed_response",
                    retryable=False,
                )
            return data
        kind, retryable = (
            classify_search_error(last_error)
            if last_error is not None
            else ("other", False)
        )
        raise SearchError(
            f"CourtListener was repeatedly unavailable for {url}: {last_error}",
            kind=kind,
            retryable=retryable,
        ) from last_error

    def _post_json(self, url: str, data: dict[str, object]) -> object:
        """POST form *data* to *url* with the same retry policy as GETs.

        CourtListener's citation-lookup API is a form-encoded POST. Pacing,
        transient-error retries, the no-token 401 hint, and proxy handling are
        deliberately identical to :meth:`_get_json` so the lookup consumes the
        same throttles and quota as every other CourtListener request.
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
                response = requests.post(
                    url,
                    data=data,
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
                            "set COURTLISTENER_API_KEY.",
                            kind="authentication",
                            retryable=False,
                        ) from exc
                    kind, retryable = classify_search_error(exc)
                    raise SearchError(
                        f"CourtListener request failed: {url}: {exc}",
                        kind=kind,
                        retryable=retryable,
                    ) from exc
                last_error = exc
                continue
            try:
                data = response.json()
            except ValueError as exc:
                raise SearchError(
                    f"CourtListener returned invalid JSON for {url}: {exc}",
                    kind="malformed_response",
                    retryable=False,
                ) from exc
            self._record_response(data)
            return data
        kind, retryable = (
            classify_search_error(last_error)
            if last_error is not None
            else ("other", False)
        )
        raise SearchError(
            f"CourtListener was repeatedly unavailable for {url}: {last_error}",
            kind=kind,
            retryable=retryable,
        ) from last_error

    def lookup_citations(self, text: str) -> list[dict[str, object]]:
        """Resolve every case citation in *text* with one batched POST.

        Returns the endpoint's per-citation result objects. A ``status`` of
        200 means the citation was found; 404, 300, 400, and 429 identify
        unresolved, ambiguous, invalid-reporter, and over-the-cap citations
        respectively. An empty list means Eyecite found no case citations (or
        *text* was blank), so no lookup request is made for blank input.
        """
        if not text.strip():
            return []
        payload = text[: self.CITATION_LOOKUP_MAX_CHARS]
        try:
            data = self._post_json(self.CITATION_LOOKUP_URL, {"text": payload})
        except SearchError as exc:
            if "COURTLISTENER_API_KEY" in str(exc):
                raise  # already the actionable no-token hint
            raise SearchError(
                f"CourtListener citation lookup failed: {exc}"
            ) from exc
        # The endpoint returns [] when no citation is parsed; some API
        # versions serialize that empty result as {}. Treat both as "nothing
        # to flag" rather than as a failed lookup.
        if data == {}:
            return []
        if not isinstance(data, list):
            raise SearchError(
                "CourtListener citation lookup returned an unexpected response "
                f"({type(data).__name__}); expected a list of citation results."
            )
        return [item for item in data if isinstance(item, dict)]

    def search(self, query: str, max_results: int = 10, page: int = 1) -> list[dict[str, str]]:
        """Keyword search over CourtListener's case-law index."""
        return self._search(query, max_results, page, semantic=False)

    def semantic_search(
        self, query: str, max_results: int = 10, page: int = 1
    ) -> list[dict[str, str]]:
        """Plain-language semantic search over CourtListener's case-law index.

        This is intentionally separate from :meth:`search`: semantic queries
        are used only for targeted gap refinement, so the initial broad
        recall fan-out keeps its cheaper keyword behavior. The endpoint's
        ``semantic=true`` mode embeds the query server-side; POSTing a local
        embedding is unnecessary for this application.
        """
        return self._search(query, max_results, page, semantic=True)

    def _search(
        self,
        query: str,
        max_results: int,
        page: int,
        semantic: bool,
    ) -> list[dict[str, str]]:
        if semantic:
            query = query.strip()
            if not query:
                raise SearchError("empty semantic query")
        else:
            # CourtListener has its own court filter; site: tokens are noise
            # there. Minimize the recall query for its full-text engine: drop
            # the quoted boilerplate, keep statute fragments for specificity,
            # and de-quote the issue phrase so the tokenizer matches individual
            # words rather than requiring exact phrase matches.
            query = _minimize_courtlistener_query(query)
            if not query.strip():
                raise SearchError("empty query after CourtListener adaptation")
        # The search endpoint returns up to 20 results per page regardless of
        # page_size and paginates with an opaque cursor in ``next``, so a
        # numeric page N means: follow the cursor chain N-1 times, then read
        # that page's results.
        params: dict[str, object] = {
            "q": _courtlistener_query(query),
            "format": "json",
        }
        if semantic:
            # Semantic search is available only for the case-law engine.
            params["semantic"] = "true"
            params["type"] = "o"
        label = "semantic search" if semantic else "search"
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
                f"CourtListener {label} failed for query: {query}: {exc}"
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

    def _get_opinion(self, opinion_id: int) -> dict[str, object] | None:
        """Fetch one opinion (plus its cluster and docket) into a result dict.

        The opinions detail endpoint carries only the opinion text and links;
        the case name, citations, decision date, and judges live on the
        opinion's *cluster*, and the docket number/court on its *docket*, so
        all three are fetched to produce the same structured fields the search
        endpoint returns in a single call. The result also carries the cluster
        id, every parallel citation, and every sub-opinion id so the citator
        can resolve the whole case rather than the one opinion it started
        from.
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

        return _opinion_result(
            opinion, cluster, docket, opinion_id, str(cluster_url or "")
        )

    def fetch_oral_argument(self, docket_id: object) -> dict[str, str]:
        """Return the newest public oral-argument link for *docket_id*.

        The audio endpoint is the only stable source of a recording's public
        page; the docket response exposes API links rather than a ready-to-use
        frontend URL. An empty result means no recording was returned, while a
        request failure raises :class:`SearchError` for the best-effort caller
        to report without invalidating the case.
        """
        text = str(docket_id or "").strip()
        if not text.isdigit() or int(text) <= 0:
            return {}
        try:
            data = self._get_json(
                self.AUDIO_URL,
                {
                    "docket": int(text),
                    "order_by": "-date_created",
                    "page_size": 1,
                    "format": "json",
                },
            )
        except SearchError as exc:
            raise SearchError(
                f"CourtListener oral-argument lookup failed for docket {text}: {exc}"
            ) from exc
        for row in data.get("results", []):
            if not isinstance(row, dict):
                continue
            url = _courtlistener_public_url(row.get("absolute_url"))
            if not url:
                continue
            return {
                "oral_argument_url": url,
                "oral_argument_date": str(row.get("date_argued") or ""),
            }
        return {}

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

    def citing_edges(
        self,
        opinion_id: int,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> dict[str, object]:
        """Return one raw page of forward-citation edges for *opinion_id*.

        The citator needs the relationship rows' own ``citing_opinion``
        links, the response ``count``, and the cursor in ``next``. Unlike
        :meth:`citing_opinions`, this deliberately does not fetch each cited
        opinion's cluster/docket; the citator reads only the capped set it
        will actually review and reports the remainder as incomplete.
        """
        params: dict[str, object] = {
            "cited_opinion": opinion_id,
            "page_size": min(max(page_size, 1), 100),
            "format": "json",
        }
        if cursor:
            params["cursor"] = cursor
        try:
            data = self._get_json(self.CITATIONS_URL, params)
        except SearchError as exc:
            raise SearchError(
                f"CourtListener citing opinions failed for opinion {opinion_id}: {exc}",
                kind=exc.kind,
                retryable=exc.retryable,
            ) from exc
        if not isinstance(data, dict):
            raise SearchError(
                "CourtListener citing opinions returned an unexpected response "
                f"for opinion {opinion_id} ({type(data).__name__}); expected an object.",
                kind="malformed_response",
                retryable=False,
            )
        return data

    def same_docket_decisions(
        self, opinion_id: int, max_results: int = 5
    ) -> list[dict[str, str]]:
        """Return other opinion clusters attached to the target's docket.

        This is deliberately a **within-docket** advisory signal. It does not
        follow an appeal to a new court, infer treatment, or establish finality.
        The docket response supplies the cluster links; each linked cluster is
        fetched for display metadata. A fetch failure is raised so the caller
        can report the signal as unavailable instead of silently showing an
        empty list.
        """
        try:
            opinion = self._get_json(f"{self.API_URL}{opinion_id}/")
            cluster_url = str(opinion.get("cluster") or "")
            if not cluster_url:
                return []
            target_cluster = self._get_json(cluster_url)
            docket_url = str(target_cluster.get("docket") or "")
            if not docket_url:
                return []
            docket = self._get_json(docket_url)
            cluster_key = cluster_url.rstrip("/")
            cluster_urls = [
                str(value).rstrip("/")
                for value in (docket.get("clusters") or [])
                if value and str(value).rstrip("/") != cluster_key
            ][: max(0, max_results)]

            decisions: list[dict[str, str]] = []
            for value in cluster_urls:
                cluster = self._get_json(value + "/")
                absolute_url = str(cluster.get("absolute_url") or "")
                url = (
                    "https://www.courtlistener.com" + absolute_url
                    if absolute_url
                    else ""
                )
                citations = _cluster_citation_values(cluster.get("citations"))
                citation = citations[0] if citations else ""
                decisions.append(
                    {
                        "case_name": str(
                            cluster.get("case_name") or "Untitled case"
                        ),
                        "citation": citation,
                        "court": _CL_COURT_NAMES.get(
                            docket.get("court_id"), COURT_UNKNOWN
                        ),
                        "decision_date": str(cluster.get("date_filed") or ""),
                        "url": url,
                        "cluster_id": str(
                            cluster.get("id") or value.rsplit("/", 1)[-1]
                        ),
                    }
                )
            return decisions
        except SearchError as exc:
            raise SearchError(
                "CourtListener same-docket lookup failed for opinion "
                f"{opinion_id}: {exc}"
            ) from exc

    def opinion_metadata(self, opinion_id: int) -> dict[str, object] | None:
        """Return the citator's opinion/cluster/docket metadata for one opinion."""
        return self._get_opinion(opinion_id)

    def opinion_metadata_batch(
        self, opinion_ids: list[int]
    ) -> dict[int, dict[str, object]]:
        """Return citator metadata for many opinions without a chain per id.

        Each chunk uses at most three collection requests (opinions, clusters,
        dockets) filtered by ``id__in`` instead of one opinion -> cluster ->
        docket chain for every candidate. Returned rows are accepted only when
        their own id matches a requested id; missing or malformed rows are
        omitted so the caller can retry them individually. A failed collection
        request raises :class:`SearchError`, which the caller must treat as
        unavailable batch metadata, not as a definitive absence.
        """
        requested: list[int] = []
        for value in opinion_ids:
            parsed = _positive_id(value)
            if parsed is not None and parsed not in requested:
                requested.append(parsed)
        results: dict[int, dict[str, object]] = {}
        for start in range(0, len(requested), self.BATCH_SIZE):
            chunk = requested[start : start + self.BATCH_SIZE]
            results.update(self._opinion_metadata_batch_chunk(chunk))
        return results

    def _opinion_metadata_batch_chunk(
        self, opinion_ids: list[int]
    ) -> dict[int, dict[str, object]]:
        """Resolve one batch chunk through collection requests."""
        requested = set(opinion_ids)
        opinions: dict[int, dict] = {}
        for row in self._batch_rows(self.API_URL, opinion_ids, "opinion"):
            opinion_id = _positive_id(
                row.get("id") or row.get("resource_uri") or row.get("url")
            )
            if opinion_id in requested:
                opinions[opinion_id] = row

        cluster_links: dict[int, tuple[str, int | None]] = {}
        cluster_ids: list[int] = []
        for opinion_id, opinion in opinions.items():
            cluster_url = str(opinion.get("cluster") or "")
            cluster_id = _id_from_url(cluster_url, _CLUSTER_ID_PATTERN)
            if cluster_url and cluster_id is None:
                continue
            cluster_links[opinion_id] = (cluster_url, cluster_id)
            if cluster_id is not None and cluster_id not in cluster_ids:
                cluster_ids.append(cluster_id)

        requested_clusters = set(cluster_ids)
        clusters: dict[int, dict] = {}
        for row in self._batch_rows(self.CLUSTERS_URL, cluster_ids, "cluster"):
            cluster_id = _cluster_identifier(row)
            if not cluster_id.isdigit():
                continue
            parsed = int(cluster_id)
            if parsed in requested_clusters:
                clusters[parsed] = row

        docket_links: dict[int, tuple[str, int | None, str, int | None]] = {}
        docket_ids: list[int] = []
        for opinion_id, (cluster_url, cluster_id) in cluster_links.items():
            if cluster_id is not None and cluster_id not in clusters:
                continue
            cluster = clusters.get(cluster_id, {}) if cluster_id is not None else {}
            docket_url = str(cluster.get("docket") or "")
            docket_id = _id_from_url(docket_url, _DOCKET_ID_PATTERN)
            if docket_url and docket_id is None:
                continue
            docket_links[opinion_id] = (
                cluster_url,
                cluster_id,
                docket_url,
                docket_id,
            )
            if docket_id is not None and docket_id not in docket_ids:
                docket_ids.append(docket_id)

        requested_dockets = set(docket_ids)
        dockets: dict[int, dict] = {}
        for row in self._batch_rows(self.DOCKETS_URL, docket_ids, "docket"):
            docket_id = _id_from_url(
                row.get("resource_uri") or row.get("url"), _DOCKET_ID_PATTERN
            )
            if docket_id is None:
                raw_id = row.get("id")
                if raw_id is not None and str(raw_id).strip().isdigit():
                    docket_id = int(str(raw_id).strip())
            if docket_id in requested_dockets:
                dockets[docket_id] = row

        results: dict[int, dict[str, object]] = {}
        for opinion_id, opinion in opinions.items():
            links = docket_links.get(opinion_id)
            if links is None:
                continue
            cluster_url, cluster_id, _docket_url, docket_id = links
            if docket_id is not None and docket_id not in dockets:
                continue
            metadata = _opinion_result(
                opinion,
                clusters.get(cluster_id, {}) if cluster_id is not None else {},
                dockets.get(docket_id, {}) if docket_id is not None else {},
                opinion_id,
                cluster_url,
            )
            if metadata is not None:
                results[opinion_id] = metadata
        return results

    def _batch_rows(
        self, url: str, ids: list[int], label: str
    ) -> list[dict]:
        """Fetch collection rows for *ids*, following cursor pages safely."""
        if not ids:
            return []
        params: dict[str, object] | None = {
            "id__in": ",".join(str(value) for value in ids),
            "page_size": self.BATCH_SIZE,
            "format": "json",
        }
        rows: list[dict] = []
        while True:
            try:
                data = self._get_json(url, params)
            except SearchError as exc:
                raise SearchError(
                    f"CourtListener batch {label} fetch failed: {exc}"
                ) from exc
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                raise SearchError(
                    f"CourtListener batch {label} response was not a collection."
                )
            rows.extend(row for row in results if isinstance(row, dict))
            next_url = data.get("next")
            if not next_url or len(rows) >= len(ids):
                break
            url = str(next_url)
            params = None
        return rows


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
    budget = _extract_rate_row(usage, _DAILY_RATE_PATTERN, "daily")
    if budget is None:
        raise SearchError(
            "Could not find the CourtListener daily request budget in the api-usage response."
        )
    return budget


def courtlistener_minute_budget(
    usage: dict[str, object],
) -> dict[str, object] | None:
    """Extract the user-scope per-minute row from an api-usage payload.

    Mirrors :func:`courtlistener_daily_budget` for the ``N/min`` window, but
    distinguishes an absent row (``None``; non-standard tier) from a malformed
    payload, which raises :class:`SearchError` from :func:`_extract_rate_row`.
    """
    return _extract_rate_row(usage, _MINUTE_RATE_PATTERN, "minute")


def _usage_counter(row: dict[str, object], key: str, label: str) -> int:
    """Coerce one usage counter, reporting a malformed payload as SearchError."""
    try:
        return int(row.get(key, 0) or 0)
    except (TypeError, ValueError) as exc:
        raise SearchError(f"malformed {label} usage row: {row!r}") from exc


def _extract_rate_row(
    usage: dict[str, object],
    pattern: re.Pattern[str],  # type: ignore[type-arg]
    label: str = "CourtListener",
) -> dict[str, object] | None:
    """Pull a single rate-window row from an api-usage payload.

    Returns ``None`` when the payload is well-formed but has no user row for
    *pattern*; malformed payloads (including a non-list ``current_usage`` or
    non-numeric counters) raise :class:`SearchError`.
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
                "used": _usage_counter(row, "used", label),
                "limit": _usage_counter(row, "limit", label),
                "remaining": _usage_counter(row, "remaining", label),
                "reset_at": row.get("reset_at"),
            }
    return None


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
    if budget is None:
        return {}  # no minute row; skip
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


def traverse_citations(
    urls: list[str], max_results: int = 10, deadline: float | None = None
) -> list[dict[str, str]]:
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
        if deadline is not None and time.monotonic() >= deadline:
            logger.warning(
                "Citation traversal aborted: deadline reached (%d opinions found so far).",
                len(merged),
            )
            break
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
_bva_session_lock = threading.Lock()


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
        with _bva_session_lock:
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
    if text.startswith('"'):
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
    # A statute-only quoted query has no issue phrase to select. Returning an
    # empty query makes search.usa.gov serve unfiltered recent decisions, so
    # keep the already site-stripped and trimmed query as a non-empty fallback.
    return " ".join(words) or text


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
        if not query:
            raise SearchError("BVA search requires a non-empty query")
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

        # A genuine results page can legitimately mention "anomaly" (e.g. a
        # congenital-anomaly claim); only treat it as a challenge when the
        # results payload is absent.
        if response.status_code == 202 or (
            "anomaly" in response.text.lower() and '"resultsData"' not in response.text
        ):
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
# Decision bodies are large; bound the per-URL text cache so a long run over
# many recent decisions cannot grow memory without limit.
_BVA_TEXT_CACHE_MAX = 512
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
            timeout=(10, settings.request_timeout_seconds),
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
            timeout=(10, settings.request_timeout_seconds),
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
            timeout=(10, settings.request_timeout_seconds),
            **http_proxy_kwargs(),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SearchError(f"BVA decision fetch failed for {url}: {exc}") from exc
    text_keys = [key for key in _bva_sitemap_cache if key.startswith("text:")]
    while len(text_keys) >= _BVA_TEXT_CACHE_MAX:
        _bva_sitemap_cache.pop(text_keys.pop(0), None)
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

    @staticmethod
    def _full_year(code: str) -> int:
        """Map a two-digit sitemap year code to its full year.

        Codes are the last two digits of the decision year (vetapp92 is 1992,
        vetapp26 is 2026). Codes 92-99 are therefore 19xx and codes 00-91 are
        20xx; comparing the two-digit code itself would pick 99 (1999) over 26
        (2026), so every ordering uses this mapping.
        """
        two_digit = int(code)
        return 1900 + two_digit if two_digit >= 92 else 2000 + two_digit

    def _recent_leaves(
        self, limit: int
    ) -> list[tuple[str, int, list[tuple[str, str]]]]:
        """The newest *limit* year leaves, as ``[(code, year, entries), ...]``.

        Years come from the official sitemap index (not the calendar), newest
        first. A year whose leaf cannot be fetched is skipped so one bad year
        cannot block the remaining shards. Returns ``[]`` when the index is
        empty or *limit* is non-positive.
        """
        if limit <= 0:
            return []
        index = _bva_sitemap_index()
        if not index:
            return []
        ordered = sorted(
            index, key=lambda pair: self._full_year(pair[0]), reverse=True
        )
        leaves: list[tuple[str, int, list[tuple[str, str]]]] = []
        for code, _url in ordered[:limit]:
            try:
                leaf = _bva_leaf_sitemap(code)
            except SearchError as exc:
                logger.warning(
                    "BVA leaf sitemap fetch failed for year %s: %s", code, exc
                )
                continue
            if leaf:
                leaves.append((code, self._full_year(code), leaf))
        return leaves

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
        newest_year = max(
            index, key=lambda pair: self._full_year(pair[0])
        )[0]
        return _bva_leaf_sitemap(newest_year)

    def search(self, query: str, max_results: int = 10, page: int = 1) -> list[dict[str, str]]:
        settings = get_settings()
        issue = _minimize_bva_query(query)
        leaf = self._most_recent_leaf()
        if not leaf:
            raise SearchError("No BVA decisions indexed; the va.gov sitemap is unavailable.")
        scan_limit = max(settings.search_bva_sitemap_scan_limit, 1)
        page = max(page, 1)
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


def _bva_local_index_paths(
    directory: str, year: int | None = None
) -> tuple[str, str]:
    """Return the (corpus, manifest) paths for one year shard.

    ``year=None`` addresses the legacy single-corpus layout (``corpus.txt`` /
    ``manifest.json``); a full year selects that year's shard
    (``corpus-2026.txt`` / ``manifest-2026.json``). Keeping the legacy form
    preserves existing callers and lets an old index keep serving while the
    sharded index is rebuilt.
    """
    if year is None:
        return (
            os.path.join(directory, _BVA_LOCAL_CORPUS),
            os.path.join(directory, _BVA_LOCAL_MANIFEST),
        )
    return (
        os.path.join(directory, f"corpus-{year}.txt"),
        os.path.join(directory, f"manifest-{year}.json"),
    )


def _bva_local_meta_path(directory: str, year: int | None = None) -> str:
    """Return the meta file path for one shard (or the legacy index)."""
    if year is None:
        return os.path.join(directory, _BVA_LOCAL_META)
    return os.path.join(directory, f"index-{year}.meta.json")


def _bva_local_write_meta(
    directory: str, most_recent_lastmod: str, year: int | None = None
) -> None:
    """Write the build-time metadata after a successful shard build.

    Retained as a standalone helper for existing test callers; production
    builds write meta inside :func:`_bva_local_build`.
    """
    meta = {
        "build_time": datetime.now(timezone.utc).isoformat(),
        "most_recent_lastmod": most_recent_lastmod,
    }
    if year is not None:
        meta["year"] = year
    with open(_bva_local_meta_path(directory, year), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)


def _bva_local_needs_rebuild(
    directory: str, max_age_hours: int, year: int | None = None
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
    fails (the existing index is better than nothing). With *year*, the check
    is scoped to that shard and leaf, so unchanged historical years are not
    rebuilt merely because time passed.
    """
    if max_age_hours <= 0:
        return False  # auto-rebuild disabled — never refresh
    meta_path = _bva_local_meta_path(directory, year)
    if not os.path.exists(meta_path):
        return True
    try:
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return True
    build_time_str = meta.get("build_time", "")
    if build_time_str:
        try:
            build_time = datetime.fromisoformat(build_time_str)
        except (TypeError, ValueError):
            return True
        # Hand-written meta, restored backups, and older builds may carry a
        # naive timestamp. Treat it as UTC instead of letting the aware/naive
        # subtraction raise TypeError.
        if build_time.tzinfo is None:
            build_time = build_time.replace(tzinfo=timezone.utc)
        try:
            age = (datetime.now(timezone.utc) - build_time).total_seconds()
        except TypeError:
            return True  # un-ageable timestamp → stale, rebuild
        if age <= max_age_hours * 3600:
            return False  # fresh — don't even fetch the sitemap
    # Age exceeded: check the live sitemap for newer content before deciding
    # whether to rebuild.
    stored_lastmod = meta.get("most_recent_lastmod", "")
    if stored_lastmod:
        try:
            if year is None:
                leaf = BvaSitemapProvider()._most_recent_leaf()
            else:
                leaf = _bva_leaf_sitemap(f"{year % 100:02d}")
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
    year: int | None = None,
) -> tuple[str, list[dict[str, object]]]:
    """Download decision files into ``corpus.txt`` + character-offset manifest.

    Concatenates each body separated by a ``\\n\\x00\\n`` sentinel so the
    manifest's ``[start, end)`` character ranges never straddle a decision.
    Returns ``(corpus_path, manifest_rows)`` where each row is
    ``{"url": str, "title": str, "lastmod": str, "start": int, "end": int}``.

    The three files are written to ``.tmp`` siblings and committed with
    ``os.replace`` only after every temp is fully written. A full-corpus
    build takes minutes-to-hours, so a hard interruption (Ctrl-C, crash,
    power loss) mid-download must leave the previous index intact rather than
    a truncated ``corpus.txt`` paired with a stale manifest — which the
    freshness check would otherwise accept and silently serve wrong results.

    The per-file swaps are individually atomic, not atomic as a set; readers
    must hold ``_bva_local_lock`` across the commit and their shard reads
    (see :class:`BvaLocalIndexProvider`) so no query observes a half-swapped
    corpus/manifest/meta generation.
    """
    os.makedirs(directory, exist_ok=True)
    corpus_path, manifest_path = _bva_local_index_paths(directory, year)
    meta_path = _bva_local_meta_path(directory, year)
    corpus_tmp = corpus_path + ".tmp"
    manifest_tmp = manifest_path + ".tmp"
    meta_tmp = meta_path + ".tmp"
    manifest: list[dict[str, object]] = []
    offset = 0
    window = leaf if max_files <= 0 else leaf[:max_files]
    # newline="" keeps the manifest's character offsets identical to what a
    # newline="" reader sees, regardless of platform line-ending translation.
    with open(corpus_tmp, "w", encoding="utf-8", newline="") as corpus:
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
    meta = {
        "build_time": datetime.now(timezone.utc).isoformat(),
        "most_recent_lastmod": most_recent,
    }
    if year is not None:
        meta["year"] = year
    with open(meta_tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    # Commit: swap the real files in only after all temps are done. The caller
    # holds _bva_local_lock across build and reads, so the set is never
    # observed half-committed.
    os.replace(corpus_tmp, corpus_path)
    os.replace(manifest_tmp, manifest_path)
    os.replace(meta_tmp, meta_path)
    return corpus_path, manifest


def _bva_local_load(
    directory: str, year: int | None = None
) -> tuple[str, list[dict[str, object]]]:
    """Read one local index shard from disk (legacy index when year is None).

    Retained for existing test callers; production queries read shards under
    ``_bva_local_lock`` inside :class:`BvaLocalIndexProvider`.
    """
    corpus_path, manifest_path = _bva_local_index_paths(directory, year)
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    return corpus_path, manifest


def _bva_local_shards(
    directory: str,
) -> list[tuple[int | None, str, str]]:
    """Return available shards, newest-first, as ``(year, corpus, manifest)``.

    A year shard is usable only when both its corpus and manifest exist. When
    no year shards are present, a legacy single-corpus index is returned as
    ``(None, ...)`` so an existing installation keeps serving results while
    the sharded index is rebuilt.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    shards: list[tuple[int | None, str, str]] = []
    for name in names:
        match = re.fullmatch(r"corpus-(\d{4})\.txt", name)
        if match is None:
            continue
        year = int(match.group(1))
        corpus_path = os.path.join(directory, name)
        manifest_path = os.path.join(directory, f"manifest-{year}.json")
        if os.path.exists(manifest_path):
            shards.append((year, corpus_path, manifest_path))
    if shards:
        return sorted(shards, key=lambda item: item[0], reverse=True)
    legacy_corpus, legacy_manifest = _bva_local_index_paths(directory)
    if os.path.exists(legacy_corpus) and os.path.exists(legacy_manifest):
        return [(None, legacy_corpus, legacy_manifest)]
    return []


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
    """Sub-second, WAF-free BVA search from per-year on-disk shards.

    On first use it reads the official sitemap index, takes the newest
    ``SEARCH_BVA_LOCAL_INDEX_YEARS`` years (default 5), and builds one
    ``corpus-YYYY.txt`` + ``manifest-YYYY.json`` shard per year. Each shard is
    downloaded and committed independently with its own metadata, so a long
    first build is resumable: a failure in one year does not roll back years
    that already finished, and the next query fills only the missing or stale
    shards. ``SEARCH_BVA_LOCAL_INDEX_MAX_FILES`` caps downloads per shard.

    Queries scan shards newest-first and load one shard at a time, keeping
    memory bounded to a single year instead of the whole five-year corpus.
    A legacy single-corpus index still serves results when no year shards
    exist. Set ``SEARCH_BVA_LOCAL_INDEX_DIR`` to an empty string to disable
    this provider.
    """

    name = "bvalocal"

    def search(
        self, query: str, max_results: int = 10, page: int = 1
    ) -> list[dict[str, str]]:
        # ``page`` is accepted for the provider protocol but ignored here: the
        # local index is a whole-shard grep (no pagination window), so every
        # page scans the same shards and returns the top *max_results* matches.
        settings = get_settings()
        directory = settings.search_bva_local_index_dir
        if not directory:
            raise SearchError(
                "bvalocal provider is disabled "
                "(SEARCH_BVA_LOCAL_INDEX_DIR is empty)."
            )
        issue = _minimize_bva_query(query)
        tokens = _bva_issue_tokens(issue)
        # The agent fans queries out across a worker pool, so several threads
        # can reach here at once. Build/load is serialized under the process
        # lock so a first-use build cannot race a concurrent shard discovery,
        # and so only one thread performs the downloads. Each file swap is
        # atomic but the three-file commit is not atomic as a set, so shard
        # reads stay under the same lock to never observe a half-swapped
        # corpus/manifest generation.
        with _bva_local_lock:
            shards = _bva_local_shards(directory)
            wanted = settings.search_bva_local_index_years
            year_shards = [shard for shard in shards if shard[0] is not None]
            # Steady state: a full set of fresh shards needs no sitemap check,
            # keeping ordinary queries HTTP-free. Discovery runs on first use,
            # when a shard is missing, and when a shard has aged out.
            fresh_full_set = len(year_shards) >= wanted
            if fresh_full_set:
                for year, _corpus, _manifest in year_shards[:wanted]:
                    if _bva_local_needs_rebuild(
                        directory,
                        settings.search_bva_local_index_max_age_hours,
                        year=year,
                    ):
                        fresh_full_set = False
                        break
            if fresh_full_set:
                targets = []
            else:
                try:
                    targets = BvaSitemapProvider()._recent_leaves(wanted)
                except SearchError as exc:
                    if not shards:
                        raise SearchError(
                            "No BVA decisions indexed; the va.gov "
                            "sitemap is unavailable."
                        ) from None
                    logger.warning(
                        "BVA sitemap unavailable; serving existing local shards: %s",
                        exc,
                    )
                    targets = []
            if targets:
                os.makedirs(directory, exist_ok=True)
                for _code, year, leaf in targets:
                    corpus_path, manifest_path = _bva_local_index_paths(
                        directory, year
                    )
                    missing = not os.path.exists(manifest_path) or not os.path.exists(
                        corpus_path
                    )
                    if missing or _bva_local_needs_rebuild(
                        directory,
                        settings.search_bva_local_index_max_age_hours,
                        year=year,
                    ):
                        _bva_local_build(
                            directory,
                            leaf,
                            settings.search_bva_local_index_max_files,
                            year=year,
                        )
                shards = _bva_local_shards(directory)
            if not shards:
                raise SearchError(
                    "No BVA decisions indexed; the va.gov sitemap is unavailable."
                )
        results: list[dict[str, str]] = []
        remaining = max_results
        with _bva_local_lock:
            for _year, corpus_path, manifest_path in shards:
                with open(corpus_path, encoding="utf-8", newline="") as fh:
                    corpus_text = fh.read()
                with open(manifest_path, encoding="utf-8") as fh:
                    manifest = json.load(fh)
                results.extend(
                    _bva_local_search_corpus(
                        corpus_text, manifest, tokens, remaining
                    )
                )
                remaining = max_results - len(results)
                if remaining <= 0:
                    break
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


_TELEMETRY_KEYS = ("queries_issued", "results", "deduped", "excluded", "failures")


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
    # Build a single whole-word regex — it is cheaper to scan each result once.
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(t) for t in tokens) + r")\b",
        re.IGNORECASE,
    )
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
    exact URL string and capped at *max_results*.

    When *telemetry* (a list) is given, one record per provider is appended
    with ``provider``, ``queries_issued`` (successful search calls; failures
    are counted separately), ``results``
    (raw results returned), ``deduped`` (dropped as exact-URL duplicates),
    ``excluded`` (dropped by ``SEARCH_EXCLUDE_TERMS``),
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
            "excluded": 0,
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
                    # Preserve the counters accumulated so far; the raise
                    # skips the loop-end append below.
                    if telemetry is not None:
                        telemetry.append(stats)
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
                    stats["excluded"] = int(stats["excluded"]) + excluded_count
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
