"""Tests for stable fetch-failure classification at the provider boundary."""

from __future__ import annotations

import pytest
import requests
from curl_cffi.requests.exceptions import (
    ConnectionError as CffiConnectionError,
    Timeout as CffiTimeout,
)
from hypothesis import given, settings, strategies as st

from va_legal_agent.citator import _Target, _fetch_cluster_forward_edges
from va_legal_agent.models import CaseRecord
from va_legal_agent.providers import CourtListenerProvider
from va_legal_agent.search import SearchError, classify_search_error

_STRUCTURED_DEFAULTS = {
    "rate_limit": True,
    "authentication": False,
    "timeout": True,
    "malformed_response": False,
    "other": False,
}
_HTTP_STATUS_CASES = (
    (401, "authentication", False),
    (403, "authentication", False),
    (408, "timeout", True),
    (429, "rate_limit", True),
    (500, "other", True),
    (504, "timeout", True),
    (400, "other", False),
)
_HTTP_MESSAGE_CASES = (
    (401, "provider failure: 429 Too Many Requests", "authentication", False),
    (429, "provider failure: invalid JSON response", "rate_limit", True),
    (408, "provider failure: malformed response", "timeout", True),
    (500, "provider failure: invalid JSON response", "malformed_response", False),
)
_TRANSPORT_CASES = (
    (requests.Timeout, "timeout", True),
    (CffiTimeout, "timeout", True),
    (requests.ConnectionError, "other", True),
    (CffiConnectionError, "other", True),
)
_MESSAGE_MARKER_CASES = (
    ("invalid json", "malformed_response", False),
    ("malformed", "malformed_response", False),
    ("unexpected response", "malformed_response", False),
    ("not a collection", "malformed_response", False),
    ("response was not", "malformed_response", False),
    ("rate limit", "rate_limit", True),
    ("rate-limit", "rate_limit", True),
    ("throttl", "rate_limit", True),
    ("too many requests", "rate_limit", True),
    ("HTTP 429", "rate_limit", True),
    ("timeout", "timeout", True),
    ("timed out", "timeout", True),
    ("time out", "timeout", True),
    ("unauthorized", "authentication", False),
    ("authentication", "authentication", False),
    ("api token", "authentication", False),
    ("forbidden", "authentication", False),
    ("HTTP 401", "authentication", False),
    ("status code 403", "authentication", False),
    ("status=429", "rate_limit", True),
)
_MIXED_MARKER_CASES = (
    (
        "malformed response: rate limit, timeout, unauthorized",
        "malformed_response",
        False,
    ),
    ("rate limit: timed out, unauthorized", "rate_limit", True),
    ("timed out: unauthorized", "timeout", True),
)
_INVALID_KIND_CASES = (
    ("server_error", "429 Too Many Requests", None, "rate_limit", True),
    ("RATE_LIMIT", "invalid JSON response", True, "malformed_response", True),
    ("server_error", "429 Too Many Requests", False, "rate_limit", False),
    ("server_error", "opaque provider failure", True, "other", True),
)


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _status_error(status: int, message: str = "opaque provider failure") -> SearchError:
    error = SearchError(message)
    error.response = _FakeResponse({}, status)  # type: ignore[attr-defined]
    return error


def _wrap_error(error: BaseException, depth: int) -> BaseException:
    for _ in range(depth):
        wrapper = SearchError("opaque provider wrapper")
        wrapper.__cause__ = error
        error = wrapper
    return error


def test_courtlistener_invalid_json_citing_edges_is_malformed(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")

    class InvalidJsonResponse(_FakeResponse):
        def json(self):
            raise ValueError("bad json")

    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda *args, **kwargs: InvalidJsonResponse({}),
    )

    with pytest.raises(SearchError) as exc_info:
        CourtListenerProvider().citing_edges(12345)

    assert exc_info.value.kind == "malformed_response"
    assert exc_info.value.retryable is False


def test_courtlistener_non_object_citing_edges_is_malformed(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.providers.CourtListenerProvider._get_json",
        lambda self, url, params=None: [],
    )

    with pytest.raises(SearchError) as exc_info:
        CourtListenerProvider().citing_edges(12345)

    assert exc_info.value.kind == "malformed_response"
    assert exc_info.value.retryable is False


@settings(max_examples=120, deadline=None)
@given(
    kind=st.sampled_from(tuple(_STRUCTURED_DEFAULTS)),
    retryable=st.one_of(st.none(), st.booleans()),
    transport_case=st.sampled_from(_TRANSPORT_CASES),
)
def test_classify_search_error_structured_values_win_over_wrapped_cause(
    kind, retryable, transport_case
) -> None:
    cause_type, cause_kind, cause_retryable = transport_case
    cause = cause_type("opaque transport failure")
    error = SearchError("opaque provider failure", kind=kind, retryable=retryable)
    error.__cause__ = cause
    expected_retryable = (
        retryable if retryable is not None else _STRUCTURED_DEFAULTS[kind]
    )

    assert classify_search_error(cause) == (cause_kind, cause_retryable)
    assert classify_search_error(error) == (kind, expected_retryable)


def test_classify_search_error_explicit_values_override_conflicting_connection_cause():
    cause = requests.ConnectionError("opaque transport failure")
    error = SearchError("opaque provider failure", kind="timeout", retryable=False)
    error.__cause__ = cause

    assert classify_search_error(cause) == ("other", True)
    assert classify_search_error(error) == ("timeout", False)


@pytest.mark.parametrize("transport_case", _TRANSPORT_CASES)
def test_classify_search_error_plain_wrapper_inherits_transport_cause(
    transport_case,
) -> None:
    cause_type, expected_kind, expected_retryable = transport_case
    cause = cause_type("opaque transport failure")
    error = SearchError("opaque provider failure")
    error.__cause__ = cause

    assert classify_search_error(error) == (expected_kind, expected_retryable)


@pytest.mark.parametrize(
    ("cause_type", "expected"),
    (
        (requests.Timeout, ("timeout", True)),
        (requests.ConnectionError, ("malformed_response", False)),
    ),
    ids=("timeout-before-message", "connection-after-message"),
)
def test_classify_search_error_wrapped_transport_precedence_over_message(
    cause_type, expected
) -> None:
    cause = cause_type("opaque transport failure")
    error = SearchError("invalid JSON response")
    error.__cause__ = cause

    assert classify_search_error(error) == expected


@pytest.mark.parametrize(
    ("marker", "expected_kind", "expected_retryable"), _MESSAGE_MARKER_CASES
)
def test_classify_search_error_message_marker_fallback(
    marker, expected_kind, expected_retryable
) -> None:
    error = SearchError(f"provider failure: {marker}")

    assert classify_search_error(error) == (expected_kind, expected_retryable)


@pytest.mark.parametrize(
    ("message", "expected_kind", "expected_retryable"), _MIXED_MARKER_CASES
)
def test_classify_search_error_mixed_message_marker_precedence(
    message, expected_kind, expected_retryable
) -> None:
    error = SearchError(message)

    assert classify_search_error(error) == (expected_kind, expected_retryable)


@pytest.mark.parametrize(
    "opinion_id",
    ("401", "403", "429", "1401", "1403", "1429"),
)
def test_numeric_status_markers_ignore_opinion_ids(opinion_id) -> None:
    error = SearchError(
        "CourtListener request failed: "
        "https://www.courtlistener.com/api/rest/v4/"
        f"opinions/{opinion_id}/: boom"
    )

    assert classify_search_error(error) == ("other", False)


@pytest.mark.parametrize(
    "message",
    ("provider failure: 401", "provider failure: 403", "provider failure: 429"),
)
def test_numeric_status_markers_require_http_context(message) -> None:
    assert classify_search_error(SearchError(message)) == ("other", False)


@pytest.mark.parametrize(
    ("kind", "message", "retryable", "expected_kind", "expected_retryable"),
    _INVALID_KIND_CASES,
)
def test_classify_search_error_invalid_kind_uses_message_fallback_and_override(
    kind, message, retryable, expected_kind, expected_retryable
) -> None:
    error = SearchError(message, kind=kind, retryable=retryable)

    assert classify_search_error(error) == (expected_kind, expected_retryable)


@settings(max_examples=80, deadline=None)
@given(
    status_case=st.sampled_from(_HTTP_STATUS_CASES),
    depth=st.integers(min_value=0, max_value=3),
)
def test_classify_search_error_http_status_survives_wrapped_causes(
    status_case, depth
) -> None:
    status, kind, retryable = status_case
    error = _wrap_error(_status_error(status), depth)

    assert classify_search_error(error) == (kind, retryable)


@pytest.mark.parametrize(
    ("status", "message", "expected_kind", "expected_retryable"),
    _HTTP_MESSAGE_CASES,
)
def test_classify_search_error_http_status_message_precedence(
    status, message, expected_kind, expected_retryable
) -> None:
    error = _status_error(status, message)

    assert classify_search_error(error) == (expected_kind, expected_retryable)


@pytest.mark.parametrize(
    ("depth", "expected"),
    ((3, ("rate_limit", True)), (4, ("other", False))),
    ids=("last-readable-cause", "beyond-chain-limit"),
)
def test_classify_search_error_cause_chain_depth_boundary(depth, expected) -> None:
    error = _wrap_error(_status_error(429), depth)

    assert classify_search_error(error) == expected


@pytest.mark.parametrize(
    ("cause_type", "depth", "expected"),
    (
        (requests.Timeout, 3, ("timeout", True)),
        (requests.Timeout, 4, ("other", False)),
        (requests.ConnectionError, 3, ("other", True)),
        (requests.ConnectionError, 4, ("other", False)),
    ),
    ids=(
        "timeout-last-readable",
        "timeout-beyond-limit",
        "connection-last-readable",
        "connection-beyond-limit",
    ),
)
def test_classify_search_error_transport_cause_depth_boundary(
    cause_type, depth, expected
) -> None:
    error = _wrap_error(cause_type("opaque transport failure"), depth)

    assert classify_search_error(error) == expected


@pytest.mark.parametrize(
    ("outer_type", "inner_type", "expected"),
    (
        (requests.Timeout, requests.ConnectionError, ("timeout", True)),
        (requests.ConnectionError, requests.Timeout, ("other", True)),
    ),
    ids=("timeout-above-connection", "connection-above-timeout"),
)
def test_classify_search_error_nearest_transport_cause_wins(
    outer_type, inner_type, expected
) -> None:
    outer = outer_type("opaque transport failure")
    outer.__cause__ = inner_type("opaque transport failure")
    error = SearchError("opaque provider failure")
    error.__cause__ = outer

    assert classify_search_error(error) == expected


@pytest.mark.parametrize(
    ("cause", "expected"),
    (
        (SearchError("opaque cause", kind="timeout", retryable=True), ("timeout", True)),
        (SearchError("opaque cause", kind="timeout"), ("timeout", True)),
        (
            SearchError("opaque cause", kind="authentication", retryable=False),
            ("authentication", False),
        ),
        (SearchError("429 Too Many Requests"), ("rate_limit", True)),
        (SearchError("invalid JSON response"), ("malformed_response", False)),
        (SearchError("opaque cause", retryable=True), ("other", True)),
    ),
    ids=(
        "structured-timeout",
        "structured-timeout-default",
        "structured-authentication",
        "message-rate-limit",
        "message-malformed",
        "retryable-only",
    ),
)
def test_classify_search_error_inherits_cause_metadata(cause, expected) -> None:
    error = SearchError("opaque provider failure")
    error.__cause__ = cause

    assert classify_search_error(error) == expected


def test_classify_search_error_outer_structured_values_override_cause_metadata():
    cause = SearchError("429 Too Many Requests", kind="rate_limit", retryable=True)
    error = SearchError("invalid JSON response", kind="authentication", retryable=False)
    error.__cause__ = cause

    assert classify_search_error(error) == ("authentication", False)


def test_classify_search_error_outer_message_precedes_cause_message():
    cause = SearchError("429 Too Many Requests")
    error = SearchError("invalid JSON response")
    error.__cause__ = cause

    assert classify_search_error(error) == ("malformed_response", False)


@pytest.mark.parametrize(
    ("status", "cause_kind", "cause_retryable", "expected"),
    (
        (429, "authentication", False, ("authentication", False)),
        (401, "timeout", True, ("timeout", True)),
    ),
    ids=("authentication-over-rate-limit", "timeout-over-authentication"),
)
def test_inherited_structured_kind_precedes_outer_status(
    status, cause_kind, cause_retryable, expected
) -> None:
    error = _status_error(status)
    error.__cause__ = SearchError(
        "opaque cause", kind=cause_kind, retryable=cause_retryable
    )

    assert classify_search_error(error) == expected


@pytest.mark.parametrize(
    ("transport_type", "cause_kind", "cause_retryable", "expected"),
    (
        (
            requests.Timeout,
            "authentication",
            False,
            ("authentication", False),
        ),
        (
            requests.ConnectionError,
            "timeout",
            True,
            ("timeout", True),
        ),
    ),
    ids=("authentication-over-timeout", "timeout-over-connection"),
)
def test_inherited_structured_kind_precedes_outer_transport(
    transport_type, cause_kind, cause_retryable, expected
) -> None:
    cause = SearchError("opaque cause", kind=cause_kind, retryable=cause_retryable)
    cause.__cause__ = transport_type("opaque transport failure")
    error = SearchError("opaque provider failure")
    error.__cause__ = cause

    assert classify_search_error(error) == expected


@pytest.mark.parametrize(
    ("message", "cause_kind", "cause_retryable", "expected"),
    (
        (
            "429 Too Many Requests",
            "authentication",
            False,
            ("authentication", False),
        ),
        ("invalid JSON response", "timeout", True, ("timeout", True)),
    ),
    ids=("authentication-over-rate-limit", "timeout-over-malformed"),
)
def test_inherited_structured_kind_precedes_outer_message(
    message, cause_kind, cause_retryable, expected
) -> None:
    error = SearchError(message)
    error.__cause__ = SearchError(
        "opaque cause", kind=cause_kind, retryable=cause_retryable
    )

    assert classify_search_error(error) == expected


def test_outer_status_precedes_inherited_cause_message():
    error = _status_error(401)
    error.__cause__ = SearchError("429 Too Many Requests")

    assert classify_search_error(error) == ("authentication", False)


@pytest.mark.parametrize(
    ("transport_type", "expected"),
    (
        (requests.Timeout, ("timeout", True)),
        (requests.ConnectionError, ("rate_limit", True)),
    ),
    ids=("timeout-before-message", "connection-after-message"),
)
def test_inherited_cause_message_follows_transport_precedence(
    transport_type, expected
) -> None:
    cause = transport_type("429 Too Many Requests")
    error = SearchError("opaque provider failure")
    error.__cause__ = cause

    assert classify_search_error(error) == expected


@pytest.mark.parametrize(
    ("outer_status", "inner_status", "expected"),
    (
        (401, 429, ("authentication", False)),
        (429, 401, ("rate_limit", True)),
        (500, 401, ("other", True)),
    ),
    ids=("outer-auth", "outer-rate-limit", "outer-server-error"),
)
def test_classify_search_error_outer_status_precedes_inner_status(
    outer_status, inner_status, expected
) -> None:
    outer = _status_error(outer_status)
    outer.__cause__ = _status_error(inner_status)

    assert classify_search_error(outer) == expected


def _target() -> _Target:
    case = CaseRecord(
        title="Synthetic v. Citation",
        court="CAVC",
        citation="30 Vet.App. 1",
        courtlistener_opinion_id="1",
    )
    return _Target(
        case=case,
        opinion_id=1,
        citation=case.citation,
        case_name=case.title,
        cluster_id="synthetic",
        opinion_ids=(1,),
    )


@pytest.mark.parametrize(
    (
        "message",
        "kind",
        "retryable",
        "cause_type",
        "status",
        "expected_kind",
        "expected_retryable",
    ),
    (
        (
            "429 Too Many Requests",
            "",
            None,
            None,
            None,
            "rate_limit",
            True,
        ),
        (
            "opaque provider failure",
            "authentication",
            False,
            None,
            None,
            "authentication",
            False,
        ),
        (
            "opaque provider failure",
            "",
            None,
            requests.Timeout,
            None,
            "timeout",
            True,
        ),
        (
            "opaque provider failure",
            "",
            None,
            requests.ConnectionError,
            None,
            "other",
            True,
        ),
        (
            "opaque provider failure",
            "",
            None,
            None,
            429,
            "rate_limit",
            True,
        ),
    ),
    ids=(
        "legacy-message",
        "structured",
        "wrapped-timeout",
        "wrapped-connection",
        "http-status",
    ),
)
def test_target_cluster_failure_rows_expose_classified_error_kind(
    message,
    kind,
    retryable,
    cause_type,
    status,
    expected_kind,
    expected_retryable,
) -> None:
    class Provider:
        def citing_edges(self, opinion_id, page_size=100, cursor=None):
            error = SearchError(message, kind=kind, retryable=retryable)
            if cause_type is not None:
                error.__cause__ = cause_type("opaque transport failure")
            if status is not None:
                error.response = _FakeResponse({}, status)  # type: ignore[attr-defined]
            raise error

    edge_fetch = _fetch_cluster_forward_edges(
        Provider(), _target(), max_pages=1, page_size=12, max_ids=12
    )
    row = edge_fetch.opinion_audits[0]

    assert row.fetch_error_kind == expected_kind
    assert row.fetch_retryable is expected_retryable
    assert row.gap_codes == ["forward_fetch_failed"]
