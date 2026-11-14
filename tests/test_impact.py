"""Tests for the nuanced impact-analysis layer (va_legal_agent.impact)."""

from va_legal_agent.impact import (
    MIXED_OUTCOME_NOTE,
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
    appellant_role="",
    precedential=True,
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
        appellant_role=appellant_role,
        precedential=precedential,
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


def test_detect_issue_tags_matches_all_topic_phrases():
    assert detect_issue_tags(_case(snippet="service-connected knee disability")) == [
        "service connection"
    ]


def test_detect_issue_tags_does_not_match_rating_inside_words():
    assert detect_issue_tags(_case(snippet="operating a motor vehicle")) == [
        "the factual and legal issue"
    ]
    assert detect_issue_tags(_case(snippet="a frustrating delay")) == [
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
    note = outcome_note_for("vacated and remanded", appellant_role="veteran")
    assert note.startswith("The decision below was vacated")
    assert "remanded" in note
    assert outcome_note_for("", appellant_role="veteran") == ""
    assert outcome_note_for("certiorari granted", appellant_role="veteran") == ""
    assert outcome_note_for("affirmed and vacated", appellant_role="veteran") == MIXED_OUTCOME_NOTE
    assert outcome_note_for("granted in part and denied in part", appellant_role="veteran") == (
        MIXED_OUTCOME_NOTE
    )


def test_outcome_note_secretary_override_affirmed():
    # When the Secretary cross-appealed, "affirmed" flips to favorable.
    note = outcome_note_for("affirmed", appellant_role="secretary")
    assert "Secretary's cross-appeal was affirmed" in note
    assert "favorable" in note


def test_outcome_note_secretary_override_dismissed():
    note = outcome_note_for("dismissed", appellant_role="secretary")
    assert "Secretary's cross-appeal was dismissed" in note


def test_outcome_note_secretary_denied_is_favorable():
    # A denied Secretary appeal means the veteran's grant stands.
    note = outcome_note_for("denied", appellant_role="secretary")
    assert "Secretary's appeal was denied" in note
    assert "stands" in note


def test_outcome_note_secretary_override_granted_and_vacatur():
    assert "was granted" in outcome_note_for("granted", appellant_role="secretary")
    assert "vacatur" in outcome_note_for("vacated", appellant_role="secretary")
    assert "remand" in outcome_note_for("remanded", appellant_role="secretary")


def test_outcome_note_no_override_for_veteran():
    # When the veteran appealed, "affirmed" stays unfavorable.
    note = outcome_note_for("affirmed", appellant_role="veteran")
    assert "affirmed, reinforcing" in note


def test_outcome_note_requires_explicit_appellant_role():
    assert outcome_note_for("affirmed", appellant_role="unknown") == ""
    assert outcome_note_for("affirmed", appellant_role="") == ""
    assert outcome_note_for("affirmed") == ""


def test_analyze_case_impact_missing_role_omits_outcome_note():
    case = _case(holding="affirmed", outcome="affirmed")
    profile = analyze_case_impact(case)
    assert profile.outcome_note == ""


def test_analyze_case_impact_secretary_affirmed_is_favorable():
    case = _case(
        snippet="service connection",
        holding="affirmed",
        outcome="affirmed",
        appellant_role="secretary",
    )
    profile = analyze_case_impact(case)
    assert "Secretary's cross-appeal was affirmed" in profile.outcome_note


def test_analyze_case_impact_veteran_affirmed_is_unfavorable():
    case = _case(
        snippet="service connection",
        holding="affirmed",
        outcome="affirmed",
        appellant_role="veteran",
    )
    profile = analyze_case_impact(case)
    assert "affirmed, reinforcing" in profile.outcome_note


def test_detect_statutes_prefer_record_and_scan_fallback():
    record = _case(statutes=["38 U.S.C. § 5107(b)"], snippet="under 38 U.S.C. § 1110")
    assert detect_statutes(record) == ["38 U.S.C. § 5107(b)"]

    scanned = _case(snippet="under 38 U.S.C. § 1110 and 38 C.F.R. § 3.303")
    assert detect_statutes(scanned) == ["38 U.S.C. § 1110", "38 C.F.R. § 3.303"]


def test_statute_note_mapping():
    assert "benefit-of-the-doubt" in statute_note_for(["38 U.S.C. § 5107(b)"])
    assert "statutory anchors" in statute_note_for(["12 U.S.C. § 999"])
    assert statute_note_for([]) == ""


def test_statute_note_distinguishes_notice_from_duty_to_assist():
    notice = statute_note_for(["38 U.S.C. § 5103"])
    duty = statute_note_for(["38 U.S.C. § 5103A"])

    assert notice == (
        "The analysis engages the notice requirements of 38 U.S.C. § 5103, "
        "which can anchor briefing."
    )
    assert duty == (
        "The analysis engages the duty to assist under 38 U.S.C. § 5103A, "
        "which can anchor briefing."
    )
    assert notice != duty


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


def test_statute_note_is_subsection_aware():
    section_5107a = statute_note_for(["38 U.S.C. § 5107(a)"])
    section_7104a = statute_note_for(["38 U.S.C. § 7104(a)"])
    nested_5107 = statute_note_for(["38 U.S.C. § 5107(b)(1)"])
    nested_7104 = statute_note_for(["38 U.S.C. § 7104(d)(1)(A)"])

    assert "benefit-of-the-doubt" not in section_5107a
    assert "reasons-and-bases" not in section_7104a
    assert "benefit-of-the-doubt" in nested_5107
    assert "reasons-and-bases" in nested_7104


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


def test_authority_note_nonprecedential_cavc_is_persuasive():
    assert authority_note_for(CAVC, precedential=False) == (
        "As a non-precedential appellate decision, this ruling is persuasive only "
        "and is not binding on the Board or VA adjudicators."
    )


def test_authority_note_unknown_precedential_is_not_binding():
    note = authority_note_for(CAVC, precedential=None)
    assert "status" in note.lower()
    assert "unknown" in note.lower()
    assert "verify" in note.lower()
    assert "binding on the Board" not in note


def test_analyze_case_impact_nonprecedential_cavc_uses_persuasive_note():
    profile = analyze_case_impact(_case(court=CAVC, precedential=False))

    assert profile.authority_note == authority_note_for(CAVC, precedential=False)
    assert "persuasive only" in profile.nuance
    assert "As appellate precedent, this ruling is binding" not in profile.nuance


def test_analyze_case_impact_precedential_cavc_keeps_binding_note():
    profile = analyze_case_impact(_case(court=CAVC, precedential=True))

    assert profile.authority_note == authority_note_for(CAVC, precedential=True)


def test_nuance_layers_posture_statute_and_authority_notes():
    case = _case(
        snippet="service connection",
        holding="vacated and remanded",
        statutes=["38 U.S.C. § 7104(d)(1)"],
        court=CAVC,
        appellant_role="veteran",
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
        appellant_role="veteran",
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
