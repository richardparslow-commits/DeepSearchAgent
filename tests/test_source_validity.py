"""Tests for advisory source-validity validation."""

from __future__ import annotations

from va_legal_agent.adjudication import build_adjudication_queue
from va_legal_agent.authority_queue import apply_citator_findings
from va_legal_agent.citator import (
    _SourceAudit,
    _SourceCandidateObservation,
    _SourceValidationCache,
    _dedupe_audit_gaps,
    _apply_source_validity_enforcement,
    _source_invalidator_check,
    _source_invalidator_checks,
    _validate_source_validity,
    check_case,
)
from va_legal_agent.models import (
    SOURCE_AUDIT_GAP_CODES,
    SOURCE_INVALIDATOR_REASON_CODES,
    SOURCE_INVALIDATOR_REVIEW_STATUSES,
    SOURCE_VALIDITY_REASON_CODES,
    CaseRecord,
    CitatorCheck,
    CitatorCoverageGap,
    LegalAnalysis,
    NegativeTreatment,
    SourceInvalidatorCheck,
    SourceValiditySignal,
    TreatmentTimelineEntry,
)
from va_legal_agent.search import SearchError
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
        self.metadata_calls: list[int] = []
        self.text_calls: list[int] = []
        self.edge_calls: list[tuple[int, int, str | None]] = []

    def citing_edges(
        self, opinion_id: int, page_size: int = 100, cursor=None
    ):
        self.edge_calls.append((opinion_id, page_size, cursor))
        key = (opinion_id, cursor)
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
    _assert_audit_vocabulary(signal)


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
