"""Centralized redaction for credentials in URLs, errors, and tracebacks.

The application handles credentials from several providers.  A raw
``requests`` exception can contain the full request URL (including an API key
query parameter) and is often chained to a higher-level error with
``raise ... from exc``.  Rendering that chain with ``traceback`` would leak the
credential even when the outer error message is safe.

This module is deliberately dependency-free apart from the standard library so
it can be used by config-free helpers and tests.  Callers pass runtime keys in
``extra_secrets``; keys present in the process environment are included
automatically.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from urllib.parse import (
    parse_qsl,
    quote,
    quote_plus,
    unquote_plus,
    urlencode,
    urlsplit,
    urlunsplit,
)

REDACTED = "[REDACTED]"

# Environment variables that are treated as credentials even when the caller
# cannot pass a Settings object (for example, a logging formatter).
_SECRET_ENV_VARS = (
    "GOVINFO_API_KEY",
    "CONGRESS_API_KEY",
    "COURTLISTENER_API_KEY",
    "REGULATIONS_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "API_KEY",
    "ACCESS_TOKEN",
)

_SENSITIVE_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "key",
        "token",
        "access_token",
        "access-token",
        "x_api_key",
        "x-api-key",
        "authorization",
        "auth",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "client-secret",
        "subscription_key",
        "subscription-key",
    }
)

# Matches a query pair in arbitrary text, including a URL-encoded ``=``
# (``%3D``).  The name is decoded before the sensitivity check, so
# ``api%5Fkey`` is handled as ``api_key``.
_QUERY_PAIR_RE = re.compile(
    r"(?i)([?&#])([^=&\s'\"#]+)(=|%3D)([^&\s'\"#]*)"
)

# Credentials embedded in a URL authority (``https://user:pass@host``).
_USERINFO_RE = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)(?!\[REDACTED\]@)([^/\s]+)@"
)


def _redact_query_text(text: str) -> str:
    """Redact sensitive query pairs in free-form text.

    Unlike :func:`redact_url`, this does not assume the surrounding string is
    a valid URL; it is used for exception messages and tracebacks.
    """

    def replace(match: re.Match[str]) -> str:
        name = unquote_plus(match.group(2))
        if _parameter_is_sensitive(name):
            return f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}"
        return match.group(0)

    return _QUERY_PAIR_RE.sub(replace, text)


def _coerce_secrets(extra_secrets: Iterable[str] | str | None) -> tuple[str, ...]:
    """Return unique non-empty secret values, longest first."""
    if extra_secrets is None:
        values: list[str] = []
    elif isinstance(extra_secrets, str):
        values = [extra_secrets]
    else:
        values = list(extra_secrets)

    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name, "")
        if value:
            values.append(value)

    # Include other conventionally named credentials without treating every
    # environment value (PATH, HOME, etc.) as a secret.
    for name, value in os.environ.items():
        upper = name.upper()
        if upper.endswith(("_API_KEY", "_ACCESS_TOKEN", "_AUTH_TOKEN", "_SECRET")):
            if value:
                values.append(value)

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "")
        # Very short values would make ordinary prose unreadable and are not
        # realistic API credentials.  Real provider keys are much longer.
        if len(text) < 4 or text == REDACTED or text in seen:
            continue
        seen.add(text)
        result.append(text)
    result.sort(key=len, reverse=True)
    return tuple(result)


def redact_secrets(
    text: str,
    extra_secrets: Iterable[str] | str | None = None,
) -> str:
    """Redact credentials and sensitive URL parameters from *text*.

    Both the literal key value and its common URL-encoded spellings are
    replaced.  Query parameter names are matched case-insensitively and after
    URL decoding, so ``api_key``, ``api-key``, ``apikey`` and ``api%5Fkey`` are
    all covered.
    """
    if not isinstance(text, str) or not text:
        return text

    redacted = text
    for secret in _coerce_secrets(extra_secrets):
        variants = {
            secret,
            quote(secret, safe=""),
            quote_plus(secret),
            quote(secret),
        }
        for variant in sorted(variants, key=len, reverse=True):
            if variant:
                redacted = redacted.replace(variant, REDACTED)

    redacted = _redact_query_text(redacted)
    redacted = _USERINFO_RE.sub(rf"\1{REDACTED}@", redacted)
    return redacted


def _parameter_is_sensitive(name: str) -> bool:
    normalized = name.strip().lower().replace(" ", "_")
    if normalized in _SENSITIVE_NAMES:
        return True
    return normalized.replace("-", "_") in _SENSITIVE_NAMES


def redact_url(url: str, extra_secrets: Iterable[str] | str | None = None) -> str:
    """Return a display-safe copy of *url*.

    Sensitive query values and userinfo are replaced, then any literal
    configured key remaining in the path or fragment is scrubbed.  The
    original URL is never modified.
    """
    if not isinstance(url, str) or not url:
        return url

    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_secrets(url, extra_secrets)

    netloc = parts.netloc

    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return redact_secrets(url, extra_secrets)

    if pairs:
        safe_pairs = [
            (name, REDACTED if _parameter_is_sensitive(name) else value)
            for name, value in pairs
        ]
        query = urlencode(safe_pairs, safe="[]")
    else:
        query = parts.query

    rebuilt = urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    return redact_secrets(rebuilt, extra_secrets)


def _redact_scalar(value: object, extra_secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        return redact_secrets(value, extra_secrets)
    return value


def _safe_setattr(target: object, name: str, value: object) -> None:
    try:
        setattr(target, name, value)
    except (AttributeError, TypeError, ValueError):
        # Some third-party exception/request objects expose read-only
        # attributes.  The textual args and cause chain are still scrubbed.
        pass


def _sanitize_request_or_response(
    holder: object,
    extra_secrets: tuple[str, ...],
    seen: set[int],
) -> None:
    if holder is None or id(holder) in seen:
        return
    seen.add(id(holder))

    url = getattr(holder, "url", None)
    if isinstance(url, str):
        _safe_setattr(holder, "url", redact_url(url, extra_secrets))

    headers = getattr(holder, "headers", None)
    if headers is not None:
        try:
            items = list(headers.items())
        except (AttributeError, TypeError):
            items = []
        for name, value in items:
            if not isinstance(value, str):
                continue
            sensitive = _parameter_is_sensitive(str(name))
            redacted = redact_secrets(value, extra_secrets)
            if sensitive or redacted != value:
                try:
                    headers[name] = redacted
                except (AttributeError, TypeError, KeyError):
                    pass

    request = getattr(holder, "request", None)
    if request is not None:
        _sanitize_request_or_response(request, extra_secrets, seen)


def _sanitize_exception(
    exc: BaseException,
    extra_secrets: tuple[str, ...],
    seen: set[int],
) -> None:
    if id(exc) in seen:
        return
    seen.add(id(exc))

    args = getattr(exc, "args", ())
    if isinstance(args, tuple):
        _safe_setattr(
            exc,
            "args",
            tuple(_redact_scalar(arg, extra_secrets) for arg in args),
        )

    for name in ("url", "filename", "msg", "reason", "message"):
        value = getattr(exc, name, None)
        if isinstance(value, str):
            _safe_setattr(exc, name, redact_secrets(value, extra_secrets))

    for name in ("request", "response"):
        holder = getattr(exc, name, None)
        if holder is not None:
            _sanitize_request_or_response(holder, extra_secrets, seen)

    notes = getattr(exc, "__notes__", None)
    if isinstance(notes, list):
        notes[:] = [
            redact_secrets(note, extra_secrets) if isinstance(note, str) else note
            for note in notes
        ]

    for name in ("__cause__", "__context__"):
        chained = getattr(exc, name, None)
        if isinstance(chained, BaseException):
            _sanitize_exception(chained, extra_secrets, seen)

    # ExceptionGroup/BaseExceptionGroup wrap child exceptions in a tuple that
    # traceback formatting also renders.  ``exceptions`` is read-only, but the
    # child exception objects can be sanitized in place.
    children = getattr(exc, "exceptions", None)
    if isinstance(children, tuple):
        for child in children:
            if isinstance(child, BaseException):
                _sanitize_exception(child, extra_secrets, seen)


def sanitize_exception(
    exc: BaseException,
    extra_secrets: Iterable[str] | str | None = None,
) -> BaseException:
    """Scrub *exc* and its chain in place, then return it.

    The function edits ``args``, common URL/message attributes, attached
    request/response URLs and sensitive headers, exception notes, and every
    ``__cause__``/``__context__`` in the chain.  Mutating in place matters:
    ``traceback.format_exception`` renders the same exception objects that are
    still attached to the live traceback.
    """
    secrets = _coerce_secrets(extra_secrets)
    _sanitize_exception(exc, secrets, set())
    return exc
