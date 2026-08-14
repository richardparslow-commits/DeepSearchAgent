from va_legal_agent.models import CaseRecord
from va_legal_agent.ranking import (
    WEIGHT_COMPLETENESS,
    WEIGHT_RECENCY,
    WEIGHT_RELEVANCE,
    completeness_score,
    rank_cases,
    recency_score,
    score_case,
)

CAVC = "Court of Appeals for Veterans Claims"
CAFC = "U.S. Court of Appeals for the Federal Circuit"


def make_case(
    title: str,
    court: str = CAVC,
    weight: int = 2,
    relevance: int = 0,
    date: str = "",
    citation: str = "",
    holding: str = "",
) -> CaseRecord:
    return CaseRecord(
        title=title,
        court=court,
        authority_weight=weight,
        relevance_score=relevance,
        decision_date=date,
        citation=citation,
        holding=holding,
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
    )


def test_rank_empty_returns_empty():
    assert rank_cases([]) == []


def test_authority_tier_dominates_within_tier_signals():
    # Perfect CAVC candidate vs a bare Federal Circuit result: the higher court still wins.
    cavc = make_case(
        "Perfect CAVC Case",
        weight=2,
        relevance=10,
        date="2025-01-01",
        citation="30 Vet.App. 1",
        holding="Holding text.",
    )
    cafc = make_case("Bare CAFC Case", court=CAFC, weight=3)

    ranked = rank_cases([cavc, cafc], current_year=2026)

    assert [case.title for case in ranked] == ["Bare CAFC Case", "Perfect CAVC Case"]


def test_within_tier_higher_relevance_wins():
    strong = make_case("Strong Relevance", relevance=8)
    weak = make_case("Weak Relevance", relevance=2)

    ranked = rank_cases([weak, strong])

    assert [case.title for case in ranked] == ["Strong Relevance", "Weak Relevance"]


def test_recency_breaks_relevance_tie_within_tier():
    newer = make_case("Newer Case", relevance=4, date="2022-05-01")
    older = make_case("Older Case", relevance=4, date="1995-05-01")

    ranked = rank_cases([older, newer], current_year=2026)

    assert [case.title for case in ranked] == ["Newer Case", "Older Case"]


def test_completeness_breaks_final_tie():
    enriched = make_case("Enriched Case", relevance=4, citation="30 Vet.App. 1", holding="Holding.")
    bare = make_case("Bare Case", relevance=4)

    ranked = rank_cases([bare, enriched])

    assert [case.title for case in ranked] == ["Enriched Case", "Bare Case"]


def test_unknown_recency_is_neutral_and_bounded():
    assert recency_score("") == 0.5
    assert recency_score("2026-01-01", current_year=2026) == 1.0
    assert recency_score("1985-06-01", current_year=2026) == 0.0
    assert recency_score("1900-01-01", current_year=2026) == 0.0  # clamped at floor
    # Unknown-date case outranks a very old dated case with equal relevance.
    unknown = make_case("Unknown Date", relevance=4)
    old = make_case("Very Old", relevance=4, date="1986-01-01")
    ranked = rank_cases([old, unknown], current_year=2026)
    assert [case.title for case in ranked] == ["Unknown Date", "Very Old"]


def test_relevance_normalized_by_batch_max():
    case = make_case("Half Relevance", relevance=5)

    tier_score, explanation = score_case(case, max_relevance=10)

    expected = WEIGHT_RELEVANCE * 0.5 + WEIGHT_RECENCY * 0.5 + WEIGHT_COMPLETENESS * 0.0
    assert tier_score == expected
    assert "relevance 0.50" in explanation

    zero_batch, _ = score_case(make_case("No Match", relevance=3), max_relevance=0)
    assert zero_batch == WEIGHT_RECENCY * 0.5  # relevance contributes nothing


def test_completeness_score_counts_populated_fields():
    assert completeness_score(make_case("None")) == 0.0
    assert completeness_score(make_case("Some", citation="30 Vet.App. 1", date="2020-01-01")) == 2 / 3
    assert completeness_score(
        make_case("All", citation="30 Vet.App. 1", date="2020-01-01", holding="Holding.")
    ) == 1.0


def test_composite_scores_and_ranks_are_consistent():
    cases = [
        make_case("SCOTUS Low Relevance", court="U.S. Supreme Court", weight=4, relevance=1),
        make_case("CAFC High Relevance", court=CAFC, weight=3, relevance=9, date="2024-06-01"),
        make_case("CAVC Medium", relevance=5, citation="30 Vet.App. 1"),
        make_case("BVA High Relevance", court="Board of Veterans' Appeals", weight=1, relevance=9),
    ]

    ranked = rank_cases(cases, current_year=2026)

    assert [case.title for case in ranked] == [
        "SCOTUS Low Relevance",
        "CAFC High Relevance",
        "CAVC Medium",
        "BVA High Relevance",
    ]
    assert [case.authority_rank for case in ranked] == [1, 2, 3, 4]
    assert all(
        ranked[i].composite_score >= ranked[i + 1].composite_score for i in range(len(ranked) - 1)
    )
    assert "authority tier 4" in ranked[0].ranking_explanation
    assert "tier score" in ranked[0].ranking_explanation


def test_rank_is_deterministic_for_exact_ties():
    ties = [make_case("Bravo"), make_case("Alpha")]

    first = [case.title for case in rank_cases(list(ties))]
    second = [case.title for case in rank_cases(list(ties))]

    assert first == second == ["Alpha", "Bravo"]  # title breaks exact ties