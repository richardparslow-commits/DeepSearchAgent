"""Reusable invariants for per-opinion source-cluster audit rows."""

from __future__ import annotations

import traceback
from collections.abc import Sequence

from va_legal_agent.models import (
    SOURCE_AUDIT_GAP_CODES,
    SOURCE_OPINION_FETCH_ERROR_KINDS,
    SOURCE_OPINION_LIMIT_REASONS,
    SourceAuditLedger,
    SourceOpinionAudit,
)


def _expected_bottlenecks(
    rows: Sequence[SourceOpinionAudit],
) -> dict[str, list[str]]:
    """Derive the expected summary independently of the model helper."""
    expected: dict[str, list[str]] = {}
    for row in rows:
        reasons = list(row.gap_codes)
        if row.candidate_allocation == 0:
            reasons.append("zero_allocation")
        elif row.candidate_pool_limit_reason in {"row", "cursor"}:
            reasons.append(f"candidate_pool_{row.candidate_pool_limit_reason}")
        if (
            row.pages_fetched > 0
            and not row.reported_count_known
            and not row.reported_count_malformed
        ):
            reasons.append("reported_count_unknown")
        if row.invalid_rows_ignored > 0:
            reasons.append("malformed_rows")
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            expected[row.opinion_id] = reasons
    return expected


def assert_exception_chain_redacted(exc: BaseException, secret: str) -> None:
    """Assert that *secret* is absent from an exception and every rendered form.

    This is deliberately independent of the production redaction helper: it
    walks ``__cause__``/``__context__`` directly and formats each node, so a
    regression that only redacts the outer message still fails.
    """
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert secret not in str(current), f"secret in str({type(current).__name__})"
        assert secret not in repr(current), f"secret in repr({type(current).__name__})"
        assert secret not in repr(getattr(current, "args", ())), "secret in args"
        formatted = "".join(
            traceback.format_exception(
                type(current), current, current.__traceback__
            )
        )
        assert secret not in formatted, "secret in formatted traceback"
        for attr in ("__cause__", "__context__"):
            chained = getattr(current, attr, None)
            if isinstance(chained, BaseException):
                pending.append(chained)


def assert_source_opinion_audit_invariants(
    rows: Sequence[SourceOpinionAudit],
    *,
    cluster_opinion_count: int | None = None,
    total_new_to_cluster: int | None = None,
    total_reported: int | None = None,
    total_pages: int | None = None,
) -> None:
    """Assert structural and accounting invariants for one cluster's rows.

    The optional totals let callers check the corresponding cluster fetch
    without requiring every test to build a full ``SourceAuditLedger`` first.
    """
    rows = list(rows)
    if cluster_opinion_count is not None:
        assert cluster_opinion_count == len(rows)
    assert [row.opinion_index for row in rows] == list(range(len(rows)))
    assert all(row.opinion_id for row in rows)
    assert len({row.opinion_id for row in rows}) == len(rows)

    for row in rows:
        assert row.candidate_allocation >= 0
        assert row.citing_ids_collected >= 0
        assert row.citing_ids_new_to_cluster >= 0
        assert row.citing_ids_duplicate_across_cluster >= 0
        assert row.citing_ids_collected == (
            row.citing_ids_new_to_cluster
            + row.citing_ids_duplicate_across_cluster
        )
        assert row.citing_ids_collected <= row.candidate_allocation
        assert row.forward_citations_reported >= 0
        assert row.duplicate_rows_ignored >= 0
        assert row.invalid_rows_ignored >= 0
        assert row.pages_fetched >= 0
        assert row.fetch_error_kind in SOURCE_OPINION_FETCH_ERROR_KINDS
        assert isinstance(row.fetch_retryable, bool)
        assert row.candidate_pool_limit_reason in SOURCE_OPINION_LIMIT_REASONS
        assert len(row.gap_codes) == len(set(row.gap_codes))
        assert set(row.gap_codes) <= SOURCE_AUDIT_GAP_CODES
        if row.pages_fetched == 0:
            assert row.reported_count_known is False
            assert row.forward_citations_reported == 0
        if row.reported_count_known:
            assert row.pages_fetched > 0
            assert row.count_mismatch is (
                row.forward_citations_reported != row.citing_ids_collected
            )
        else:
            assert row.count_mismatch is False
        if row.reported_count_malformed:
            assert row.reported_count_known is False
            assert "count_malformed" in row.gap_codes
            assert row.complete is False
        if "count_malformed" in row.gap_codes:
            assert row.reported_count_malformed is True
        if not row.fetch_error_kind:
            assert row.fetch_retryable is False

        if row.candidate_allocation == 0:
            assert row.fetch_attempted is False
            assert row.pages_fetched == 0
            assert row.citing_ids_collected == 0
            assert row.complete is False
            assert row.truncated is True
            assert row.candidate_pool_limit_reached is True
            assert row.candidate_pool_limit_reason == "allocation"
            assert "source_candidate_pool_truncated" in row.gap_codes
            assert row.forward_citations_reported == 0
        else:
            assert row.fetch_attempted is True

        if row.fetch_error_kind:
            assert row.fetch_attempted is True
            assert row.complete is False
            assert "forward_fetch_failed" in row.gap_codes

        if row.page_limit_reached:
            assert row.complete is False
            assert row.truncated is True
            assert "page_cap" in row.gap_codes

        if row.candidate_pool_limit_reached:
            assert row.truncated is True
            assert row.candidate_pool_limit_reason in {
                "row",
                "cursor",
                "allocation",
            }
        if row.candidate_pool_limit_reason in {"row", "cursor"}:
            assert row.candidate_pool_limit_reached is True
            assert "source_candidate_pool_truncated" in row.gap_codes
        if row.candidate_pool_limit_reason == "allocation":
            assert row.candidate_allocation == 0

        if row.count_mismatch:
            assert row.complete is False
            assert "count_mismatch" in row.gap_codes
            if row.forward_citations_reported > row.citing_ids_collected:
                assert row.truncated is True

        if not row.complete:
            assert row.gap_codes
        if row.complete:
            assert row.truncated is False
            assert row.gap_codes == []

    ledger = SourceAuditLedger(opinion_audits=rows)
    expected = _expected_bottlenecks(rows)
    assert ledger.opinion_audit_bottlenecks() == expected

    if total_new_to_cluster is not None:
        assert sum(row.citing_ids_new_to_cluster for row in rows) == (
            total_new_to_cluster
        )
    if total_reported is not None:
        assert sum(row.forward_citations_reported for row in rows) == total_reported
    if total_pages is not None:
        assert sum(row.pages_fetched for row in rows) == total_pages
