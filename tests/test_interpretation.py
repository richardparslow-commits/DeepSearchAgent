"""Tests for the interpretive analysis layer (va_legal_agent.interpretation)."""

import pytest

from va_legal_agent.interpretation import (
    _COURT_ORDER,
    _build_element_library,
    _outcome_direction,
    build_interpretive_analysis,
    build_statute_outcome_matrix,
    detect_claim_elements,
    detect_contradictions,
    extract_principle_findings,
    uncovered_element_names,
)
from va_legal_agent.llm import ReasoningResult
from va_legal_agent.models import CaseRecord, Contradiction, LegalAnalysis
from va_legal_agent.topics import COURT_SUPREME, TOPICS


def _case(
    title: str,
    snippet: str = "",
    holding: str = "",
    outcome: str = "",
    statutes: list[str] | None = None,
    deep_summary: str = "",
    appellant_role: str = "veteran",
    court: str = "",
    precedential: bool | None = True,
) -> CaseRecord:
    # Contradiction fixtures below share a statute; the detector now also
    # requires a shared legal element, so give those fixtures a nexus snippet
    # unless they supply their own source text.
    if statutes and not snippet:
        snippet = "nexus"
    return CaseRecord(
        title=title,
        court=court or "Court of Appeals for Veterans Claims",
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
        snippet=snippet,
        holding=holding,
        outcome=outcome,
        statutes=statutes or [],
        deep_summary=deep_summary,
        appellant_role=appellant_role,
        precedential=precedential,
    )


def test_element_library_builds_wellformed_specs():
    # The library is built once at import time; calling the builder directly must
    # yield the same well-formed, topic-mirroring specs every time.
    specs = _build_element_library()

    assert [spec.name for spec in specs] == [topic.name for topic in TOPICS]
    assert all(spec.name for spec in specs)
    assert all(spec.phrases and spec.description and spec.guidance and spec.step for spec in specs)


def test_detect_claim_elements_from_issue_text():
    specs = detect_claim_elements("service connection for tinnitus and benefit of the doubt")
    names = [spec.name for spec in specs]
    assert "service connection" in names
    assert "benefit of the doubt" in names


def test_detect_claim_elements_returns_empty_for_bare_condition():
    # A bare condition or a non-service-connection benefit names no element
    # phrase; do not expand it into all nine Caluza-style elements.
    assert detect_claim_elements("tinnitus") == []
    assert detect_claim_elements("Aid and Attendance") == []


def test_detect_claim_elements_matches_all_topic_phrases_with_boundaries():
    service_connected = [spec.name for spec in detect_claim_elements("service-connected knee")]
    aggravated = [spec.name for spec in detect_claim_elements("aggravated knee")]

    assert "service connection" in service_connected
    assert "aggravation" in aggravated
    # "rating" must not match inside "operating" or "frustrating".
    assert detect_claim_elements("operating a vehicle") == []
    assert detect_claim_elements("a frustrating delay") == []


def test_rating_coverage_ignores_operating_and_frustrating():
    cases = [_case("Smith", snippet="operating a vehicle during a frustrating delay")]

    result = build_interpretive_analysis("rating", "Compensation", cases)

    assert result.detected_elements[0].name == "rating"
    assert result.detected_elements[0].covered_by == []
    assert result.coverage_score == 0.0


@pytest.mark.parametrize("score", [7.5, -2.0])
def test_legal_analysis_rejects_out_of_range_coverage_scores(score):
    with pytest.raises(ValueError):
        LegalAnalysis(
            issue="i",
            summary="s",
            how_it_affects_va_claims="h",
            coverage_score=score,
        )


def test_uncovered_element_names_flags_elements_without_coverage():
    cases = [_case("Smith v. Wilkie", snippet="service connection requires a nexus")]

    # service connection is covered; presumption is detected but uncovered.
    assert uncovered_element_names("service connection", cases) == ()
    assert uncovered_element_names(
        "service connection and presumption of exposure", cases
    ) == ("presumption",)


def test_uncovered_element_names_is_empty_for_bare_issue():
    # No detected elements means there is nothing to flag as uncovered. The
    # analysis reports an unclassified issue instead of inventing a library.
    assert uncovered_element_names("tinnitus", []) == ()
    assert uncovered_element_names("Aid and Attendance", []) == ()

    # Even a case with element-like text cannot create uncovered elements when
    # the issue itself detected none.
    assert uncovered_element_names(
        "tinnitus", [_case("Smith v. Wilkie", snippet="service connection requires a nexus")]
    ) == ()


def test_extract_principle_findings_attributes_source_cases():
    cases = [
        _case("Smith v. Wilkie", snippet="the benefit of the doubt rule applies"),
        _case("Jones v. McDonough", holding="The Board must provide reasons and bases."),
    ]

    findings = extract_principle_findings(cases)

    benefit = next(f for f in findings if "approximate balance" in f.principle)
    assert benefit.source_cases == ["Smith v. Wilkie"]
    assert "not limited to equipoise" in benefit.principle
    assert "relative equipoise" not in benefit.principle
    reasons = next(f for f in findings if "7104" in f.principle)
    assert reasons.source_cases == ["Jones v. McDonough"]


def test_principle_findings_use_jandreau_and_colvin_limits():
    case = _case("Smith v. Wilkie", snippet="lay evidence medical evidence")

    findings = extract_principle_findings([case])
    lay = next(f for f in findings if "circumstances Jandreau identifies" in f.principle)
    medical = next(f for f in findings if "medical expertise" in f.principle)

    assert "cannot substitute for medical evidence beyond lay competence" in lay.principle
    assert "may not substitute its own medical judgment" in medical.principle
    assert "Medical evidence is required to establish diagnosis" not in medical.principle


def test_outcome_direction_classifies_favorable_unfavorable_unknown():
    assert _outcome_direction("granted", "veteran") == 1
    assert _outcome_direction("vacated", "veteran") == 1
    assert _outcome_direction("remanded", "veteran") == 1
    assert _outcome_direction("vacated and remanded", "veteran") == 1
    assert _outcome_direction("denied", "veteran") == -1
    assert _outcome_direction("affirmed", "veteran") == -1
    assert _outcome_direction("dismissed", "veteran") == -1
    # Mixed-direction compounds, partial dispositions, and unknown signals
    # are never guessed.
    assert _outcome_direction("granted and denied", "veteran") == 0
    assert _outcome_direction("granted in part and denied in part", "veteran") == 0
    assert _outcome_direction("granted in part", "veteran") == 0
    assert _outcome_direction("reversed", "veteran") == 0
    assert _outcome_direction("", "veteran") == 0


def test_outcome_direction_secretary_flips_affirmed_and_dismissed():
    # When the Secretary cross-appealed, "affirmed" and "dismissed" flip
    # to favorable (the veteran's grant stands).
    assert _outcome_direction("affirmed", appellant_role="secretary") == 1
    assert _outcome_direction("dismissed", appellant_role="secretary") == 1


def test_outcome_direction_secretary_denied_means_veteran_won():
    """A denied Secretary appeal upholds the veteran's favorable outcome.

    ``extract_outcome`` reads the appeal's disposition ("denied"); when the
    Secretary is the appellant, that denial means the veteran's grant stands.
    The same inversion applies to every other signal: a granted Secretary
    appeal, or a vacatur/remand of the grant, goes against the veteran.
    """
    assert _outcome_direction("denied", appellant_role="secretary") == 1
    assert _outcome_direction("granted", appellant_role="secretary") == -1
    assert _outcome_direction("vacated", appellant_role="secretary") == -1
    assert _outcome_direction("remanded", appellant_role="secretary") == -1


def test_outcome_direction_veteran_stays_unfavorable_for_affirmed():
    # When the veteran appealed, "affirmed" stays unfavorable.
    assert _outcome_direction("affirmed", appellant_role="veteran") == -1
    assert _outcome_direction("dismissed", appellant_role="veteran") == -1


def test_outcome_direction_requires_explicit_appellant_role():
    assert _outcome_direction("affirmed", appellant_role="unknown") == 0
    assert _outcome_direction("affirmed", appellant_role="") == 0
    assert _outcome_direction("affirmed") == 0
    assert _outcome_direction("granted", appellant_role="unknown") == 0


def test_detect_contradictions_party_aware_affirmed():
    # Two cases on the same statute: one veteran-appeal affirmed
    # (unfavorable), one Secretary-appeal affirmed (favorable). The
    # detector should flag them as contradictory because the same
    # outcome word means opposite things.
    cases = [
        _case("Smith v. Wilkie", outcome="affirmed", statutes=["38 U.S.C. § 5107(b)"], appellant_role="veteran"),
        _case("Jones v. McDonough", outcome="affirmed", statutes=["38 U.S.C. § 5107(b)"], appellant_role="secretary"),
    ]
    contradictions = detect_contradictions(cases)
    assert len(contradictions) == 1
    assert "affirmed" in contradictions[0].statement


def test_detect_contradictions_both_secretary_affirmed_no_flag():
    # Two Secretary-appeal affirmed cases on the same statute: both
    # favorable, no contradiction.
    cases = [
        _case("Smith v. Wilkie", outcome="affirmed", statutes=["38 U.S.C. § 5107(b)"], appellant_role="secretary"),
        _case("Jones v. McDonough", outcome="affirmed", statutes=["38 U.S.C. § 5107(b)"], appellant_role="secretary"),
    ]
    assert detect_contradictions(cases) == []


def test_detect_contradictions_flags_opposite_outcomes_on_shared_statute():
    cases = [
        _case("Smith v. Wilkie", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones v. McDonough", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    contradictions = detect_contradictions(cases)

    assert len(contradictions) == 1
    assert contradictions[0].case_a == "Smith v. Wilkie"
    assert contradictions[0].case_b == "Jones v. McDonough"
    assert "38 U.S.C. § 5107(b)" in contradictions[0].statement
    assert "granted" in contradictions[0].statement and "denied" in contradictions[0].statement


def test_detect_contradictions_ignores_same_direction_outcomes():
    cases = [
        _case("Smith v. Wilkie", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones v. McDonough", outcome="vacated", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    assert detect_contradictions(cases) == []


def test_detect_contradictions_requires_shared_statute():
    cases = [
        _case("Smith v. Wilkie", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones v. McDonough", outcome="denied", statutes=["38 U.S.C. § 7104(d)(1)"]),
    ]

    assert detect_contradictions(cases) == []


def test_detect_contradictions_skips_first_case_without_statutes():
    # A case with an explicit outcome but no statute (enriched or in text)
    # cannot anchor a contradiction and is skipped, not guessed.
    cases = [
        _case("Smith v. Wilkie", outcome="granted", snippet="no statutory citation here"),
        _case("Jones v. McDonough", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    assert detect_contradictions(cases) == []


def test_detect_contradictions_skips_first_case_without_elements():
    # A shared statute is necessary but not sufficient: a case that does not
    # affirmatively address any claim element cannot anchor a conflict.
    cases = [
        _case(
            "Smith v. Wilkie",
            snippet="purely procedural posture",
            outcome="granted",
            statutes=["38 U.S.C. § 5107(b)"],
        ),
        _case(
            "Jones v. McDonough",
            snippet="service connection requires a nexus",
            outcome="denied",
            statutes=["38 U.S.C. § 5107(b)"],
        ),
    ]

    assert detect_contradictions(cases) == []


def test_detect_contradictions_dedupes_duplicate_titles():
    # Three distinct case records share the title "Smith v. Wilkie"; the same
    # title pair recurs across index pairs, so the detector reports each pair
    # only once (the seen-set branch).
    cases = [
        _case("Smith v. Wilkie", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Smith v. Wilkie", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Smith v. Wilkie", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    contradictions = detect_contradictions(cases)

    assert len(contradictions) == 1
    assert contradictions[0].case_a == "Smith v. Wilkie"
    assert contradictions[0].case_b == "Smith v. Wilkie"


def test_detect_contradictions_flags_outcomes_in_holding_text():
    # Outcomes that only appear in the holding text (no enriched outcome field)
    # are still detected via impact.detect_outcome's title/holding fallback.
    cases = [
        _case(
            "Smith v. Wilkie",
            holding="The Court denied the claim.",
            statutes=["38 U.S.C. § 5107(b)"],
        ),
        _case(
            "Jones v. McDonough",
            holding="The Court granted the claim.",
            statutes=["38 U.S.C. § 5107(b)"],
        ),
    ]

    contradictions = detect_contradictions(cases)

    assert len(contradictions) == 1
    assert contradictions[0].case_a == "Smith v. Wilkie"
    assert contradictions[0].case_b == "Jones v. McDonough"
    # The statement names the resolved outcomes, not the empty fields.
    assert "denied" in contradictions[0].statement
    assert "granted" in contradictions[0].statement


def test_detect_contradictions_ignores_outcomes_only_in_snippet():
    # detect_outcome deliberately scans title/holding only (snippets often
    # describe lower-level actions), so a snippet-only signal stays unflagged.
    cases = [
        _case("Smith v. Wilkie", snippet="the Court denied the claim", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones v. McDonough", snippet="the Court granted the claim", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    assert detect_contradictions(cases) == []


def test_detect_contradictions_ignores_unknown_outcomes():
    cases = [
        _case("Smith v. Wilkie", outcome="", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones v. McDonough", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    assert detect_contradictions(cases) == []


def test_detect_contradictions_dedupes_case_pair():
    # Three cases sharing a statute: one favorable, two unfavorable. Only one
    # contradiction per pair is reported (Smith/Jones and Smith/Adams).
    cases = [
        _case("Smith v. Wilkie", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones v. McDonough", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Adams v. Shulkin", outcome="affirmed", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    contradictions = detect_contradictions(cases)

    assert len(contradictions) == 2
    pairs = {(c.case_a, c.case_b) for c in contradictions}
    assert pairs == {
        ("Smith v. Wilkie", "Jones v. McDonough"),
        ("Smith v. Wilkie", "Adams v. Shulkin"),
    }


def test_detect_contradictions_falls_back_to_text_statutes():
    # Unenriched cases: statutes are pulled from the case text.
    cases = [
        _case(
            "Smith v. Wilkie",
            outcome="granted",
            snippet="nexus under 38 U.S.C. § 5107(b)",
        ),
        _case(
            "Jones v. McDonough",
            outcome="denied",
            snippet="nexus under 38 U.S.C. § 5107(b)",
        ),
    ]

    contradictions = detect_contradictions(cases)

    assert len(contradictions) == 1
    assert contradictions[0].case_a == "Smith v. Wilkie"
    assert contradictions[0].case_b == "Jones v. McDonough"


def test_detect_contradictions_continues_past_unknown_first_case():
    # A leading unknown-direction case is skipped (not a terminal break), so a
    # contradiction between the later cases is still reported.
    cases = [
        _case("Unknown", outcome="", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Granted", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Denied", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    contradictions = detect_contradictions(cases)

    assert [(c.case_a, c.case_b) for c in contradictions] == [("Granted", "Denied")]


def test_detect_contradictions_continues_past_statuteless_first_case():
    # A leading case with no citable statute is skipped (not a terminal break),
    # so the later opposite-outcome pair on a shared statute still surfaces.
    cases = [
        _case("NoStatutes", outcome="granted", snippet="no statutory citation here"),
        _case("Granted", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Denied", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    contradictions = detect_contradictions(cases)

    assert [(c.case_a, c.case_b) for c in contradictions] == [("Granted", "Denied")]


def test_detect_contradictions_continues_past_unknown_second_case():
    # An unknown-direction peer in the middle of the inner scan is skipped, so
    # the later opposite-outcome peer on the same statute is still compared.
    cases = [
        _case("Granted", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Unknown", outcome="", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Denied", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    contradictions = detect_contradictions(cases)

    assert [(c.case_a, c.case_b) for c in contradictions] == [("Granted", "Denied")]


def test_detect_contradictions_continues_past_unshared_statute():
    # A peer that cites a different statute is skipped, so a later peer on the
    # shared statute is still compared instead of aborting the inner scan.
    cases = [
        _case("Granted", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("DeniedOther", outcome="denied", statutes=["38 U.S.C. § 7104(d)(1)"]),
        _case("Denied", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    contradictions = detect_contradictions(cases)

    assert [(c.case_a, c.case_b) for c in contradictions] == [("Granted", "Denied")]


def test_detect_contradictions_continues_past_seen_pair():
    # A recurring case pair is deduped, but the scan continues past it to the
    # next distinct pair rather than aborting at the first duplicate.
    cases = [
        _case("Smith", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Smith", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Adams", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    contradictions = detect_contradictions(cases)

    assert {(c.case_a, c.case_b) for c in contradictions} == {
        ("Smith", "Jones"),
        ("Smith", "Adams"),
    }


def test_merge_contradictions_dedupes_within_secondary():
    from va_legal_agent.interpretation import _merge_contradictions

    primary = [Contradiction(statement="primary", case_a="X", case_b="Y")]
    secondary = [
        Contradiction(statement="first", case_a="A", case_b="B"),
        Contradiction(statement="second", case_a="A", case_b="B"),
    ]

    merged = _merge_contradictions(primary, secondary)

    assert [c.statement for c in merged] == ["primary", "first"]


def test_detect_contradictions_skips_superseded_first_case():
    """A superseded case cannot anchor a live contradiction."""
    cases = [
        _case("Smith v. Wilkie", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones v. McDonough", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]
    cases[0].superseded_by = "Jones v. McDonough"

    assert detect_contradictions(cases) == []


def test_detect_contradictions_skips_pair_when_second_superseded():
    """When the second case in a pair is superseded, the pair is skipped."""
    cases = [
        _case("Smith v. Wilkie", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones v. McDonough", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]
    cases[1].superseded_by = "Adams v. Shulkin"

    assert detect_contradictions(cases) == []


def test_detect_contradictions_reports_live_pair_when_superseded_also_present():
    """A third live case still triggers contradiction with the non-superseded
    first case, while the superseded pair is silently dropped."""
    cases = [
        _case("Smith v. Wilkie", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case(
            "Jones v. McDonough", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]
        ),
        _case("Adams v. Shulkin", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]
    cases[1].superseded_by = "Smith v. Wilkie"  # Jones is superseded

    contradictions = detect_contradictions(cases)

    # Smith↔Adams is the live pair; Smith↔Jones is skipped (Jones superseded)
    assert len(contradictions) == 1
    assert {(c.case_a, c.case_b) for c in contradictions} == {
        ("Smith v. Wilkie", "Adams v. Shulkin")
    }


def test_build_analysis_template_path_reports_deterministic_contradictions(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = [
        _case("Smith v. Wilkie", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones v. McDonough", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    result = build_interpretive_analysis("benefit of the doubt", "Compensation", cases)

    # The always-on detector surfaces the conflict even on the template path.
    assert result.interpretation_source == "template"
    assert len(result.contradictions) == 1
    assert result.contradictions[0].case_a == "Smith v. Wilkie"
    assert result.contradictions[0].case_b == "Jones v. McDonough"


def test_build_analysis_merges_llm_and_deterministic_contradictions(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    llm_contradiction = Contradiction(
        statement="The two decisions split on the nexus standard.",
        case_a="Smith v. Wilkie",
        case_b="Jones v. McDonough",
    )
    reasoning = ReasoningResult(
        reconciled_principles=[],
        contradictions=[llm_contradiction],
        synthesis="Smith controls.",
    )
    monkeypatch.setattr(
        "va_legal_agent.interpretation.reason_cases",
        lambda issue, claim_type, cases: reasoning,
    )
    # A second deterministic-only pair the LLM did not report.
    cases = [
        _case("Smith v. Wilkie", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones v. McDonough", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Adams v. Shulkin", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    result = build_interpretive_analysis("benefit of the doubt", "Compensation", cases)

    # LLM contradiction wins for the shared pair; deterministic fills in Adams.
    assert len(result.contradictions) == 2
    by_pair = {(c.case_a, c.case_b): c for c in result.contradictions}
    assert by_pair[("Smith v. Wilkie", "Jones v. McDonough")].statement == (
        "The two decisions split on the nexus standard."
    )
    assert by_pair[("Smith v. Wilkie", "Adams v. Shulkin")].case_b == "Adams v. Shulkin"


def test_build_analysis_deterministic_contradictions_dedup_with_llm(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # The LLM and the deterministic detector both flag the same pair: only the
    # LLM's richer statement survives.
    llm_contradiction = Contradiction(
        statement="Granted for Smith, denied for Jones on the same statute.",
        case_a="Smith v. Wilkie",
        case_b="Jones v. McDonough",
    )
    reasoning = ReasoningResult(
        reconciled_principles=[],
        contradictions=[llm_contradiction],
        synthesis="",
    )
    monkeypatch.setattr(
        "va_legal_agent.interpretation.reason_cases",
        lambda issue, claim_type, cases: reasoning,
    )
    cases = [
        _case("Smith v. Wilkie", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Jones v. McDonough", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    result = build_interpretive_analysis("benefit of the doubt", "Compensation", cases)

    assert len(result.contradictions) == 1
    assert result.contradictions[0].statement == (
        "Granted for Smith, denied for Jones on the same statute."
    )


def test_build_analysis_reports_strengths_gaps_and_coverage(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = [_case("Smith v. Wilkie", snippet="service connection requires competent evidence")]

    result = build_interpretive_analysis(
        "service connection and presumption of exposure", "Compensation", cases
    )

    assert [element.name for element in result.detected_elements] == [
        "service connection",
        "presumption",
    ]
    assert result.detected_elements[0].covered_by == ["Smith v. Wilkie"]
    assert result.detected_elements[1].covered_by == []
    # service connection covered (weight 0.30), presumption not (weight 0.03)
    # → 0.30 / 0.33 ≈ 0.909
    assert abs(result.coverage_score - (0.30 / 0.33)) < 0.001
    assert any("service connection" in strength for strength in result.strengths)
    assert any("presumption" in gap for gap in result.gaps)
    assert result.interpretation_source == "template"
    assert "Smith v. Wilkie" in result.how_it_affects_va_claims
    assert result.likely_applicable_principles  # "competent evidence" principle found
    assert all(step for step in result.next_steps)


def test_deep_summary_does_not_feed_element_coverage():
    # The deep-read summary is model-generated. It may inform the narrative,
    # but it is not source evidence that the case addresses an element.
    cases = [
        _case(
            "Smith v. Wilkie",
            deep_summary="Full opinion: service connection requires a nexus.",
        )
    ]

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    assert result.detected_elements[0].covered_by == []
    assert result.coverage_score == 0.0


def test_generated_impact_does_not_feed_element_coverage():
    case = _case("Smith v. Wilkie")
    case.impact = "service connection requires a nexus"

    result = build_interpretive_analysis("service connection", "Compensation", [case])

    assert result.detected_elements[0].covered_by == []
    assert result.coverage_score == 0.0


def test_build_analysis_uses_llm_text_when_available(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "va_legal_agent.interpretation.reason_cases", lambda issue, claim_type, cases: None
    )
    captured: dict[str, object] = {}

    def fake_llm(issue, claim_type, cases):
        captured["issue"] = issue
        captured["claim_type"] = claim_type
        captured["cases"] = cases
        return "LLM says: Smith controls."

    monkeypatch.setattr("va_legal_agent.interpretation.interpret_cases", fake_llm)

    result = build_interpretive_analysis(
        "service connection", "Compensation", [_case("Smith v. Wilkie", snippet="service connection")]
    )

    assert result.interpretation_source == "llm"
    assert result.how_it_affects_va_claims == "LLM says: Smith controls."
    assert result.contradictions == []
    # The issue, claim type, and capped case list are passed through unchanged.
    assert captured["issue"] == "service connection"
    assert captured["claim_type"] == "Compensation"
    assert [c.title for c in captured["cases"]] == ["Smith v. Wilkie"]
    # Structured fields remain populated even when the narrative is LLM-enhanced.
    assert result.detected_elements[0].name == "service connection"


def test_build_analysis_falls_back_to_template_when_llm_unavailable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "va_legal_agent.interpretation.reason_cases", lambda issue, claim_type, cases: None
    )
    monkeypatch.setattr(
        "va_legal_agent.interpretation.interpret_cases",
        lambda issue, claim_type, cases: None,
    )

    result = build_interpretive_analysis(
        "service connection", "Compensation", [_case("Smith v. Wilkie", snippet="service connection")]
    )

    assert result.interpretation_source == "template"
    assert "Smith v. Wilkie" in result.how_it_affects_va_claims


def test_build_analysis_flags_missing_principles():
    cases = [_case("Doe v. VA", snippet="procedural posture only")]

    result = build_interpretive_analysis("presumption of exposure", "Compensation", cases)

    assert result.principle_findings == []
    # The missing-principle flag leads the gaps (before element-level gaps).
    assert result.gaps[0] == (
        "No explicit legal principle was extracted from the retrieved results; "
        "verify the query terms or broaden the search."
    )
    assert "No explicit principle" in result.how_it_affects_va_claims


def test_build_analysis_bare_condition_does_not_invent_elements():
    cases = [_case("Doe v. VA", snippet="service connection requires a nexus")]

    result = build_interpretive_analysis("tinnitus", "Compensation", cases)

    # The bare condition is unclassified: no unrelated elements are invented
    # and the analysis is explicitly flagged instead of reporting fake coverage.
    assert result.detected_elements == []
    assert result.coverage_score == 0.0
    assert result.coverage_confidence == "low"
    assert any("No claim elements were detected" in gap for gap in result.gaps)
    assert "No claim elements were detected from the issue." in result.how_it_affects_va_claims
    assert all("Caluza" not in step for step in result.next_steps)
    assert "XXXX" not in result.how_it_affects_va_claims


def test_template_narrative_is_exact(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = [
        _case("Smith v. Wilkie", snippet="service connection requires competent evidence"),
        _case("Jones v. McDonough", snippet="the benefit of the doubt rule applies"),
    ]

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    text = result.how_it_affects_va_claims
    assert "For the issue of service connection under Compensation" in text
    assert "(Smith v. Wilkie, Jones v. McDonough)" in text
    assert "governing principles:" in text
    assert "Key elements to establish: service connection." in text
    assert (
        "This guidance is research support derived from public decisions, not legal advice." in text
    )
    assert "None" not in text
    assert "XX" not in text


def test_template_joins_elements_with_semicolons(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = build_interpretive_analysis(
        "service connection and presumption of exposure",
        "Compensation",
        [_case("Smith v. Wilkie", snippet="service connection requires competent evidence")],
    )

    assert "Key elements to establish: service connection; presumption." in result.how_it_affects_va_claims


def test_strengths_and_principles_cite_sources_exactly(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = [
        _case("Case A", snippet="service connection requires a nexus"),
        _case("Case B", snippet="service connection requires a nexus"),
        _case("Case C", snippet="service connection requires a nexus"),
        _case("Case D", snippet="service connection requires a nexus"),
    ]

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    # The principle strength leads; element strengths cite at most three sources.
    assert result.strengths[0].startswith("Retrieved authority articulates")
    strength = next(s for s in result.strengths if "addresses 'service connection'" in s)
    assert strength == "Retrieved authority addresses 'service connection': Case A, Case B, Case C."
    assert any("(see: Case A, Case B, Case C)" in p for p in result.likely_applicable_principles)


def test_template_caps_principles_at_three(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    case = _case(
        "Many v. VA",
        snippet=(
            "benefit of the doubt reasons and bases nexus competent evidence "
            "lay evidence medical evidence presumption duty to assist"
        ),
    )

    result = build_interpretive_analysis("service connection", "Compensation", [case])

    text = result.how_it_affects_va_claims
    first = "the benefit of the doubt is given to the veteran"
    second = "adequate statement of reasons and bases"
    third = "nexus linking the current disability"
    fourth = "competent evidence addressing the required elements"
    assert first in text and second in text and third in text
    assert fourth not in text  # template lists at most three governing principles


def test_build_analysis_limits_read_env(monkeypatch):
    monkeypatch.setenv("INTERPRET_CASE_LIMIT", "1")
    monkeypatch.setenv("PRINCIPLE_SCAN_LIMIT", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cases = [
        _case("Case A", snippet="benefit of the doubt rule applies"),
        _case("Case B", snippet="benefit of the doubt rule applies"),
    ]

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    assert result.principle_findings[0].source_cases == ["Case A"]
    assert "Case A" in result.how_it_affects_va_claims
    assert "Case B" not in result.how_it_affects_va_claims


def test_build_analysis_uses_llm_reasoning_when_available(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    reasoning = ReasoningResult(
        reconciled_principles=[
            "Service connection requires a nexus opinion (Smith v. Wilkie)."
        ],
        contradictions=[
            Contradiction(
                statement="The two decisions split on the nexus standard.",
                case_a="Smith v. Wilkie",
                case_b="Jones v. McDonough",
            )
        ],
        synthesis="Smith controls; Jones is distinguishable.",
    )
    captured: dict[str, object] = {}

    def recording_reason(issue, claim_type, cases):
        captured.update(issue=issue, claim_type=claim_type, cases=cases)
        return reasoning

    monkeypatch.setattr(
        "va_legal_agent.interpretation.reason_cases", recording_reason
    )
    monkeypatch.setattr(
        "va_legal_agent.interpretation.interpret_cases",
        lambda issue, claim_type, cases: "narrative",
    )

    result = build_interpretive_analysis(
        "service connection", "Compensation", [_case("Smith v. Wilkie", snippet="service connection")]
    )

    assert result.interpretation_source == "llm"
    # The reconciling synthesis replaces the lighter narrative.
    assert result.how_it_affects_va_claims == "Smith controls; Jones is distinguishable."
    # LLM-reconciled principles (with inline citations) back the report.
    assert result.likely_applicable_principles == [
        "Service connection requires a nexus opinion (Smith v. Wilkie)."
    ]
    assert len(result.contradictions) == 1
    assert result.contradictions[0].statement == "The two decisions split on the nexus standard."
    assert result.contradictions[0].case_a == "Smith v. Wilkie"
    assert result.contradictions[0].case_b == "Jones v. McDonough"
    # The issue, claim type, and capped case list reach the reasoning pass.
    assert captured["issue"] == "service connection"
    assert captured["claim_type"] == "Compensation"
    assert [c.title for c in captured["cases"]] == ["Smith v. Wilkie"]


def test_build_analysis_reasoning_without_synthesis_uses_narrative(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # The reasoning pass returns principles/contradictions but no synthesis;
    # the lighter LLM narrative still supplies the prose.
    reasoning = ReasoningResult(
        reconciled_principles=["A principle (Smith v. Wilkie)."], synthesis=""
    )
    monkeypatch.setattr(
        "va_legal_agent.interpretation.reason_cases",
        lambda issue, claim_type, cases: reasoning,
    )
    monkeypatch.setattr(
        "va_legal_agent.interpretation.interpret_cases",
        lambda issue, claim_type, cases: "LLM narrative.",
    )

    result = build_interpretive_analysis(
        "service connection", "Compensation", [_case("Smith v. Wilkie", snippet="service connection")]
    )

    assert result.how_it_affects_va_claims == "LLM narrative."
    assert result.likely_applicable_principles == ["A principle (Smith v. Wilkie)."]


def test_build_analysis_reasoning_falls_back_to_template_principles(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # The LLM returns only a synthesis (no reconciled principles): the
    # deterministic principle findings still back the report.
    reasoning = ReasoningResult(reconciled_principles=[], synthesis="Only a synthesis.")
    monkeypatch.setattr(
        "va_legal_agent.interpretation.reason_cases",
        lambda issue, claim_type, cases: reasoning,
    )

    result = build_interpretive_analysis(
        "service connection",
        "Compensation",
        [_case("Smith v. Wilkie", snippet="service connection requires competent evidence")],
    )

    assert result.how_it_affects_va_claims == "Only a synthesis."
    assert result.likely_applicable_principles  # deterministic findings kept
    assert any("(see: Smith v. Wilkie)" in p for p in result.likely_applicable_principles)


# ── Weighted coverage score ──────────────────────────────────────────────────


def test_weighted_coverage_single_covered_high_weight_element():
    """Covering nexus (weight 0.40) alone gives higher score than covering
    a low-weight element alone."""
    # Only nexus is detected and it is covered.
    cases = [_case("Smith v. Wilkie", snippet="nexus opinion required")]

    result = build_interpretive_analysis("nexus", "Compensation", cases)

    # The single detected element has weight 0.40 and is covered → 1.0
    assert result.detected_elements[0].weight == 0.40
    assert result.coverage_score == 1.0


def test_weighted_coverage_nexus_covered_rates_higher_than_duty_covered():
    """Both elements fully covered produce 1.0, but the stored weight
    differs — nexus is 0.40, duty-to-assist is 0.03."""
    nexus_cases = [_case("Smith v. Wilkie", snippet="nexus opinion required")]
    duty_cases = [_case("Jones v. VA", snippet="duty to assist was violated")]

    nexus_result = build_interpretive_analysis("nexus", "Compensation", nexus_cases)
    duty_result = build_interpretive_analysis("duty to assist", "Compensation", duty_cases)

    # Both are single-element with full coverage.
    assert nexus_result.coverage_score == 1.0
    assert duty_result.coverage_score == 1.0
    # The weight is stored on the element for downstream inspection (the
    # strength/gap messages, CSV exports, and JSON output all carry it).
    assert nexus_result.detected_elements[0].weight == 0.40
    assert duty_result.detected_elements[0].weight == 0.03


def test_weighted_coverage_two_elements_one_missing():
    """When nexus (high weight) is covered but rating (low weight) is not,
    the score is higher than the 0.5 the unweighted formula would give."""
    cases = [_case("Smith v. Wilkie", snippet="nexus opinion and service connection")]

    result = build_interpretive_analysis(
        "nexus and rating", "Compensation", cases
    )

    # nexus (0.40) covered, rating (0.02) not → 0.40 / 0.42 ≈ 0.952
    assert abs(result.coverage_score - (0.40 / 0.42)) < 0.001
    assert result.detected_elements[0].covered_by == ["Smith v. Wilkie"]
    assert result.detected_elements[1].covered_by == []


def test_weighted_coverage_two_elements_high_weight_missing():
    """When nexus is missing but rating is covered, the score drops sharply
    because nexus carries 0.40 of the denominator's 0.42 total."""
    cases = [_case("Smith v. Wilkie", snippet="rating schedule criteria apply")]

    result = build_interpretive_analysis(
        "nexus and rating", "Compensation", cases
    )

    # rating (0.02) covered, nexus (0.40) not → 0.02 / 0.42 ≈ 0.048
    assert abs(result.coverage_score - (0.02 / 0.42)) < 0.001


def test_weighted_coverage_all_covered():
    """Full coverage across all nine elements yields 1.0 regardless of weights."""
    issue = (
        "service connection nexus benefit of the doubt reasons and bases "
        "evidence evaluation presumption rating aggravation duty to assist"
    )
    cases = [_case("Omni Case", snippet=issue)]

    result = build_interpretive_analysis(issue, "Compensation", cases)

    assert len(result.detected_elements) == len(TOPICS)
    assert result.coverage_score == 1.0
    assert all(element.covered_by for element in result.detected_elements)


def test_weighted_coverage_none_covered():
    """No coverage yields 0.0 regardless of weights."""
    cases = [_case("Smith v. Wilkie")]  # empty snippet/holding, no matches

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    assert result.coverage_score == 0.0


def test_weighted_coverage_element_carries_weight_in_model():
    """Every ClaimElement in the output carries its weight for downstream
    inspection (CSV/JSON/strength-message consumers)."""
    issue = (
        "service connection nexus benefit of the doubt reasons and bases "
        "evidence evaluation presumption rating aggravation duty to assist"
    )
    cases = [_case("Smith v. Wilkie", snippet=issue)]

    result = build_interpretive_analysis(issue, "Compensation", cases)

    # All 9 elements carry their declared weight.
    for element in result.detected_elements:
        assert element.weight > 0
        assert element.weight <= 1.0
    # Pin the two highest weights.
    weights = {element.name: element.weight for element in result.detected_elements}
    assert weights["nexus"] == 0.40
    assert weights["service connection"] == 0.30


# ── Statute × Court × Outcome matrix ─────────────────────────────────────────


def test_court_order_uses_canonical_supreme_court_constant():
    assert _COURT_ORDER[-1] == COURT_SUPREME


def test_statute_outcome_matrix_empty_cases():
    assert build_statute_outcome_matrix([]) == []


def test_statute_outcome_matrix_single_statute_two_opposite_outcomes():
    cases = [
        _case("Granted", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Denied", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    rows = build_statute_outcome_matrix(cases)

    assert len(rows) == 1
    assert rows[0].statute == "38 U.S.C. § 5107(b)"
    assert rows[0].favorable == 1
    assert rows[0].unfavorable == 1
    assert rows[0].unknown == 0


def test_statute_outcome_matrix_multiple_courts():
    cases = [
        _case("CAVC Granted", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case(
            "Fed Cir Denied",
            court="U.S. Court of Appeals for the Federal Circuit",
            outcome="denied",
            statutes=["38 U.S.C. § 5107(b)"],
        ),
    ]

    rows = build_statute_outcome_matrix(cases)

    # Two rows: one per court. Sorted by court authority order.
    assert len(rows) == 2
    assert rows[0].court == "Court of Appeals for Veterans Claims"
    assert rows[1].court == "U.S. Court of Appeals for the Federal Circuit"
    assert rows[0].favorable == 1
    assert rows[1].unfavorable == 1


def test_statute_outcome_matrix_multiple_statutes_same_court():
    cases = [
        _case(
            "Multi Statutes",
            outcome="granted",
            statutes=["38 U.S.C. § 5107(b)", "38 U.S.C. § 7104(d)(1)"],
        ),
    ]

    rows = build_statute_outcome_matrix(cases)

    assert len(rows) == 2
    # Sorted lexicographically within the same court.
    assert rows[0].statute < rows[1].statute
    for row in rows:
        assert row.favorable == 1


def test_statute_outcome_matrix_unknown_outcome_counts_as_unknown():
    cases = [
        _case("Unknown Outcome", outcome="", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    rows = build_statute_outcome_matrix(cases)

    assert len(rows) == 1
    assert rows[0].unknown == 1
    assert rows[0].favorable == 0
    assert rows[0].unfavorable == 0


def test_statute_outcome_matrix_skips_cases_without_statutes():
    cases = [
        _case("No Statutes", outcome="granted", statutes=[], snippet="no statutory citation here"),
    ]

    assert build_statute_outcome_matrix(cases) == []


def test_statute_outcome_matrix_falls_back_to_text_statutes():
    # Unenriched cases: statutes pulled from snippet text.
    cases = [
        _case(
            "Text Case",
            outcome="granted",
            statutes=[],
            snippet="under 38 U.S.C. § 5107(b)",
        ),
    ]

    rows = build_statute_outcome_matrix(cases)

    assert len(rows) == 1
    assert rows[0].statute == "38 U.S.C. § 5107(b)"
    assert rows[0].favorable == 1


def test_statute_outcome_matrix_sorts_court_by_ascending_authority():
    cases = [
        _case(
            "Fed Cir",
            court="U.S. Court of Appeals for the Federal Circuit",
            outcome="denied",
            statutes=["38 U.S.C. § 5107(b)"],
        ),
        _case("BVA", court="Board of Veterans' Appeals", outcome="granted",
              statutes=["38 U.S.C. § 5107(b)"]),
        _case("CAVC", outcome="vacated", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    rows = build_statute_outcome_matrix(cases)

    courts = [row.court for row in rows]
    assert courts == [
        "Board of Veterans' Appeals",
        "Court of Appeals for Veterans Claims",
        "U.S. Court of Appeals for the Federal Circuit",
    ]


def test_statute_outcome_matrix_appears_in_build_interpretive_analysis(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = [
        _case("Granted", outcome="granted", statutes=["38 U.S.C. § 5107(b)"]),
        _case("Denied", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    assert len(result.statute_outcome_matrix) == 1
    assert result.statute_outcome_matrix[0].favorable == 1
    assert result.statute_outcome_matrix[0].unfavorable == 1


# ── Coverage confidence ──────────────────────────────────────────────────────


def test_confidence_low_with_few_cases_no_deep_no_holdings_no_llm(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = [_case("Smith v. Wilkie")]  # no snippet, no holdings, no deep_summary

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    assert result.coverage_confidence == "low"


def test_confidence_medium_with_five_supported_cases(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = [
        _case(f"Case {i}", snippet="service connection requires a nexus",
              holding="The Board erred.", deep_summary="Full text.")
        for i in range(5)
    ]

    # Five binding-court cases with full element coverage reach medium.
    result = build_interpretive_analysis("service connection", "Compensation", cases)

    assert result.coverage_confidence == "medium"


def test_confidence_high_with_ten_cases_deep_holdings_llm(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from va_legal_agent.llm import ReasoningResult

    reasoning = ReasoningResult(
        reconciled_principles=["The Board must provide reasons and bases."],
        synthesis="A synthesis.",
    )
    monkeypatch.setattr(
        "va_legal_agent.interpretation.reason_cases",
        lambda issue, claim_type, cases: reasoning,
    )

    cases = [
        _case(f"Case {i}", snippet="service connection nexus",
              holding="The Board erred.", deep_summary="Full-text summary.")
        for i in range(10)
    ]

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    assert result.coverage_confidence == "high"


def test_confidence_deep_read_alone_does_not_raise_confidence(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Deep summaries are generated text and no longer earn confidence points.
    # These cases have no source element coverage, so confidence stays low.
    cases = [
        _case(f"Case {i}", deep_summary="Full text.", holding="The Board erred.")
        for i in range(5)
    ]

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    assert result.coverage_score == 0.0
    assert result.coverage_confidence == "low"


def test_confidence_llm_narrative_does_not_add_confidence(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from va_legal_agent.llm import ReasoningResult

    reasoning = ReasoningResult(
        reconciled_principles=["A principle."],
        synthesis="A synthesis.",
    )
    monkeypatch.setattr(
        "va_legal_agent.interpretation.reason_cases",
        lambda issue, claim_type, cases: reasoning,
    )

    # Five source-supported binding-court cases reach medium; the LLM
    # narrative is not a separate confidence point.
    cases = [
        _case(f"Case {i}", snippet="nexus required", holding="Remanded.")
        for i in range(5)
    ]

    result = build_interpretive_analysis("nexus", "Compensation", cases)

    assert result.coverage_confidence == "medium"


def test_confidence_ten_supported_cases_is_high(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = [
        _case(f"Case {i}", snippet="nexus required", holding="Remanded.")
        for i in range(10)
    ]

    result = build_interpretive_analysis("nexus", "Compensation", cases)

    assert result.coverage_score == 1.0
    assert result.coverage_confidence == "high"


def test_statute_outcome_matrix_unknown_court_sort():
    """An unknown court sorts after all known courts in the matrix."""
    cases = [
        _case("Some Court", outcome="granted", statutes=["38 U.S.C. § 5107(b)"],
              court="Some County District Court"),
        _case("CAVC", outcome="denied", statutes=["38 U.S.C. § 5107(b)"]),
    ]

    rows = build_statute_outcome_matrix(cases)

    # CAVC (known) sorts before the unknown court.
    assert rows[0].court == "Court of Appeals for Veterans Claims"
    assert rows[1].court == "Some County District Court"


# ── Pass 2 regressions: element detection and source-only evidence ──────────


def test_detect_claim_elements_aid_and_attendance_is_not_all_nine():
    assert detect_claim_elements("Aid and Attendance") == []
    assert detect_claim_elements("tinnitus") == []


def test_build_analysis_aid_and_attendance_has_no_caluza_elements():
    # Aid and Attendance is an SMC benefit, not a service-connection claim. It
    # must not inherit the nine-element Caluza library.
    cases = [_case("Smith v. Wilkie", snippet="service connection requires a nexus")]

    result = build_interpretive_analysis("Aid and Attendance", "Compensation", cases)

    assert result.detected_elements == []
    assert result.coverage_score == 0.0
    assert result.coverage_confidence == "low"
    assert any("No claim elements were detected" in gap for gap in result.gaps)
    assert "No claim elements were detected from the issue." in result.how_it_affects_va_claims


def test_generated_fields_do_not_create_principle_findings():
    case = _case("Smith v. Wilkie")
    case.impact = "The benefit of the doubt applies."
    case.deep_summary = "The Board must provide reasons and bases."

    assert extract_principle_findings([case]) == []


def test_negated_holding_does_not_feed_element_coverage_or_principles():
    case = _case(
        "Smith v. Wilkie",
        holding="No nexus was established; the benefit of the doubt does not apply.",
    )

    result = build_interpretive_analysis(
        "nexus and benefit of the doubt", "Compensation", [case]
    )

    assert all(element.covered_by == [] for element in result.detected_elements)
    assert result.coverage_score == 0.0
    assert result.likely_applicable_principles == []


# ── Pass 2 regressions: same-statute contradictions ─────────────────────────


def test_detect_contradictions_requires_shared_claim_element():
    cases = [
        _case(
            "Tinnitus v. Wilkie",
            snippet="nexus",
            outcome="granted",
            statutes=["38 U.S.C. § 1110"],
        ),
        _case(
            "Knee v. McDonough",
            snippet="rating",
            outcome="denied",
            statutes=["38 U.S.C. § 1110"],
        ),
    ]

    assert detect_contradictions(cases) == []


def test_detect_contradictions_reports_shared_element_in_statement():
    cases = [
        _case(
            "Tinnitus v. Wilkie",
            snippet="nexus",
            outcome="granted",
            statutes=["38 U.S.C. § 1110"],
        ),
        _case(
            "Hearing loss v. McDonough",
            snippet="nexus",
            outcome="denied",
            statutes=["38 U.S.C. § 1110"],
        ),
    ]

    contradictions = detect_contradictions(cases)

    assert len(contradictions) == 1
    assert "regarding nexus" in contradictions[0].statement


# ── Pass 2 regressions: authority-aware coverage confidence ─────────────────


def test_confidence_unknown_precedential_is_not_binding(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = [
        _case(
            f"Unknown {i}",
            snippet="nexus required",
            holding="Remanded.",
            precedential=None,
        )
        for i in range(10)
    ]

    result = build_interpretive_analysis("nexus", "Compensation", cases)

    assert result.coverage_score == 1.0
    assert result.coverage_confidence == "low"


def test_confidence_all_bva_is_low_despite_full_coverage(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = [
        _case(
            f"BVA {i}",
            court="Board of Veterans' Appeals",
            snippet="service connection nexus",
            holding="Remanded.",
        )
        for i in range(10)
    ]

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    assert result.coverage_score == 1.0
    assert result.coverage_confidence == "low"


def test_confidence_ten_cavc_cases_with_zero_coverage_is_low(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = [
        _case(f"Case {i}", holding="The Board erred.")
        for i in range(10)
    ]

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    assert result.coverage_score == 0.0
    assert result.coverage_confidence == "low"


def test_confidence_high_requires_multiple_cases_supporting_an_element(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = [_case("Only Source", snippet="service connection", holding="Remanded.")]
    cases.extend(_case(f"Other {i}", holding="The Board erred.") for i in range(9))

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    # Coverage is full, but only one case supports the element; that is medium,
    # not high, confidence.
    assert result.coverage_score == 1.0
    assert result.coverage_confidence == "medium"


def test_statute_outcome_matrix_requires_explicit_appellant_role():
    cases = [
        _case(
            "Granted",
            outcome="granted",
            statutes=["38 U.S.C. § 1110"],
            appellant_role="",
        ),
        _case(
            "Denied",
            outcome="denied",
            statutes=["38 U.S.C. § 1110"],
            appellant_role="",
        ),
    ]

    rows = build_statute_outcome_matrix(cases)

    assert len(rows) == 1
    assert rows[0].favorable == 0
    assert rows[0].unfavorable == 0
    assert rows[0].unknown == 2


def test_statute_outcome_matrix_treats_mixed_compound_as_unknown():
    case = _case(
        "Mixed",
        outcome="granted in part and denied in part",
        statutes=["38 U.S.C. § 1110"],
        appellant_role="veteran",
    )

    rows = build_statute_outcome_matrix([case])

    assert len(rows) == 1
    assert rows[0].favorable == 0
    assert rows[0].unfavorable == 0
    assert rows[0].unknown == 1
