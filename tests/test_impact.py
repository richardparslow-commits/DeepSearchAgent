"""Tests for the nuanced impact-analysis layer (va_legal_agent.impact)."""

from va_legal_agent.impact import (
    analyze_case_impact,
    authority_note_for,
    detect_issue_tags,
    detect_outcome,
    detect_statutes,
    outcome_note_for,
    statute_note_for,
)
from va_legal_agent.models import CaseRecord

CAVC = "Court of Appeals for Veterans Claims"
CAFC = "U.S. Court of Appeals for the Federal Circuit"
BVA = "Board of Veterans' Appeals"


def _case(
    title="Zzz",
    snippet="",
    holding="",
    impact="",
    issue="",
    court=CAFC,
    outcome="",
    statutes=None,
):
    return CaseRecord(
        title=title,
        court=court,
        url="https://example.com/x",
        snippet=snippet,
        holding=holding,
        impact=impact,
        issue=issue,
        outcome=outcome,
        statutes=statutes or [],
    )


def test_detect_issue_tags_priority_order():
    case = _case(
        snippet="service connection benefit of the doubt reasons and bases evidence nexus rating"
    )

    assert detect_issue_tags(case) == [
        "service connection",
        "benefit of the doubt",
        "reasons and bases",
        "evidence evaluation",
        "nexus",
        "rating",
    ]


def test_detect_issue_tags_fallback():
    assert detect_issue_tags(_case(snippet="procedural posture", issue="Hearing Loss")) == ["hearing loss"]
    assert detect_issue_tags(_case(snippet="procedural posture", issue="")) == [
        "the factual and legal issue"
    ]


def test_nuance_starts_with_relevance_sentence():
    profile = analyze_case_impact(_case(snippet="nexus opinion"))

    assert profile.nuance.startswith("This ruling is relevant to nexus in VA compensation claims.")


def test_outcome_prefers_record_field_over_text():
    case = _case(holding="The decision was affirmed.", outcome="vacated")

    assert detect_outcome(case) == "vacated"


def test_outcome_scanned_from_title_and_holding():
    assert detect_outcome(_case(holding="vacated and remanded for further development")) == (
        "vacated and remanded"
    )


def test_outcome_not_scanned_from_snippet():
    # Snippet text often describes lower-level actions, not the ruling's disposition.
    assert detect_outcome(_case(snippet="VA denied the veteran's claim below")) == ""


def test_outcome_note_mapping():
    assert outcome_note_for("vacated and remanded").startswith("The decision below was vacated")
    assert outcome_note_for("") == ""
    assert outcome_note_for("certiorari granted") == ""


def test_detect_statutes_prefer_record_and_scan_fallback():
    record = _case(statutes=["38 U.S.C. § 5107(b)"], snippet="under 38 U.S.C. § 1110")
    assert detect_statutes(record) == ["38 U.S.C. § 5107(b)"]

    scanned = _case(snippet="under 38 U.S.C. § 1110 and 38 C.F.R. § 3.303")
    assert detect_statutes(scanned) == ["38 U.S.C. § 1110", "38 C.F.R. § 3.303"]


def test_statute_note_mapping():
    assert "benefit-of-the-doubt" in statute_note_for(["38 U.S.C. § 5107(b)"])
    assert "statutory anchors" in statute_note_for(["12 U.S.C. § 999"])
    assert statute_note_for([]) == ""


def test_statute_note_single_hint_is_exact():
    assert statute_note_for(["38 U.S.C. § 5107(b)"]) == (
        "The analysis engages the benefit-of-the-doubt rule under 38 U.S.C. § 5107(b), "
        "which can anchor briefing."
    )


def test_statute_note_two_hints_join_with_and():
    note = statute_note_for(["38 U.S.C. § 5107(b)", "38 U.S.C. § 7104(d)(1)"])

    assert " and " in note
    assert note.startswith("The analysis engages the benefit-of-the-doubt rule")
    assert "the reasons-and-bases requirement" in note


def test_statute_note_cites_non_hint_statutes_exactly():
    assert statute_note_for(["12 U.S.C. § 999", "15 U.S.C. § 777"]) == (
        "The ruling cites 12 U.S.C. § 999, 15 U.S.C. § 777, which may provide statutory anchors."
    )


def test_statute_note_caps_cited_statutes_at_three():
    note = statute_note_for(["12 U.S.C. § 999", "15 U.S.C. § 777", "26 U.S.C. § 501", "42 U.S.C. § 1983"])

    assert note == (
        "The ruling cites 12 U.S.C. § 999, 15 U.S.C. § 777, 26 U.S.C. § 501, "
        "which may provide statutory anchors."
    )


def test_authority_note_binding_vs_board_vs_unknown():
    assert authority_note_for(CAVC) == (
        "As appellate precedent, this ruling is binding on the Board and VA adjudicators."
    )
    assert authority_note_for("U.S. Supreme Court") == (
        "As appellate precedent, this ruling is binding on the Board and VA adjudicators."
    )
    assert authority_note_for(BVA) == (
        "As a Board-level decision, this ruling is persuasive but not binding precedent."
    )
    assert authority_note_for("Veterans law research result") == (
        "The issuing body is unidentified, so treat this ruling as persuasive only."
    )


def test_nuance_layers_posture_statute_and_authority_notes():
    case = _case(
        snippet="service connection",
        holding="vacated and remanded",
        statutes=["38 U.S.C. § 7104(d)(1)"],
        court=CAVC,
    )

    profile = analyze_case_impact(case)

    assert "Procedural posture:" in profile.nuance
    assert "reasons-and-bases" in profile.nuance
    assert "binding on the Board" in profile.nuance
    assert "reviewable on appeal" in profile.nuance
    assert profile.outcome == "vacated and remanded"
    assert profile.statutes == ["38 U.S.C. § 7104(d)(1)"]


def test_analyze_case_impact_is_deterministic():
    case = _case(snippet="nexus", holding="affirmed", court=BVA)

    assert analyze_case_impact(case).nuance == analyze_case_impact(case).nuance


def test_analyze_case_impact_lists_tags_and_completes_profile():
    case = _case(
        snippet=(
            "service connection benefit of the doubt reasons and bases "
            "evidence evaluation nexus rating"
        ),
        holding="vacated and remanded",
        statutes=["38 U.S.C. § 5107(b)"],
        court=CAVC,
    )

    profile = analyze_case_impact(case)

    assert profile.nuance.startswith(
        "This ruling is relevant to service connection, benefit of the doubt, "
        "reasons and bases in VA compensation claims."
    )
    assert "XX" not in profile.nuance
    assert profile.issue_tags
    assert profile.outcome_note
    assert profile.statute_note
    assert profile.authority_note
