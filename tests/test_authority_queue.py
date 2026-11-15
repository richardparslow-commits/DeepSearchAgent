"""Tests for the prioritized authority queue."""

from __future__ import annotations

from va_legal_agent.authority_queue import (
    build_authority_queue,
    prioritize_authorities,
)
from va_legal_agent.models import CaseRecord

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
