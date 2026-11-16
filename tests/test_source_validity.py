"""Tests for advisory source-validity validation."""

from __future__ import annotations

from va_legal_agent.adjudication import build_adjudication_queue
from va_legal_agent.authority_queue import apply_citator_findings
from va_legal_agent.citator import (
    _SourceValidationCache,
    _apply_source_validity_enforcement,
    _validate_source_validity,
    check_case,
)
from va_legal_agent.models import (
    CaseRecord,
    CitatorCheck,
    LegalAnalysis,
    NegativeTreatment,
    SourceValiditySignal,
    TreatmentTimelineEntry,
)
from va_legal_agent.search import SearchError
from va_legal_agent.topics import COURT_BVA, COURT_CAVC, COURT_UNKNOWN

TARGET_ID = 111
SOURCE_ID = 201
HISTORY_ID = 301


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
) -> dict[str, object]:
    return {
        "title": "Later v. Secretary",
        "citation": "32 Vet.App. 1",
        "citations": ["32 Vet.App. 1"],
        "cluster_id": "999",
        "cluster_resolved": True,
        "opinion_ids": [HISTORY_ID],
        "court": COURT_CAVC,
        "decision_date": "2022-01-01",
        "url": "https://www.courtlistener.com/opinion/301/",
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
        return self.texts.get(opinion_id, "")


def _provider() -> SourceProvider:
    provider = SourceProvider()
    provider.pages[None] = _page([SOURCE_ID])
    provider.opinion_pages[(SOURCE_ID, None)] = _page([])
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


def test_advisory_missing_status_is_unverified():
    provider = _provider()
    provider.metadata[SOURCE_ID] = _source_meta(status="")
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].precedence == "unverified"


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
    assert check.source_validity[0].withheld is False
    assert "only in part" in check.source_validity[0].reason


def test_advisory_incomplete_source_history_is_unverified():
    provider = _provider()
    provider.opinion_pages[(SOURCE_ID, None)] = _page([HISTORY_ID])

    check = check_case(
        _case(), provider, source_validity=True, source_validity_mode="advisory"
    )

    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].history_complete is False


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

    assert check.source_validity[0].status == "unverified"
    assert "gated source-history" in check.source_validity[0].reason


def test_advisory_metadata_failure_is_unverified():
    provider = _provider()
    provider.metadata_errors[SOURCE_ID] = SearchError("metadata unavailable")
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].withheld is True
    assert check.source_validity[0].reason == "source metadata unavailable"


def test_advisory_source_cluster_mismatch_is_unverified():
    provider = _provider()
    provider.metadata[SOURCE_ID] = _source_meta(cluster_id="999")
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "unverified"
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

    assert check.source_validity[0].status == "valid"
    assert check.source_validity[0].withheld is False
    assert check.source_validity[0].history_complete is True
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

    assert check.source_validity[0].status == "unverified"
    assert check.source_validity[0].withheld is True
    assert check.source_validity[0].history_complete is False


def test_advisory_unresolved_source_cluster_is_unverified():
    provider = _provider()
    metadata = _source_meta()
    metadata["cluster_resolved"] = False
    provider.metadata[SOURCE_ID] = metadata
    check = _check()

    _validate_source_validity(provider, check, _SourceValidationCache())

    assert check.source_validity[0].status == "unverified"
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
