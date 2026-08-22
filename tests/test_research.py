"""Advanced research tests for case relevance scoring and legal impact interpretation."""

from va_legal_agent.agent import (
    _cosine_similarity,
    _semantic_similarity,
    score_case_relevance,
    summarize_case_impact,
)
from va_legal_agent.interpretation import build_interpretive_analysis
from va_legal_agent.models import CaseRecord

CAVC = "Court of Appeals for Veterans Claims"
CAFC = "U.S. Court of Appeals for the Federal Circuit"
BVA = "Board of Veterans' Appeals"


def _case(
    title: str = "Zzz",
    snippet: str = "",
    holding: str = "",
    impact: str = "",
    issue: str = "",
    court: str = CAFC,
) -> CaseRecord:
    return CaseRecord(
        title=title,
        court=court,
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
        snippet=snippet,
        holding=holding,
        impact=impact,
        issue=issue,
    )


# ---------------------------------------------------------------------------
# Case relevance scoring
# ---------------------------------------------------------------------------


def test_exact_issue_phrase_scores_two_points():
    case = _case(snippet="discusses service connection for tinnitus in detail")

    assert score_case_relevance(case, "service connection for tinnitus") == 10
    # +2 exact issue phrase, +2 keyword-family synonym, +2 global phrase bonus,
    # +4 semantic similarity (tinnitus, service, connection overlap weighted).


def test_synonym_match_scores_without_exact_phrase():
    case = _case(snippet="a medical nexus opinion is required", court=CAVC)

    # No literal "service connection" text, but the keyword family synonym fires.
    assert score_case_relevance(case, "service connection") == 3  # +2 synonym, +1 veterans court


def test_keyword_family_caps_at_single_contribution():
    one_synonym = _case(snippet="nexus opinion")
    many_synonyms = _case(snippet="nexus diagnosis compensation")

    assert score_case_relevance(one_synonym, "service connection") == 2
    assert score_case_relevance(many_synonyms, "service connection") == 4
    # Multiple synonyms from the same family still contribute only once for
    # the keyword-family path; semantic overlap adds 2 more for partial
    # term overlap (nexus, diagnosis, compensation vs weighted issue vector).


def test_multiple_issue_keywords_accumulate():
    case = _case(
        snippet=(
            "service connection compensation nexus diagnosis "
            "benefit of the doubt reasonable doubt doubtful"
        ),
        court=BVA,
    )

    # +2 per keyword family in the issue, +2 global phrase bonus, +1 veterans
    # court, +4 semantic similarity (full overlap across both keyword families).
    assert score_case_relevance(case, "service connection and benefit of the doubt") == 11


def test_global_bonus_applies_regardless_of_issue():
    case = _case(snippet="the board erred on service connection")

    # "service connection" is not in the issue, so no keyword points, but the
    # global phrase bonus still applies.
    assert score_case_relevance(case, "rating") == 2


def test_veterans_court_bonus_and_zero_floor():
    neutral_snippet = "procedural history"

    federal_circuit = _case(snippet=neutral_snippet, court=CAFC)
    cavc = _case(snippet=neutral_snippet, court=CAVC)

    assert score_case_relevance(federal_circuit, "tinnitus") == 0
    assert score_case_relevance(cavc, "tinnitus") == 1  # +1 for "Veterans" in court name


def test_score_is_case_insensitive():
    case = _case(snippet="Service Connection Veterans", court=CAVC)

    # +2 exact issue phrase, +2 keyword family, +1 veterans text, +2 global,
    # +1 court, +4 semantic similarity (service, connection overlap).
    assert score_case_relevance(case, "SERVICE CONNECTION") == 12


def test_title_and_issue_fields_contribute_to_text():
    titled = _case(title="Service connection for tinnitus", snippet="unrelated")
    assert score_case_relevance(titled, "tinnitus") == 8  # +2 issue match, +2 global, +4 semantic

    base = _case(snippet="unrelated procedural")
    with_issue_field = _case(snippet="unrelated procedural", issue="service connection")
    assert score_case_relevance(base, "service connection") == 0
    assert score_case_relevance(with_issue_field, "service connection") == 10


def test_impact_field_contributes_to_text():
    base = _case(snippet="unrelated procedural")
    with_impact = _case(snippet="unrelated procedural", impact="service connection required")

    assert score_case_relevance(base, "service connection") == 0
    # +2 exact issue phrase (via impact), +2 keyword family, +2 global phrase,
    # +2 semantic similarity (partial overlap, sim ≈ 0.49).
    assert score_case_relevance(with_impact, "service connection") == 8


def test_richer_text_strictly_outranks_sparser_text():
    sparse = _case(snippet="procedural posture")
    mid = _case(snippet="service connection")
    rich = _case(snippet="service connection veterans")

    scores = [score_case_relevance(case, "service connection") for case in (sparse, mid, rich)]

    # mid: +2 exact, +2 keyword, +2 global, +4 semantic = 10;
    # rich: +2 exact, +2 keyword, +1 veterans, +2 global, +4 semantic = 11.
    assert scores == [0, 10, 11]
    assert scores[0] < scores[1] < scores[2]


def test_full_signal_case_reaches_expected_score():
    case = _case(
        title="Smith",
        snippet="service connection compensation veterans",
        issue="service connection",
        court=CAVC,
    )

    # +2 exact (via issue), +2 keyword family, +1 veterans text, +2 global,
    # +1 veterans court, +4 semantic = 12
    assert score_case_relevance(case, "service connection") == 12

# Test case: paraphrased concept — a decision that discusses
# hearing loss but never uses the word tinnitus should still score
# above zero because the semantic similarity path picks up the
# weighted-term overlap (hearing, loss, service, connection).
def test_semantic_similarity_catches_paraphrased_concept():
    """Paraphrased concept gets a bonus when the issue has TOPICS synonym expansion.

    The issue ``"service connection"`` expands the weighted term vector with
    synonyms (compensation, nexus, diagnosis), so a case whose text shares
    those tokens gets a similarity bonus even when the exact issue phrase
    never appears."""
    case = _case(
        snippet="The veteran received a diagnosis and compensation for the nexus opinion.",
        court=CAVC,
    )
    score = score_case_relevance(case, "service connection")
    # +2 keyword family (compensation/nexus/diagnosis are synonyms),
    # +1 CAVC court, +2 semantic (sim ≈ 0.34 ≥ 0.3) = 5.
    assert score == 5, f"Expected 5, got {score}"


def test_semantic_similarity_auditory_deficit_paraphrase():
    """Tinnitus has no TOPICS entry, so no synonym expansion; only court point."""
    case = _case(snippet="The veteran suffers from an auditory deficit.", court=CAVC)
    score = score_case_relevance(case, "tinnitus")
    assert score == 1, f"Expected 1 (court only), got {score}"


def test_semantic_similarity_zero_for_unrelated_text():
    """Totally unrelated text must not generate semantic bonus."""
    case = _case(snippet="procedural history only", issue="")
    score = score_case_relevance(case, "tinnitus")
    assert score == 0


def test_cosine_similarity_empty_vectors():
    assert _cosine_similarity({}, {}) == 0.0
    assert _cosine_similarity({"a": 1.0}, {}) == 0.0
    assert _cosine_similarity({}, {"a": 1.0}) == 0.0


def test_cosine_similarity_zero_magnitudes():
    assert _cosine_similarity({"a": 0.0}, {"b": 0.0}) == 0.0


def test_semantic_similarity_empty_issue_yields_no_terms():
    assert _semantic_similarity("some case text here", "  ") == 0.0


def test_semantic_similarity_empty_case_text():
    assert _semantic_similarity("  ", "tinnitus") == 0.0


# ---------------------------------------------------------------------------
# Legal impact interpretation
# ---------------------------------------------------------------------------


def test_impact_lists_top_three_issue_tags_in_priority_order():
    case = _case(
        snippet=(
            "service connection benefit of the doubt reasons and bases "
            "evidence nexus rating"
        )
    )

    summary = summarize_case_impact(case)

    assert summary.startswith(
        "This ruling is relevant to service connection, benefit of the doubt, "
        "reasons and bases in VA compensation claims."
    )
    # Tags beyond the top three are not listed.
    assert "nexus" not in summary.split(".")[0]


def test_impact_orders_mid_priority_tags():
    case = _case(snippet="the nexus and rating depend on evidence")

    summary = summarize_case_impact(case)

    assert "relevant to evidence evaluation, nexus, rating in VA compensation claims" in summary
    assert "service connection" not in summary


def test_impact_falls_back_to_case_issue():
    case = _case(snippet="procedural posture only", issue="Hearing Loss")

    summary = summarize_case_impact(case)

    assert "relevant to hearing loss in VA compensation claims" in summary


def test_impact_falls_back_to_generic_phrase_when_issue_empty():
    case = _case(snippet="procedural posture only", issue="")

    summary = summarize_case_impact(case)

    assert "relevant to the factual and legal issue in VA compensation claims" in summary


def test_impact_includes_reviewability_boilerplate():
    summary = summarize_case_impact(_case(snippet="nexus"))

    assert "governing standards" in summary
    assert "reviewable on appeal" in summary


def test_impact_lowercases_case_issue():
    summary = summarize_case_impact(_case(snippet="procedural posture only", issue="TINNITUS"))

    assert "tinnitus" in summary
    assert "TINNITUS" not in summary


# ---------------------------------------------------------------------------
# Interpretive narrative construction
# ---------------------------------------------------------------------------


def test_template_narrative_limits_cases_and_principles(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Snippet triggers every principle pattern; six cases exceed both limits.
    snippet = (
        "benefit of the doubt reasons and bases nexus competent evidence "
        "lay evidence medical evidence presumption duty to assist"
    )
    cases = [_case(title=f"Case {i}", snippet=snippet) for i in range(6)]

    result = build_interpretive_analysis("service connection", "Compensation", cases)
    narrative = result.how_it_affects_va_claims

    # Principle scanning is capped at PRINCIPLE_SCAN_LIMIT cases.
    assert result.principle_findings[0].source_cases == [f"Case {i}" for i in range(5)]
    # The narrative names only INTERPRET_CASE_LIMIT cases.
    assert "Case 0, Case 1, Case 2" in narrative
    assert "Case 3" not in narrative
    # Only the first three principles (benefit of the doubt, reasons and bases,
    # nexus) are woven into the narrative; later ones stay out.
    assert "5107" in narrative
    assert "7104" in narrative
    assert "lay evidence may establish" not in narrative
    # Attribution strings are capped at three source cases.
    assert "(see: Case 0, Case 1, Case 2)" in result.likely_applicable_principles[0]
    assert "not legal advice" in narrative
