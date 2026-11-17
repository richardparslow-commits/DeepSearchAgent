"""Negative and cross-field coverage for reusable audit-row invariants."""

from __future__ import annotations

import pytest
import requests

from audit_invariants import (
    assert_exception_chain_redacted,
    assert_source_opinion_audit_invariants,
)
from va_legal_agent.models import SourceOpinionAudit
from va_legal_agent.redaction import sanitize_exception


def _valid_row(**updates: object) -> SourceOpinionAudit:
    row = SourceOpinionAudit(
        opinion_id="201",
        candidate_allocation=2,
        fetch_attempted=True,
        reported_count_known=True,
        forward_citations_reported=1,
        citing_ids_collected=1,
        citing_ids_new_to_cluster=1,
        pages_fetched=1,
        complete=True,
    )
    return row.model_copy(update=updates)


def test_valid_row_passes_invariants() -> None:
    assert_source_opinion_audit_invariants([_valid_row()])


@pytest.mark.parametrize(
    "updates",
    [
        pytest.param({"complete": False, "truncated": True}, id="incomplete"),
        pytest.param({"gap_codes": ["page_cap"]}, id="complete-with-gap"),
        pytest.param({"fetch_retryable": True}, id="retryable-without-error"),
        pytest.param({"pages_fetched": 0}, id="known-count-without-pages"),
        pytest.param(
            {
                "pages_fetched": 0,
                "reported_count_known": False,
                "forward_citations_reported": 1,
            },
            id="reported-without-pages",
        ),
        pytest.param(
            {"candidate_pool_limit_reason": "allocation"},
            id="allocation-reason-with-budget",
        ),
        pytest.param(
            {
                "count_mismatch": True,
                "complete": False,
                "truncated": True,
                "gap_codes": ["page_cap"],
            },
            id="mismatch-without-count-gap",
        ),
        pytest.param({"opinion_id": ""}, id="empty-opinion-id"),
        pytest.param(
            {"candidate_pool_limit_reason": "row"},
            id="row-cap-without-truncation",
        ),
    ],
)
def test_helper_rejects_invalid_cross_field_state(
    updates: dict[str, object],
) -> None:
    with pytest.raises(AssertionError):
        assert_source_opinion_audit_invariants([_valid_row(**updates)])


def test_helper_rejects_duplicate_opinion_ids() -> None:
    rows = [_valid_row(), _valid_row(opinion_index=1)]

    with pytest.raises(AssertionError):
        assert_source_opinion_audit_invariants(rows)


def test_helper_rejects_incorrect_aggregate_total() -> None:
    with pytest.raises(AssertionError):
        assert_source_opinion_audit_invariants(
            [_valid_row()],
            total_new_to_cluster=2,
        )


def test_secret_redaction_invariant_checks_raw_cause_chain() -> None:
    secret = "SUPERSECRET123"
    try:
        try:
            raise requests.HTTPError(
                "401 for url: https://api.govinfo.gov/search?api_key=" + secret
            )
        except requests.HTTPError as cause:
            raise RuntimeError("analysis failed") from cause
    except RuntimeError as error:
        with pytest.raises(AssertionError):
            assert_exception_chain_redacted(error, secret)
        sanitize_exception(error)
        assert_exception_chain_redacted(error, secret)
