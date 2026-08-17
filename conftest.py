"""Shared pytest fixtures.

The app loads the user's local ``.env`` on import (``va_legal_agent/__init__
``), which would otherwise leak non-default settings into the test suite — a
provider list of ``courtlistener,bva`` or an RPM budget in ``.env`` would make
tests hit the live network or sleep on real throttles. This autouse fixture
clears every config env var before each test so the suite is hermetic: tests
that need a specific value set it explicitly via ``monkeypatch``.
"""

import pytest

_CONFIG_ENV_VARS = (
    "REQUEST_TIMEOUT_SECONDS",
    "USER_AGENT",
    "MAX_FETCH_BYTES",
    "BATCH_STATE_DIR",
    "SEARCH_MAX_WORKERS",
    "SEARCH_DELAY_SECONDS",
    "SEARCH_RETRY_ATTEMPTS",
    "SEARCH_BACKOFF_BASE_SECONDS",
    "SEARCH_BACKOFF_MAX_SECONDS",
    "SEARCH_MIN_INTERVAL_SECONDS",
    "SEARCH_MAX_WALL_SECONDS",
    "SEARCH_PROVIDERS",
    "SEARCH_PAGES_PER_QUERY",
    "SEARCH_PAGES_PER_QUERY_BY_PROVIDER",
    "SEARCH_QUERY_VARIANTS",
    "SEARCH_QUERY_VARIANTS_BY_PROVIDER",
    "SEARCH_MAX_RPM_BY_PROVIDER",
    "SEARCH_MAX_RESULTS",
    "COURTLISTENER_API_KEY",
    "COURTLISTENER_USAGE_GUARD",
    "CITATION_TRAVERSAL",
    "CITATION_TRAVERSE_LIMIT",
    "ENRICH_CASE_LIMIT",
    "INTERPRET_CASE_LIMIT",
    "PRINCIPLE_SCAN_LIMIT",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_TIMEOUT_SECONDS",
    "OPENAI_MAX_TOKENS",
    "LLM_REASONING",
    "LLM_REASONING_LIMIT",
    "DEEP_READ",
    "DEEP_READ_LIMIT",
    "DEEP_READ_PAGES",
    "DEEP_CHUNK_CHARS",
    "LOG_JSON",
    "RUN_ID",
)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Start every test from a clean environment (no .env leakage)."""
    for var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # The CourtListener usage guard queries the live api-usage endpoint from
    # fetch_cases_for_issue; stub it to a full budget so the suite never makes
    # that network call. Tests exercising the guard re-patch this name in
    # their own body, which replaces the stub.
    monkeypatch.setattr(
        "va_legal_agent.agent.check_courtlistener_daily_budget",
        lambda min_remaining: {
            "used": 0,
            "limit": 125,
            "remaining": 125,
            "reset_at": None,
        },
    )
