"""Direct coverage for the AUD4-001 redaction primitives."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote_plus

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
