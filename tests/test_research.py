"""Advanced research tests for case relevance scoring and legal impact interpretation."""

from va_legal_agent.agent import score_case_relevance, summarize_case_impact
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

    assert score_case_relevance(case, "service connection for tinnitus") == 6
    # +2 exact issue phrase, +2 keyword-family synonym, +2 global phrase bonus


def test_synonym_match_scores_without_exact_phrase():
    case = _case(snippet="a medical nexus opinion is required", court=CAVC)

    # No literal "service connection" text, but the keyword family synonym fires.
    assert score_case_relevance(case, "service connection") == 3  # +2 synonym, +1 veterans court


def test_keyword_family_caps_at_single_contribution():
    one_synonym = _case(snippet="nexus opinion")
    many_synonyms = _case(snippet="nexus diagnosis compensation")

    assert score_case_relevance(one_synonym, "service connection") == 2
    # Multiple synonyms from the same family still contribute only once.
    assert score_case_relevance(many_synonyms, "service connection") == 2


def test_multiple_issue_keywords_accumulate():
    case = _case(
        snippet=(
            "service connection compensation nexus diagnosis "
            "benefit of the doubt reasonable doubt doubtful"
        ),
        court=BVA,
    )

    # +2 per keyword family in the issue, +2 global phrase bonus, +1 veterans court.
    assert score_case_relevance(case, "service connection and benefit of the doubt") == 7


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

    # +2 exact issue phrase, +2 keyword family, +1 veterans text, +2 global, +1 court.
    assert score_case_relevance(case, "SERVICE CONNECTION") == 8


def test_title_and_issue_fields_contribute_to_text():
    titled = _case(title="Service connection for tinnitus", snippet="unrelated")
    assert score_case_relevance(titled, "tinnitus") == 4  # +2 issue match, +2 global phrase

    base = _case(snippet="unrelated procedural")
    with_issue_field = _case(snippet="unrelated procedural", issue="service connection")
    assert score_case_relevance(base, "service connection") == 0
    assert score_case_relevance(with_issue_field, "service connection") == 6


def test_impact_field_contributes_to_text():
    base = _case(snippet="unrelated procedural")
    with_impact = _case(snippet="unrelated procedural", impact="service connection required")

    assert score_case_relevance(base, "service connection") == 0
    # +2 exact issue phrase (via impact), +2 keyword family, +2 global phrase.
    assert score_case_relevance(with_impact, "service connection") == 6


def test_richer_text_strictly_outranks_sparser_text():
    sparse = _case(snippet="procedural posture")
    mid = _case(snippet="service connection")
    rich = _case(snippet="service connection veterans")

    scores = [score_case_relevance(case, "service connection") for case in (sparse, mid, rich)]

    # mid: +2 exact issue phrase, +2 keyword family, +2 global phrase;
    # rich additionally gets +1 for "veterans" in the text.
    assert scores == [0, 6, 7]
    assert scores[0] < scores[1] < scores[2]


def test_full_signal_case_reaches_expected_score():
    case = _case(
        title="Smith",
        snippet="service connection compensation veterans",
        issue="service connection",
        court=CAVC,
    )

    # Exact issue phrase (via issue field), keyword family, veterans text,
    # global phrase bonus, and veterans-court bonus all fire.
    assert score_case_relevance(case, "service connection") == 8

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
