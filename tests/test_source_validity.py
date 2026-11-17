"""Tests for advisory source-validity validation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from itertools import product
import json
import random

import pytest
from hypothesis import given, settings, strategies as st

from audit_invariants import assert_source_opinion_audit_invariants
from va_legal_agent.adjudication import build_adjudication_queue
from va_legal_agent.authority_queue import apply_citator_findings
from va_legal_agent.courtlistener_cache import CourtListenerCache
from va_legal_agent.citator import (
    _SourceAudit,
    _SourceCandidateObservation,
    _SourceValidationCache,
    _Target,
    _apply_source_audit_trace,
    _citing_priority_pool_size,
    _dedupe_audit_gaps,
    _apply_source_validity_enforcement,
    _fetch_cluster_forward_edges,
    _source_invalidator_check,
    _source_invalidator_checks,
    _validate_source_validity,
    check_case,
)
from va_legal_agent.models import (
    _opinion_bottleneck_reasons,
    SOURCE_AUDIT_GAP_CODES,
    SOURCE_AUDIT_LOGIC_VERSION,
    SOURCE_AUDIT_PROVENANCE_VERSION,
    SOURCE_OPINION_FETCH_ERROR_KINDS,
    SOURCE_OPINION_LIMIT_REASONS,
    SOURCE_INVALIDATOR_REASON_CODES,
    SOURCE_INVALIDATOR_REVIEW_STATUSES,
    SOURCE_VALIDITY_REASON_CODES,
    CaseRecord,
    CitatorCheck,
    CitatorCoverageGap,
    LegalAnalysis,
    NegativeTreatment,
    SourceAuditLedger,
    SourceAuditProvenance,
    SourceInvalidatorCheck,
    SourceOpinionAudit,
    SourceValiditySignal,
    TreatmentTimelineEntry,
)
from va_legal_agent.providers import ProviderResponseTrace
from va_legal_agent.search import SearchError, classify_search_error
from va_legal_agent.topics import COURT_BVA, COURT_CAVC, COURT_UNKNOWN

TARGET_ID = 111
SOURCE_ID = 201
HISTORY_ID = 301
SECOND_HISTORY_ID = 401


def _case() -> CaseRecord:
    return CaseRecord(
        title="Smith v. Wilkie",
        court=COURT_CAVC,
        citation="30 Vet.App. 123",
        decision_date="2018-01-01",
        courtlistener_opinion_id=str(TARGET_ID),
        url="https://www.courtlistener.com/opinion/111/",
    )


def _target_meta() -> dict[str, object]:
    return {
        "title": "Smith v. Wilkie",
        "citation": "30 Vet.App. 123",
        "citations": ["30 Vet.App. 123"],
        "cluster_id": "777",
        "cluster_resolved": True,
        "opinion_ids": [TARGET_ID],
        "court": COURT_CAVC,
        "decision_date": "2018-01-01",
        "url": "https://www.courtlistener.com/opinion/111/",
        "opinion_type": "020lead",
        "precedential_status": "Published",
    }


def _source_meta(
    *,
    court: str = COURT_CAVC,
    status: str = "Published",
    opinion_type: str = "020lead",
    cluster_id: str = "888",
) -> dict[str, object]:
    return {
        "title": "Jones v. McDonough",
        "citation": "31 Vet.App. 456",
        "citations": ["31 Vet.App. 456"],
        "cluster_id": cluster_id,
        "cluster_resolved": True,
        "opinion_ids": [SOURCE_ID],
        "court": court,
        "decision_date": "2020-02-02",
        "url": "https://www.courtlistener.com/opinion/201/",
        "opinion_type": opinion_type,
        "precedential_status": status,
    }


def _history_meta(
    *,
    status: str = "Published",
    opinion_type: str = "020lead",
    opinion_id: int = HISTORY_ID,
    title: str = "Later v. Secretary",
    citation: str = "32 Vet.App. 1",
    cluster_id: str = "999",
    court: str = COURT_CAVC,
    decision_date: str = "2022-01-01",
) -> dict[str, object]:
    return {
        "title": title,
        "citation": citation,
        "citations": [citation],
        "cluster_id": cluster_id,
        "cluster_resolved": True,
        "opinion_ids": [opinion_id],
        "court": court,
        "decision_date": decision_date,
        "url": f"https://www.courtlistener.com/opinion/{opinion_id}/",
        "opinion_type": opinion_type,
        "precedential_status": status,
    }


def _page(ids: list[int]) -> dict[str, object]:
    return {
        "count": len(ids),
        "results": [
            {
                "citing_opinion": (
                    "https://www.courtlistener.com/api/rest/v4/"
                    f"opinions/{opinion_id}/"
                )
            }
            for opinion_id in ids
        ],
        "next": "",
    }


class SourceProvider:
    """Small provider surface for source-validation tests."""

    def __init__(self) -> None:
        self.pages: dict[str | None, dict[str, object]] = {}
        self.opinion_pages: dict[tuple[int, str | None], dict[str, object]] = {}
        self.metadata: dict[int, dict[str, object] | None] = {
            TARGET_ID: _target_meta(),
            SOURCE_ID: _source_meta(),
        }
        self.texts: dict[int, str] = {
            SOURCE_ID: "The court overruled Smith v. Wilkie, 30 Vet.App. 123."
        }
        self.metadata_errors: dict[int, SearchError] = {}
        self.text_errors: dict[int, SearchError] = {}
        self.edge_errors: dict[tuple[int, str | None], SearchError] = {}
        self.metadata_calls: list[int] = []
        self.text_calls: list[int] = []
        self.edge_calls: list[tuple[int, int, str | None]] = []

    def citing_edges(
        self, opinion_id: int, page_size: int = 100, cursor=None
    ):
        self.edge_calls.append((opinion_id, page_size, cursor))
        key = (opinion_id, cursor)
        if key in self.edge_errors:
            raise self.edge_errors[key]
        if key in self.opinion_pages:
            return self.opinion_pages[key]
        return self.pages.get(cursor, {"count": 0, "results": [], "next": ""})

    def same_docket_decisions(self, opinion_id: int, max_results: int = 5):
        return []

    def opinion_metadata(self, opinion_id: int):
        self.metadata_calls.append(opinion_id)
        if opinion_id in self.metadata_errors:
            raise self.metadata_errors[opinion_id]
        return self.metadata.get(opinion_id)

    def fetch_opinion_text(self, opinion_id: int):
        self.text_calls.append(opinion_id)
        if opinion_id in self.text_errors:
            raise self.text_errors[opinion_id]
        return self.texts.get(opinion_id, "")


def _provider() -> SourceProvider:
    provider = SourceProvider()
    provider.pages[None] = _page([SOURCE_ID])
    provider.opinion_pages[(SOURCE_ID, None)] = _page([])
    # The invalidator's own history is empty unless a test supplies one.
    provider.opinion_pages[(HISTORY_ID, None)] = _page([])
    return provider


def _check() -> CitatorCheck:
    return CitatorCheck(
        target_case="Smith v. Wilkie",
        target_court=COURT_CAVC,
        status="negative",
        negative_treatments=[
            NegativeTreatment(
                citing_case="Jones v. McDonough",
                source_opinion_id=str(SOURCE_ID),
                source_cluster_id="888",
                treatment="overruled",
                excerpt="The court overruled Smith v. Wilkie.",
            )
        ],
        source_validity=[
            SourceValiditySignal(
                source_opinion_id=str(SOURCE_ID),
                source_cluster_id="888",
            )
        ],
    )


def test_advisory_valid_source_reports_valid_without_changing_status():
    provider = _provider()

    check = check_case(
        _case(), provider, source_validity=True, source_validity_mode="advisory"
    )

    assert check.status == "negative"
    assert check.source_validity[0].status == "valid"
    assert check.source_validity[0].precedence == "authoritative"
    assert check.source_validity[0].withheld is False
    assert check.source_validity[0].history_complete is True
    assert check.review_required is False
    assert check.flags == []


def test_extract_mode_skips_source_history_lookups():
    provider = _provider()

    check = check_case(_case(), provider, source_validity=True)

    assert check.source_validity[0].status == "not_assessed"
    assert check.source_validity[0].withheld is True
    assert SOURCE_ID not in [call[0] for call in provider.edge_calls]


def _invalid_source_provider() -> SourceProvider:
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID])
    provider.metadata[HISTORY_ID] = _history_meta()
    provider.texts[HISTORY_ID] = (
        "The court overruled Jones v. McDonough, 31 Vet.App. 456."
    )
    return provider


def _history_provider(
    *,
    text: str = "The court overruled Jones v. McDonough, 31 Vet.App. 456.",
    status: str = "Published",
    opinion_type: str = "020lead",
    court: str = COURT_CAVC,
    decision_date: str = "2022-01-01",
    opinion_id: int = HISTORY_ID,
    title: str = "Later v. Secretary",
    citation: str = "32 Vet.App. 1",
    cluster_id: str = "999",
) -> SourceProvider:
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page([opinion_id])
    provider.metadata[opinion_id] = _history_meta(
        status=status,
        opinion_type=opinion_type,
        opinion_id=opinion_id,
        title=title,
        citation=citation,
        cluster_id=cluster_id,
        court=court,
        decision_date=decision_date,
    )
    provider.texts[opinion_id] = text
    return provider


def _nested_invalidator_provider() -> SourceProvider:
    provider = _invalid_source_provider()
    provider.opinion_pages[(HISTORY_ID, None)] = _page([SECOND_HISTORY_ID])
    provider.metadata[SECOND_HISTORY_ID] = _history_meta(
        opinion_id=SECOND_HISTORY_ID,
        title="Second v. Secretary",
        citation="33 Vet.App. 2",
        cluster_id="1000",
        decision_date="2023-01-01",
    )
    provider.texts[SECOND_HISTORY_ID] = (
        "The court overruled Later v. Secretary, 32 Vet.App. 1."
    )
    return provider


def _cycle_provider() -> SourceProvider:
    provider = _invalid_source_provider()
    provider.opinion_pages[(HISTORY_ID, None)] = _page([SOURCE_ID])
    provider.texts[SOURCE_ID] = (
        "The court overruled Later v. Secretary, 32 Vet.App. 1."
    )
    return provider


class _InvalidatorMetadataFailureProvider(SourceProvider):
    """Fail the invalidator audit's metadata read, not its history candidate."""

    def __init__(self) -> None:
        super().__init__()
        self.history_metadata_calls = 0

    def opinion_metadata(self, opinion_id: int):
        if opinion_id == HISTORY_ID:
            self.history_metadata_calls += 1
            if self.history_metadata_calls == 2:
                raise SearchError("invalidator metadata unavailable")
        return super().opinion_metadata(opinion_id)


def _mixed_invalidator_provider() -> SourceProvider:
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page(
        [HISTORY_ID, SECOND_HISTORY_ID]
    )
    provider.metadata[HISTORY_ID] = _history_meta(
        court=COURT_BVA, decision_date="2023-01-01"
    )
    provider.texts[HISTORY_ID] = (
        "The court overruled Jones v. McDonough, 31 Vet.App. 456."
    )
    provider.metadata[SECOND_HISTORY_ID] = _history_meta(
        opinion_id=SECOND_HISTORY_ID,
        title="Second v. Secretary",
        citation="33 Vet.App. 2",
        cluster_id="1000",
        decision_date="2022-06-01",
    )
    provider.texts[SECOND_HISTORY_ID] = (
        "The court overruled Jones v. McDonough, 31 Vet.App. 456."
    )
    provider.opinion_pages[(SECOND_HISTORY_ID, None)] = _page([])
    return provider


def test_advisory_invalid_source_exposes_accepted_invalidator_check():
    check = _check()

    _validate_source_validity(
        _invalid_source_provider(), check, _SourceValidationCache()
    )

    signal = check.source_validity[0]
    assert signal.status == "invalid"
    assert signal.reason_code == "source_invalidator_accepted_full"
    assert len(signal.invalidator_checks) == 1
    invalidator = signal.invalidator_checks[0]
    assert invalidator.source_opinion_id == str(HISTORY_ID)
    assert invalidator.source_cluster_id == "999"
    assert invalidator.citing_case == "Later v. Secretary"
    assert invalidator.citing_url.endswith(f"/opinion/{HISTORY_ID}/")
    assert invalidator.review_status == "reviewed"
    assert invalidator.authoritative is True
    assert invalidator.binding is True
    assert invalidator.invalidator_status == "valid"
    assert invalidator.accepted is True
    assert invalidator.reason_code == "invalidator_accepted"
    assert "valid" in invalidator.reason


def test_advisory_lower_court_invalidator_is_unverified_without_audit():
    provider = _history_provider(court=COURT_BVA)
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    assert signal.reason_code == "source_invalidator_rejected"
    assert signal.history_complete is False
    invalidator = signal.invalidator_checks[0]
    assert invalidator.review_status == "reviewed"
    assert invalidator.binding is False
    assert invalidator.invalidator_status == "not_checked"
    assert invalidator.accepted is False
    assert invalidator.reason_code == "invalidator_authority_lower"
    assert "lower" in invalidator.reason
    assert (HISTORY_ID, 100, None) not in provider.edge_calls


def test_advisory_unknown_authority_invalidator_is_unverified():
    provider = _history_provider(court=COURT_UNKNOWN)
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    invalidator = signal.invalidator_checks[0]
    assert invalidator.binding is None
    assert invalidator.accepted is False
    assert invalidator.reason_code == "invalidator_authority_unknown"
    assert "unknown" in invalidator.reason


def test_advisory_ambiguous_invalidator_is_unverified():
    provider = _history_provider(
        text="The court overruled Jones v. McDonough."
    )
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    invalidator = signal.invalidator_checks[0]
    assert invalidator.invalidator_status == "not_checked"
    assert invalidator.accepted is False
    assert invalidator.reason_code == "invalidator_attribution_ambiguous"
    assert "ambiguous" in invalidator.reason


def test_advisory_unresolved_invalidator_cluster_is_unverified():
    provider = _history_provider()
    provider.metadata[HISTORY_ID]["cluster_resolved"] = False
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    invalidator = signal.invalidator_checks[0]
    assert invalidator.invalidator_status == "unverified"
    assert invalidator.accepted is False
    assert "cluster" in invalidator.reason


def test_source_invalidator_missing_id_is_rejected_before_lookup():
    check = _source_invalidator_check(
        _provider(),
        NegativeTreatment(
            citing_case="Later v. Secretary",
            source_opinion_id="",
            treatment="overruled",
            scope="all",
            binding=True,
            excerpt="The court overruled Later v. Secretary.",
        ),
        "",
        _SourceValidationCache(),
        0,
    )

    assert check.invalidator_status == "unverified"
    assert check.accepted is False
    assert check.reason_code == "invalidator_missing_id"
    assert "missing" in check.reason


def test_advisory_nested_invalidator_is_unverified_at_depth_limit():
    provider = _nested_invalidator_provider()
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    invalidator = signal.invalidator_checks[0]
    assert invalidator.invalidator_status == "unverified"
    assert invalidator.accepted is False
    assert invalidator.reason_code == "invalidator_depth_exceeded"
    assert "depth limit" in signal.reason
    assert (SECOND_HISTORY_ID, 100, None) not in provider.edge_calls


def test_advisory_cached_invalid_or_qualified_invalidator_is_rejected():
    for status in ("invalid", "qualified"):
        provider = _invalid_source_provider()
        cache = _SourceValidationCache()
        cache.audits[(HISTORY_ID, "", 1)] = _SourceAudit(
            validity_status=status,
            reason=f"invalidator is {status}",
            history_complete=True,
        )
        check = _check()

        _validate_source_validity(provider, check, cache)

        signal = check.source_validity[0]
        assert signal.status == "unverified"
        assert signal.invalidator_checks[0].invalidator_status == status
        assert signal.invalidator_checks[0].accepted is False
        assert signal.invalidator_checks[0].reason_code == f"invalidator_{status}"
        assert status in signal.invalidator_checks[0].reason


def test_advisory_invalidator_metadata_failure_is_unverified():
    provider = _InvalidatorMetadataFailureProvider()
    provider.pages[None] = _page([SOURCE_ID])
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID])
    provider.opinion_pages[(HISTORY_ID, None)] = _page([])
    provider.metadata[HISTORY_ID] = _history_meta()
    provider.texts[HISTORY_ID] = (
        "The court overruled Jones v. McDonough, 31 Vet.App. 456."
    )
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    assert signal.invalidator_checks[0].invalidator_status == "unverified"
    assert signal.invalidator_checks[0].reason_code == "invalidator_unverified"
    assert "metadata unavailable" in signal.invalidator_checks[0].reason


def test_advisory_invalidator_cycle_does_not_recurse():
    provider = _cycle_provider()
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "unverified"
    assert sum(call[0] == SOURCE_ID for call in provider.edge_calls) == 1


def test_advisory_mixed_invalidators_accept_the_binding_candidate():
    provider = _mixed_invalidator_provider()
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "invalid"
    by_id = {item.source_opinion_id: item for item in signal.invalidator_checks}
    assert by_id[str(HISTORY_ID)].binding is False
    assert by_id[str(HISTORY_ID)].accepted is False
    assert by_id[str(SECOND_HISTORY_ID)].binding is True
    assert by_id[str(SECOND_HISTORY_ID)].accepted is True


def test_advisory_as_of_ignores_invalidator_history_after_cutoff():
    provider = _nested_invalidator_provider()
    provider.metadata[HISTORY_ID]["decision_date"] = "2020-06-01"
    check = _check().model_copy(update={"as_of_date": "2021-01-01"})

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "invalid"
    assert signal.invalidator_checks[0].invalidator_status == "valid"


def test_advisory_invalidator_cache_reuses_depth_one_audit():
    provider = _invalid_source_provider()
    cache = _SourceValidationCache()
    check = _check()

    _validate_source_validity(provider, check, cache)
    history_edges = [
        call for call in provider.edge_calls if call[0] == HISTORY_ID
    ]
    second = _check()

    _validate_source_validity(provider, second, cache)

    assert second.source_validity[0].status == "invalid"
    assert [
        call for call in provider.edge_calls if call[0] == HISTORY_ID
    ] == history_edges


def test_source_invalidator_checks_round_trip_through_json():
    signal = SourceValiditySignal(
        source_opinion_id="201",
        source_cluster_id="888",
        status="invalid",
        reason_code="source_invalidator_accepted_full",
        invalidator_checks=[
            SourceInvalidatorCheck(
                source_opinion_id="301",
                source_cluster_id="999",
                citing_case="Later v. Secretary",
                citing_url="https://www.courtlistener.com/opinion/301/",
                review_status="reviewed",
                treatment="overruled",
                scope="all",
                authoritative=True,
                gate_reason="",
                binding=True,
                invalidator_status="valid",
                accepted=True,
                reason_code="invalidator_accepted",
                reason="invalidator's own bounded status is valid",
            )
        ],
    )

    restored = SourceValiditySignal.model_validate_json(signal.model_dump_json())

    assert restored == signal


def test_advisory_invalid_source_keeps_negative_status_and_queue_behavior():
    extract_check = check_case(
        _case(),
        _invalid_source_provider(),
        source_validity=True,
    )
    advisory_check = check_case(
        _case(),
        _invalid_source_provider(),
        source_validity=True,
        source_validity_mode="advisory",
    )

    assert advisory_check.source_validity[0].status == "invalid"
    assert advisory_check.source_validity[0].withheld is False
    assert extract_check.source_validity[0].status == "not_assessed"
    assert advisory_check.status == "negative"
    assert advisory_check.status == extract_check.status
    assert advisory_check.good_law_as_of == extract_check.good_law_as_of
    assert advisory_check.coverage == extract_check.coverage
    assert advisory_check.flags == extract_check.flags
    assert advisory_check.review_required is extract_check.review_required
    assert advisory_check.review_reasons == extract_check.review_reasons
    assert advisory_check.negative_treatments == extract_check.negative_treatments

    cases = [_case()]
    extract_queue = apply_citator_findings(cases, [extract_check])
    advisory_queue = apply_citator_findings(cases, [advisory_check])
    assert [item.entry for item in extract_queue] == [
        item.entry for item in advisory_queue
    ]
    assert extract_queue[0].entry.citator_demoted is True


def test_advisory_nonprecedential_source_is_precedence_defect():
    provider = _provider()
    provider.metadata[SOURCE_ID] = _source_meta(status="Unpublished")
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "precedence_defect"
    assert check.source_validity[0].reason_code == "source_precedence_defect"
    assert check.source_validity[0].precedence == "nonprecedential"
    assert check.source_validity[0].withheld is True
    assert check.status == "negative"


def test_advisory_lower_court_source_is_precedence_defect():
    provider = _provider()
    provider.metadata[SOURCE_ID] = _source_meta(court=COURT_BVA)

    check = check_case(
        _case(), provider, source_validity=True, source_validity_mode="advisory"
    )

    assert check.source_validity[0].status == "precedence_defect"
    assert check.source_validity[0].reason_code == "source_precedence_defect"
    assert check.source_validity[0].precedence == "persuasive"
    assert check.source_validity[0].withheld is True
    assert "withheld from enforcement" in check.source_validity[0].reason


def test_known_lower_court_source_is_visible_but_withheld_from_enforcement():
    def lower_court_provider() -> SourceProvider:
        provider = _provider()
        provider.metadata[SOURCE_ID] = _source_meta(court=COURT_BVA)
        return provider

    extract_check = check_case(
        _case(), lower_court_provider(), source_validity=True
    )
    advisory_check = check_case(
        _case(),
        lower_court_provider(),
        source_validity=True,
        source_validity_mode="advisory",
    )

    assert extract_check.source_validity[0].status == "not_assessed"
    assert advisory_check.source_validity[0].status == "precedence_defect"
    assert advisory_check.source_validity[0].precedence == "persuasive"
    assert advisory_check.source_validity[0].withheld is True
    assert advisory_check.status == extract_check.status
    assert advisory_check.coverage == extract_check.coverage
    assert advisory_check.flags == extract_check.flags
    assert advisory_check.review_required is extract_check.review_required
    assert advisory_check.review_reasons == extract_check.review_reasons
    assert advisory_check.negative_treatments == extract_check.negative_treatments

    cases = [_case()]
    extract_queue = apply_citator_findings(cases, [extract_check])
    advisory_queue = apply_citator_findings(cases, [advisory_check])
    assert [item.entry for item in extract_queue] == [
        item.entry for item in advisory_queue
    ]


def test_advisory_unknown_court_is_unverified():
    provider = _provider()
    provider.metadata[SOURCE_ID] = _source_meta(court=COURT_UNKNOWN)

    check = check_case(
        _case(), provider, source_validity=True, source_validity_mode="advisory"
    )

    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].precedence == "unverified"
    assert check.source_validity[0].reason_code == "source_precedence_unverified"
    assert check.source_validity[0].withheld is True


def test_lower_court_source_is_withheld_even_when_its_history_is_invalid():
    def lower_court_invalid_provider() -> SourceProvider:
        provider = _invalid_source_provider()
        provider.metadata[SOURCE_ID] = _source_meta(court=COURT_BVA)
        return provider

    extract_check = check_case(
        _case(), lower_court_invalid_provider(), source_validity=True
    )
    advisory_check = check_case(
        _case(),
        lower_court_invalid_provider(),
        source_validity=True,
        source_validity_mode="advisory",
    )

    assert advisory_check.source_validity[0].status == "invalid"
    assert advisory_check.source_validity[0].precedence == "persuasive"
    assert advisory_check.source_validity[0].withheld is True
    assert advisory_check.status == extract_check.status
    assert advisory_check.coverage == extract_check.coverage
    assert advisory_check.negative_treatments == extract_check.negative_treatments


def test_advisory_separate_role_is_precedence_defect():
    provider = _provider()
    provider.metadata[SOURCE_ID] = _source_meta(opinion_type="040dissent")
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "precedence_defect"
    assert check.source_validity[0].precedence == "separate"
    assert check.source_validity[0].withheld is True


def test_advisory_unknown_role_and_status_are_unverified():
    provider = _provider()
    provider.metadata[SOURCE_ID] = _source_meta(
        opinion_type="", status=""
    )
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].precedence == "unverified"
    assert check.source_validity[0].reason_code == "source_precedence_gate_failed"


def test_advisory_missing_status_is_unverified():
    provider = _provider()
    provider.metadata[SOURCE_ID] = _source_meta(status="")
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].precedence == "unverified"
    assert check.source_validity[0].reason_code == "source_precedence_gate_failed"


def test_advisory_partial_source_history_is_qualified():
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID])
    provider.metadata[HISTORY_ID] = _history_meta()
    provider.texts[HISTORY_ID] = (
        "The court overruled Jones v. McDonough, 31 Vet.App. 456, in part."
    )

    check = check_case(
        _case(), provider, source_validity=True, source_validity_mode="advisory"
    )

    assert check.source_validity[0].status == "qualified"
    assert check.source_validity[0].reason_code == "source_invalidator_accepted_partial"
    assert check.source_validity[0].withheld is False
    assert "only in part" in check.source_validity[0].reason
    assert check.source_validity[0].invalidator_checks[0].accepted is True


def test_advisory_incomplete_source_history_is_unverified():
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID])

    check = check_case(
        _case(), provider, source_validity=True, source_validity_mode="advisory"
    )

    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].reason_code == "source_history_incomplete"
    assert check.source_validity[0].history_complete is False
    assert check.source_validity[0].invalidator_checks[0].review_status == (
        "metadata_unavailable"
    )
    assert check.source_validity[0].invalidator_checks[0].reason_code == (
        "citing_metadata_unavailable"
    )


def test_advisory_gated_source_history_is_unverified():
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID])
    provider.metadata[HISTORY_ID] = _history_meta(opinion_type="040dissent")
    provider.texts[HISTORY_ID] = (
        "The dissent would overrule Jones v. McDonough, 31 Vet.App. 456."
    )

    check = check_case(
        _case(), provider, source_validity=True, source_validity_mode="advisory"
    )

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    assert signal.reason_code == "source_invalidator_gated"
    assert "gated source-history" in signal.reason
    invalidator = signal.invalidator_checks[0]
    assert invalidator.review_status == "gated"
    assert invalidator.reason_code == "invalidator_gated"
    assert invalidator.authoritative is False
    assert invalidator.treatment == "overruled"
    assert invalidator.gate_reason


def test_advisory_metadata_failure_is_unverified():
    provider = _provider()
    provider.metadata_errors[SOURCE_ID] = SearchError("metadata unavailable")
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].reason_code == "source_metadata_unavailable"
    assert check.source_validity[0].withheld is True
    assert check.source_validity[0].reason == "source metadata unavailable"


def test_advisory_source_cluster_mismatch_is_unverified():
    provider = _provider()
    provider.metadata[SOURCE_ID] = _source_meta(cluster_id="999")
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].reason_code == "source_cluster_mismatch"
    assert check.source_validity[0].withheld is True
    assert "cluster id" in check.source_validity[0].reason


def test_advisory_cache_reuses_source_audit():
    provider = _provider()
    cache = _SourceValidationCache()

    check_case(
        _case(),
        provider,
        source_validity=True,
        source_validity_mode="advisory",
        source_validation_cache=cache,
    )
    source_edges = [
        call for call in provider.edge_calls if call[0] == SOURCE_ID
    ]

    second = check_case(
        _case(),
        provider,
        source_validity=True,
        source_validity_mode="advisory",
        source_validation_cache=cache,
    )

    assert second.source_validity[0].status == "valid"
    assert [
        call for call in provider.edge_calls if call[0] == SOURCE_ID
    ] == source_edges


def test_advisory_missing_source_id_does_not_lookup():
    provider = _provider()
    check = _check().model_copy(
        update={
            "source_validity": [
                SourceValiditySignal(source_opinion_id="not-an-id")
            ]
        }
    )

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "not_assessed"
    assert check.source_validity[0].withheld is True
    assert provider.metadata_calls == []


def test_advisory_as_of_excludes_post_cutoff_source_history():
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID])
    provider.metadata[HISTORY_ID] = _history_meta()
    provider.texts[HISTORY_ID] = (
        "The court overruled Jones v. McDonough, 31 Vet.App. 456."
    )
    check = _check().model_copy(update={"as_of_date": "2021-01-01"})

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "valid"
    assert signal.reason_code == "source_no_invalidating_treatment"
    assert signal.withheld is False
    assert signal.history_complete is True
    assert signal.invalidator_checks[0].review_status == "out_of_scope"
    assert signal.invalidator_checks[0].reason_code == "post_as_of"
    assert check.status == "negative"
    assert HISTORY_ID not in provider.text_calls


def test_advisory_as_of_includes_history_on_the_cutoff_date():
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID])
    provider.metadata[HISTORY_ID] = _history_meta()
    provider.metadata[HISTORY_ID]["decision_date"] = "2021-01-01"
    provider.texts[HISTORY_ID] = (
        "The court overruled Jones v. McDonough, 31 Vet.App. 456."
    )
    check = _check().model_copy(update={"as_of_date": "2021-01-01"})

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "invalid"
    assert check.source_validity[0].withheld is False
    assert check.source_validity[0].history_complete is True


def test_advisory_as_of_in_scope_history_is_validated():
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID])
    provider.metadata[HISTORY_ID] = _history_meta()
    provider.metadata[HISTORY_ID]["decision_date"] = "2020-06-01"
    provider.texts[HISTORY_ID] = (
        "The court overruled Jones v. McDonough, 31 Vet.App. 456."
    )
    check = _check().model_copy(update={"as_of_date": "2021-01-01"})

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "invalid"
    assert check.source_validity[0].withheld is False
    assert check.source_validity[0].history_complete is True


def test_advisory_as_of_undated_history_is_unverified():
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID])
    provider.metadata[HISTORY_ID] = _history_meta()
    provider.metadata[HISTORY_ID]["decision_date"] = ""
    provider.texts[HISTORY_ID] = (
        "The court overruled Jones v. McDonough, 31 Vet.App. 456."
    )
    check = _check().model_copy(update={"as_of_date": "2021-01-01"})

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    assert signal.reason_code == "source_as_of_undated"
    assert signal.withheld is True
    assert signal.history_complete is False
    assert signal.invalidator_checks[0].review_status == "out_of_scope"
    assert signal.invalidator_checks[0].reason_code == "undated_as_of"


def test_advisory_unresolved_source_cluster_is_unverified():
    provider = _provider()
    metadata = _source_meta()
    metadata["cluster_resolved"] = False
    provider.metadata[SOURCE_ID] = metadata
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].reason_code == "source_cluster_unresolved"
    assert check.source_validity[0].withheld is True
    assert "cluster could not be resolved" in check.source_validity[0].reason


def test_advisory_fills_missing_signal_cluster_id():
    provider = _provider()
    check = _check().model_copy(
        update={
            "source_validity": [
                SourceValiditySignal(source_opinion_id=str(SOURCE_ID))
            ]
        }
    )

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].source_cluster_id == "888"
    assert check.source_validity[0].status == "valid"


def test_advisory_preserves_status_and_review_fields():
    provider = _provider()
    check = _check().model_copy(
        update={
            "status": "good_law",
            "review_required": True,
            "review_reasons": ["manual review"],
            "flags": ["existing flag"],
        }
    )

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.status == "good_law"
    assert check.review_required is True
    assert check.review_reasons == ["manual review"]
    assert check.flags == ["existing flag"]


def _valid_enforcement_signal(source_id: str = str(SOURCE_ID)):
    return SourceValiditySignal(
        source_opinion_id=source_id,
        source_cluster_id="888",
        status="valid",
        precedence="authoritative",
        withheld=False,
        reason="clean source history",
    )


def _invalid_enforcement_signal(
    source_id: str = str(SOURCE_ID), *, reason: str = "source was overruled"
):
    return SourceValiditySignal(
        source_opinion_id=source_id,
        source_cluster_id="888",
        status="invalid",
        precedence="authoritative",
        withheld=False,
        reason=reason,
    )


def test_enforcement_authoritative_valid_source_is_noop():
    check = _check().model_copy(
        update={"source_validity": [_valid_enforcement_signal()]}
    )

    _apply_source_validity_enforcement(check)

    assert check.source_validity_enforced is True
    assert check.status == "negative"
    assert check.source_validity_actions == []
    assert check.source_validity_blocked_source_ids() == set()
    assert check.review_required is False


def test_enforcement_authoritative_invalid_source_downgrades_status():
    check = _check().model_copy(
        update={
            "good_law_as_of": "2021-01-01",
            "source_validity": [_invalid_enforcement_signal()],
        }
    )

    _apply_source_validity_enforcement(check)

    assert check.status == "unknown"
    assert check.good_law_as_of == ""
    assert check.source_validity_enforced is True
    assert check.source_validity_blocked_source_ids() == {str(SOURCE_ID)}
    assert check.source_validity_actions
    assert "invalid" in check.source_validity_actions[0]
    assert check.review_required is True
    assert any("removed all authoritative support" in reason for reason in check.review_reasons)


def test_enforcement_authoritative_qualified_source_downgrades_status():
    check = _check().model_copy(
        update={
            "source_validity": [
                SourceValiditySignal(
                    source_opinion_id=str(SOURCE_ID),
                    status="qualified",
                    precedence="authoritative",
                    withheld=False,
                    reason="source was overruled only in part",
                )
            ]
        }
    )

    _apply_source_validity_enforcement(check)

    assert check.status == "unknown"
    assert check.source_validity_actions
    assert "qualified" in check.source_validity_actions[0]
    assert check.review_required is True


def test_enforcement_lower_court_with_valid_other_source_keeps_negative():
    base = _check()
    second = NegativeTreatment(
        citing_case="Second v. McDonough",
        source_opinion_id="202",
        source_cluster_id="999",
        treatment="overruled",
        excerpt="The court overruled Smith v. Wilkie.",
    )
    lower_court = SourceValiditySignal(
        source_opinion_id=str(SOURCE_ID),
        status="precedence_defect",
        precedence="persuasive",
        withheld=True,
        reason="source court is lower than the target court",
    )
    check = base.model_copy(
        update={
            "negative_treatments": [*base.negative_treatments, second],
            "source_validity": [
                lower_court,
                _valid_enforcement_signal("202"),
            ],
        }
    )

    _apply_source_validity_enforcement(check)

    assert check.status == "negative"
    assert check.source_validity_blocked_source_ids() == {str(SOURCE_ID)}
    assert check.review_required is False


def test_enforcement_mixed_sources_keep_negative_and_block_invalid_source():
    base = _check()
    second = NegativeTreatment(
        citing_case="Second v. McDonough",
        source_opinion_id="202",
        source_cluster_id="999",
        treatment="overruled",
        excerpt="The court overruled Smith v. Wilkie.",
    )
    check = base.model_copy(
        update={
            "negative_treatments": [*base.negative_treatments, second],
            "source_validity": [
                _valid_enforcement_signal(),
                _invalid_enforcement_signal("202"),
            ],
        }
    )

    _apply_source_validity_enforcement(check)

    assert check.status == "negative"
    assert check.source_validity_blocked_source_ids() == {"202"}
    assert check.source_validity_actions
    assert check.review_required is True
    assert any("202" in reason for reason in check.review_reasons)


def test_enforcement_filters_derived_treatment_views():
    blocked_binding = NegativeTreatment(
        citing_case="Blocked v. McDonough",
        source_opinion_id="201",
        treatment="overruled",
        binding=True,
        excerpt="Blocked overruled Smith v. Wilkie.",
    )
    valid_binding = NegativeTreatment(
        citing_case="Valid v. McDonough",
        source_opinion_id="202",
        treatment="overruled",
        binding=True,
        excerpt="Valid overruled Smith v. Wilkie.",
    )
    blocked_persuasive = NegativeTreatment(
        citing_case="Lower v. McDonough",
        source_opinion_id="203",
        treatment="criticized",
        binding=False,
        excerpt="Lower criticized Smith v. Wilkie.",
    )
    check = _check().model_copy(
        update={
            "negative_treatments": [
                blocked_binding,
                valid_binding,
                blocked_persuasive,
            ],
            "binding_negative_treatments": [
                blocked_binding,
                valid_binding,
            ],
            "persuasive_negative_treatments": [blocked_persuasive],
            "treatment_timeline": [
                TreatmentTimelineEntry(
                    signal="negative",
                    treatment="overruled",
                    citing_case="Blocked v. McDonough",
                    citing_date="2025-01-01",
                    source_opinion_id="201",
                    excerpt="Blocked overruled Smith v. Wilkie.",
                    authoritative=True,
                ),
                TreatmentTimelineEntry(
                    signal="negative",
                    treatment="overruled",
                    citing_case="Valid v. McDonough",
                    citing_date="2023-01-01",
                    source_opinion_id="202",
                    excerpt="Valid overruled Smith v. Wilkie.",
                    authoritative=True,
                ),
                TreatmentTimelineEntry(
                    signal="negative",
                    treatment="criticized",
                    citing_case="Lower v. McDonough",
                    citing_date="2025-02-01",
                    source_opinion_id="203",
                    excerpt="Lower criticized Smith v. Wilkie.",
                    authoritative=True,
                ),
                TreatmentTimelineEntry(
                    signal="reaffirmed",
                    treatment="reaffirmed",
                    citing_case="Valid v. McDonough",
                    citing_date="2024-01-01",
                    source_opinion_id="202",
                    excerpt="Valid reaffirmed Smith v. Wilkie.",
                    authoritative=True,
                ),
            ],
            "later_reaffirmance": False,
            "source_validity": [
                _invalid_enforcement_signal("201"),
                _valid_enforcement_signal("202"),
                SourceValiditySignal(
                    source_opinion_id="203",
                    status="precedence_defect",
                    precedence="persuasive",
                    withheld=True,
                ),
            ],
        }
    )

    _apply_source_validity_enforcement(check)

    # Raw findings remain available for audit.
    assert check.negative_treatments == [
        blocked_binding,
        valid_binding,
        blocked_persuasive,
    ]
    assert check.binding_negative_treatments == [valid_binding]
    assert check.persuasive_negative_treatments == []
    assert [entry.source_opinion_id for entry in check.treatment_timeline] == [
        "202",
        "202",
    ]
    assert [entry.signal for entry in check.treatment_timeline] == [
        "negative",
        "reaffirmed",
    ]
    assert check.treatment_timeline[-1].later_than_negative is True
    assert check.later_reaffirmance is True


def test_enforcement_output_round_trips_through_serialized_analysis():
    base = _check()
    finding = base.negative_treatments[0]
    check = base.model_copy(
        update={
            "binding_negative_treatments": [finding],
            "treatment_timeline": [
                TreatmentTimelineEntry(
                    signal="negative",
                    treatment="overruled",
                    citing_case="Jones v. McDonough",
                    citing_date="2020-02-02",
                    source_opinion_id=str(SOURCE_ID),
                    excerpt="The court overruled Smith v. Wilkie.",
                    authoritative=True,
                )
            ],
            "source_validity": [_invalid_enforcement_signal()],
        }
    )
    _apply_source_validity_enforcement(check)
    analysis = LegalAnalysis(
        issue="service connection",
        summary="Test analysis.",
        how_it_affects_va_claims="Test impact.",
        citator_checks=[check],
    )

    loaded = LegalAnalysis.model_validate_json(analysis.model_dump_json())
    loaded_check = loaded.citator_checks[0]

    assert loaded_check.source_validity_enforced is True
    assert loaded_check.source_validity_actions == check.source_validity_actions
    assert loaded_check.status == "unknown"
    assert [
        item.source_opinion_id for item in loaded_check.negative_treatments
    ] == [str(SOURCE_ID)]
    assert loaded_check.binding_negative_treatments == []
    assert loaded_check.treatment_timeline == []
    queue = build_adjudication_queue(loaded.citator_checks)
    assert len(queue.items) == 1
    item = queue.items[0]
    assert item.priority == "low"
    assert item.evidence.source_validity_blocked is True
    assert item.proposal.disposition == "withheld"
    assert "source-validity enforcement blocked" in item.queue_reason


def test_enforcement_lower_court_source_is_withheld_and_downgrades_sole_support():
    signal = SourceValiditySignal(
        source_opinion_id=str(SOURCE_ID),
        source_cluster_id="888",
        status="precedence_defect",
        precedence="persuasive",
        withheld=True,
        reason="source court is lower than the target court",
    )
    check = _check().model_copy(update={"source_validity": [signal]})

    _apply_source_validity_enforcement(check)

    assert check.status == "unknown"
    assert check.source_validity_blocked_source_ids() == {str(SOURCE_ID)}
    assert check.source_validity_actions[0].startswith(
        "source 201 withheld from enforcement"
    )
    assert check.review_required is True


def test_enforcement_unverified_source_without_reason_is_withheld():
    signal = SourceValiditySignal(
        source_opinion_id=str(SOURCE_ID),
        status="unverified",
        precedence="unverified",
        withheld=True,
        reason="",
    )
    check = _check().model_copy(update={"source_validity": [signal]})

    _apply_source_validity_enforcement(check)

    assert check.status == "unknown"
    assert "unverified" in check.source_validity_actions[0]
    assert "no reason recorded" not in check.source_validity_actions[0]


def test_enforcement_without_source_signals_is_a_noop():
    check = _check().model_copy(update={"source_validity": []})

    _apply_source_validity_enforcement(check)

    assert check.source_validity_enforced is True
    assert check.source_validity_actions == []
    assert check.status == "negative"


def test_enforcement_is_idempotent():
    check = _check().model_copy(
        update={"source_validity": [_invalid_enforcement_signal()]}
    )

    _apply_source_validity_enforcement(check)
    first_status = check.status
    first_actions = list(check.source_validity_actions)
    first_reasons = list(check.review_reasons)
    _apply_source_validity_enforcement(check)

    assert check.status == first_status
    assert check.source_validity_actions == first_actions
    assert check.review_reasons == first_reasons


def test_enforce_mode_valid_source_keeps_negative_and_queue_demotion():
    check = check_case(
        _case(),
        _provider(),
        source_validity=True,
        source_validity_mode="enforce",
    )

    assert check.status == "negative"
    assert check.source_validity_enforced is True
    assert check.source_validity_actions == []
    queue = apply_citator_findings([_case()], [check])
    assert queue[0].entry.citator_demoted is True


def test_enforcement_good_law_with_blocked_signal_downgrades():
    check = _check().model_copy(
        update={
            "status": "good_law",
            "good_law_as_of": "2021-01-01",
            "source_validity": [_invalid_enforcement_signal()],
        }
    )

    _apply_source_validity_enforcement(check)

    assert check.status == "unknown"
    assert check.good_law_as_of == ""
    assert check.review_required is True


def test_enforce_mode_invalid_source_downgrades_and_filters_queue():
    check = check_case(
        _case(),
        _invalid_source_provider(),
        source_validity=True,
        source_validity_mode="enforce",
    )

    assert check.status == "unknown"
    assert check.source_validity[0].status == "invalid"
    assert check.source_validity_enforced is True
    assert check.source_validity_actions
    queue = apply_citator_findings([_case()], [check])
    assert queue[0].entry.citator_demoted is False
    assert queue[0].entry.citator_flagged is True
    assert any(
        "enforcement withheld" in reason
        for reason in queue[0].entry.citator_reasons
    )


def test_enforce_mode_lower_court_source_is_withheld_from_queue():
    provider = _provider()
    provider.metadata[SOURCE_ID] = _source_meta(court=COURT_BVA)

    check = check_case(
        _case(),
        provider,
        source_validity=True,
        source_validity_mode="enforce",
    )

    assert check.status == "unknown"
    assert check.source_validity[0].withheld is True
    assert check.source_validity_enforced is True
    queue = apply_citator_findings([_case()], [check])
    assert queue[0].entry.citator_demoted is False
    assert queue[0].entry.citator_flagged is True


def test_enforce_mode_as_of_post_cutoff_source_history_is_noop():
    provider = _invalid_source_provider()
    provider.metadata[HISTORY_ID]["decision_date"] = "2022-01-01"

    check = check_case(
        _case(),
        provider,
        as_of_date="2021-01-01",
        source_validity=True,
        source_validity_mode="enforce",
    )

    assert check.status == "negative"
    assert check.source_validity[0].status == "valid"
    assert check.source_validity_actions == []
    queue = apply_citator_findings([_case()], [check])
    assert queue[0].entry.citator_demoted is True


def test_enforce_mode_lower_court_invalid_source_is_withheld_from_queue():
    provider = _invalid_source_provider()
    provider.metadata[SOURCE_ID] = _source_meta(court=COURT_BVA)

    check = check_case(
        _case(),
        provider,
        source_validity=True,
        source_validity_mode="enforce",
    )

    assert check.source_validity[0].status == "invalid"
    assert check.source_validity[0].withheld is True
    assert check.status == "unknown"
    queue = apply_citator_findings([_case()], [check])
    assert queue[0].entry.citator_demoted is False
    assert queue[0].entry.citator_flagged is True


def test_enforce_mode_as_of_undated_source_history_is_withheld():
    provider = _invalid_source_provider()
    provider.metadata[HISTORY_ID]["decision_date"] = ""

    check = check_case(
        _case(),
        provider,
        as_of_date="2021-01-01",
        source_validity=True,
        source_validity_mode="enforce",
    )

    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].withheld is True
    assert check.status == "unknown"
    queue = apply_citator_findings([_case()], [check])
    assert queue[0].entry.citator_demoted is False


def test_enforce_mode_as_of_in_scope_source_history_downgrades():
    provider = _invalid_source_provider()
    provider.metadata[HISTORY_ID]["decision_date"] = "2020-06-01"

    check = check_case(
        _case(),
        provider,
        as_of_date="2021-01-01",
        source_validity=True,
        source_validity_mode="enforce",
    )

    assert check.status == "unknown"
    assert check.source_validity[0].status == "invalid"
    queue = apply_citator_findings([_case()], [check])
    assert queue[0].entry.citator_demoted is False


def test_enforce_mode_reuses_source_validation_cache():
    provider = _provider()
    cache = _SourceValidationCache()

    check_case(
        _case(),
        provider,
        source_validity=True,
        source_validity_mode="enforce",
        source_validation_cache=cache,
    )
    source_edges = [
        call for call in provider.edge_calls if call[0] == SOURCE_ID
    ]

    check_case(
        _case(),
        provider,
        source_validity=True,
        source_validity_mode="enforce",
        source_validation_cache=cache,
    )

    assert [
        call for call in provider.edge_calls if call[0] == SOURCE_ID
    ] == source_edges


def test_enforce_mode_rejected_invalidator_downgrades_and_is_idempotent():
    check = check_case(
        _case(),
        _history_provider(court=COURT_BVA),
        source_validity=True,
        source_validity_mode="enforce",
    )

    assert check.status == "unknown"
    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].withheld is True
    assert check.source_validity_actions
    first_status = check.status
    first_actions = list(check.source_validity_actions)
    first_reasons = list(check.review_reasons)

    _apply_source_validity_enforcement(check)

    assert check.status == first_status
    assert check.source_validity_actions == first_actions
    assert check.review_reasons == first_reasons


def test_enforce_mode_without_source_validity_does_nothing():
    check = check_case(
        _case(),
        _invalid_source_provider(),
        source_validity=False,
        source_validity_mode="enforce",
    )

    assert check.status == "negative"
    assert check.source_validity == []
    assert check.source_validity_enforced is False
    assert check.source_validity_actions == []


def _assert_audit_vocabulary(signal: SourceValiditySignal) -> None:
    assert signal.reason_code in SOURCE_VALIDITY_REASON_CODES
    for invalidator in signal.invalidator_checks:
        assert invalidator.review_status in SOURCE_INVALIDATOR_REVIEW_STATUSES
        assert invalidator.reason_code in SOURCE_INVALIDATOR_REASON_CODES
    for gap in signal.audit_ledger.gaps:
        assert gap.code in SOURCE_AUDIT_GAP_CODES
    for opinion in signal.audit_ledger.opinion_audits:
        assert opinion.candidate_pool_limit_reason in SOURCE_OPINION_LIMIT_REASONS
        for code in opinion.gap_codes:
            assert code in SOURCE_AUDIT_GAP_CODES


def test_source_candidate_metadata_fetch_failure_is_audited():
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID])
    provider.metadata_errors[HISTORY_ID] = SearchError("metadata rejected")
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    assert signal.reason_code == "source_history_incomplete"
    assert len(signal.invalidator_checks) == 1
    invalidator = signal.invalidator_checks[0]
    assert invalidator.source_opinion_id == str(HISTORY_ID)
    assert invalidator.review_status == "metadata_failed"
    assert invalidator.reason_code == "citing_metadata_fetch_failed"
    assert invalidator.treatment == ""
    assert invalidator.authoritative is None
    _assert_audit_vocabulary(signal)


def test_source_candidate_text_failures_are_audited():
    cases = [
        ("text_failed", "citing_text_fetch_failed", True),
        ("text_empty", "citing_text_empty", False),
    ]
    for review_status, reason_code, fetch_error in cases:
        provider = _history_provider(text="")
        if fetch_error:
            provider.text_errors[HISTORY_ID] = SearchError("text rejected")
        check = _check()

        _validate_source_validity(provider, check, _SourceValidationCache())

        signal = check.source_validity[0]
        assert signal.status == "unverified"
        assert signal.reason_code == "source_history_incomplete"
        invalidator = signal.invalidator_checks[0]
        assert invalidator.review_status == review_status
        assert invalidator.reason_code == reason_code
        assert invalidator.authoritative is None
        _assert_audit_vocabulary(signal)


def test_source_candidate_target_not_located_is_audited():
    provider = _history_provider(
        text="This opinion discusses a different decision entirely."
    )
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    assert signal.reason_code == "source_history_incomplete"
    invalidator = signal.invalidator_checks[0]
    assert invalidator.review_status == "target_not_located"
    assert invalidator.reason_code == "target_not_located"
    assert invalidator.authoritative is None
    _assert_audit_vocabulary(signal)


def _cap_provider() -> SourceProvider:
    provider = _provider()
    ids = [HISTORY_ID, SECOND_HISTORY_ID, 501, 601]
    provider.opinion_pages[(SOURCE_ID, None)] = _page(ids)
    dates = ["2024-04-01", "2023-04-01", "2022-04-01", "2021-04-01"]
    for opinion_id, decision_date in zip(ids, dates, strict=True):
        provider.metadata[opinion_id] = _history_meta(
            opinion_id=opinion_id,
            title=f"Later {opinion_id} v. Secretary",
            citation=f"{opinion_id} Vet.App. 1",
            cluster_id=str(opinion_id + 1000),
            decision_date=decision_date,
        )
        provider.texts[opinion_id] = (
            "The court overruled Jones v. McDonough, 31 Vet.App. 456."
        )
    return provider


def test_source_candidate_cap_records_unreviewed_candidates():
    provider = _cap_provider()
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "invalid"
    assert signal.reason_code == "source_invalidator_accepted_full"
    assert signal.history_complete is False
    assert [item.source_opinion_id for item in signal.invalidator_checks] == [
        "301",
        "401",
        "501",
        "601",
    ]
    assert [
        item.review_status for item in signal.invalidator_checks
    ] == ["reviewed", "reviewed", "reviewed", "unreviewed"]
    unreviewed = signal.invalidator_checks[-1]
    assert unreviewed.reason_code == "citing_opinion_cap"
    assert unreviewed.treatment == ""
    assert 601 not in provider.text_calls
    _assert_audit_vocabulary(signal)


def test_source_candidate_check_uses_finding_identity_when_present():
    observation = _SourceCandidateObservation(
        opinion_id=HISTORY_ID,
        metadata=_history_meta(),
        review_status="reviewed",
        reason_code="not_checked",
        reason="",
        finding=NegativeTreatment(
            citing_case="Later v. Secretary",
            source_opinion_id="",
            treatment="overruled",
            scope="all",
            binding=True,
            excerpt="The court overruled Jones v. McDonough.",
        ),
    )

    checks = _source_invalidator_checks(
        _provider(), [observation], "", _SourceValidationCache(), 0
    )

    assert checks[0].source_opinion_id == ""
    assert checks[0].reason_code == "invalidator_missing_id"
    assert checks[0].review_status == "reviewed"


def test_source_invalidator_checks_deduplicate_identical_observations():
    observation = _SourceCandidateObservation(
        opinion_id=HISTORY_ID,
        metadata=_history_meta(),
        review_status="metadata_unavailable",
        reason_code="citing_metadata_unavailable",
        reason="metadata unavailable",
    )

    checks = _source_invalidator_checks(
        _provider(), [observation, observation], "", _SourceValidationCache(), 0
    )

    assert len(checks) == 1
    assert checks[0].review_status == "metadata_unavailable"


def test_source_reviewed_noninvalidating_finding_is_not_audited():
    provider = _history_provider(
        text="The court criticized Jones v. McDonough, 31 Vet.App. 456."
    )
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "valid"
    assert signal.reason_code == "source_no_invalidating_treatment"
    assert signal.invalidator_checks == []


def test_source_clean_reviewed_candidate_is_not_an_invalidator_candidate():
    provider = _history_provider(
        text="The court discussed Jones v. McDonough, 31 Vet.App. 456."
    )
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "valid"
    assert signal.reason_code == "source_no_invalidating_treatment"
    assert signal.invalidator_checks == []


def test_source_rejected_candidate_precedes_gated_candidate():
    provider = _history_provider(court=COURT_BVA)
    provider.opinion_pages[(SECOND_HISTORY_ID, None)] = _page([])
    provider.opinion_pages[(SOURCE_ID, None)] = _page(
        [HISTORY_ID, SECOND_HISTORY_ID]
    )
    provider.metadata[SECOND_HISTORY_ID] = _history_meta(
        opinion_id=SECOND_HISTORY_ID,
        title="Dissent v. Secretary",
        citation="33 Vet.App. 2",
        cluster_id="1000",
        opinion_type="040dissent",
        decision_date="2023-01-01",
    )
    provider.texts[SECOND_HISTORY_ID] = (
        "The dissent would overrule Jones v. McDonough, 31 Vet.App. 456."
    )
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    assert signal.reason_code == "source_invalidator_rejected"
    statuses = {
        item.source_opinion_id: item.review_status
        for item in signal.invalidator_checks
    }
    assert statuses == {"301": "reviewed", "401": "gated"}
    _assert_audit_vocabulary(signal)


def test_source_gated_candidate_keeps_gated_status_with_as_of():
    provider = _history_provider(
        text="The dissent would overrule Jones v. McDonough, 31 Vet.App. 456."
    )
    provider.metadata[HISTORY_ID]["opinion_type"] = "040dissent"
    check = _check().model_copy(update={"as_of_date": "2023-01-01"})

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    assert signal.reason_code == "source_invalidator_gated"
    assert len(signal.invalidator_checks) == 1
    assert signal.invalidator_checks[0].review_status == "gated"


def test_source_audit_ledger_records_complete_clean_review():
    provider = _history_provider(
        text="The court discussed Jones v. McDonough, 31 Vet.App. 456."
    )
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    ledger = signal.audit_ledger
    assert signal.status == "valid"
    assert ledger.assessed is True
    assert ledger.history_attempted is True
    assert ledger.coverage_complete is True
    assert ledger.as_of_date == ""
    assert ledger.cluster_opinion_count == 1
    assert ledger.forward_citations_reported == 1
    assert ledger.citing_ids_collected == 1
    assert ledger.candidates_metadata_loaded == 1
    assert ledger.candidates_in_scope == 1
    assert ledger.candidates_reviewed == 1
    assert ledger.candidates_without_invalidating_finding == 1
    assert ledger.candidates_with_invalidating_finding == 0
    assert ledger.candidates_gated == 0
    assert ledger.candidates_unreviewed == 0
    assert ledger.candidates_post_as_of == 0
    assert ledger.candidates_undated_as_of == 0
    assert ledger.metadata_fetch_failures == 0
    assert ledger.metadata_unavailable == 0
    assert ledger.text_fetch_failures == 0
    assert ledger.text_empty == 0
    assert ledger.target_not_located == 0
    assert ledger.pages_fetched == 1
    assert ledger.page_limit_reached is False
    assert ledger.candidate_pool_limit_reached is False
    assert ledger.decisive_candidate_id == ""
    assert ledger.gaps == []
    _assert_audit_vocabulary(signal)


@pytest.mark.parametrize(
    ("adaptive", "allocation_policy"),
    ((False, "equal"), (True, "adaptive")),
)
def test_source_audit_provenance_records_reproducible_envelope(
    tmp_path, adaptive, allocation_policy
):
    provider = _history_provider(
        text="The court discussed Jones v. McDonough, 31 Vet.App. 456."
    )
    provider._cache = CourtListenerCache(tmp_path / "cache", ttl_seconds=900)
    check = _check()

    _validate_source_validity(
        provider,
        check,
        _SourceValidationCache(),
        adaptive_allocation=adaptive,
    )

    ledger = check.source_validity[0].audit_ledger
    provenance = ledger.provenance
    assert ledger.provenance_recorded is True
    assert provenance is not None
    assert provenance.is_recorded is True
    assert provenance.version == SOURCE_AUDIT_PROVENANCE_VERSION
    assert provenance.source_opinion_id == str(SOURCE_ID)
    assert provenance.source_cluster_id == "888"
    assert provenance.provider == "courtlistener"
    assert provenance.provider_version == "v4"
    assert provenance.audit_logic_version == SOURCE_AUDIT_LOGIC_VERSION
    assert provenance.max_pages == 1
    assert provenance.page_size == 12
    assert provenance.max_ids == 12
    assert provenance.max_citing_opinions == 3
    assert provenance.max_invalidator_depth == 1
    assert provenance.allocation_policy == allocation_policy
    assert provenance.cache_mode == "read-through"
    assert provenance.cache_ttl_seconds == 900
    assert datetime.fromisoformat(provenance.generated_at).tzinfo is not None
    restored = SourceAuditLedger.model_validate_json(ledger.model_dump_json())
    assert restored.provenance == provenance
    assert restored.provenance_recorded is True


def test_source_audit_provenance_records_observed_data_identity():
    provider = _history_provider(
        text="The court discussed Jones v. McDonough, 31 Vet.App. 456."
    )
    provider.BASE_URL = "https://www.courtlistener.com/api/rest/v4/"
    provider.cache_version = 1
    trace = ProviderResponseTrace(
        cache_hits=2,
        cache_misses=3,
        cache_entry_age_seconds=77,
    )
    trace.record_response({"payload": "same underlying response"})

    @contextmanager
    def request_trace():
        yield trace

    provider.request_trace = request_trace
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    provenance = check.source_validity[0].audit_ledger.provenance
    assert provenance is not None
    assert provenance.provider_base_url == provider.BASE_URL
    assert provenance.cache_version == 1
    assert provenance.cache_hits == 2
    assert provenance.cache_misses == 3
    assert provenance.cache_entry_age_seconds == 77
    assert len(provenance.response_digest) == 64


def test_source_audit_trace_can_be_skipped_without_provenance():
    ledger = SourceAuditLedger(assessed=True)

    _apply_source_audit_trace(ledger, ProviderResponseTrace(cache_hits=1))

    assert ledger.provenance is None


def test_source_audit_provenance_is_recorded_before_history_review():
    provider = _provider()
    provider.metadata[SOURCE_ID] = _source_meta(status="Unpublished")
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    ledger = check.source_validity[0].audit_ledger
    provenance = ledger.provenance
    assert provenance is not None
    assert ledger.history_attempted is False
    assert provenance.source_opinion_id == str(SOURCE_ID)
    assert provenance.source_cluster_id == "888"
    assert provenance.max_pages == 1
    assert provenance.page_size == 12
    assert provenance.max_ids == 12
    assert provenance.allocation_policy == "equal"
    assert provenance.cache_mode == "disabled"
    assert provenance.cache_ttl_seconds == 0


def test_source_audit_provenance_rejects_unknown_execution_modes():
    with pytest.raises(ValueError):
        SourceAuditProvenance(allocation_policy="round-robin")
    with pytest.raises(ValueError):
        SourceAuditProvenance(cache_mode="write-back")


def test_old_recorded_provenance_remains_an_older_envelope_version():
    legacy = SourceAuditProvenance(
        version=1,
        source_opinion_id=str(SOURCE_ID),
        source_cluster_id="888",
        provider="courtlistener",
        provider_version="v4",
        generated_at="2026-01-01T00:00:00+00:00",
        max_pages=1,
        page_size=12,
        max_ids=12,
        max_citing_opinions=3,
        max_invalidator_depth=1,
        allocation_policy="equal",
        cache_mode="disabled",
    )
    ledger = SourceAuditLedger(assessed=True, provenance=legacy)

    assert ledger.provenance is not None
    assert ledger.provenance.version == 1
    assert ledger.provenance.version != SOURCE_AUDIT_PROVENANCE_VERSION
    assert ledger.provenance.audit_logic_version == ""
    assert ledger.provenance.response_digest == ""
    assert ledger.provenance_recorded is True


def test_legacy_source_audit_ledgers_have_no_recorded_provenance():
    ledger = SourceAuditLedger(assessed=True)
    assert ledger.provenance is None
    assert ledger.provenance_recorded is False
    payload = json.loads(ledger.model_dump_json())
    assert payload["provenance"] is None
    assert payload["provenance_recorded"] is False
    assert SourceAuditLedger.model_validate({"assessed": True}).provenance is None
    assert (
        SourceAuditLedger.model_validate({"assessed": True}).provenance_recorded
        is False
    )

    placeholder = SourceAuditProvenance().model_dump()
    migrated = SourceAuditLedger.model_validate(
        {"assessed": True, "provenance": placeholder}
    )
    assert migrated.provenance is None
    assert migrated.provenance_recorded is False

    signal = SourceValiditySignal.model_validate(
        {
            "source_opinion_id": str(SOURCE_ID),
            "audit_ledger": {"assessed": True, "provenance": placeholder},
        }
    )
    assert signal.audit_ledger.provenance is None
    assert signal.audit_ledger.provenance_recorded is False


def test_source_audit_provenance_fingerprint_is_stable_and_time_free():
    provenance = SourceAuditProvenance(
        source_opinion_id=str(SOURCE_ID),
        source_cluster_id="888",
        provider="courtlistener",
        provider_version="v4",
        provider_base_url="https://www.courtlistener.com/api/rest/v4/",
        audit_logic_version=SOURCE_AUDIT_LOGIC_VERSION,
        generated_at="2026-01-01T00:00:00+00:00",
        max_pages=1,
        page_size=12,
        max_ids=12,
        max_citing_opinions=3,
        max_invalidator_depth=1,
        allocation_policy="equal",
        cache_mode="disabled",
        cache_version=1,
    )
    later = provenance.model_copy(
        update={"generated_at": "2026-02-01T00:00:00+00:00"}
    )

    assert provenance.is_recorded is True
    assert len(provenance.execution_fingerprint) == 64
    assert provenance.execution_fingerprint == later.execution_fingerprint
    restored = SourceAuditProvenance.model_validate_json(
        provenance.model_dump_json()
    )
    assert restored.execution_fingerprint == provenance.execution_fingerprint

    for update in (
        {"version": SOURCE_AUDIT_PROVENANCE_VERSION + 1},
        {"source_opinion_id": "202"},
        {"source_cluster_id": "999"},
        {"provider": "other"},
        {"provider_version": "v5"},
        {"provider_base_url": "https://other.test/api/"},
        {"audit_logic_version": "next"},
        {"max_pages": 2},
        {"page_size": 13},
        {"max_ids": 13},
        {"max_citing_opinions": 4},
        {"max_invalidator_depth": 2},
        {"allocation_policy": "adaptive"},
        {"cache_mode": "read-through"},
        {"cache_version": 2},
        {"cache_ttl_seconds": 900},
    ):
        changed = provenance.model_copy(update=update)
        assert changed.execution_fingerprint != provenance.execution_fingerprint

    for update in (
        {"cache_hits": 2},
        {"cache_misses": 3},
        {"cache_entry_age_seconds": 9},
        {"response_digest": "a" * 64},
    ):
        observed = provenance.model_copy(update=update)
        assert observed.execution_fingerprint == provenance.execution_fingerprint


def test_source_audit_provenance_comparability_checks_config_and_code():
    provenance = SourceAuditProvenance(
        source_opinion_id=str(SOURCE_ID),
        source_cluster_id="888",
        provider="courtlistener",
        provider_version="v4",
        provider_base_url="https://www.courtlistener.com/api/rest/v4/",
        audit_logic_version=SOURCE_AUDIT_LOGIC_VERSION,
        generated_at="2026-01-01T00:00:00+00:00",
        max_pages=1,
        page_size=12,
        max_ids=12,
        max_citing_opinions=3,
        max_invalidator_depth=1,
        allocation_policy="equal",
        cache_mode="disabled",
        cache_version=1,
    )
    later_observation = provenance.model_copy(
        update={
            "generated_at": "2026-02-01T00:00:00+00:00",
            "cache_hits": 2,
            "response_digest": "a" * 64,
        }
    )

    assert provenance.is_comparable_to(later_observation) is True
    assert (
        provenance.comparability_key
        == later_observation.comparability_key
    )
    assert provenance.is_comparable_to(None) is False
    assert provenance.is_comparable_to(SourceAuditProvenance()) is False
    unknown_code = provenance.model_copy(update={"audit_logic_version": ""})
    assert provenance.is_comparable_to(unknown_code) is False
    assert unknown_code.is_comparable_to(unknown_code.model_copy()) is False
    assert (
        provenance.is_comparable_to(
            provenance.model_copy(update={"max_pages": 2})
        )
        is False
    )
    assert (
        provenance.is_comparable_to(
            provenance.model_copy(update={"audit_logic_version": "next"})
        )
        is False
    )
    assert (
        provenance.is_comparable_to(
            provenance.model_copy(
                update={"version": SOURCE_AUDIT_PROVENANCE_VERSION - 1}
            )
        )
        is False
    )

    ledger = SourceAuditLedger(assessed=True, provenance=provenance)
    same = SourceAuditLedger(assessed=True, provenance=later_observation)
    assert ledger.is_comparable_to(same) is True
    assert ledger.is_comparable_to(SourceAuditLedger(assessed=True)) is False
    assert ledger.is_comparable_to(None) is False


def test_source_audit_ledger_records_decisive_invalidator():
    check = _check()

    _validate_source_validity(
        _invalid_source_provider(), check, _SourceValidationCache()
    )

    signal = check.source_validity[0]
    ledger = signal.audit_ledger
    assert signal.status == "invalid"
    assert ledger.coverage_complete is True
    assert ledger.candidates_metadata_loaded == 1
    assert ledger.candidates_in_scope == 1
    assert ledger.candidates_reviewed == 1
    assert ledger.candidates_with_invalidating_finding == 1
    assert ledger.candidates_without_invalidating_finding == 0
    assert ledger.decisive_candidate_id == str(HISTORY_ID)
    _assert_audit_vocabulary(signal)


def test_source_audit_ledger_coverage_can_complete_with_rejected_or_gated():
    rejected = _check()
    _validate_source_validity(
        _history_provider(court=COURT_BVA), rejected, _SourceValidationCache()
    )
    rejected_signal = rejected.source_validity[0]
    assert rejected_signal.status == "unverified"
    assert rejected_signal.history_complete is False
    assert rejected_signal.audit_ledger.coverage_complete is True
    assert rejected_signal.audit_ledger.decisive_candidate_id == ""

    gated = _check()
    gated_provider = _history_provider(
        text="The dissent would overrule Jones v. McDonough, 31 Vet.App. 456."
    )
    gated_provider.metadata[HISTORY_ID]["opinion_type"] = "040dissent"
    _validate_source_validity(gated_provider, gated, _SourceValidationCache())
    gated_signal = gated.source_validity[0]
    assert gated_signal.status == "unverified"
    assert gated_signal.history_complete is True
    assert gated_signal.audit_ledger.coverage_complete is True
    assert gated_signal.audit_ledger.candidates_gated == 1
    assert gated_signal.audit_ledger.candidates_with_invalidating_finding == 1
    assert gated_signal.audit_ledger.decisive_candidate_id == ""


def test_source_audit_ledger_records_early_gaps():
    metadata_failure = _check()
    metadata_provider = _provider()
    metadata_provider.metadata_errors[SOURCE_ID] = SearchError("missing")
    _validate_source_validity(
        metadata_provider, metadata_failure, _SourceValidationCache()
    )
    ledger = metadata_failure.source_validity[0].audit_ledger
    assert ledger.assessed is True
    assert ledger.history_attempted is False
    assert ledger.coverage_complete is False
    assert [gap.code for gap in ledger.gaps] == ["source_metadata_unavailable"]
    assert ledger.provenance is not None
    assert ledger.provenance.source_opinion_id == str(SOURCE_ID)
    assert ledger.provenance.source_cluster_id == ""
    assert ledger.provenance.max_pages == 1
    assert ledger.provenance.page_size == 12

    gate_failure = _check()
    gate_provider = _provider()
    gate_provider.metadata[SOURCE_ID] = _source_meta(status="Unpublished")
    _validate_source_validity(gate_provider, gate_failure, _SourceValidationCache())
    ledger = gate_failure.source_validity[0].audit_ledger
    assert ledger.assessed is True
    assert ledger.history_attempted is False
    assert ledger.coverage_complete is False
    assert [gap.code for gap in ledger.gaps] == [
        "source_precedence_gate_failed"
    ]
    assert ledger.provenance is not None
    assert ledger.provenance.source_cluster_id == "888"
    assert ledger.provenance.allocation_policy == "equal"

    unresolved = _check()
    unresolved_provider = _provider()
    unresolved_metadata = _source_meta()
    unresolved_metadata["cluster_resolved"] = False
    unresolved_provider.metadata[SOURCE_ID] = unresolved_metadata
    _validate_source_validity(unresolved_provider, unresolved, _SourceValidationCache())
    ledger = unresolved.source_validity[0].audit_ledger
    assert ledger.assessed is True
    assert ledger.history_attempted is False
    assert ledger.coverage_complete is False
    assert [gap.code for gap in ledger.gaps] == ["source_cluster_unresolved"]
    assert ledger.provenance is not None
    assert ledger.provenance.source_opinion_id == str(SOURCE_ID)
    assert ledger.provenance.source_cluster_id == "888"


def test_source_audit_ledger_adds_cluster_mismatch_gap():
    provider = _provider()
    provider.metadata[SOURCE_ID] = _source_meta(cluster_id="999")
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    assert signal.status == "unverified"
    assert signal.audit_ledger.coverage_complete is False
    assert [gap.code for gap in signal.audit_ledger.gaps] == [
        "source_cluster_mismatch"
    ]


def test_source_audit_ledger_keeps_post_as_of_coverage_complete():
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page(
        [HISTORY_ID, SECOND_HISTORY_ID]
    )
    provider.metadata[HISTORY_ID] = _history_meta(decision_date="2022-01-01")
    provider.metadata[SECOND_HISTORY_ID] = _history_meta(
        opinion_id=SECOND_HISTORY_ID,
        title="Later 401 v. Secretary",
        citation="33 Vet.App. 2",
        cluster_id="1000",
        decision_date="2023-01-01",
    )
    check = _check().model_copy(update={"as_of_date": "2021-01-01"})

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    ledger = signal.audit_ledger
    assert signal.status == "valid"
    assert ledger.as_of_date == "2021-01-01"
    assert ledger.coverage_complete is True
    assert ledger.candidates_metadata_loaded == 2
    assert ledger.candidates_in_scope == 0
    assert ledger.candidates_reviewed == 0
    assert ledger.candidates_post_as_of == 2
    assert ledger.candidates_undated_as_of == 0
    assert ledger.gaps == []


def test_source_audit_ledger_marks_undated_as_of_incomplete():
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID])
    provider.metadata[HISTORY_ID] = _history_meta(decision_date="")
    check = _check().model_copy(update={"as_of_date": "2021-01-01"})

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    ledger = signal.audit_ledger
    assert signal.status == "unverified"
    assert ledger.coverage_complete is False
    assert ledger.candidates_metadata_loaded == 1
    assert ledger.candidates_in_scope == 0
    assert ledger.candidates_post_as_of == 0
    assert ledger.candidates_undated_as_of == 1
    assert "source_as_of_undated" in [gap.code for gap in ledger.gaps]


def test_source_audit_ledger_marks_page_cap_truncation():
    provider = _history_provider(
        text="The court discussed Jones v. McDonough, 31 Vet.App. 456."
    )
    page = _page([HISTORY_ID])
    page["count"] = 2
    page["next"] = "https://www.courtlistener.com/api/rest/v4/opinions/201/citing/?cursor=next"
    provider.opinion_pages[(SOURCE_ID, None)] = page
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    ledger = check.source_validity[0].audit_ledger
    assert ledger.forward_citations_reported == 2
    assert ledger.citing_ids_collected == 1
    assert ledger.page_limit_reached is True
    assert ledger.candidate_pool_limit_reached is False
    assert ledger.coverage_complete is False
    gap_codes = [gap.code for gap in ledger.gaps]
    assert "page_cap" in gap_codes
    assert "count_mismatch" in gap_codes
    assert len(ledger.opinion_audits) == 1
    opinion = ledger.opinion_audits[0]
    assert opinion.page_limit_reached is True
    assert opinion.candidate_pool_limit_reached is False
    assert opinion.count_mismatch is True
    assert "page_cap" in opinion.gap_codes
    assert "count_mismatch" in opinion.gap_codes
    assert ledger.opinion_audit_bottlenecks() == {
        str(SOURCE_ID): ["page_cap", "count_mismatch"]
    }


def test_source_audit_ledger_marks_candidate_pool_truncation():
    provider = _provider()
    ids = list(range(701, 714))
    provider.opinion_pages[(SOURCE_ID, None)] = _page(ids)
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    ledger = check.source_validity[0].audit_ledger
    assert ledger.forward_citations_reported == 13
    assert ledger.citing_ids_collected == 12
    assert ledger.page_limit_reached is False
    assert ledger.candidate_pool_limit_reached is True
    assert ledger.coverage_complete is False
    gap_codes = [gap.code for gap in ledger.gaps]
    assert "count_mismatch" in gap_codes
    assert "source_candidate_pool_truncated" in gap_codes
    assert "page_cap" not in gap_codes
    opinion = ledger.opinion_audits[0]
    assert opinion.candidate_pool_limit_reached is True
    assert opinion.candidate_pool_limit_reason == "row"
    assert opinion.complete is False
    assert opinion.truncated is True
    assert "source_candidate_pool_truncated" in opinion.gap_codes
    assert "page_cap" not in opinion.gap_codes
    assert ledger.opinion_audit_bottlenecks() == {
        str(SOURCE_ID): [
            "count_mismatch",
            "source_candidate_pool_truncated",
            "candidate_pool_row",
        ]
    }


def test_source_audit_ledger_does_not_flag_duplicate_row_as_pool_truncation():
    provider = _provider()
    ids = list(range(701, 713))
    duplicate_page = _page([*ids, ids[0]])
    duplicate_page["count"] = len(ids)
    provider.opinion_pages[(SOURCE_ID, None)] = duplicate_page
    for position, opinion_id in enumerate(ids):
        provider.metadata[opinion_id] = _history_meta(
            opinion_id=opinion_id,
            title=f"Later {opinion_id} v. Secretary",
            citation=f"{opinion_id} Vet.App. 1",
            cluster_id=str(opinion_id + 1000),
            decision_date=(
                "2020-01-01" if position < 3 else "2022-01-01"
            ),
        )
        if position < 3:
            provider.texts[opinion_id] = (
                "The court discussed Jones v. McDonough, 31 Vet.App. 456."
            )
    check = _check().model_copy(update={"as_of_date": "2021-01-01"})

    _validate_source_validity(provider, check, _SourceValidationCache())

    ledger = check.source_validity[0].audit_ledger
    assert ledger.forward_citations_reported == len(ids)
    assert ledger.citing_ids_collected == len(ids)
    assert ledger.page_limit_reached is False
    assert ledger.candidate_pool_limit_reached is False
    assert ledger.coverage_complete is True
    assert ledger.candidates_metadata_loaded == len(ids)
    assert ledger.candidates_in_scope == 3
    assert ledger.candidates_post_as_of == len(ids) - 3
    assert ledger.candidates_reviewed == 3
    assert ledger.gaps == []
    opinion = ledger.opinion_audits[0]
    assert opinion.duplicate_rows_ignored == 1
    assert opinion.invalid_rows_ignored == 0
    assert opinion.candidate_pool_limit_reached is False
    assert opinion.complete is True
    assert opinion.truncated is False
    assert opinion.gap_codes == []
    assert ledger.opinion_audit_bottlenecks() == {}


def test_source_audit_ledger_separates_multi_opinion_reported_and_unique_counts():
    provider = _provider()
    source_metadata = _source_meta()
    source_metadata["opinion_ids"] = [SOURCE_ID, 202]
    provider.metadata[SOURCE_ID] = source_metadata
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID, 501])
    provider.opinion_pages[(202, None)] = _page([501, SECOND_HISTORY_ID])
    for opinion_id, title in (
        (HISTORY_ID, "Later v. Secretary"),
        (501, "Later 501 v. Secretary"),
        (SECOND_HISTORY_ID, "Later 401 v. Secretary"),
    ):
        provider.metadata[opinion_id] = _history_meta(
            opinion_id=opinion_id,
            title=title,
            citation=f"{opinion_id} Vet.App. 1",
            cluster_id=str(opinion_id + 1000),
            decision_date="2020-06-01",
        )
        provider.texts[opinion_id] = (
            "The court discussed Jones v. McDonough, 31 Vet.App. 456."
        )
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    ledger = signal.audit_ledger
    assert signal.status == "valid"
    assert ledger.cluster_opinion_count == 2
    assert ledger.forward_citations_reported == 4
    assert ledger.citing_ids_collected == 3
    assert ledger.candidates_metadata_loaded == 3
    assert ledger.candidates_in_scope == 3
    assert ledger.candidates_reviewed == 3
    assert ledger.candidates_without_invalidating_finding == 3
    assert ledger.coverage_complete is True
    assert len(ledger.opinion_audits) == 2
    first, second = ledger.opinion_audits
    assert first.opinion_id == str(SOURCE_ID)
    assert second.opinion_id == "202"
    assert first.opinion_index == 0
    assert second.opinion_index == 1
    assert first.candidate_allocation == 6
    assert second.candidate_allocation == 6
    assert first.citing_ids_collected == 2
    assert first.citing_ids_new_to_cluster == 2
    assert first.citing_ids_duplicate_across_cluster == 0
    assert second.citing_ids_collected == 2
    assert second.citing_ids_new_to_cluster == 1
    assert second.citing_ids_duplicate_across_cluster == 1
    assert sum(row.citing_ids_new_to_cluster for row in ledger.opinion_audits) == 3
    assert sum(row.forward_citations_reported for row in ledger.opinion_audits) == 4
    assert sum(row.pages_fetched for row in ledger.opinion_audits) == 2
    assert ledger.pages_fetched == 2
    assert ledger.opinion_audit_bottlenecks() == {}
    _assert_audit_vocabulary(signal)


def test_source_audit_ledger_records_cursor_candidate_pool_truncation(
    monkeypatch,
):
    monkeypatch.setattr(
        "va_legal_agent.citator._SOURCE_HISTORY_MAX_PAGES", 2
    )
    provider = _provider()
    ids = list(range(701, 713))
    page = _page(ids)
    page["count"] = len(ids)
    page["next"] = (
        "https://www.courtlistener.com/api/rest/v4/opinions/201/citing/"
        "?cursor=next"
    )
    provider.opinion_pages[(SOURCE_ID, None)] = page
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    ledger = check.source_validity[0].audit_ledger
    opinion = ledger.opinion_audits[0]
    assert opinion.page_limit_reached is False
    assert opinion.candidate_pool_limit_reached is True
    assert opinion.candidate_pool_limit_reason == "cursor"
    assert opinion.complete is False
    assert opinion.truncated is True
    assert opinion.gap_codes == ["source_candidate_pool_truncated"]
    assert ledger.candidate_pool_limit_reached is True
    assert ledger.opinion_audit_bottlenecks() == {
        str(SOURCE_ID): [
            "source_candidate_pool_truncated",
            "candidate_pool_cursor",
        ]
    }


def test_source_audit_ledger_prefers_row_cap_when_cursor_also_remains(
    monkeypatch,
):
    monkeypatch.setattr(
        "va_legal_agent.citator._SOURCE_HISTORY_MAX_PAGES", 2
    )
    provider = _provider()
    ids = list(range(701, 714))
    page = _page(ids)
    page["count"] = len(ids)
    page["next"] = (
        "https://www.courtlistener.com/api/rest/v4/opinions/201/citing/"
        "?cursor=next"
    )
    provider.opinion_pages[(SOURCE_ID, None)] = page
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    ledger = check.source_validity[0].audit_ledger
    opinion = ledger.opinion_audits[0]
    assert opinion.candidate_pool_limit_reached is True
    assert opinion.candidate_pool_limit_reason == "row"
    assert opinion.page_limit_reached is False
    assert opinion.complete is False
    assert opinion.truncated is True
    assert "source_candidate_pool_truncated" in opinion.gap_codes
    assert "count_mismatch" in opinion.gap_codes
    assert ledger.opinion_audit_bottlenecks() == {
        str(SOURCE_ID): [
            "count_mismatch",
            "source_candidate_pool_truncated",
            "candidate_pool_row",
        ]
    }


def test_source_audit_ledger_marks_reported_count_unknown_when_omitted():
    provider = _provider()
    page = _page([])
    page.pop("count")
    provider.opinion_pages[(SOURCE_ID, None)] = page
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    ledger = check.source_validity[0].audit_ledger
    opinion = ledger.opinion_audits[0]
    assert opinion.fetch_attempted is True
    assert opinion.reported_count_known is False
    assert opinion.forward_citations_reported == 0
    assert opinion.pages_fetched == 1
    assert opinion.complete is True
    assert opinion.gap_codes == []
    assert ledger.opinion_audit_bottlenecks() == {
        str(SOURCE_ID): ["reported_count_unknown"]
    }


def test_source_audit_ledger_preserves_underreported_count():
    provider = _provider()
    page = _page([HISTORY_ID])
    page["count"] = 0
    provider.opinion_pages[(SOURCE_ID, None)] = page
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    ledger = check.source_validity[0].audit_ledger
    opinion = ledger.opinion_audits[0]
    assert ledger.forward_citations_reported == 0
    assert ledger.citing_ids_collected == 1
    assert ledger.coverage_complete is False
    assert opinion.reported_count_known is True
    assert opinion.reported_count_malformed is False
    assert opinion.forward_citations_reported == 0
    assert opinion.citing_ids_collected == 1
    assert opinion.count_mismatch is True
    assert opinion.complete is False
    assert opinion.truncated is False
    assert opinion.gap_codes == ["count_mismatch"]
    assert ledger.opinion_audit_bottlenecks() == {
        str(SOURCE_ID): ["count_mismatch"]
    }


def test_source_audit_ledger_flags_malformed_count():
    provider = _provider()
    page = _page([HISTORY_ID])
    page["count"] = "not-a-count"
    provider.opinion_pages[(SOURCE_ID, None)] = page
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    ledger = check.source_validity[0].audit_ledger
    opinion = ledger.opinion_audits[0]
    assert ledger.forward_citations_reported == 0
    assert ledger.citing_ids_collected == 1
    assert ledger.coverage_complete is False
    assert opinion.reported_count_known is False
    assert opinion.reported_count_malformed is True
    assert opinion.forward_citations_reported == 0
    assert opinion.citing_ids_collected == 1
    assert opinion.count_mismatch is False
    assert opinion.complete is False
    assert opinion.truncated is False
    assert opinion.gap_codes == ["count_malformed"]
    assert ledger.opinion_audit_bottlenecks() == {
        str(SOURCE_ID): ["count_malformed"]
    }


def test_source_opinion_audit_rejects_unknown_limit_reason():
    with pytest.raises(ValueError, match="candidate_pool_limit_reason"):
        SourceOpinionAudit(
            opinion_id="201",
            candidate_pool_limit_reason="page",
        )


def test_source_audit_ledger_records_invalid_and_duplicate_rows_per_opinion():
    provider = _provider()
    ids = list(range(701, 713))
    page = _page(ids)
    page["results"] = [
        *page["results"],
        {
            "citing_opinion": (
                "https://www.courtlistener.com/api/rest/v4/"
                f"opinions/{ids[0]}/"
            )
        },
        {"citing_opinion": "not-an-opinion-id"},
        "not-a-row",
    ]
    page["count"] = len(ids)
    provider.opinion_pages[(SOURCE_ID, None)] = page
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    ledger = check.source_validity[0].audit_ledger
    opinion = ledger.opinion_audits[0]
    assert opinion.duplicate_rows_ignored == 1
    assert opinion.invalid_rows_ignored == 2
    assert opinion.complete is True
    assert opinion.truncated is False
    assert opinion.candidate_pool_limit_reached is False
    assert opinion.gap_codes == []
    assert ledger.citing_ids_collected == len(ids)
    assert ledger.opinion_audit_bottlenecks() == {
        str(SOURCE_ID): ["malformed_rows"]
    }


def test_source_audit_ledger_records_forward_fetch_failure_per_opinion():
    provider = _provider()
    provider.edge_errors[(SOURCE_ID, None)] = SearchError("boom")
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    ledger = check.source_validity[0].audit_ledger
    opinion = ledger.opinion_audits[0]
    assert opinion.candidate_allocation == 12
    assert opinion.fetch_attempted is True
    assert opinion.reported_count_known is False
    assert opinion.pages_fetched == 0
    assert opinion.complete is False
    assert opinion.truncated is False
    assert opinion.count_mismatch is False
    assert opinion.gap_codes == ["forward_fetch_failed"]
    assert "forward_fetch_failed" in [gap.code for gap in ledger.gaps]
    assert ledger.opinion_audit_bottlenecks() == {
        str(SOURCE_ID): ["forward_fetch_failed"]
    }


@pytest.mark.parametrize(
    ("error", "expected_kind", "expected_retryable"),
    [
        (SearchError("429 Too Many Requests"), "rate_limit", True),
        (SearchError("401 Unauthorized"), "authentication", False),
        (SearchError("request timed out"), "timeout", True),
        (SearchError("invalid JSON response"), "malformed_response", False),
        (SearchError("boom"), "other", False),
        (
            SearchError("opaque provider error", kind="timeout", retryable=True),
            "timeout",
            True,
        ),
    ],
)
def test_source_opinion_fetch_failure_records_stable_kind_and_retryability(
    error: SearchError, expected_kind: str, expected_retryable: bool
) -> None:
    provider = _provider()
    provider.edge_errors[(SOURCE_ID, None)] = error
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    opinion = check.source_validity[0].audit_ledger.opinion_audits[0]
    assert opinion.fetch_error_kind == expected_kind
    assert opinion.fetch_error_kind in SOURCE_OPINION_FETCH_ERROR_KINDS
    assert opinion.fetch_retryable is expected_retryable
    assert opinion.gap_codes == ["forward_fetch_failed"]


def test_source_opinion_fetch_success_has_no_fetch_error_signal():
    provider = _provider()
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    opinion = check.source_validity[0].audit_ledger.opinion_audits[0]
    assert opinion.fetch_error_kind == ""
    assert opinion.fetch_retryable is False


def test_source_opinion_audit_rejects_unknown_fetch_error_kind():
    with pytest.raises(ValueError, match="fetch_error_kind"):
        SourceOpinionAudit(opinion_id="201", fetch_error_kind="server_error")


def test_classify_search_error_uses_structured_kind_and_http_status():
    assert classify_search_error(
        SearchError("opaque throttling", kind="rate_limit")
    ) == ("rate_limit", True)
    assert classify_search_error(
        SearchError("opaque authentication", kind="authentication")
    ) == ("authentication", False)

    response = type("Response", (), {"status_code": 429})()
    status_error = SearchError("opaque provider failure")
    status_error.response = response  # type: ignore[attr-defined]
    assert classify_search_error(status_error) == ("rate_limit", True)

    timeout_response = type("Response", (), {"status_code": 408})()
    timeout_error = SearchError("opaque provider failure")
    timeout_error.response = timeout_response  # type: ignore[attr-defined]
    assert classify_search_error(timeout_error) == ("timeout", True)
    assert classify_search_error(SearchError("opaque", retryable=True)) == (
        "other",
        True,
    )


def test_source_opinion_fetch_malformed_response_is_not_retryable():
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = []  # type: ignore[assignment]
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    opinion = check.source_validity[0].audit_ledger.opinion_audits[0]
    assert opinion.fetch_attempted is True
    assert opinion.fetch_error_kind == "malformed_response"
    assert opinion.fetch_retryable is False
    assert opinion.gap_codes == ["forward_fetch_failed"]


def test_source_audit_ledger_records_zero_allocation_for_extra_opinions():
    provider = _provider()
    opinion_ids = list(range(SOURCE_ID, SOURCE_ID + 13))
    source_metadata = _source_meta()
    source_metadata["opinion_ids"] = opinion_ids
    provider.metadata[SOURCE_ID] = source_metadata
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    ledger = check.source_validity[0].audit_ledger
    assert ledger.cluster_opinion_count == 13
    assert len(ledger.opinion_audits) == 13
    assert [row.opinion_index for row in ledger.opinion_audits] == list(range(13))
    assert [row.opinion_id for row in ledger.opinion_audits] == [
        str(opinion_id) for opinion_id in opinion_ids
    ]
    assert all(row.candidate_allocation == 1 for row in ledger.opinion_audits[:12])
    last = ledger.opinion_audits[-1]
    assert last.candidate_allocation == 0
    assert last.fetch_attempted is False
    assert last.complete is False
    assert last.truncated is True
    assert last.candidate_pool_limit_reached is True
    assert last.candidate_pool_limit_reason == "allocation"
    assert last.gap_codes == ["source_candidate_pool_truncated"]
    assert ledger.candidate_pool_limit_reached is True
    assert ledger.coverage_complete is False
    assert ledger.opinion_audit_bottlenecks() == {
        last.opinion_id: [
            "source_candidate_pool_truncated",
            "zero_allocation",
        ]
    }


def _opinion_audit_row(opinion_id: str, **updates) -> SourceOpinionAudit:
    values = {
        "candidate_allocation": 1,
        "fetch_attempted": True,
        "reported_count_known": True,
        "pages_fetched": 1,
        "complete": True,
    }
    values.update(updates)
    return SourceOpinionAudit(opinion_id=opinion_id, **values)


def test_source_audit_ledger_derives_minimum_bottleneck_summary():
    rows = [
        _opinion_audit_row("200"),
        _opinion_audit_row(
            "201",
            candidate_allocation=0,
            fetch_attempted=False,
            pages_fetched=0,
            complete=False,
            truncated=True,
            candidate_pool_limit_reached=True,
            candidate_pool_limit_reason="allocation",
            gap_codes=["source_candidate_pool_truncated"],
        ),
        _opinion_audit_row(
            "202",
            complete=False,
            truncated=True,
            candidate_pool_limit_reached=True,
            candidate_pool_limit_reason="row",
            gap_codes=["source_candidate_pool_truncated"],
        ),
        _opinion_audit_row(
            "203",
            complete=False,
            truncated=True,
            candidate_pool_limit_reached=True,
            candidate_pool_limit_reason="cursor",
            gap_codes=["source_candidate_pool_truncated"],
        ),
        _opinion_audit_row("204", reported_count_known=False),
        _opinion_audit_row("205", invalid_rows_ignored=2),
        _opinion_audit_row("206", duplicate_rows_ignored=1),
        _opinion_audit_row(
            "207",
            reported_count_known=False,
            pages_fetched=0,
            complete=False,
            gap_codes=["forward_fetch_failed"],
        ),
    ]

    bottlenecks = SourceAuditLedger(
        cluster_opinion_count=len(rows), opinion_audits=rows
    ).opinion_audit_bottlenecks()

    assert list(bottlenecks) == ["201", "202", "203", "204", "205", "207"]
    assert bottlenecks == {
        "201": ["source_candidate_pool_truncated", "zero_allocation"],
        "202": ["source_candidate_pool_truncated", "candidate_pool_row"],
        "203": ["source_candidate_pool_truncated", "candidate_pool_cursor"],
        "204": ["reported_count_unknown"],
        "205": ["malformed_rows"],
        "207": ["forward_fetch_failed"],
    }


def test_source_audit_ledger_bottleneck_summary_rejects_invalid_opinion_ids():
    duplicate = SourceAuditLedger(
        opinion_audits=[
            _opinion_audit_row("201"),
            _opinion_audit_row(
                "201",
                complete=False,
                gap_codes=["forward_fetch_failed"],
            ),
        ]
    )
    with pytest.raises(ValueError, match="duplicate opinion_id"):
        duplicate.opinion_audit_bottlenecks()

    empty = SourceAuditLedger(opinion_audits=[_opinion_audit_row("")])
    with pytest.raises(ValueError, match="non-empty opinion_id"):
        empty.opinion_audit_bottlenecks()


def test_opinion_bottleneck_reasons_exhaustive_state_matrix():
    gap_cases = (
        (),
        ("page_cap",),
        ("page_cap", "count_mismatch"),
        ("page_cap", "page_cap"),
        ("source_candidate_pool_truncated",),
    )
    for gaps, allocation, limit_reason, pages, known in product(
        gap_cases,
        (0, 1, 2),
        ("", "row", "cursor", "allocation"),
        (0, 1),
        (False, True),
    ):
        row = SourceOpinionAudit(
            opinion_id="201",
            candidate_allocation=allocation,
            fetch_attempted=True,
            reported_count_known=known,
            pages_fetched=pages,
            # These flags conflict with several matrix states; the pure
            # mapping must rely only on gaps, allocation, pages, count, and
            # malformed-row counts.
            complete=False,
            truncated=True,
            page_limit_reached=True,
            candidate_pool_limit_reached=True,
            candidate_pool_limit_reason=limit_reason,
            count_mismatch=True,
            invalid_rows_ignored=2,
            duplicate_rows_ignored=1,
            gap_codes=list(gaps),
        )
        expected = list(dict.fromkeys(gaps))
        if allocation == 0:
            expected.append("zero_allocation")
        elif limit_reason in ("row", "cursor"):
            expected.append(f"candidate_pool_{limit_reason}")
        if pages and not known:
            expected.append("reported_count_unknown")
        expected.append("malformed_rows")
        expected = list(dict.fromkeys(expected))

        actual = _opinion_bottleneck_reasons(row)

        assert actual == expected, (
            gaps,
            allocation,
            limit_reason,
            pages,
            known,
            actual,
            expected,
        )


def test_source_audit_ledger_serializes_derived_bottlenecks_without_storage():
    ledger = SourceAuditLedger(
        opinion_audits=[
            _opinion_audit_row("200"),
            _opinion_audit_row(
                "201",
                candidate_allocation=0,
                fetch_attempted=False,
                pages_fetched=0,
                complete=False,
                truncated=True,
                candidate_pool_limit_reached=True,
                candidate_pool_limit_reason="allocation",
                gap_codes=["source_candidate_pool_truncated"],
            ),
            _opinion_audit_row("202", reported_count_known=False),
            _opinion_audit_row("205", invalid_rows_ignored=1),
        ]
    )

    expected = {
        "201": ["source_candidate_pool_truncated", "zero_allocation"],
        "202": ["reported_count_unknown"],
        "205": ["malformed_rows"],
    }
    payload = json.loads(ledger.model_dump_json())

    assert payload["opinion_bottlenecks"] == expected
    assert ledger.opinion_bottlenecks == expected
    assert "opinion_bottlenecks" not in ledger.__dict__
    assert "opinion_bottlenecks" not in SourceAuditLedger.model_fields

    restored = SourceAuditLedger.model_validate_json(ledger.model_dump_json())
    assert restored == ledger
    assert restored.opinion_bottlenecks == expected


def _generated_edge_cluster(seed: int):
    """Build a deterministic cluster with pagination, pool, and fetch states."""
    rng = random.Random(seed)
    opinion_count = rng.randint(3, 6)
    if seed % 4 == 0:
        # More opinions than the pool budget, so trailing rows get no slot.
        opinion_count = 13 + rng.randint(0, 4)
    opinion_ids = tuple(
        SOURCE_ID + 1000 + index for index in range(opinion_count)
    )
    target = _Target(
        _case(),
        opinion_ids[0],
        "",
        f"Generated cluster {seed}",
        f"generated-{seed}",
        opinion_ids,
    )
    provider = SourceProvider()
    for opinion_id in opinion_ids:
        page_ids = [rng.randint(700, 760) for _ in range(rng.randint(0, 12))]
        page = _page(page_ids)
        page["count"] = rng.randint(0, 14)
        if rng.random() < 0.4:
            page["next"] = (
                "https://www.courtlistener.com/api/rest/v4/"
                f"opinions/{opinion_id}/citing/?cursor=next"
            )
        provider.opinion_pages[(opinion_id, None)] = page
    # Exercise two distinct fetch states explicitly: a successful page that
    # omitted count, and a fetch that never reached a page.
    omitted_count = _page([])
    omitted_count.pop("count")
    provider.opinion_pages[(opinion_ids[0], None)] = omitted_count
    provider.edge_errors[(opinion_ids[1], None)] = SearchError("generated failure")
    return provider, target


@pytest.mark.parametrize("seed", range(16))
def test_generated_cluster_rows_preserve_audit_invariants(seed):
    provider, target = _generated_edge_cluster(seed)
    max_ids = 2 + seed % 8

    edge_fetch = _fetch_cluster_forward_edges(
        provider,
        target,
        max_pages=1 + seed % 3,
        page_size=max_ids,
        max_ids=max_ids,
    )

    rows = edge_fetch.opinion_audits
    assert any(row.page_limit_reached for row in rows) is (
        edge_fetch.page_limit_reached
    )
    assert any(row.candidate_pool_limit_reached for row in rows) is (
        edge_fetch.candidate_limit_reached
    )
    if seed % 4 == 0:
        assert any(row.candidate_allocation == 0 for row in rows)
    assert any(
        row.pages_fetched > 0 and not row.reported_count_known
        for row in rows
    )
    assert any("forward_fetch_failed" in row.gap_codes for row in rows)
    assert_source_opinion_audit_invariants(
        rows,
        cluster_opinion_count=len(target.opinion_ids),
        total_new_to_cluster=edge_fetch.unique_count,
        total_reported=edge_fetch.reported_count,
        total_pages=edge_fetch.pages,
    )


def test_source_adaptive_allocation_reallocates_unused_opinion_budget():
    def run(adaptive: bool):
        provider = _provider()
        opinion_ids = [SOURCE_ID, 202, 203]
        metadata = _source_meta()
        metadata["opinion_ids"] = opinion_ids
        provider.metadata[SOURCE_ID] = metadata
        provider.opinion_pages[(SOURCE_ID, None)] = _page([])
        provider.opinion_pages[(202, None)] = _page([9001])
        provider.opinion_pages[(203, None)] = _page(list(range(9100, 9120)))
        check = _check()

        _validate_source_validity(
            provider,
            check,
            _SourceValidationCache(),
            adaptive_allocation=adaptive,
        )
        return check.source_validity[0].audit_ledger.opinion_audits

    equal = run(False)
    adaptive = run(True)

    assert [row.candidate_allocation for row in equal] == [4, 4, 4]
    assert [row.citing_ids_collected for row in equal] == [0, 1, 4]
    assert [row.candidate_allocation for row in adaptive] == [4, 8, 11]
    assert [row.citing_ids_collected for row in adaptive] == [0, 1, 11]
    assert sum(row.citing_ids_new_to_cluster for row in adaptive) == 12
    assert sum(row.citing_ids_new_to_cluster for row in equal) == 5


def test_source_adaptive_allocation_does_not_change_target_fetch():
    def run(adaptive: bool):
        provider = _provider()
        target_metadata = provider.metadata[TARGET_ID]
        target_metadata["opinion_ids"] = [TARGET_ID, TARGET_ID + 1]
        provider.opinion_pages[(TARGET_ID, None)] = _page([SOURCE_ID])
        provider.opinion_pages[(TARGET_ID + 1, None)] = _page([])
        return check_case(
            _case(),
            provider,
            target_cluster_audit=True,
            source_validity=True,
            source_validity_mode="advisory",
            source_adaptive_allocation=adaptive,
        )

    default = run(False)
    adaptive = run(True)

    assert adaptive.target_cluster_audit == default.target_cluster_audit
    assert [
        row.candidate_allocation for row in adaptive.target_cluster_audit
    ] == [row.candidate_allocation for row in default.target_cluster_audit]


@settings(max_examples=40, deadline=None)
@given(
    cluster_size=st.integers(min_value=1, max_value=4),
    page_sizes=st.lists(
        st.integers(min_value=0, max_value=5),
        min_size=4,
        max_size=4,
    ),
)
def test_adaptive_source_allocation_preserves_target_equal_split(
    cluster_size: int,
    page_sizes: list[int],
) -> None:
    def run(adaptive: bool):
        provider = _provider()
        target_metadata = provider.metadata[TARGET_ID]
        opinion_ids = [TARGET_ID + index for index in range(cluster_size)]
        target_metadata["opinion_ids"] = opinion_ids
        for index, opinion_id in enumerate(opinion_ids):
            base = 8000 + index * 10
            provider.opinion_pages[(opinion_id, None)] = _page(
                list(range(base, base + page_sizes[index]))
            )
        return check_case(
            _case(),
            provider,
            target_cluster_audit=True,
            source_validity=True,
            source_validity_mode="advisory",
            source_adaptive_allocation=adaptive,
        )

    default = run(False)
    adaptive = run(True)

    assert adaptive.target_cluster_audit == default.target_cluster_audit
    pool = _citing_priority_pool_size(5, 1)
    share, remainder = divmod(pool, cluster_size)
    expected_allocations = [
        share + (1 if index < remainder else 0)
        for index in range(cluster_size)
    ]
    assert [
        row.candidate_allocation for row in default.target_cluster_audit
    ] == expected_allocations
    assert [
        row.candidate_allocation for row in adaptive.target_cluster_audit
    ] == expected_allocations


@settings(max_examples=30, deadline=None)
@given(
    failure_kind=st.sampled_from(
        ("rate_limit", "authentication", "timeout", "malformed_response", "other")
    ),
    failure_retryable=st.booleans(),
    malformed_count=st.integers(min_value=1, max_value=2),
    row_cap_rows=st.integers(min_value=4, max_value=6),
    page_cap_rows=st.integers(min_value=1, max_value=2),
)
def test_source_validation_mixed_ledger_round_trips_through_signal_json(
    failure_kind: str,
    failure_retryable: bool,
    malformed_count: int,
    row_cap_rows: int,
    page_cap_rows: int,
) -> None:
    provider = _provider()
    source_meta = _source_meta()
    source_meta["opinion_ids"] = [SOURCE_ID, 202, 203, 204]
    provider.metadata[SOURCE_ID] = source_meta
    provider.edge_errors[(SOURCE_ID, None)] = SearchError(
        "generated source failure",
        kind=failure_kind,
        retryable=failure_retryable,
    )
    malformed_page = _page([9101])
    malformed_page["results"] += [None] * malformed_count
    provider.opinion_pages[(202, None)] = malformed_page
    provider.opinion_pages[(203, None)] = _page(
        list(range(9200, 9200 + row_cap_rows))
    )
    page_cap_page = _page(list(range(9300, 9300 + page_cap_rows)))
    page_cap_page["next"] = (
        "https://www.courtlistener.com/api/rest/v4/"
        "opinions/204/citing/?cursor=next"
    )
    provider.opinion_pages[(204, None)] = page_cap_page

    check = _check()
    _validate_source_validity(provider, check, _SourceValidationCache())

    signal = check.source_validity[0]
    ledger = signal.audit_ledger
    failure_row, malformed_row, row_cap_row, page_cap_row = ledger.opinion_audits
    assert failure_row.fetch_error_kind == failure_kind
    assert failure_row.fetch_retryable is failure_retryable
    assert failure_row.gap_codes == ["forward_fetch_failed"]
    assert malformed_row.invalid_rows_ignored == malformed_count
    assert "malformed_rows" in ledger.opinion_bottlenecks["202"]
    assert row_cap_row.candidate_pool_limit_reached is True
    assert row_cap_row.candidate_pool_limit_reason == "row"
    assert page_cap_row.page_limit_reached is True
    assert "page_cap" in ledger.opinion_bottlenecks["204"]

    serialized = signal.model_dump_json()
    assert json.loads(serialized)["audit_ledger"][
        "opinion_bottlenecks"
    ] == ledger.opinion_bottlenecks

    restored = SourceValiditySignal.model_validate_json(serialized)
    assert restored == signal
    assert restored.audit_ledger.opinion_bottlenecks == (
        ledger.opinion_bottlenecks
    )


def test_source_adaptive_allocation_cache_is_mode_specific():
    provider = _provider()
    check = _check()
    cache = _SourceValidationCache()

    _validate_source_validity(provider, check, cache, adaptive_allocation=False)
    _validate_source_validity(provider, check, cache, adaptive_allocation=True)

    source_calls = [
        call for call in provider.edge_calls if call[0] == SOURCE_ID
    ]
    assert len(source_calls) == 2


def test_source_audit_ledger_accepts_legacy_json_without_opinion_rows():
    ledger = SourceAuditLedger.model_validate(
        {"assessed": True, "cluster_opinion_count": 2}
    )

    assert ledger.opinion_audits == []


def test_source_audit_ledger_round_trips_through_json():
    check = _check()
    _validate_source_validity(
        _invalid_source_provider(), check, _SourceValidationCache()
    )
    signal = check.source_validity[0]

    restored = SourceValiditySignal.model_validate_json(signal.model_dump_json())

    assert restored.audit_ledger == signal.audit_ledger
    assert restored == signal


def test_source_audit_gaps_deduplicate_exact_matches_in_order():
    first = CitatorCoverageGap(code="page_cap", detail="more pages remain")
    second = CitatorCoverageGap(code="count_mismatch", detail="2 reported")

    assert _dedupe_audit_gaps([first, first, second]) == [first, second]
