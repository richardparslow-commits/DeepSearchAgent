"""Property-based invariants for source-cluster per-opinion audit rows."""

from __future__ import annotations

from dataclasses import dataclass
import json

from hypothesis import given, settings, strategies as st

from audit_invariants import assert_source_opinion_audit_invariants
from va_legal_agent.citator import _Target, _fetch_cluster_forward_edges
from va_legal_agent.models import (
    CaseRecord,
    CitatorCoverageGap,
    SourceAuditLedger,
    SourceOpinionAudit,
    SourceValiditySignal,
)
from va_legal_agent.search import SearchError


@dataclass(frozen=True)
class _Page:
    rows: tuple[object, ...]
    count: int | None
    next: bool


def _page_payload(
    page: _Page,
    opinion_id: int,
    index: int,
    *,
    has_next_page: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "results": list(page.rows),
        "next": (
            "https://www.courtlistener.com/api/rest/v4/"
            f"opinions/{opinion_id}/citing/?cursor={index + 1}"
            if page.next and has_next_page
            else ""
        ),
    }
    if page.count is not None:
        payload["count"] = page.count
    return payload


class _Provider:
    """Deterministic provider returning one generated page sequence per opinion."""

    def __init__(self, pages: tuple[_Page, ...]) -> None:
        self.pages = pages

    def citing_edges(
        self, opinion_id: int, page_size: int = 100, cursor: str | None = None
    ) -> dict[str, object]:
        index = 0 if cursor is None else int(cursor)
        return _page_payload(
            self.pages[index],
            opinion_id,
            index,
            has_next_page=index + 1 < len(self.pages),
        )


class _PerOpinionProvider:
    """Return a separate page sequence for each opinion."""

    def __init__(self, pages_by_opinion: dict[int, tuple[_Page, ...]]) -> None:
        self.pages_by_opinion = pages_by_opinion

    def citing_edges(
        self, opinion_id: int, page_size: int = 100, cursor: str | None = None
    ) -> dict[str, object]:
        pages = self.pages_by_opinion[opinion_id]
        index = 0 if cursor is None else int(cursor)
        return _page_payload(
            pages[index],
            opinion_id,
            index,
            has_next_page=index + 1 < len(pages),
        )


_FAILURE_KINDS = (
    "rate_limit",
    "authentication",
    "timeout",
    "malformed_response",
)
_RETRYABLE_KINDS = frozenset({"rate_limit", "timeout"})


@dataclass(frozen=True)
class _Failure:
    kind: str
    outcome: SearchError | tuple[object, ...]

    @property
    def retryable(self) -> bool:
        return self.kind in _RETRYABLE_KINDS


def _structured_failure(kind: str) -> _Failure:
    return _Failure(
        kind,
        SearchError("synthetic provider failure", kind=kind),
    )


_FAILURE_CASES = tuple(
    _structured_failure(kind) for kind in _FAILURE_KINDS
) + (_Failure("malformed_response", ()),)

_COUNT_CASES = (
    (None, False, 0),
    ("not-a-count", True, 0),
    (0, True, 0),
    (3, True, 3),
    ("3", True, 3),
    (2.5, True, 2),
    (-1, True, -1),
)
_RESULTS_CASE = st.one_of(
    st.none(),
    st.just("not-a-list"),
    st.just(0),
    st.just({"id": 1}),
    st.just((1, 2)),
)
_NEXT_CASE = st.one_of(
    st.sampled_from(
        (
            (None, False),
            ("", False),
            (0, False),
            ("not-a-url", True),
            (
                "https://www.courtlistener.com/api/rest/v4/"
                "opinions/101/citing/",
                True,
            ),
        )
    ),
    st.just(({"cursor": "2"}, True)),
    st.just((["2"], True)),
)


class _FailureProvider:
    """Fail selected opinions while returning complete empty pages elsewhere."""

    def __init__(self, failures: dict[int, _Failure | None]) -> None:
        self.failures = failures

    def citing_edges(
        self, opinion_id: int, page_size: int = 100, cursor: str | None = None
    ) -> object:
        failure = self.failures.get(opinion_id)
        if failure is None:
            return {"count": 0, "results": [], "next": ""}
        if isinstance(failure.outcome, SearchError):
            raise failure.outcome
        return failure.outcome


class _LateFailureProvider:
    """Return configured pages, then fail the following fetch for each opinion."""

    def __init__(self, pages: tuple[_Page, ...], failures: dict[int, _Failure]) -> None:
        self.pages = pages
        self.failures = failures

    def citing_edges(
        self, opinion_id: int, page_size: int = 100, cursor: str | None = None
    ) -> object:
        index = 0 if cursor is None else int(cursor)
        if index < len(self.pages):
            return _page_payload(
                self.pages[index],
                opinion_id,
                index,
                has_next_page=True,
            )

        failure = self.failures[opinion_id]
        if isinstance(failure.outcome, SearchError):
            raise failure.outcome
        return failure.outcome


class _PayloadProvider:
    """Return one fixed raw payload for every fetch."""

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def citing_edges(
        self, opinion_id: int, page_size: int = 100, cursor: str | None = None
    ) -> object:
        return self.payload


class _SequenceProvider:
    """Return raw payloads in call order, independent of cursor values."""

    def __init__(self, payloads: tuple[dict[str, object], ...]) -> None:
        self.payloads = payloads
        self.calls = 0

    def citing_edges(
        self, opinion_id: int, page_size: int = 100, cursor: str | None = None
    ) -> dict[str, object]:
        payload = self.payloads[self.calls]
        self.calls += 1
        return payload


def _target(size: int) -> _Target:
    opinion_ids = tuple(range(101, 101 + size))
    case = CaseRecord(
        title="Synthetic v. Audit",
        court="CAVC",
        citation="30 Vet.App. 1",
        courtlistener_opinion_id=str(opinion_ids[0]),
    )
    return _Target(
        case=case,
        opinion_id=opinion_ids[0],
        citation=case.citation,
        case_name=case.title,
        cluster_id="synthetic-audit",
        opinion_ids=opinion_ids,
    )


def _valid_row(citing_id: int) -> dict[str, str]:
    return {
        "citing_opinion": (
            "https://www.courtlistener.com/api/rest/v4/"
            f"opinions/{citing_id}/"
        )
    }


_MALFORMED_ROW = st.one_of(
    st.none(),
    st.just({"citing_opinion": "not-an-opinion-id"}),
)


def _assert_edge_fetch_invariants(edge_fetch, cluster_size: int) -> None:
    assert_source_opinion_audit_invariants(
        edge_fetch.opinion_audits,
        cluster_opinion_count=cluster_size,
        total_new_to_cluster=edge_fetch.unique_count,
        total_reported=edge_fetch.reported_count,
        total_pages=edge_fetch.pages,
    )


def _mixed_state_ledger(
    *,
    failure_kind: str,
    failure_retryable: bool,
    malformed_rows: int,
    duplicate_rows: int,
    cap_reason: str,
) -> SourceAuditLedger:
    def row(
        opinion_id: str, opinion_index: int, **updates: object
    ) -> SourceOpinionAudit:
        values: dict[str, object] = {
            "candidate_allocation": 2,
            "fetch_attempted": True,
            "reported_count_known": True,
            "forward_citations_reported": 1,
            "citing_ids_collected": 1,
            "citing_ids_new_to_cluster": 1,
            "pages_fetched": 1,
            "complete": True,
        }
        values.update(updates)
        return SourceOpinionAudit(
            opinion_id=opinion_id,
            opinion_index=opinion_index,
            **values,
        )

    cap_gap = ["source_candidate_pool_truncated"]
    if cap_reason == "allocation":
        cap_updates: dict[str, object] = {
            "candidate_allocation": 0,
            "fetch_attempted": False,
            "reported_count_known": False,
            "forward_citations_reported": 0,
            "citing_ids_collected": 0,
            "citing_ids_new_to_cluster": 0,
            "pages_fetched": 0,
            "complete": False,
            "truncated": True,
            "candidate_pool_limit_reached": True,
            "candidate_pool_limit_reason": "allocation",
            "gap_codes": cap_gap,
        }
    else:
        cap_updates = {
            "complete": False,
            "truncated": True,
            "candidate_pool_limit_reached": True,
            "candidate_pool_limit_reason": cap_reason,
            "gap_codes": cap_gap,
        }

    rows = [
        row("200", 0),
        row(
            "201",
            1,
            fetch_error_kind=failure_kind,
            fetch_retryable=failure_retryable,
            complete=False,
            gap_codes=["forward_fetch_failed"],
        ),
        row("202", 2, invalid_rows_ignored=malformed_rows),
        row("203", 3, **cap_updates),
        row(
            "204",
            4,
            page_limit_reached=True,
            complete=False,
            truncated=True,
            gap_codes=["page_cap"],
        ),
        row("205", 5, duplicate_rows_ignored=duplicate_rows),
    ]
    return SourceAuditLedger(
        cluster_opinion_count=len(rows),
        coverage_complete=False,
        page_limit_reached=True,
        candidate_pool_limit_reached=True,
        gaps=[
            CitatorCoverageGap(
                code="forward_fetch_failed",
                detail="generated failure",
            ),
            CitatorCoverageGap(code="page_cap", detail="generated cap"),
        ],
        opinion_audits=rows,
    )


def _mixed_summary(cap_reason: str) -> dict[str, list[str]]:
    if cap_reason == "allocation":
        cap_reasons = ["source_candidate_pool_truncated", "zero_allocation"]
    else:
        cap_reasons = [
            "source_candidate_pool_truncated",
            f"candidate_pool_{cap_reason}",
        ]
    return {
        "201": ["forward_fetch_failed"],
        "202": ["malformed_rows"],
        "203": cap_reasons,
        "204": ["page_cap"],
    }


_MIXED_LEDGER = st.builds(
    _mixed_state_ledger,
    failure_kind=st.sampled_from((*_FAILURE_KINDS, "other")),
    failure_retryable=st.booleans(),
    malformed_rows=st.integers(min_value=1, max_value=3),
    duplicate_rows=st.integers(min_value=0, max_value=3),
    cap_reason=st.sampled_from(("row", "cursor", "allocation")),
)
_LEGACY_ROW_FIELDS = (
    "fetch_error_kind",
    "fetch_retryable",
    "duplicate_rows_ignored",
    "invalid_rows_ignored",
)


def _legacy_payload(
    ledger: SourceAuditLedger, *, include_rows: bool = True
) -> dict[str, object]:
    payload = json.loads(ledger.model_dump_json())
    payload.pop("opinion_bottlenecks")
    if include_rows:
        for row in payload["opinion_audits"]:
            for field in _LEGACY_ROW_FIELDS:
                row.pop(field, None)
    else:
        payload.pop("opinion_audits")
    return payload


_VALID_ROW = st.integers(min_value=1000, max_value=1100).map(_valid_row)
_PAGE_ROW = st.one_of(_VALID_ROW, _MALFORMED_ROW)
_PAGE = st.builds(
    _Page,
    rows=st.lists(_PAGE_ROW, max_size=12).map(tuple),
    count=st.one_of(st.none(), st.integers(min_value=0, max_value=20)),
    next=st.booleans(),
)
_PAGES = st.lists(_PAGE, min_size=1, max_size=4).map(tuple)


@settings(max_examples=80, deadline=None)
@given(
    cluster_size=st.integers(min_value=1, max_value=8),
    max_ids=st.integers(min_value=1, max_value=24),
    max_pages=st.integers(min_value=1, max_value=3),
    pages=_PAGES,
    adaptive=st.booleans(),
)
def test_generated_clusters_preserve_per_opinion_invariants(
    cluster_size: int,
    max_ids: int,
    max_pages: int,
    pages: tuple[_Page, ...],
    adaptive: bool,
) -> None:
    edge_fetch = _fetch_cluster_forward_edges(
        _Provider(pages),
        _target(cluster_size),
        max_pages=max_pages,
        page_size=max_ids,
        max_ids=max_ids,
        adaptive_allocation=adaptive,
    )

    _assert_edge_fetch_invariants(edge_fetch, cluster_size)


@settings(max_examples=80, deadline=None)
@given(
    cluster_size=st.integers(min_value=1, max_value=8),
    max_ids=st.integers(min_value=1, max_value=24),
    max_pages=st.integers(min_value=1, max_value=3),
    pages=_PAGES,
)
def test_adaptive_allocation_stays_within_cluster_budget(
    cluster_size: int,
    max_ids: int,
    max_pages: int,
    pages: tuple[_Page, ...],
) -> None:
    edge_fetch = _fetch_cluster_forward_edges(
        _Provider(pages),
        _target(cluster_size),
        max_pages=max_pages,
        page_size=max_ids,
        max_ids=max_ids,
        adaptive_allocation=True,
    )

    _assert_edge_fetch_invariants(edge_fetch, cluster_size)
    rows = edge_fetch.opinion_audits
    assert len(set(edge_fetch.ids)) == edge_fetch.unique_count
    assert edge_fetch.unique_count <= max_ids
    assert sum(row.citing_ids_new_to_cluster for row in rows) == (
        edge_fetch.unique_count
    )
    assert sum(row.citing_ids_collected for row in rows) <= max_ids
    assert all(row.candidate_allocation <= max_ids for row in rows)


@settings(max_examples=60, deadline=None)
@given(
    cluster_size=st.integers(min_value=3, max_value=6),
    shared_count=st.integers(min_value=1, max_value=3),
    budget_extra=st.integers(min_value=0, max_value=3),
)
def test_distinct_opinion_pages_exercise_adaptive_carry_and_dedup(
    cluster_size: int,
    shared_count: int,
    budget_extra: int,
) -> None:
    target = _target(cluster_size)
    shared_ids = tuple(range(1000, 1000 + shared_count))
    shared_rows = tuple(_valid_row(citing_id) for citing_id in shared_ids)
    pages_by_opinion: dict[int, tuple[_Page, ...]] = {
        target.opinion_ids[0]: (_Page(rows=(), count=0, next=False),),
        target.opinion_ids[1]: (
            _Page(rows=shared_rows, count=shared_count, next=False),
        ),
    }
    for index, opinion_id in enumerate(target.opinion_ids[2:], start=2):
        unique_row = _valid_row(7000 + index)
        pages_by_opinion[opinion_id] = (
            _Page(
                rows=(*shared_rows, unique_row),
                count=shared_count + 1,
                next=False,
            ),
        )

    max_ids = (shared_count + 1) * cluster_size + budget_extra
    edge_fetch = _fetch_cluster_forward_edges(
        _PerOpinionProvider(pages_by_opinion),
        target,
        max_pages=1,
        page_size=max_ids,
        max_ids=max_ids,
        adaptive_allocation=True,
    )

    _assert_edge_fetch_invariants(edge_fetch, cluster_size)
    first, second = edge_fetch.opinion_audits[:2]
    assert first.citing_ids_collected == 0
    assert second.candidate_allocation > first.candidate_allocation
    assert second.citing_ids_collected == shared_count
    assert edge_fetch.unique_count == shared_count + cluster_size - 2
    assert sum(
        row.citing_ids_duplicate_across_cluster for row in edge_fetch.opinion_audits
    ) == shared_count * (cluster_size - 2)
    assert edge_fetch.complete is True


@settings(max_examples=50, deadline=None)
@given(max_ids=st.integers(min_value=1, max_value=10), extra=st.integers(min_value=1, max_value=5))
def test_row_cap_preserves_per_opinion_invariants(
    max_ids: int, extra: int
) -> None:
    rows = tuple(_valid_row(value) for value in range(1, max_ids + extra + 1))
    edge_fetch = _fetch_cluster_forward_edges(
        _Provider((_Page(rows=rows, count=len(rows), next=False),)),
        _target(1),
        max_pages=1,
        page_size=max_ids,
        max_ids=max_ids,
    )

    assert any(
        row.candidate_pool_limit_reason == "row"
        for row in edge_fetch.opinion_audits
    )
    _assert_edge_fetch_invariants(edge_fetch, 1)


@settings(max_examples=50, deadline=None)
@given(max_ids=st.integers(min_value=1, max_value=10))
def test_cursor_cap_preserves_per_opinion_invariants(max_ids: int) -> None:
    rows = tuple(_valid_row(value) for value in range(1, max_ids + 1))
    pages = (
        _Page(rows=rows, count=max_ids, next=True),
        _Page(rows=(), count=0, next=False),
    )
    edge_fetch = _fetch_cluster_forward_edges(
        _Provider(pages),
        _target(1),
        max_pages=2,
        page_size=max_ids,
        max_ids=max_ids,
    )

    assert any(
        row.candidate_pool_limit_reason == "cursor"
        for row in edge_fetch.opinion_audits
    )
    _assert_edge_fetch_invariants(edge_fetch, 1)


@settings(max_examples=50, deadline=None)
@given(max_ids=st.integers(min_value=2, max_value=20))
def test_page_cap_preserves_per_opinion_invariants(max_ids: int) -> None:
    pages = (
        _Page(rows=(_valid_row(1),), count=1, next=True),
        _Page(rows=(), count=0, next=False),
    )
    edge_fetch = _fetch_cluster_forward_edges(
        _Provider(pages),
        _target(1),
        max_pages=1,
        page_size=max_ids,
        max_ids=max_ids,
    )

    assert any(row.page_limit_reached for row in edge_fetch.opinion_audits)
    _assert_edge_fetch_invariants(edge_fetch, 1)


@settings(max_examples=50, deadline=None)
@given(max_ids=st.integers(min_value=1, max_value=10), extra=st.integers(min_value=1, max_value=5))
def test_allocation_cap_preserves_per_opinion_invariants(
    max_ids: int, extra: int
) -> None:
    edge_fetch = _fetch_cluster_forward_edges(
        _Provider((_Page(rows=(), count=0, next=False),)),
        _target(max_ids + extra),
        max_pages=1,
        page_size=max_ids,
        max_ids=max_ids,
    )

    assert any(row.candidate_allocation == 0 for row in edge_fetch.opinion_audits)
    _assert_edge_fetch_invariants(edge_fetch, max_ids + extra)


@settings(max_examples=40, deadline=None)
@given(
    duplicates=st.integers(min_value=1, max_value=3),
    malformed=st.integers(min_value=0, max_value=3),
)
def test_duplicate_and_malformed_rows_preserve_invariants(
    duplicates: int, malformed: int
) -> None:
    rows = (
        tuple(_valid_row(1000) for _ in range(duplicates + 1))
        + tuple(None for _ in range(malformed))
    )
    edge_fetch = _fetch_cluster_forward_edges(
        _Provider((_Page(rows=rows, count=1, next=False),)),
        _target(1),
        max_pages=1,
        page_size=8,
        max_ids=8,
    )

    row = edge_fetch.opinion_audits[0]
    assert row.duplicate_rows_ignored == duplicates
    assert row.invalid_rows_ignored == malformed
    _assert_edge_fetch_invariants(edge_fetch, 1)


@settings(max_examples=50, deadline=None)
@given(count_case=st.sampled_from(_COUNT_CASES))
def test_malformed_count_payload_preserves_accounting(
    count_case: tuple[object, bool, int],
) -> None:
    count_value, known, parsed = count_case
    edge_fetch = _fetch_cluster_forward_edges(
        _PayloadProvider(
            {
                "count": count_value,
                "results": [_valid_row(1)],
                "next": "",
            }
        ),
        _target(1),
        max_pages=1,
        page_size=3,
        max_ids=3,
    )

    _assert_edge_fetch_invariants(edge_fetch, 1)
    row = edge_fetch.opinion_audits[0]
    expected_reported = max(parsed, 1)
    count_mismatch = expected_reported > 1
    expected_gaps = ["count_mismatch"] if count_mismatch else []
    assert row.pages_fetched == 1
    assert row.citing_ids_collected == 1
    assert row.reported_count_known is known
    assert row.forward_citations_reported == expected_reported
    assert row.gap_codes == expected_gaps
    assert row.complete is not count_mismatch
    assert row.truncated is count_mismatch
    assert [gap.code for gap in edge_fetch.gaps] == expected_gaps


@settings(max_examples=50, deadline=None)
@given(
    results=_RESULTS_CASE,
    count=st.one_of(st.none(), st.integers(min_value=0, max_value=3)),
)
def test_malformed_results_payload_preserves_accounting(
    results: object,
    count: int | None,
) -> None:
    edge_fetch = _fetch_cluster_forward_edges(
        _PayloadProvider(
            {"count": count, "results": results, "next": ""}
        ),
        _target(1),
        max_pages=1,
        page_size=3,
        max_ids=3,
    )

    _assert_edge_fetch_invariants(edge_fetch, 1)
    row = edge_fetch.opinion_audits[0]
    count_mismatch = count is not None and count > 0
    expected_gaps = ["count_mismatch"] if count_mismatch else []
    assert row.pages_fetched == 1
    assert row.citing_ids_collected == 0
    assert row.invalid_rows_ignored == 0
    assert row.reported_count_known is (count is not None)
    assert row.forward_citations_reported == (count or 0)
    assert row.gap_codes == expected_gaps
    assert row.complete is not count_mismatch
    assert row.truncated is count_mismatch
    assert [gap.code for gap in edge_fetch.gaps] == expected_gaps


@settings(max_examples=50, deadline=None)
@given(next_case=_NEXT_CASE)
def test_malformed_next_payload_preserves_cursor_gap(
    next_case: tuple[object, bool],
) -> None:
    next_value, missing_cursor = next_case
    edge_fetch = _fetch_cluster_forward_edges(
        _PayloadProvider(
            {
                "count": 1,
                "results": [_valid_row(1)],
                "next": next_value,
            }
        ),
        _target(1),
        max_pages=1,
        page_size=3,
        max_ids=3,
    )

    _assert_edge_fetch_invariants(edge_fetch, 1)
    row = edge_fetch.opinion_audits[0]
    expected_gaps = ["pagination_cursor_missing"] if missing_cursor else []
    assert row.pages_fetched == 1
    assert row.citing_ids_collected == 1
    assert row.forward_citations_reported == 1
    assert row.gap_codes == expected_gaps
    assert row.complete is not missing_cursor
    assert row.truncated is False
    assert [gap.code for gap in edge_fetch.gaps] == expected_gaps


@settings(max_examples=50, deadline=None)
@given(
    first_id=st.integers(min_value=1000, max_value=2000),
    second_id=st.integers(min_value=2001, max_value=3000),
    count=st.sampled_from((None, 2)),
)
def test_repeated_pagination_cursor_preserves_accounting(
    first_id: int,
    second_id: int,
    count: int | None,
) -> None:
    repeated_cursor = (
        "https://www.courtlistener.com/api/rest/v4/"
        "opinions/101/citing/?cursor=repeat"
    )
    provider = _SequenceProvider(
        (
            {
                "count": count,
                "results": [_valid_row(first_id)],
                "next": repeated_cursor,
            },
            {
                "count": count,
                "results": [_valid_row(second_id)],
                "next": repeated_cursor,
            },
        )
    )
    edge_fetch = _fetch_cluster_forward_edges(
        provider,
        _target(1),
        max_pages=3,
        page_size=3,
        max_ids=3,
    )

    _assert_edge_fetch_invariants(edge_fetch, 1)
    row = edge_fetch.opinion_audits[0]
    assert row.pages_fetched == 2
    assert row.citing_ids_collected == 2
    assert row.reported_count_known is (count is not None)
    assert row.forward_citations_reported == 2
    assert row.gap_codes == ["pagination_cursor_repeated"]
    assert row.complete is False
    assert row.truncated is False
    assert [gap.code for gap in edge_fetch.gaps] == [
        "pagination_cursor_repeated"
    ]


@settings(max_examples=40, deadline=None)
@given(ledger=_MIXED_LEDGER)
def test_audit_ledger_json_round_trip_preserves_mixed_states(
    ledger: SourceAuditLedger,
) -> None:
    assert_source_opinion_audit_invariants(
        ledger.opinion_audits, cluster_opinion_count=6
    )
    failure_row, malformed_row, cap_row = ledger.opinion_audits[1:4]
    expected_summary = _mixed_summary(cap_row.candidate_pool_limit_reason)
    assert ledger.opinion_bottlenecks == expected_summary

    payload = json.loads(ledger.model_dump_json())
    assert payload["opinion_bottlenecks"] == expected_summary
    failure_payload, malformed_payload, cap_payload = payload[
        "opinion_audits"
    ][1:4]
    assert failure_payload["fetch_error_kind"] == failure_row.fetch_error_kind
    assert failure_payload["fetch_retryable"] is failure_row.fetch_retryable
    assert failure_payload["invalid_rows_ignored"] == 0
    assert (
        malformed_payload["invalid_rows_ignored"]
        == malformed_row.invalid_rows_ignored
    )
    assert (
        cap_payload["candidate_pool_limit_reason"]
        == cap_row.candidate_pool_limit_reason
    )

    restored = SourceAuditLedger.model_validate_json(ledger.model_dump_json())
    assert restored == ledger
    assert restored.opinion_bottlenecks == expected_summary


@settings(max_examples=40, deadline=None)
@given(ledger=_MIXED_LEDGER)
def test_source_validity_signal_round_trip_preserves_nested_ledger(
    ledger: SourceAuditLedger,
) -> None:
    signal = SourceValiditySignal(
        source_opinion_id="201",
        audit_ledger=ledger,
    )
    expected_summary = ledger.opinion_bottlenecks
    serialized = signal.model_dump_json()

    payload = json.loads(serialized)
    assert payload["audit_ledger"]["opinion_bottlenecks"] == expected_summary

    restored = SourceValiditySignal.model_validate_json(serialized)
    assert restored == signal
    assert restored.audit_ledger.opinion_bottlenecks == expected_summary


@settings(max_examples=40, deadline=None)
@given(ledger=_MIXED_LEDGER)
def test_legacy_audit_ledger_rows_default_new_failure_fields(
    ledger: SourceAuditLedger,
) -> None:
    restored = SourceAuditLedger.model_validate(_legacy_payload(ledger))

    assert len(restored.opinion_audits) == 6
    assert all(row.fetch_error_kind == "" for row in restored.opinion_audits)
    assert all(row.fetch_retryable is False for row in restored.opinion_audits)
    assert all(
        row.invalid_rows_ignored == 0 for row in restored.opinion_audits
    )
    assert all(
        row.duplicate_rows_ignored == 0 for row in restored.opinion_audits
    )
    cap_row = ledger.opinion_audits[3]
    expected_legacy_summary = _mixed_summary(
        cap_row.candidate_pool_limit_reason
    )
    expected_legacy_summary.pop("202")
    assert restored.opinion_bottlenecks == expected_legacy_summary
    assert restored.gaps == ledger.gaps

    round_trip = SourceAuditLedger.model_validate_json(restored.model_dump_json())
    assert round_trip == restored


@settings(max_examples=40, deadline=None)
@given(ledger=_MIXED_LEDGER)
def test_legacy_audit_ledger_without_rows_defaults_empty_summary(
    ledger: SourceAuditLedger,
) -> None:
    restored = SourceAuditLedger.model_validate(
        _legacy_payload(ledger, include_rows=False)
    )

    assert restored.opinion_audits == []
    assert restored.opinion_bottlenecks == {}
    assert restored.cluster_opinion_count == ledger.cluster_opinion_count
    assert restored.gaps == ledger.gaps


@settings(max_examples=80, deadline=None)
@given(
    cluster_size=st.integers(min_value=len(_FAILURE_CASES), max_value=8),
    successful_pages=st.integers(min_value=1, max_value=3),
    budget_extra=st.integers(min_value=0, max_value=4),
    reported_extra=st.integers(min_value=0, max_value=3),
    count_known=st.booleans(),
    adaptive=st.booleans(),
)
def test_failures_after_successful_pages_preserve_partial_coverage(
    cluster_size: int,
    successful_pages: int,
    budget_extra: int,
    reported_extra: int,
    count_known: bool,
    adaptive: bool,
) -> None:
    target = _target(cluster_size)
    failures = tuple(
        _FAILURE_CASES[index % len(_FAILURE_CASES)]
        for index in range(cluster_size)
    )
    reported_total = successful_pages + reported_extra
    pages = tuple(
        _Page(
            rows=(_valid_row(9000 + page_index),),
            count=reported_total if count_known else None,
            next=True,
        )
        for page_index in range(successful_pages)
    )
    provider = _LateFailureProvider(
        pages,
        dict(zip(target.opinion_ids, failures, strict=True)),
    )
    max_ids = (successful_pages + 1) * cluster_size + budget_extra
    edge_fetch = _fetch_cluster_forward_edges(
        provider,
        target,
        max_pages=successful_pages + 2,
        page_size=max_ids,
        max_ids=max_ids,
        adaptive_allocation=adaptive,
    )

    _assert_edge_fetch_invariants(edge_fetch, cluster_size)
    count_mismatch = count_known and reported_extra > 0
    assert edge_fetch.complete is False
    assert edge_fetch.truncated is count_mismatch
    assert edge_fetch.pages == sum(
        successful_pages + (0 if isinstance(failure.outcome, SearchError) else 1)
        for failure in failures
    )

    expected_gaps = ["forward_fetch_failed"]
    if count_mismatch:
        expected_gaps.append("count_mismatch")
    expected_summary = {
        str(opinion_id): (
            expected_gaps
            if count_known
            else [*expected_gaps, "reported_count_unknown"]
        )
        for opinion_id in target.opinion_ids
    }
    assert (
        SourceAuditLedger(
            opinion_audits=edge_fetch.opinion_audits
        ).opinion_audit_bottlenecks()
        == expected_summary
    )

    for index, failure in enumerate(failures):
        row = edge_fetch.opinion_audits[index]
        expected_pages = successful_pages + (
            0 if isinstance(failure.outcome, SearchError) else 1
        )
        assert row.opinion_id == str(target.opinion_ids[index])
        assert row.pages_fetched == expected_pages
        assert row.citing_ids_collected == successful_pages
        assert row.citing_ids_new_to_cluster == (
            successful_pages if index == 0 else 0
        )
        assert row.reported_count_known is count_known
        assert row.forward_citations_reported == (
            reported_total if count_known else successful_pages
        )
        assert row.fetch_error_kind == failure.kind
        assert row.fetch_retryable is failure.retryable
        assert row.gap_codes == expected_gaps
        assert row.count_mismatch is count_mismatch
        assert row.complete is False
        assert row.truncated is count_mismatch


@settings(max_examples=80, deadline=None)
@given(
    data=st.data(),
    cluster_size=st.integers(min_value=len(_FAILURE_CASES), max_value=8),
    extra=st.integers(min_value=0, max_value=6),
    max_pages=st.integers(min_value=1, max_value=3),
    adaptive=st.booleans(),
)
def test_per_opinion_fetch_failures_preserve_kind_and_retryability(
    data: st.DataObject,
    cluster_size: int,
    extra: int,
    max_pages: int,
    adaptive: bool,
) -> None:
    offset = data.draw(
        st.integers(
            min_value=0,
            max_value=cluster_size - len(_FAILURE_CASES),
        )
    )
    failures = (
        (None,) * offset
        + _FAILURE_CASES
        + (None,) * (cluster_size - len(_FAILURE_CASES) - offset)
    )

    target = _target(cluster_size)
    provider = _FailureProvider(dict(zip(target.opinion_ids, failures, strict=True)))
    max_ids = cluster_size + extra
    edge_fetch = _fetch_cluster_forward_edges(
        provider,
        target,
        max_pages=max_pages,
        page_size=max_ids,
        max_ids=max_ids,
        adaptive_allocation=adaptive,
    )

    _assert_edge_fetch_invariants(edge_fetch, cluster_size)
    for index, failure in enumerate(failures):
        row = edge_fetch.opinion_audits[index]
        if failure is None:
            assert row.fetch_attempted is True
            assert row.fetch_error_kind == ""
            assert row.fetch_retryable is False
            assert row.complete is True
            assert row.gap_codes == []
            continue
        assert row.fetch_attempted is True
        assert row.fetch_error_kind == failure.kind
        assert row.fetch_retryable is failure.retryable
        assert row.gap_codes == ["forward_fetch_failed"]

    assert edge_fetch.complete is False
