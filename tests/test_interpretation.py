"""Tests for the interpretive analysis layer (va_legal_agent.interpretation)."""

from va_legal_agent.interpretation import (
    build_interpretive_analysis,
    detect_claim_elements,
    extract_principle_findings,
)
from va_legal_agent.models import CaseRecord


def _case(title: str, snippet: str = "", holding: str = "") -> CaseRecord:
    return CaseRecord(
        title=title,
        court="Court of Appeals for Veterans Claims",
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
        snippet=snippet,
        holding=holding,
    )


def test_detect_claim_elements_from_issue_text():
    specs = detect_claim_elements("service connection for tinnitus and benefit of the doubt")
    names = [spec.name for spec in specs]
    assert "service connection" in names
    assert "benefit of the doubt" in names


def test_detect_claim_elements_empty_when_no_match():
    assert detect_claim_elements("completely unrelated phrase") == []


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
        "va_legal_agent.interpretation.interpret_cases",
        lambda issue, claim_type, cases: "LLM says: Smith controls.",
    )

    result = build_interpretive_analysis(
        "service connection", "Compensation", [_case("Smith v. Wilkie", snippet="service connection")]
    )

    assert result.interpretation_source == "llm"
    assert result.how_it_affects_va_claims == "LLM says: Smith controls."
    # Structured fields remain populated even when the narrative is LLM-enhanced.
    assert result.detected_elements[0].name == "service connection"


def test_build_analysis_falls_back_to_template_when_llm_unavailable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
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

    result = build_interpretive_analysis("service connection", "Compensation", cases)

    assert result.principle_findings == []
    assert any("No explicit legal principle" in gap for gap in result.gaps)
    assert "No explicit principle" in result.how_it_affects_va_claims


def test_build_analysis_handles_no_detected_elements():
    cases = [_case("Doe v. VA", snippet="service connection requires a nexus")]

    result = build_interpretive_analysis("totally unrelated issue", "Compensation", cases)

    assert result.detected_elements == []
    assert result.coverage_score == 0.0
    assert any("precise legal issue" in step for step in result.next_steps)