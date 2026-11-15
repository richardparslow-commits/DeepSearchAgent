"""Tests for the prioritized authority queue."""

from __future__ import annotations

from va_legal_agent.authority_queue import (
    apply_citator_findings,
    build_authority_queue,
    prioritize_authorities,
)
from va_legal_agent.models import (
    CaseRecord,
    CitatorCheck,
    NegativeTreatment,
    SourceValiditySignal,
)

CAVC = "Court of Appeals for Veterans Claims"
CAFC = "U.S. Court of Appeals for the Federal Circuit"
SUPREME = "U.S. Supreme Court"
BVA = "Board of Veterans' Appeals"


def _case(
    title: str,
    *,
    court: str = CAVC,
    weight: int = 2,
    rank: int = 0,
    relevance: int = 0,
    composite: float = 0.0,
    precedential: bool | None = True,
    superseded_by: str = "",
    opinion_id: str = "",
    citation: str = "",
    date: str = "",
    url: str = "",
) -> CaseRecord:
    return CaseRecord(
        title=title,
        court=court,
        authority_weight=weight,
        authority_rank=rank,
        relevance_score=relevance,
        composite_score=composite,
        precedential=precedential,
        superseded_by=superseded_by,
        courtlistener_opinion_id=opinion_id,
        citation=citation,
        decision_date=date,
        url=url or f"https://example.test/{title.replace(' ', '-').lower()}",
    )


def _finding(
    *,
    citing_case: str = "Later v. McDonough",
    treatment: str = "overruled",
    date: str = "2023-04-05",
    authoritative: bool = True,
    gate_reason: str = "",
    source_opinion_id: str = "",
    source_cluster_id: str = "",
    excerpt: str = "Later v. McDonough overruled the earlier decision.",
) -> NegativeTreatment:
    return NegativeTreatment(
        citing_case=citing_case,
        citing_citation="35 Vet.App. 10",
        citing_date=date,
        source_opinion_id=source_opinion_id,
        source_cluster_id=source_cluster_id,
        treatment=treatment,
        authoritative=authoritative,
        gate_reason=gate_reason,
        excerpt=excerpt,
    )


def _check(
    case_title: str,
    *,
    status: str = "unknown",
    opinion_id: str = "",
    citation: str = "",
    negative: list[NegativeTreatment] | None = None,
    gated: list[NegativeTreatment] | None = None,
    post_as_of: list[NegativeTreatment] | None = None,
    review_required: bool = False,
    review_reasons: list[str] | None = None,
    as_of_date: str = "",
    post_as_of_incomplete: bool = False,
    url: str = "",
    source_validity: list[SourceValiditySignal] | None = None,
    source_validity_enforced: bool = False,
) -> CitatorCheck:
    return CitatorCheck(
        target_case=case_title,
        target_url=url,
        target_citation=citation,
        target_citations=[citation] if citation else [],
        target_opinion_id=opinion_id,
        target_opinion_ids=[opinion_id] if opinion_id else [],
        status=status,
        negative_treatments=negative or [],
        gated_treatment_findings=gated or [],
        post_as_of_treatment_findings=post_as_of or [],
        review_required=review_required,
        review_reasons=review_reasons or [],
        as_of_date=as_of_date,
        post_as_of_incomplete=post_as_of_incomplete,
        source_validity=source_validity or [],
        source_validity_enforced=source_validity_enforced,
    )


def test_queue_orders_by_authority_tier_then_actionability():
    bva = _case("BVA Case", court=BVA, weight=1, citation="20 Vet.App. 1")
    cavc = _case(
        "CAVC Case",
        opinion_id="111",
        rank=5,
    )
    cafc = _case("CAFC Case", court=CAFC, weight=3, citation="900 F.3d 1")

    queue = prioritize_authorities([bva, cavc, cafc])

    assert [item.case.title for item in queue] == [
        "CAFC Case",
        "CAVC Case",
        "BVA Case",
    ]
    assert [item.entry.priority for item in queue] == [1, 2, 3]
    assert queue[0].entry.eligible is True


def test_queue_deprioritizes_unresolvable_superseded_and_nonprecedential_cases():
    superseded = _case(
        "Superseded",
        opinion_id="1",
        superseded_by="Newer v. McDonough",
    )
    nonprecedential = _case(
        "Nonprecedential",
        opinion_id="2",
        precedential=False,
    )
    unknown_status = _case("Unknown Status", opinion_id="3", precedential=None)
    eligible = _case("Eligible", opinion_id="4")
    unresolvable = _case("Unresolvable", precedential=True)

    queue = prioritize_authorities(
        [unresolvable, superseded, nonprecedential, unknown_status, eligible]
    )

    assert [item.case.title for item in queue] == [
        "Eligible",
        "Unknown Status",
        "Nonprecedential",
        "Superseded",
        "Unresolvable",
    ]
    assert "superseded in the retrieved corpus" in queue[3].entry.reasons[1]
    assert "non-precedential; deprioritized" in queue[2].entry.reasons
    assert any(
        "no citation or CourtListener opinion id" in reason
        for reason in queue[4].entry.reasons
    )


def test_queue_uses_rank_score_relevance_and_stable_input_order():
    ranked = _case("Ranked", rank=1, relevance=1)
    higher_relevance = _case("Higher Relevance", relevance=9)
    higher_composite = _case("Higher Composite", composite=3.0)
    first_stable = _case("First Stable")
    second_stable = _case("Second Stable")

    queue = prioritize_authorities(
        [second_stable, ranked, higher_relevance, higher_composite, first_stable]
    )

    assert [item.case.title for item in queue] == [
        "Ranked",
        "Higher Composite",
        "Higher Relevance",
        "Second Stable",
        "First Stable",
    ]
    assert "retrieved authority rank 1" in queue[0].entry.reasons
    assert "relevance score 9" in queue[2].entry.reasons


def test_queue_deduplicates_by_opinion_citation_and_title_url():
    by_opinion = _case("Opinion Copy A", opinion_id="7")
    by_opinion_duplicate = _case("Opinion Copy B", opinion_id="7")
    by_citation_date = _case(
        "Citation Copy A", citation="30 Vet.App. 1", date="2020-01-01"
    )
    by_citation_date_duplicate = _case(
        "Citation Copy B", citation="30 Vet.App. 1", date="2020-01-01"
    )
    by_citation_only = _case("Citation Only", citation="31 Vet.App. 2")
    by_citation_only_duplicate = _case(
        "Citation Only Duplicate", citation="31 Vet.App. 2"
    )
    by_title_url = _case("Title Copy", url="https://example.test/same")
    by_title_url_duplicate = _case("Title Copy", url="https://example.test/same")

    queue = prioritize_authorities(
        [
            by_opinion,
            by_opinion_duplicate,
            by_citation_date,
            by_citation_date_duplicate,
            by_citation_only,
            by_citation_only_duplicate,
            by_title_url,
            by_title_url_duplicate,
        ]
    )

    assert [item.case.title for item in queue] == [
        "Opinion Copy A",
        "Citation Copy A",
        "Citation Only",
        "Title Copy",
    ]


def test_queue_limit_and_empty_cases():
    cases = [_case("A", opinion_id="1"), _case("B", opinion_id="2")]

    assert prioritize_authorities(cases, limit=1)[0].case.title == "A"
    assert prioritize_authorities(cases, limit=0) == []
    assert prioritize_authorities(cases, limit=-2) == []
    assert prioritize_authorities([]) == []
    assert build_authority_queue(cases, limit=1)[0].case_title == "A"


def test_queue_entry_carries_case_metadata():
    case = _case(
        "Metadata Case",
        citation="30 Vet.App. 9",
        date="2020-01-01",
        rank=2,
        relevance=5,
        composite=2.5,
        precedential=None,
    )
    entry = build_authority_queue([case])[0]

    assert entry.case_title == "Metadata Case"
    assert entry.citation == "30 Vet.App. 9"
    assert entry.base_priority == 1
    assert entry.court == CAVC
    assert entry.authority_weight == 2
    assert entry.authority_rank == 2
    assert entry.relevance_score == 5
    assert entry.composite_score == 2.5
    assert entry.precedential is None
    assert entry.superseded_by == ""
    assert entry.eligible is True
    assert entry.reasons


def test_queue_does_not_mutate_cases_or_ranking_fields():
    case = _case("Stable", opinion_id="1", rank=4, composite=1.25)

    prioritize_authorities([case], limit=1)

    assert case.authority_rank == 4
    assert case.composite_score == 1.25
    assert case.title == "Stable"


def test_queue_infers_authority_weight_from_court_when_unset():
    entry = build_authority_queue(
        [_case("Inferred Tier", court=BVA, weight=0, opinion_id="9")]
    )[0]

    assert entry.authority_weight == 1
    assert "authority tier 1" in entry.reasons[0]


def test_source_identifiers_do_not_change_queue_behavior():
    undermined = _case("Undermined", opinion_id="1", rank=1)
    clean = _case("Clean", opinion_id="2", rank=9)
    plain = _finding()
    with_ids = _finding(
        source_opinion_id="201", source_cluster_id="888"
    )

    plain_queue = apply_citator_findings(
        [undermined, clean],
        [_check("Undermined", status="negative", opinion_id="1", negative=[plain])],
    )
    identified_queue = apply_citator_findings(
        [undermined, clean],
        [
            _check(
                "Undermined",
                status="negative",
                opinion_id="1",
                negative=[with_ids],
            )
        ],
    )

    def behavior(queue):
        return [
            (
                item.case.title,
                item.entry.priority,
                item.entry.base_priority,
                item.entry.citator_status,
                item.entry.citator_demoted,
                item.entry.citator_flagged,
                tuple(item.entry.citator_reasons),
            )
            for item in queue
        ]

    assert behavior(identified_queue) == behavior(plain_queue)


def test_enforcement_withheld_negative_status_cannot_demote_queue():
    undermined = _case("Undermined", opinion_id="1", rank=1)
    finding = _finding(source_opinion_id="201")
    signal = SourceValiditySignal(
        source_opinion_id="201",
        status="invalid",
        precedence="authoritative",
        withheld=False,
    )
    check = _check(
        "Undermined",
        status="negative",
        opinion_id="1",
        negative=[finding],
        source_validity=[signal],
        source_validity_enforced=True,
    )

    queue = apply_citator_findings([undermined], [check])

    assert queue[0].entry.citator_demoted is False
    assert queue[0].entry.citator_flagged is True


def test_enforcement_withheld_negative_finding_does_not_demote():
    undermined = _case("Undermined", opinion_id="1", rank=1)
    clean = _case("Clean", opinion_id="2", rank=9)
    finding = _finding(source_opinion_id="201")
    signal = SourceValiditySignal(
        source_opinion_id="201",
        status="precedence_defect",
        precedence="persuasive",
        withheld=True,
        reason="source court is lower than the target court",
    )
    check = _check(
        "Undermined",
        status="unknown",
        opinion_id="1",
        negative=[finding],
        source_validity=[signal],
        source_validity_enforced=True,
    )

    queue = apply_citator_findings([undermined, clean], [check])

    assert queue[0].entry.citator_demoted is False
    assert queue[0].entry.citator_flagged is True
    assert queue[0].entry.citator_findings == [finding]
    assert any(
        "citator enforcement withheld" in reason
        for reason in queue[0].entry.citator_reasons
    )


def test_enforcement_invalid_authoritative_finding_does_not_demote():
    undermined = _case("Undermined", opinion_id="1", rank=1)
    finding = _finding(source_opinion_id="201")
    signal = SourceValiditySignal(
        source_opinion_id="201",
        status="invalid",
        precedence="authoritative",
        withheld=False,
        reason="source was overruled",
    )
    check = _check(
        "Undermined",
        status="unknown",
        opinion_id="1",
        negative=[finding],
        source_validity=[signal],
        source_validity_enforced=True,
    )

    queue = apply_citator_findings([undermined], [check])

    assert queue[0].entry.citator_demoted is False
    assert queue[0].entry.citator_flagged is True
    assert any(
        "citator enforcement withheld" in reason
        for reason in queue[0].entry.citator_reasons
    )


def test_enforcement_valid_source_keeps_existing_demotion():
    undermined = _case("Undermined", opinion_id="1", rank=1)
    finding = _finding(source_opinion_id="201")
    signal = SourceValiditySignal(
        source_opinion_id="201",
        status="valid",
        precedence="authoritative",
        withheld=False,
        reason="clean source history",
    )
    check = _check(
        "Undermined",
        status="negative",
        opinion_id="1",
        negative=[finding],
        source_validity=[signal],
        source_validity_enforced=True,
    )

    queue = apply_citator_findings([undermined], [check])

    assert queue[0].entry.citator_demoted is True
    assert any(
        "citator demotion" in reason
        for reason in queue[0].entry.citator_reasons
    )


def test_advisory_withheld_source_does_not_filter_queue():
    undermined = _case("Undermined", opinion_id="1", rank=1)
    finding = _finding(source_opinion_id="201")
    signal = SourceValiditySignal(
        source_opinion_id="201",
        status="precedence_defect",
        precedence="persuasive",
        withheld=True,
    )
    check = _check(
        "Undermined",
        status="negative",
        opinion_id="1",
        negative=[finding],
        source_validity=[signal],
    )

    queue = apply_citator_findings([undermined], [check])

    assert queue[0].entry.citator_demoted is True
    assert not any(
        "enforcement withheld" in reason
        for reason in queue[0].entry.citator_reasons
    )


def test_enforcement_withholds_post_as_of_finding():
    undermined = _case("Undermined", opinion_id="1", rank=1)
    finding = _finding(source_opinion_id="201", date="2024-02-03")
    signal = SourceValiditySignal(
        source_opinion_id="201",
        status="invalid",
        precedence="authoritative",
        withheld=False,
    )
    check = _check(
        "Undermined",
        status="unknown",
        opinion_id="1",
        post_as_of=[finding],
        source_validity=[signal],
        source_validity_enforced=True,
    )

    queue = apply_citator_findings([undermined], [check])

    assert queue[0].entry.citator_demoted is False
    assert queue[0].entry.citator_flagged is True
    assert any(
        "citator enforcement withheld" in reason
        for reason in queue[0].entry.citator_reasons
    )


def test_citator_negative_treatment_demotes_with_provenance():
    undermined = _case("Undermined", opinion_id="1", rank=1)
    clean = _case("Clean", opinion_id="2", rank=9)
    finding = _finding()
    check = _check(
        "Undermined",
        status="negative",
        opinion_id="1",
        negative=[finding],
    )

    queue = apply_citator_findings([undermined, clean], [check])

    assert [item.case.title for item in queue] == ["Clean", "Undermined"]
    assert [item.entry.priority for item in queue] == [1, 2]
    assert queue[1].entry.base_priority == 1
    assert queue[1].entry.citator_status == "negative"
    assert queue[1].entry.citator_demoted is True
    assert queue[1].entry.citator_flagged is False
    assert queue[1].entry.citator_findings == [finding]
    assert any(
        "citator demotion: overruled by Later v. McDonough" in reason
        and finding.excerpt in reason
        for reason in queue[1].entry.citator_reasons
    )
    assert queue[0].entry.citator_status == ""
    assert queue[0].entry.citator_demoted is False


def test_citator_gated_finding_flags_without_demoting():
    flagged = _case("Flagged", opinion_id="1", rank=1)
    clean = _case("Clean", opinion_id="2", rank=9)
    finding = _finding(
        authoritative=False,
        gate_reason="finding is attributed to a separate opinion",
    )
    check = _check(
        "Flagged",
        opinion_id="1",
        gated=[finding],
    )

    queue = apply_citator_findings([flagged, clean], [check])

    assert [item.case.title for item in queue] == ["Flagged", "Clean"]
    assert queue[0].entry.priority == 1
    assert queue[0].entry.citator_demoted is False
    assert queue[0].entry.citator_flagged is True
    assert queue[0].entry.citator_findings == [finding]
    assert any(
        "citator gated finding" in reason
        and "gate: finding is attributed to a separate opinion" in reason
        for reason in queue[0].entry.citator_reasons
    )


def test_citator_review_gap_flags_without_reordering():
    needs_review = _case("Needs Review", opinion_id="1", rank=1)
    clean = _case("Clean", opinion_id="2", rank=9)
    check = _check(
        "Needs Review",
        opinion_id="1",
        review_required=True,
        review_reasons=["coverage gap [review_cap]: one citing opinion unread"],
    )

    queue = apply_citator_findings([needs_review, clean], [check])

    assert [item.case.title for item in queue] == ["Needs Review", "Clean"]
    assert queue[0].entry.citator_demoted is False
    assert queue[0].entry.citator_flagged is True
    assert queue[0].entry.citator_reasons == [
        "citator review flag: coverage gap [review_cap]: one citing opinion unread"
    ]


def test_citator_post_as_of_findings_demote_or_flag_without_changing_status():
    undermined = _case("Undermined", opinion_id="1", rank=1)
    clean = _case("Clean", opinion_id="2", rank=9)
    later = _finding(date="2024-02-03")
    later_gated = _finding(authoritative=False, date="2024-03-04")
    check = _check(
        "Undermined",
        status="good_law",
        opinion_id="1",
        post_as_of=[later, later_gated],
        as_of_date="2020-01-01",
        post_as_of_incomplete=True,
    )

    queue = apply_citator_findings([undermined, clean], [check])

    assert [item.case.title for item in queue] == ["Clean", "Undermined"]
    assert queue[1].entry.citator_status == "good_law"
    assert queue[1].entry.citator_demoted is True
    assert queue[1].entry.citator_flagged is True
    assert any(
        "citator post-as-of demotion" in reason
        for reason in queue[1].entry.citator_reasons
    )
    assert any(
        "citator post-as-of flag" in reason
        for reason in queue[1].entry.citator_reasons
    )
    assert "citator review flag: post-as-of review was incomplete" in (
        queue[1].entry.citator_reasons
    )


def test_citator_matches_by_url_for_cases_without_identifiers():
    undermined = _case(
        "Undermined",
        rank=1,
        url="https://example.test/shared-decision",
    )
    clean = _case("Clean", opinion_id="2", rank=9)
    check = _check(
        "Different Retrieved Title",
        status="negative",
        negative=[_finding()],
        url="https://example.test/shared-decision",
    )

    queue = apply_citator_findings([undermined, clean], [check])

    assert [item.case.title for item in queue] == ["Clean", "Undermined"]
    assert queue[1].entry.citator_demoted is True


def test_citator_matches_by_citation_when_opinion_id_is_absent():
    undermined = _case("Undermined", citation="30 Vet.App. 1", rank=1)
    clean = _case("Clean", opinion_id="2", rank=9)
    check = _check(
        "Undermined",
        citation="30 Vet.App. 1",
        status="negative",
        negative=[_finding()],
    )

    queue = apply_citator_findings([undermined, clean], [check])

    assert [item.case.title for item in queue] == ["Clean", "Undermined"]
    assert queue[1].entry.citator_demoted is True


def test_citator_annotations_do_not_feed_back_into_ranking_or_order():
    high = _case("High", opinion_id="1", rank=1, composite=4.0)
    low = _case(
        "Low",
        opinion_id="2",
        rank=9,
        composite=1.0,
        superseded_by="Newer v. VA",
    )
    check = _check(
        "High",
        status="negative",
        opinion_id="1",
        negative=[_finding()],
    )
    check.authority_priority = 1

    first = apply_citator_findings([high, low], [check])
    second = apply_citator_findings([high, low], [check])

    assert [item.entry for item in first] == [item.entry for item in second]
    assert [item.case.title for item in first] == ["Low", "High"]
    assert high.authority_rank == 1
    assert high.composite_score == 4.0
    assert high.superseded_by == ""
    assert low.superseded_by == "Newer v. VA"
    assert check.status == "negative"
    assert check.authority_priority == 1
