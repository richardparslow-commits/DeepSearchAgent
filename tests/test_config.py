"""Tests for guarded environment parsing and typed settings (va_legal_agent.config)."""

import ast
import re
from pathlib import Path

import pytest

from va_legal_agent.config import DEFAULT_USER_AGENT, Settings, env_bool, env_float, env_int, get_settings


_ENV_VARS = (
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
    "SEARCH_HTTP_PROXY",
    "SEARCH_MAX_WALL_SECONDS",
    "SEARCH_MAX_REFINEMENT_ROUNDS",
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
)


def test_env_int_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_MISSING_INT", raising=False)
    assert env_int("SOME_MISSING_INT", 7) == 7


def test_env_int_parses_positive_value(monkeypatch):
    monkeypatch.setenv("SOME_INT", "12")
    assert env_int("SOME_INT", 7) == 12


def test_env_int_falls_back_on_garbage_and_non_positive(monkeypatch):
    monkeypatch.setenv("SOME_INT", "abc")
    assert env_int("SOME_INT", 7) == 7
    monkeypatch.setenv("SOME_INT", "0")
    assert env_int("SOME_INT", 7) == 7
    monkeypatch.setenv("SOME_INT", "-3")
    assert env_int("SOME_INT", 7) == 7


def test_env_int_allows_zero_when_min_value_is_zero(monkeypatch):
    monkeypatch.setenv("SOME_INT", "0")
    assert env_int("SOME_INT", 2, min_value=0) == 0


def test_env_int_accepts_one_with_default_min_value(monkeypatch):
    # The default min_value is 1, so exactly 1 is valid (not replaced by default).
    monkeypatch.setenv("SOME_INT", "1")
    assert env_int("SOME_INT", 7) == 1


def test_env_int_falls_back_on_value_above_max(monkeypatch):
    # A value above max_value is rejected and replaced by the default, so a
    # typo like SEARCH_MAX_WORKERS=10000 can't spawn a huge thread pool.
    monkeypatch.setenv("SOME_INT", "40")
    assert env_int("SOME_INT", 7, max_value=32) == 7


def test_env_int_accepts_value_at_max(monkeypatch):
    # max_value is inclusive: exactly 32 is valid, not replaced by default.
    monkeypatch.setenv("SOME_INT", "32")
    assert env_int("SOME_INT", 7, max_value=32) == 32


def test_env_int_no_max_allows_large_value(monkeypatch):
    # Without max_value there is no upper bound, preserving the historical
    # behavior for every other integer setting.
    monkeypatch.setenv("SOME_INT", "100000")
    assert env_int("SOME_INT", 7) == 100000


def test_env_float_accepts_zero(monkeypatch):
    monkeypatch.setenv("SOME_FLOAT", "0")
    assert env_float("SOME_FLOAT", 1.5) == 0.0


def test_env_float_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_MISSING_FLOAT", raising=False)
    assert env_float("SOME_MISSING_FLOAT", 1.5) == 1.5


def test_env_float_parses_value_and_falls_back_on_garbage_and_negative(monkeypatch):
    monkeypatch.setenv("SOME_FLOAT", "0.25")
    assert env_float("SOME_FLOAT", 1.5) == 0.25
    monkeypatch.setenv("SOME_FLOAT", "nope")
    assert env_float("SOME_FLOAT", 1.5) == 1.5
    monkeypatch.setenv("SOME_FLOAT", "-0.5")
    assert env_float("SOME_FLOAT", 1.5) == 1.5


def test_env_bool_truthy_set(monkeypatch):
    for raw in ("1", "true", "yes", "on", "TRUE", "On"):
        monkeypatch.setenv("SOME_BOOL", raw)
        assert env_bool("SOME_BOOL", False) is True


def test_env_bool_falsy_set(monkeypatch):
    for raw in ("0", "false", "no", "off", "FALSE", "Off"):
        monkeypatch.setenv("SOME_BOOL", raw)
        assert env_bool("SOME_BOOL", True) is False


def test_env_bool_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("SOME_MISSING_BOOL", raising=False)
    assert env_bool("SOME_MISSING_BOOL", True) is True
    assert env_bool("SOME_MISSING_BOOL", False) is False
    # Unrecognized tokens are neither truthy nor falsy: the default wins.
    monkeypatch.setenv("SOME_BOOL", "garbage")
    assert env_bool("SOME_BOOL", True) is True
    monkeypatch.setenv("SOME_BOOL", "")
    assert env_bool("SOME_BOOL", False) is False


def test_settings_defaults(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    settings = get_settings()

    assert settings.request_timeout_seconds == 20
    assert settings.user_agent == DEFAULT_USER_AGENT
    assert settings.max_fetch_bytes == 20 * 1024 * 1024
    assert settings.batch_state_dir.endswith("va_legal_agent_batches")
    assert settings.search_max_workers == 4
    assert settings.search_delay_seconds == 0.5
    assert settings.search_retry_attempts == 2
    assert settings.search_backoff_base_seconds == 1.0
    assert settings.search_backoff_max_seconds == 10.0
    assert settings.search_min_interval_seconds == 0.0
    assert settings.search_max_wall_seconds == 0.0
    assert settings.search_max_refinement_rounds == 3
    assert settings.search_providers == "duckduckgo"
    assert settings.search_pages_per_query == 1
    assert settings.search_pages_per_query_by_provider == {}
    assert settings.search_query_variants == 3
    assert settings.search_query_variants_by_provider == {}
    assert settings.search_max_rpm_by_provider == {}
    assert settings.search_max_results == 10
    assert settings.courtlistener_api_key is None
    assert settings.courtlistener_usage_guard is True
    assert settings.citation_traversal is False
    assert settings.citation_traverse_limit == 3
    assert settings.enrich_case_limit == 5
    assert settings.interpret_case_limit == 3
    assert settings.principle_scan_limit == 5
    assert settings.openai_api_key is None
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.openai_base_url is None
    assert settings.openai_timeout_seconds == 60.0
    assert settings.openai_max_tokens == 700
    assert settings.llm_reasoning is True
    assert settings.llm_reasoning_limit == 10
    assert settings.deep_read is False
    assert settings.deep_read_limit == 3
    assert settings.deep_read_pages == 0
    assert settings.deep_chunk_chars == 6000


def test_search_max_workers_is_capped(monkeypatch):
    # The worker pool has a hard upper bound so a mistyped env var fails fast
    # back to the default instead of spawning thousands of daemon threads.
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "10000")
    assert get_settings().search_max_workers == 4

    # The cap's own boundary is accepted (inclusive).
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "32")
    assert get_settings().search_max_workers == 32


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("USER_AGENT", "Custom-Agent/9.9")
    monkeypatch.setenv("MAX_FETCH_BYTES", "10")
    monkeypatch.setenv("SEARCH_HTTP_PROXY", "http://user:pass@proxy.example:8080")
    monkeypatch.setenv("BATCH_STATE_DIR", "/tmp/custom-batches")
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "6")
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo,courtlistener")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY", "3")
    monkeypatch.setenv("SEARCH_PAGES_PER_QUERY_BY_PROVIDER", "bva=1,duckduckgo=4")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS", "5")
    monkeypatch.setenv("SEARCH_QUERY_VARIANTS_BY_PROVIDER", "duckduckgo=2,bva=0")
    monkeypatch.setenv("SEARCH_MAX_RPM_BY_PROVIDER", "courtlistener=5,bva=10")
    monkeypatch.setenv("SEARCH_MAX_RESULTS", "25")
    monkeypatch.setenv("COURTLISTENER_API_KEY", "tok-123")
    monkeypatch.setenv("COURTLISTENER_USAGE_GUARD", "0")
    monkeypatch.setenv("CITATION_TRAVERSAL", "1")
    monkeypatch.setenv("CITATION_TRAVERSE_LIMIT", "5")
    monkeypatch.setenv("ENRICH_CASE_LIMIT", "4")
    monkeypatch.setenv("INTERPRET_CASE_LIMIT", "7")
    monkeypatch.setenv("PRINCIPLE_SCAN_LIMIT", "9")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("OPENAI_MAX_TOKENS", "300")
    monkeypatch.setenv("LLM_REASONING", "0")
    monkeypatch.setenv("LLM_REASONING_LIMIT", "4")
    monkeypatch.setenv("DEEP_READ", "1")
    monkeypatch.setenv("DEEP_READ_LIMIT", "5")
    monkeypatch.setenv("DEEP_READ_PAGES", "0")
    monkeypatch.setenv("DEEP_CHUNK_CHARS", "4000")
    monkeypatch.setenv("SEARCH_MAX_WALL_SECONDS", "7.5")
    monkeypatch.setenv("SEARCH_MAX_REFINEMENT_ROUNDS", "5")

    settings = get_settings()

    assert settings.request_timeout_seconds == 30
    assert settings.user_agent == "Custom-Agent/9.9"
    assert settings.max_fetch_bytes == 10
    assert settings.search_http_proxy == "http://user:pass@proxy.example:8080"
    assert settings.batch_state_dir == "/tmp/custom-batches"
    assert settings.search_max_workers == 6
    assert settings.search_max_wall_seconds == 7.5
    assert settings.search_max_refinement_rounds == 5
    assert settings.search_providers == "duckduckgo,courtlistener"
    assert settings.search_pages_per_query == 3
    assert settings.search_pages_per_query_by_provider == {"bva": 1, "duckduckgo": 4}
    assert settings.search_query_variants == 5
    assert settings.search_query_variants_by_provider == {"duckduckgo": 2, "bva": 0}
    assert settings.search_max_rpm_by_provider == {"courtlistener": 5, "bva": 10}
    assert settings.search_max_results == 25
    assert settings.courtlistener_api_key == "tok-123"
    assert settings.courtlistener_usage_guard is False
    assert settings.citation_traversal is True
    assert settings.citation_traverse_limit == 5
    assert settings.enrich_case_limit == 4
    assert settings.interpret_case_limit == 7
    assert settings.principle_scan_limit == 9
    assert settings.openai_api_key == "sk-test"
    assert settings.openai_model == "gpt-4o"
    assert settings.openai_timeout_seconds == 12.5
    assert settings.openai_max_tokens == 300
    assert settings.llm_reasoning is False
    assert settings.llm_reasoning_limit == 4
    assert settings.deep_read is True
    assert settings.deep_read_limit == 5
    assert settings.deep_read_pages == 0
    assert settings.deep_chunk_chars == 4000


def test_env_provider_int_map_skips_malformed_entries(monkeypatch):
    from va_legal_agent.config import env_provider_int_map

    parsed = env_provider_int_map("duckduckgo=2, ,bva=0,courtlistener=wat,=3,foo=-1,bar")
    assert parsed == {"duckduckgo": 2, "bva": 0}
    assert env_provider_int_map("") == {}
    # min_value=1 rejects 0 (pages can't be disabled), keeping the fallback.
    assert env_provider_int_map("bva=0,duckduckgo=3", min_value=1) == {"duckduckgo": 3}


def test_env_provider_int_map_continues_after_bad_entries(monkeypatch):
    from va_legal_agent.config import env_provider_int_map

    # A malformed entry or a non-integer value must not stop later entries from
    # being parsed.
    assert env_provider_int_map("duckduckgo=2, malformed, bva=3") == {"duckduckgo": 2, "bva": 3}
    assert env_provider_int_map("duckduckgo=wat, bva=3") == {"bva": 3}
    # A name containing "=" is malformed and ignored entirely.
    assert env_provider_int_map("duckduckgo=2=3") == {}


def test_settings_are_frozen_and_typed():
    assert isinstance(Settings().search_max_workers, int)
    assert isinstance(Settings().search_delay_seconds, float)
    assert Settings().search_query_variants_by_provider == {}
    assert Settings().search_max_rpm_by_provider == {}


def _env_vars_read_in(tree: ast.Module) -> set[str]:
    """Env var names read with a literal string key in an AST.

    Matches ``os.getenv("NAME")`` calls and the config helpers
    ``env_int``/``env_float``/``env_bool`` (whose first argument is the env
    name). Dynamic keys -- the helper implementations' own ``os.getenv(name)``
    parameters -- are excluded because the argument is a variable, not a
    literal, so multi-line formatting can't hide a name.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        is_helper = (
            isinstance(fn, ast.Name) and fn.id in {"env_int", "env_float", "env_bool"}
        )
        is_getenv = (
            isinstance(fn, ast.Attribute)
            and fn.attr == "getenv"
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "os"
        )
        if not (is_helper or is_getenv):
            continue
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def test_env_var_documentation_parity():
    """Every env var the package reads is documented in both reference files.

    The contract, enforced by the manual settings sweep: every env var read in
    ``config.py`` must appear in the README settings table and have a
    ``.env.example`` entry; every ``.env.example`` entry must be in the README
    table; and every documented var must actually be read by some module (so
    docs can't outlive the code). This test would have caught the two gaps the
    sweep found -- CITATION_TRAVERSAL / CITATION_TRAVERSE_LIMIT and USER_AGENT
    were read but undocumented in one or both files.
    """
    root = Path(__file__).resolve().parents[1]

    config_source = (root / "va_legal_agent" / "config.py").read_text()
    if "mutmut.mutation.trampoline" in config_source or "_mutmut_mutated" in config_source:
        # Under the mutation pass, this test runs from the mutants/ sandbox
        # against mutmut's trampoline-instrumented config.py (every function
        # wrapped, mutant copies inline), so the AST parser would harvest
        # mutant scaffolding like os.getenv("") instead of the real env
        # names. The parity contract is enforced by the real suite; skip here
        # so the sandbox's stats-collection run doesn't fail on it.
        pytest.skip("config.py is mutmut-instrumented; parity is enforced by the real suite")

    config_vars = _env_vars_read_in(ast.parse(config_source))
    package_vars: set[str] = set()
    for path in (root / "va_legal_agent").glob("*.py"):
        package_vars |= _env_vars_read_in(ast.parse(path.read_text()))

    readme_vars = set(
        re.findall(
            r"^\| `([A-Z][A-Z0-9_]+)`",
            (root / "README.md").read_text(),
            re.MULTILINE,
        )
    )
    template_vars = set(
        re.findall(r"^([A-Z][A-Z0-9_]+)=", (root / ".env.example").read_text(), re.MULTILINE)
    )

    missing_from_readme = config_vars - readme_vars
    missing_from_template = config_vars - template_vars
    undocumented_template = template_vars - readme_vars
    dead_docs = (readme_vars | template_vars) - package_vars

    assert not missing_from_readme, (
        "Settings read in config.py are missing from the README settings table: "
        f"{sorted(missing_from_readme)}"
    )
    assert not missing_from_template, (
        "Settings read in config.py are missing from .env.example: "
        f"{sorted(missing_from_template)}"
    )
    assert not undocumented_template, (
        ".env.example entries are missing from the README settings table: "
        f"{sorted(undocumented_template)}"
    )
    assert not dead_docs, (
        "Documented env vars that no module reads (dead documentation): "
        f"{sorted(dead_docs)}"
    )
