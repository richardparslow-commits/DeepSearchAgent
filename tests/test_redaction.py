"""Direct coverage for the AUD4-001 redaction primitives."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote_plus

import pytest
import requests

from va_legal_agent.redaction import (
    REDACTED,
    redact_secrets,
    redact_url,
    sanitize_exception,
)


def test_redact_secrets_scrubs_literal_and_url_encoded_values():
    secret = "SUPERSECRET123"

    text = f"literal={secret} encoded={quote_plus(secret)}"

    redacted = redact_secrets(text, (secret,))

    assert secret not in redacted
    assert redacted.count(REDACTED) == 2

    encoded_name = redact_secrets(f"?api%5Fkey={secret}", (secret,))
    assert secret not in encoded_name
    assert f"api%5Fkey={REDACTED}" in encoded_name


def test_redact_url_scrubs_sensitive_query_and_userinfo():
    secret = "SUPERSECRET123"
    url = (
        "https://user:password@api.example/search"
        f"?api_key={secret}&api%5Fkey={secret}&q=ok#fragment"
    )

    redacted = redact_url(url)

    assert secret not in redacted
    assert "password" not in redacted
    assert f"{REDACTED}@api.example" in redacted
    assert redacted.count(f"api_key={REDACTED}") == 2
    assert "q=ok" in redacted
    assert redacted.endswith("#fragment")


def test_redact_url_falls_back_for_malformed_authority():
    secret = "SUPERSECRET123"

    redacted = redact_url(f"http://[broken?api_key={secret}")

    assert secret not in redacted
    assert f"api_key={REDACTED}" in redacted


def test_sanitize_exception_scrubs_args_attributes_and_request():
    secret = "SUPERSECRET123"
    request = SimpleNamespace(
        url=f"https://api.example/search?api_key={secret}",
        headers={"Authorization": secret, "Accept": "application/json"},
    )
    response = SimpleNamespace(
        url=f"https://api.example/search?api_key={secret}",
        request=request,
        headers={"X-Api-Key": secret},
    )
    error = requests.HTTPError(
        f"401 for url: https://api.example/search?api_key={secret}",
        response=response,
    )
    error.url = f"https://api.example/search?api_key={secret}"
    error.filename = f"/tmp/{secret}"
    error.msg = f"failed with {secret}"
    error.__notes__ = [f"note contains {secret}"]

    sanitize_exception(error, (secret,))

    assert secret not in str(error)
    assert secret not in repr(error.args)
    assert secret not in error.url
    assert secret not in error.filename
    assert secret not in error.msg
    assert secret not in error.__notes__[0]
    assert secret not in request.url
    assert secret not in request.headers["Authorization"]
    assert secret not in response.url
    assert secret not in response.headers["X-Api-Key"]


def test_sanitize_exception_scrubs_exception_group_children():
    secret = "SUPERSECRET123"
    group = ExceptionGroup(
        "group failed", [RuntimeError(f"child failed with {secret}")]
    )

    sanitize_exception(group, (secret,))

    assert secret not in str(group.exceptions[0])


def test_sanitize_exception_handles_a_cyclic_cause_chain():
    secret = "SUPERSECRET123"
    error = RuntimeError(f"failed with {secret}")
    error.__cause__ = error
    error.__context__ = error

    sanitize_exception(error, (secret,))

    assert secret not in str(error)


def test_sanitize_exception_handles_a_mutual_cycle_with_distinct_secrets():
    # AUD5 row-4: a two-node cycle where each node carries its OWN secret in a
    # different chain slot (explicit cause vs implicit context). A traversal
    # that only follows __cause__, or that stops at the first revisit, would
    # leave one secret behind.
    secret_a = "SECRET-ALPHA-123"
    secret_b = "SECRET-BRAVO-456"
    error_a = RuntimeError(f"outer failed with {secret_a}")
    error_b = ValueError(f"inner failed with {secret_b}")
    error_a.__cause__ = error_b
    error_b.__context__ = error_a

    sanitize_exception(error_a, (secret_a, secret_b))

    assert secret_a not in str(error_a)
    assert secret_b not in str(error_b)
    assert REDACTED in str(error_a)
    assert REDACTED in str(error_b)


def test_sanitize_exception_scrubs_secret_bearing_implicit_context():
    # Implicit chaining (`raise ... during handling`) stores the secret-bearing
    # exception in __context__ with __cause__ unset; it must still be scrubbed.
    secret = "SUPERSECRET123"
    inner = requests.HTTPError(f"401 for url: https://api.example/?api_key={secret}")
    outer = RuntimeError("search failed")
    outer.__context__ = inner
    assert outer.__cause__ is None

    sanitize_exception(outer, (secret,))

    assert secret not in str(inner)
    assert f"api_key={REDACTED}" in str(inner)


def test_redact_secrets_string_extra_secret_and_empty_input():
    # A bare-string extra_secrets is coerced (redaction.py line 104); empty or
    # non-string text is returned unchanged (line 147).
    assert redact_secrets("k=SECRETVALUE", extra_secrets="SECRETVALUE") == "k=[REDACTED]"
    assert redact_secrets("") == ""
    assert redact_secrets(None) is None


def test_redact_url_empty_input_and_parse_qsl_failure(monkeypatch):
    # Empty/non-string URL returned unchanged (line 181).
    assert redact_url("") == ""
    assert redact_url(None) is None
    # parse_qsl raising ValueError falls back to free-text redaction (192-193).
    import va_legal_agent.redaction as redaction_mod

    def boom(*args, **kwargs):
        raise ValueError("bad query")

    monkeypatch.setattr(redaction_mod, "parse_qsl", boom)
    out = redaction_mod.redact_url("https://host/path?api_key=SECRETVALUE")
    assert "SECRETVALUE" not in out


def test_coerce_secrets_skips_empty_env_credential(monkeypatch):
    # A credential-suffixed env var with an empty value is skipped (118->115).
    monkeypatch.setenv("COVERAGE_PROBE_API_KEY", "")
    from va_legal_agent.redaction import _coerce_secrets

    assert "" not in _coerce_secrets(None)


def test_sanitize_exception_tolerates_odd_headers():
    from types import MappingProxyType, SimpleNamespace

    # headers.items() raises -> treated as no headers (redaction.py 240-241).
    exc = Exception("boom")
    exc.response = SimpleNamespace(
        url="https://h/?api_key=SECRETVALUE", headers=object()
    )
    sanitize_exception(exc)
    assert "SECRETVALUE" not in exc.response.url
    # A non-string header value is skipped (244).
    exc2 = Exception("boom")
    exc2.response = SimpleNamespace(url="https://h/", headers={"X-Api-Key": 123})
    sanitize_exception(exc2)
    # A read-only mapping rejects assignment; the error is swallowed (250-251).
    exc3 = Exception("boom")
    exc3.response = SimpleNamespace(
        url="https://h/", headers=MappingProxyType({"X-Api-Key": "SECRETVALUE"})
    )
    sanitize_exception(exc3)


def test_sanitize_exception_skips_non_exception_group_child():
    # A child that is not a BaseException is skipped (redaction.py 303->302).
    exc = Exception("boom")
    exc.exceptions = ("not-an-exception",)
    sanitize_exception(exc)


def test_sanitize_exception_tolerates_non_tuple_args():
    # A stand-in whose args is not a tuple skips arg redaction (redaction.py
    # 268->275). BaseException coerces args to a tuple, so override the property.
    class _OddArgs(Exception):
        @property
        def args(self):
            return ["SECRETVALUE"]

    sanitize_exception(_OddArgs("boom"))


@pytest.fixture
def no_env_secrets(monkeypatch):
    """Deterministic secret set: ignore ambient *_API_KEY / *_SECRET env values."""
    import va_legal_agent.redaction as mod

    monkeypatch.setattr(mod.os, "environ", {})
    return mod


def test_coerce_secrets_orders_longest_first_and_filters(no_env_secrets):
    # Longest-first is a SECURITY property: if a short secret is a substring of a
    # longer one, replacing the short one first leaves fragments of the long
    # credential behind. Also pins dedup, the [REDACTED] skip and the length floor.
    mod = no_env_secrets

    result = mod._coerce_secrets(
        ("ZZZZ", "AAAAAZZZZBBBB", "ZZZZ", REDACTED, "abc")
    )

    assert result == ("AAAAAZZZZBBBB", "ZZZZ")


def test_redact_secrets_never_leaves_a_fragment_of_a_longer_secret(no_env_secrets):
    mod = no_env_secrets

    out = mod.redact_secrets(
        "Authorization: Bearer AAAAAZZZZBBBB",
        extra_secrets=("ZZZZ", "AAAAAZZZZBBBB"),
    )

    assert out == f"Authorization: Bearer {REDACTED}"
    assert "AAAAA" not in out
    assert "BBBB" not in out


def test_redact_secrets_keeps_four_char_secrets_and_skips_shorter(no_env_secrets):
    mod = no_env_secrets

    # A 4-character key is still a credential and must be scrubbed.
    assert mod.redact_secrets("Bearer Z4kQ", ("Z4kQ",)) == f"Bearer {REDACTED}"
    # The documented floor: shorter values are not treated as credentials.
    assert mod.redact_secrets("Bearer abc", ("abc",)) == "Bearer abc"
    assert "Z4kQ" in mod._coerce_secrets(("Z4kQ",))
    assert "abc" not in mod._coerce_secrets(("abc",))


def test_redaction_returns_non_string_input_unchanged():
    # The guard is `not isinstance(...) or not text`; an `and` would let a
    # non-string truthy value reach str.replace and raise.
    assert redact_secrets(123) == 123
    assert redact_url(123) == 123


def test_redact_url_preserves_blank_sensitive_parameters():
    # keep_blank_values=True: a blank credential param must still be surfaced
    # and redacted, not silently dropped from the rebuilt query.
    out = redact_url("https://host/p?api_key=&q=ok")

    assert f"api_key={REDACTED}" in out
    assert "q=ok" in out


def test_redact_url_preserves_bracket_characters_in_query_names():
    # urlencode(safe="[]") keeps Rails-style bracket names readable.
    out = redact_url("https://host/p?filter[]=x&api_key=SECRETVALUE")

    assert "filter[]=x" in out
    assert "SECRETVALUE" not in out


def test_redact_url_scrubs_path_secrets_via_extra_secrets(monkeypatch):
    import va_legal_agent.redaction as mod

    secret = "SUPERSECRET123"
    # Normal rebuild path.
    assert secret not in mod.redact_url(f"https://host/{secret}/p", (secret,))
    # urlsplit ValueError fallback.
    assert secret not in mod.redact_url(f"http://[broken/{secret}", (secret,))

    # parse_qsl ValueError fallback must also forward extra_secrets.
    def boom(*args, **kwargs):
        raise ValueError("bad query")

    monkeypatch.setattr(mod, "parse_qsl", boom)
    assert secret not in mod.redact_url(f"https://host/{secret}?a=1", (secret,))


def test_parameter_is_sensitive_normalizes_spaces_and_case():
    from va_legal_agent.redaction import _parameter_is_sensitive

    assert _parameter_is_sensitive("  API KEY  ") is True
    assert _parameter_is_sensitive("Client Secret") is True
    assert _parameter_is_sensitive("X-Api-Key") is True
    assert _parameter_is_sensitive("q") is False


def test_sanitize_exception_scrubs_non_sensitive_header_bearing_a_secret():
    # The condition is `sensitive or redacted != value`: a NON-sensitive header
    # name whose value contains a credential must still be scrubbed.
    secret = "SUPERSECRET123"
    exc = Exception("boom")
    exc.response = SimpleNamespace(
        url="https://host/", headers={"X-Custom": f"Bearer {secret}"}
    )

    sanitize_exception(exc, (secret,))

    assert secret not in exc.response.headers["X-Custom"]


def test_sanitize_exception_scrubs_every_message_attribute_and_notes():
    secret = "SUPERSECRET123"
    exc = Exception("boom")
    for name in ("url", "filename", "msg", "reason", "message"):
        setattr(exc, name, f"{name}={secret}")
    exc.__notes__ = [f"note {secret}", 42]

    sanitize_exception(exc, (secret,))

    for name in ("url", "filename", "msg", "reason", "message"):
        assert secret not in getattr(exc, name), name
    assert secret not in exc.__notes__[0]
    assert exc.__notes__[1] == 42

def test_redact_secrets_double_encoded_query_param():
    from va_legal_agent.redaction import redact_secrets

    assert redact_secrets("https://api.example.com?api%255Fkey=secret123") == (
        "https://api.example.com?api%255Fkey=[REDACTED]"
    )


