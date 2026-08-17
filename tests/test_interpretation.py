"""Tests for the interpretive analysis layer (va_legal_agent.interpretation)."""

from va_legal_agent.interpretation import (
    _build_element_library,
    _outcome_direction,
    build_interpretive_analysis,
    detect_claim_elements,
    detect_contradictions,
    extract_principle_findings,
    uncovered_element_names,
)
from va_legal_agent.llm import ReasoningResult
from va_legal_agent.models import CaseRecord, Contradiction
from va_legal_agent.topics import TOPICS


def _case(
    title: str,
    snippet: str = "",
    holding: str = "",
    outcome: str = "",
    statutes: list[str] | None = None,
) -> CaseRecord:
    return CaseRecord(
        title=title,
        court="Court of Appeals for Veterans Claims",
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
        snippet=snippet,
        holding=holding,
        outcome=outcome,
        statutes=statutes or [],
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


def test_detect_claim_elements_empty_when_no_match():
    assert detect_claim_elements("completely unrelated phrase") == []


def test_uncovered_element_names_flags_elements_without_coverage():
    cases = [_case("Smith v. Wilkie", snippet="service connection requires a nexus")]

    # service connection is covered; presumption is detected but uncovered.
    assert uncovered_element_names("service connection", cases) == ()
    assert uncovered_element_names(
        "service connection and presumption of exposure", cases
    ) == ("presumption",)


def test_uncovered_element_names_empty_when_no_elements_detected():
    assert uncovered_element_names("totally unrelated issue", []) == ()
    assert uncovered_element_names("totally unrelated issue", [_case("Any")]) == ()


def test_extract_principle_findings_attributes_source_cases():
    cases = [
        _case("Smith v. Wilkie", snippet="the benefit of the doubt rule applies"),
        _case("Jones v. McDonough", holding="The Board must provide reasons and bases."),
    ]

    findings = extract_principle_findings(cases)

    benefit = next(f for f in findings if "equipoise" in f.principle)
    assert benefit.source_cases == ["Smith v. Wilkie"]
    reasons = next(f for f in findings if "7104" in f.principle)
    assert reasons.source_cases == ["Jones v. McDonough"]


def test_outcome_direction_classifies_favorable_unfavorable_unknown():
    assert _outcome_direction("granted") == 1
    assert _outcome_direction("vacated") == 1
    assert _outcome_direction("remanded") == 1
    assert _outcome_direction("vacated and remanded") == 1
    assert _outcome_direction("denied") == -1
    assert _outcome_direction("affirmed") == -1
    assert _outcome_direction("dismissed") == -1
    # Mixed-direction compounds and unknown signals are never guessed.
    assert _outcome_direction("granted and denied") == 0
    assert _outcome_direction("reversed") == 0
    assert _outcome_direction("") == 0


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
        _case("Smith v. Wilkie", outcome="granted", snippet="under 38 U.S.C. § 5107(b)"),
        _case("Jones v. McDonough", outcome="denied", snippet="under 38 U.S.C. § 5107(b)"),
    ]

    contradictions = detect_contradictions(cases)

    assert len(contradictions) == 1
    assert contradictions[0].case_a == "Smith v. Wilkie"
    assert contradictions[0].case_b == "Jones v. McDonough"


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
    assert result.coverage_score == 0.5
    assert any("service connection" in strength for strength in result.strengths)
    assert any("presumption" in gap for gap in result.gaps)
    assert result.interpretation_source == "template"
    assert "Smith v. Wilkie" in result.how_it_affects_va_claims
    assert result.likely_applicable_principles  # "competent evidence" principle found
    assert all(step for step in result.next_steps)


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


def test_build_analysis_handles_no_detected_elements():
    cases = [_case("Doe v. VA", snippet="service connection requires a nexus")]

    result = build_interpretive_analysis("totally unrelated issue", "Compensation", cases)

    assert result.detected_elements == []
    assert result.coverage_score == 0.0
    assert result.next_steps[0] == (
        "Identify the precise legal issue and the evidence in the claim file."
    )
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
