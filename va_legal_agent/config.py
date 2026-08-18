"""Shared configuration: a typed settings object plus guarded env parsing."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; VA-Legal-Agent/1.0; veterans-law research assistant)"
)

# Default cap on downloaded response size (bytes). Court opinions are typically
# well under 5 MB; the cap protects against runaway downloads and parse-time
# memory blowups on unexpected payloads.
MAX_FETCH_BYTES = 20 * 1024 * 1024


def env_int(name: str, default: int, min_value: int = 1) -> int:
    """Read an integer from the environment, falling back to *default*.

    Garbage or values below *min_value* are logged and replaced with the
    default instead of raising at import time. Defaults to requiring a
    positive integer; pass ``min_value=0`` to allow zero.
    """
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using default %s.", name, raw, default)
        return default
    if value < min_value:
        logger.warning("%s=%s must be >= %s; using default %s.", name, value, min_value, default)
        return default
    return value


def env_provider_int_map(raw: str, min_value: int = 0) -> dict[str, int]:
    """Parse a ``provider=count`` override env var like ``bva=0,courtlistener=5``.

    Used for ``SEARCH_QUERY_VARIANTS_BY_PROVIDER`` (``min_value=0``, where 0
    disables expansion) and ``SEARCH_PAGES_PER_QUERY_BY_PROVIDER``
    (``min_value=1``, where a backend must fetch at least one page).
    Malformed entries (no ``=``, non-integer, or below *min_value*) are logged
    and skipped; the global setting then applies for that provider.
    """
    result: dict[str, int] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        if not sep or not name.strip():
            logger.warning("Ignoring malformed provider-override entry %r.", part)
            continue
        try:
            count = int(value.strip())
        except ValueError:
            logger.warning("%s=%r is not an integer; ignoring.", name.strip(), value.strip())
            continue
        if count < min_value:
            logger.warning("%s=%s must be >= %s; ignoring.", name.strip(), count, min_value)
            continue
        result[name.strip()] = count
    return result


def env_float(name: str, default: float) -> float:
    """Read a non-negative float from the environment, falling back to *default*."""
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using default %s.", name, raw, default)
        return default
    if value < 0:
        logger.warning("%s=%s must be >= 0; using default %s.", name, value, default)
        return default
    return value


def env_bool(name: str, default: bool) -> bool:
    """Read a boolean flag from the environment, falling back to *default*.

    Recognizes the truthy set ``1/true/yes/on`` (case-insensitive); everything
    else, including an unset variable, resolves to *default*.
    """
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class Settings:
    """Typed runtime settings, populated from environment variables.

    Field definitions hold the defaults; :meth:`from_env` re-reads the
    environment on demand so changes are honored at call time.
    """

    # Outbound HTTP
    request_timeout_seconds: int = 20
    user_agent: str = DEFAULT_USER_AGENT
    max_fetch_bytes: int = MAX_FETCH_BYTES
    # Optional HTTP(S) proxy URL for the search layer (e.g.
    # ``http://user:pass@host:port``). When set, every DuckDuckGo/CourtListener/
    # BVA request routes through it, so a flagged local IP can be swapped for a
    # residential proxy without touching the provider code. Empty = direct.
    search_http_proxy: str = ""

    # Batch state
    batch_state_dir: str = field(
        default_factory=lambda: str(Path(tempfile.gettempdir()) / "va_legal_agent_batches")
    )

    # Search
    search_max_workers: int = 4
    search_delay_seconds: float = 0.5
    search_retry_attempts: int = 2
    search_backoff_base_seconds: float = 1.0
    search_backoff_max_seconds: float = 10.0
    search_min_interval_seconds: float = 0.0
    # Cap on total wall time spent searching for one issue (0 disables).
    # Enforced cooperatively between provider calls and as a hard wait bound in
    # fetch_cases_for_issue, so operators can bound runs without tuning the
    # nested retry/backoff loops individually.
    search_max_wall_seconds: float = 0.0
    search_providers: str = "duckduckgo"
    search_pages_per_query: int = 1
    # Per-provider overrides of SEARCH_PAGES_PER_QUERY, e.g. {"bva": 1} to
    # fetch only page 1 from a backend that rate-limits pagination. Providers
    # not listed fall back to search_pages_per_query.
    search_pages_per_query_by_provider: dict[str, int] = field(default_factory=dict)
    search_query_variants: int = 3
    # Per-provider overrides of SEARCH_QUERY_VARIANTS, e.g. {"bva": 0} to
    # disable expansion on a backend that throttles extra queries. Providers
    # not listed fall back to search_query_variants.
    search_query_variants_by_provider: dict[str, int] = field(default_factory=dict)
    # Per-provider requests-per-minute budget, e.g. {"courtlistener": 5} to
    # cap CourtListener at 5 requests/minute. Enforced in _throttle() between
    # actual HTTP requests for that provider, on top of the global
    # SEARCH_MIN_INTERVAL_SECONDS. Providers not listed have no budget.
    search_max_rpm_by_provider: dict[str, int] = field(default_factory=dict)
    courtlistener_api_key: str | None = None
    # Pre-flight check against CourtListener's /api/rest/v4/api-usage/ before a
    # run: when the free-tier daily budget (125 requests) can't cover the
    # estimated per-issue request cost, abort with the window reset time
    # instead of grinding through 429s. The api-usage endpoint has its own
    # throttle, so the check itself doesn't burn the search budget.
    courtlistener_usage_guard: bool = True

    # Multi-hop citation traversal over CourtListener's citation graph.
    # Off by default: it makes extra authenticated API calls per run, so
    # operators opt in when they want deeper, trail-following recall.
    citation_traversal: bool = False
    # How many top cases seed the citation traversal.
    citation_traverse_limit: int = 3

    # Pipeline limits
    # Cap on results merged per search query. The CLI's --max-results flag
    # overrides this; raise it for multi-provider runs (SEARCH_PROVIDERS) so a
    # backend listed first can't fill the cap alone and starve the others.
    search_max_results: int = 10
    enrich_case_limit: int = 5
    interpret_case_limit: int = 3
    principle_scan_limit: int = 5

    # LLM interpretation
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str | None = None
    openai_timeout_seconds: float = 60.0
    openai_max_tokens: int = 700
    # LLM reasoning pass: reconciles holdings across all ranked cases, flags
    # contradictions, and cites each claim. Disable with LLM_REASONING=0 to
    # keep only the lighter single-call narrative.
    llm_reasoning: bool = True
    # How many ranked cases the reasoning pass is fed.
    llm_reasoning_limit: int = 10

    # Deep-read mode: ingest full opinion text (instead of snippets) and
    # summarize it via chunked map-reduce, so the reasoning pass cross-
    # references holdings across the whole corpus. Off by default because it
    # fetches the full body of every top case.
    deep_read: bool = False
    # How many top cases are deep-read.
    deep_read_limit: int = 3
    # PDF page cap for deep-read fetches (0 reads every page).
    deep_read_pages: int = 0
    # Approximate character size of each chunk in the map-reduce pass.
    deep_chunk_chars: int = 6000

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from the environment, using the field defaults as fallbacks."""
        d = cls()
        return cls(
            request_timeout_seconds=env_int("REQUEST_TIMEOUT_SECONDS", d.request_timeout_seconds),
            user_agent=os.getenv("USER_AGENT", d.user_agent),
            max_fetch_bytes=env_int("MAX_FETCH_BYTES", d.max_fetch_bytes),
            search_http_proxy=os.getenv("SEARCH_HTTP_PROXY", d.search_http_proxy),
            batch_state_dir=os.getenv("BATCH_STATE_DIR", d.batch_state_dir),
            search_max_workers=env_int("SEARCH_MAX_WORKERS", d.search_max_workers),
            search_delay_seconds=env_float("SEARCH_DELAY_SECONDS", d.search_delay_seconds),
            search_retry_attempts=env_int(
                "SEARCH_RETRY_ATTEMPTS", d.search_retry_attempts, min_value=0
            ),
            search_backoff_base_seconds=env_float(
                "SEARCH_BACKOFF_BASE_SECONDS", d.search_backoff_base_seconds
            ),
            search_backoff_max_seconds=env_float(
                "SEARCH_BACKOFF_MAX_SECONDS", d.search_backoff_max_seconds
            ),
            search_min_interval_seconds=env_float(
                "SEARCH_MIN_INTERVAL_SECONDS", d.search_min_interval_seconds
            ),
            search_max_wall_seconds=env_float(
                "SEARCH_MAX_WALL_SECONDS", d.search_max_wall_seconds
            ),
            search_providers=os.getenv("SEARCH_PROVIDERS", d.search_providers),
            search_pages_per_query=env_int(
                "SEARCH_PAGES_PER_QUERY", d.search_pages_per_query
            ),
            search_pages_per_query_by_provider=env_provider_int_map(
                os.getenv("SEARCH_PAGES_PER_QUERY_BY_PROVIDER", ""), min_value=1
            ),
            search_query_variants=env_int(
                "SEARCH_QUERY_VARIANTS", d.search_query_variants, min_value=0
            ),
            search_query_variants_by_provider=env_provider_int_map(
                os.getenv("SEARCH_QUERY_VARIANTS_BY_PROVIDER", "")
            ),
            search_max_rpm_by_provider=env_provider_int_map(
                os.getenv("SEARCH_MAX_RPM_BY_PROVIDER", ""), min_value=0
            ),
            courtlistener_api_key=os.getenv("COURTLISTENER_API_KEY") or None,
            courtlistener_usage_guard=env_bool(
                "COURTLISTENER_USAGE_GUARD", d.courtlistener_usage_guard
            ),
            citation_traversal=env_bool("CITATION_TRAVERSAL", d.citation_traversal),
            citation_traverse_limit=env_int(
                "CITATION_TRAVERSE_LIMIT", d.citation_traverse_limit
            ),
            search_max_results=env_int("SEARCH_MAX_RESULTS", d.search_max_results),
            enrich_case_limit=env_int("ENRICH_CASE_LIMIT", d.enrich_case_limit),
            interpret_case_limit=env_int("INTERPRET_CASE_LIMIT", d.interpret_case_limit),
            principle_scan_limit=env_int("PRINCIPLE_SCAN_LIMIT", d.principle_scan_limit),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("OPENAI_MODEL", d.openai_model),
            openai_base_url=os.getenv("OPENAI_BASE_URL") or None,
            openai_timeout_seconds=env_float("OPENAI_TIMEOUT_SECONDS", d.openai_timeout_seconds),
            openai_max_tokens=env_int("OPENAI_MAX_TOKENS", d.openai_max_tokens),
            llm_reasoning=env_bool("LLM_REASONING", d.llm_reasoning),
            llm_reasoning_limit=env_int("LLM_REASONING_LIMIT", d.llm_reasoning_limit),
            deep_read=env_bool("DEEP_READ", d.deep_read),
            deep_read_limit=env_int("DEEP_READ_LIMIT", d.deep_read_limit),
            deep_read_pages=env_int("DEEP_READ_PAGES", d.deep_read_pages, min_value=0),
            deep_chunk_chars=env_int("DEEP_CHUNK_CHARS", d.deep_chunk_chars),
        )


def get_settings() -> Settings:
    """Return the current settings, read fresh from the environment.

    Re-read on each call so environment changes (including test overrides) are
    honored at call time rather than frozen at import.
    """
    return Settings.from_env()
