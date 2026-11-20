"""Tests for the nuanced impact-analysis layer (va_legal_agent.impact)."""

from va_legal_agent.impact import (
    MIXED_OUTCOME_NOTE,
    _statute_hint_matches,
    analyze_case_impact,
    authority_note_for,
    detect_issue_tags,
    detect_outcome,
    detect_statutes,
    outcome_note_for,
    statute_note_for,
)
from va_legal_agent.models import CaseRecord
from va_legal_agent.va_domain import build_case_abstract, extract_provision_impacts

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


def test_outcome_note_treats_in_part_as_mixed_exactly():
    assert outcome_note_for("granted in part", appellant_role="veteran") == MIXED_OUTCOME_NOTE


def test_outcome_note_splits_only_on_a_literal_space():
    # A tab-separated token is not a disposition signal; only the first
    # literal-space token may be classified.
    assert outcome_note_for("granted\tin", appellant_role="veteran") == ""


def test_outcome_note_uses_only_the_first_token():
    note = outcome_note_for("granted on appeal", appellant_role="veteran")
    assert "request was granted" in note


def test_outcome_note_deduplicates_repeated_signal():
    # Repeated signals map to the same note; keep one copy of the prose.
    note = outcome_note_for("vacated and vacated", appellant_role="veteran")
    assert note.count("The decision below was vacated") == 1


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


def test_statute_hint_matches_uppercase_subsection():
    assert _statute_hint_matches("5107", "38 U.S.C. § 5107(B)") is True
    assert _statute_hint_matches("5107", "38 U.S.C. § 5107(b)") is True


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
    assert authority_note_for(CAVC, precedential=None) == (
        "The precedential status of this appellate decision is unknown; verify its "
        "publication status before relying on it as binding."
    )


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


def test_aud4_provision_impact_objects_name_stage_effect_and_m21():
    text = (
        "We hold that the Board erred by failing to assign a staged rating under "
        "Fenderson v. West. The examiner did not consider the ameliorative effect "
        "of medication under Jones v. Shinseki. See 38 C.F.R. § 4.130 and "
        "Diagnostic Code 9411."
    )
    case = _case(title="Staged Rating Case", court=CAVC)
    case.case_abstract = build_case_abstract(text, case)
    case.provision_impacts = extract_provision_impacts(text, case)

    profile = analyze_case_impact(case)

    citations = {impact.citation for impact in profile.provision_impacts}
    assert "38 C.F.R. § 4.130" in citations
    assert "Diagnostic Code 9411" in citations
    assert "Staged rating (Fenderson/Hart)" in citations
    assert "Ameliorative effect of medication (Jones/McCarroll)" in citations
    assert all(impact.stage for impact in profile.provision_impacts)
    assert all(impact.legal_effect for impact in profile.provision_impacts)
    assert all(impact.provenance_anchor for impact in profile.provision_impacts)
    assert all(impact.m21_1_citations for impact in profile.provision_impacts)
    assert "Provision-level VBA impact" in profile.nuance


def test_aud4_medication_is_not_inferred_from_a_bare_medication_reference():
    text = "The record includes prescription medications and a medication list."
    case = _case(title="Bare Medication Case", court=CAVC)
    case.case_abstract = build_case_abstract(text, case)
    case.provision_impacts = extract_provision_impacts(text, case)

    assert not any(
        impact.medication_effect for impact in case.provision_impacts
    )


def test_impact_profile_retains_every_renderable_field():
    case = _case(
        snippet="service connection requires a nexus opinion",
        holding="The Board must provide reasons and bases.",
        outcome="remanded",
        statutes=["38 C.F.R. § 3.303"],
        appellant_role="veteran",
    )
    case.provision_impacts = extract_provision_impacts(
        "38 C.F.R. § 3.303 and Diagnostic Code 9411", case
    )

    profile = analyze_case_impact(case)

    assert profile.issue_tags
    assert profile.outcome == "remanded"
    assert profile.outcome_note
    assert profile.statutes == ["38 C.F.R. § 3.303"]
    assert profile.statute_note
    assert profile.authority_note
    assert profile.provision_impacts
    assert profile.nuance


def test_provision_impact_note_without_m21_citations():
    from va_legal_agent.impact import provision_impact_note
    from va_legal_agent.models import ProvisionImpact

    impact = ProvisionImpact(
        citation="38 U.S.C. § 1110",
        stage="appellate_review",
        legal_effect="clarifying",
        m21_1_citations=[],
    )
    note = provision_impact_note([impact])
    assert "38 U.S.C. § 1110" in note
    assert "->" not in note


def test_va_domain_sentence_helpers():
    import re

    from va_legal_agent.va_domain import _first_match_sentence, _sentences

    assert _sentences("") == []
    assert _sentences("   ") == []
    assert _first_match_sentence("First one. Second two.", re.compile(r"Second")) == (
        "Second two."
    )
    assert _first_match_sentence("First one.", re.compile(r"zzz")) == ""


def test_va_domain_effect_for_branches():
    from va_legal_agent.va_domain import STAGE_APPELLATE, ProvisionSpec, _effect_for

    spec = ProvisionSpec("38 U.S.C. § 1110", "statute", STAGE_APPELLATE, "clarifying")
    assert _effect_for("The rule is invalid.", spec) == "narrowing"
    assert _effect_for("The manual provision is invalid.", spec, m21=True) == (
        "invalidating_m21_1_rule"
    )
    assert _effect_for("The court cannot apply the manual rule.", spec) == "narrowing"
    assert _effect_for("The veteran is entitled to benefits.", spec) == "expanding"
    assert _effect_for("The Board must clarify the standard.", spec) == "clarifying"
    assert _effect_for("The court will remand.", spec) == "procedural"


def test_va_domain_extract_provision_impacts_empty_text():
    from va_legal_agent.models import CaseRecord
    from va_legal_agent.va_domain import extract_provision_impacts

    case = CaseRecord(
        title="Smith v. Wilkie",
        court="Court of Appeals for Veterans Claims",
        url="https://example.com/x",
    )
    assert extract_provision_impacts("", case) == []


def test_va_domain_impacts_from_cases_dedupes_same_citation_stage():
    from va_legal_agent.models import CaseRecord, ProvisionImpact
    from va_legal_agent.va_domain import impacts_from_cases

    impact = ProvisionImpact(citation="38 U.S.C. § 1110", stage="appellate_review")
    c1 = CaseRecord(title="A v. B", court="CAVC", url="https://example.com/a")
    c2 = CaseRecord(title="C v. D", court="CAVC", url="https://example.com/c")
    c1.provision_impacts = [impact]
    c2.provision_impacts = [impact]
    assert len(impacts_from_cases([c1, c2])) == 1


def test_va_domain_panel_and_precedential_status_edge_cases():
    from va_legal_agent.models import CaseRecord
    from va_legal_agent.va_domain import _panel_status, _precedential_status

    en_banc = CaseRecord(
        title="Smith v. Wilkie",
        court="Court of Appeals for Veterans Claims",
        url="https://example.com/x",
        snippet="The en banc court decided.",
        precedential=True,
    )
    assert _panel_status(en_banc) == "en_banc"
    assert _precedential_status(en_banc) == "en_banc"
    other = CaseRecord(
        title="X v. Y", court="Some Other Court", url="https://example.com/y"
    )
    assert _precedential_status(other) == "unknown"


def test_va_domain_history_status_unknown_and_authority_refs_dedupe():
    from va_legal_agent.models import CaseRecord
    from va_legal_agent.va_domain import _authority_refs, _history_status

    assert _history_status("zzz_unrecognized") == "unknown"
    case = CaseRecord(
        title="Smith v. Wilkie",
        court="CAVC",
        citation="1 Vet.App. 1",
        url="https://example.com/x",
    )
    assert len(_authority_refs([case, case])) == 1


def test_va_domain_build_case_abstract_empty_title_and_incomplete():
    from va_legal_agent.models import CaseRecord
    from va_legal_agent.va_domain import build_case_abstract

    case = CaseRecord(title="", court="CAVC", url="https://example.com/x")
    abstract = build_case_abstract("", case)
    assert abstract.abstract_complete is False
    assert abstract.abstract_source == "record_metadata"
    assert "Claimant" in abstract.claimant_and_conditions


def test_va_domain_m21_stage_appellate_and_intake():
    from va_legal_agent.models import CaseRecord
    from va_legal_agent.va_domain import (
        STAGE_APPELLATE,
        STAGE_INTAKE,
        extract_provision_impacts,
    )

    case = CaseRecord(
        title="Smith v. Wilkie", court="CAVC", url="https://example.com/x"
    )
    appellate = extract_provision_impacts("The Board must follow M21-1, Part X.", case)
    assert any(
        i.authority_type == "m21_1" and i.stage == STAGE_APPELLATE for i in appellate
    )
    intake = extract_provision_impacts("VA must provide notice under M21-1, Part III.", case)
    assert any(i.authority_type == "m21_1" and i.stage == STAGE_INTAKE for i in intake)


def test_va_domain_apply_citator_checks_history_branches():
    from va_legal_agent.models import (
        AppellateHistoryCheck,
        AppellateHistoryRecord,
        CitatorCheck,
        LegalAnalysis,
        NegativeTreatment,
        SurfacedCaseRecord,
    )
    from va_legal_agent.va_domain import apply_citator_checks

    def record(title, citation):
        return SurfacedCaseRecord(
            case_title=title, court="CAVC", reporter_citation=citation
        )

    verified_item = AppellateHistoryRecord(
        candidate_case="Later v. X",
        candidate_citation="2 Vet.App. 2",
        relationship="appeal",
        disposition="affirmed",
        scope="full",
        source_quality="official",
        source_url="https://example.com/h",
        reviewer="r",
        reviewed_at="2024-01-01",
        rationale="ok",
    )
    # A negative treatment that duplicates the verified history entry (dedupe).
    dup_treatment = NegativeTreatment(
        citing_case="Later v. X",
        citing_citation="2 Vet.App. 2",
        treatment="affirmed",
        excerpt="Later v. X affirmed the decision.",
        authoritative=True,
    )
    check_verified = CitatorCheck(
        target_case="Smith v. Wilkie",
        target_citation="1 Vet.App. 1",
        status="negative",
        appellate_history=AppellateHistoryCheck(
            target_case="Smith v. Wilkie", status="verified", verified_history=[verified_item]
        ),
        negative_treatments=[dup_treatment],
    )
    check_negative = CitatorCheck(
        target_case="Jones v. Y",
        target_citation="3 Vet.App. 3",
        status="negative",
        negative_treatments=[
            NegativeTreatment(
                citing_case="Other v. Z",
                citing_citation="4 Vet.App. 4",
                treatment="overruled",
                excerpt="Other v. Z overruled the decision.",
                authoritative=True,
            )
        ],
    )
    check_other = CitatorCheck(
        target_case="Other v. Q", target_citation="5 Vet.App. 5", status="not_assessed"
    )
    analysis = LegalAnalysis(
        issue="x",
        summary="s",
        how_it_affects_va_claims="h",
        surfaced_cases=[
            record("Smith v. Wilkie", "1 Vet.App. 1"),
            record("Jones v. Y", "3 Vet.App. 3"),
            record("Other v. Q", "5 Vet.App. 5"),
            record("No Match v. Nobody", "9 Vet.App. 9"),
        ],
    )

    apply_citator_checks(
        analysis, [check_verified, check_negative, check_other]
    )

    smith, jones, other, no_match = analysis.surfaced_cases
    assert smith.history_status == "verified_history"
    assert smith.citator_status == "negative"
    # The duplicated history entry collapsed to one.
    assert len(smith.appellate_history) == 1
    assert jones.history_status == "negative_treatment"
    assert other.history_status == "not_assessed"
    assert no_match.citator_status == "not_assessed"


def test_va_domain_rvsr_guidance_musculoskeletal_and_tdiu():
    from va_legal_agent.models import CaseRecord
    from va_legal_agent.va_domain import build_rvsr_guidance

    case = CaseRecord(
        title="Smith v. Wilkie",
        court="CAVC",
        url="https://example.com/x",
        snippet="The veteran has a knee condition and seeks TDIU individual unemployability.",
    )
    guidance = build_rvsr_guidance("service connection for knee", [case])
    assert any("Musculoskeletal" in item for item in guidance.evidence_review_checklist)
    assert any("TDIU" in item for item in guidance.rating_codesheet_actions)


def test_va_domain_claimant_condition_too_short_after_split_is_rejected():
    from va_legal_agent.models import CaseRecord
    from va_legal_agent.va_domain import _claimant_and_conditions

    case = CaseRecord(
        title="Smith v. Wilkie", court="CAVC", url="https://example.com/x"
    )
    # The captured condition collapses to "a" (len 1) after the split, so it is
    # rejected by the 2..140 length guard (va_domain.py 684->671).
    result = _claimant_and_conditions("service connection for a and assigned a rating", case)
    assert "No specific condition" in result


def test_provision_impact_note_caps_at_six_and_keeps_the_full_label():
    from va_legal_agent.impact import provision_impact_note
    from va_legal_agent.models import ProvisionImpact

    impacts = [
        ProvisionImpact(
            citation=f"38 U.S.C. § {1100 + i}",
            stage="appellate_review",
            legal_effect="clarifying",
            m21_1_citations=["M21-1, Part V"],
        )
        for i in range(7)
    ]

    note = provision_impact_note(impacts)

    expected = "Provision-level VBA impact: " + "; ".join(
        f"38 U.S.C. § {1100 + i} (appellate_review; clarifying) -> M21-1, Part V"
        for i in range(6)
    ) + "."
    assert note == expected


def test_outcome_note_for_uses_the_secretary_override_text():
    from va_legal_agent.impact import outcome_note_for

    secretary = outcome_note_for("affirmed", "secretary")

    assert secretary == (
        "The Secretary's cross-appeal was affirmed, meaning the veteran's "
        "favorable outcome below stands."
    )
    assert outcome_note_for("affirmed", "veteran") != secretary


def test_provision_provenance_requires_both_host_and_anchor():
    from va_legal_agent.va_domain import STAGE_APPELLATE, ProvisionSpec, _impact

    official = ProvisionSpec(
        "38 U.S.C. § 1110",
        "statute",
        STAGE_APPELLATE,
        "clarifying",
        source_url="https://uscode.house.gov/view.xhtml?req=1110",
    )
    anchor = "The Board must apply 38 U.S.C. § 1110 to basic entitlement."

    # Official host AND a citation-bearing anchor -> verified.
    assert _impact(official, anchor).provenance_verified is True
    # Official host but an EMPTY anchor -> not verified (host alone is not proof).
    assert _impact(official, "").provenance_verified is False
    # Real anchor on a SECONDARY host -> still not verified.
    secondary = ProvisionSpec(
        "38 U.S.C. § 1110",
        "statute",
        STAGE_APPELLATE,
        "clarifying",
        source_url="https://example.com/summary",
    )
    assert _impact(secondary, anchor).provenance_verified is False


def test_provision_alias_subsection_must_match_the_registry_spec():
    from va_legal_agent.models import CaseRecord
    from va_legal_agent.va_domain import extract_provision_impacts

    case = CaseRecord(
        title="Smith v. Wilkie", court="CAVC", url="https://uscourts.cavc.gov/x"
    )
    benefit_of_doubt = "38 U.S.C. § 5107(b)"

    # Bare alias (no subsection) still maps to the registry provision.
    bare = extract_provision_impacts("The Board applied 38 U.S.C. § 5107 here.", case)
    assert any(i.citation == benefit_of_doubt for i in bare)
    # Explicit matching subsection maps too.
    matching = extract_provision_impacts("The Board applied 38 U.S.C. § 5107(b).", case)
    assert any(i.citation == benefit_of_doubt for i in matching)
    # A DIFFERENT subsection is not the benefit-of-the-doubt provision.
    mismatched = extract_provision_impacts("The Board applied 38 U.S.C. § 5107(a).", case)
    assert not any(i.citation == benefit_of_doubt for i in mismatched)


def test_provision_impact_provenance_uses_retrieved_url_not_registry_url():
    """Item 7(b): Impact provenance_verified uses retrieved case.url, not registry source_url."""
    from va_legal_agent.va_domain import extract_provision_impacts
    from va_legal_agent.models import CaseRecord

    # Case with a secondary blog URL (not official) but citing an official provision
    blog_case = CaseRecord(
        title="Blog Post About VA Law",
        court="",
        url="https://law-blog.example.com/post-about-3-303",
        snippet="The Board applied 38 C.F.R. § 3.303 for service connection.",
        holding="The blog discusses 38 C.F.R. § 3.303.",
        decision_date="2024-01-01",
    )

    impacts = extract_provision_impacts(
        "The Board applied 38 C.F.R. § 3.303 for service connection.", blog_case
    )
    impact_303 = next((i for i in impacts if "3.303" in i.citation), None)
    assert impact_303 is not None
    # provenance_verified should be False because the retrieved URL (blog) is not official
    assert impact_303.provenance_verified is False
    # source_url should still be the canonical registry URL
    assert impact_303.source_url == "https://www.ecfr.gov/current/title-38/part-3/section-3.303"

    # Case with official CAVC URL (official) citing the same provision
    official_case = CaseRecord(
        title="Official CAVC Decision",
        court="Court of Appeals for Veterans Claims",
        url="https://www.uscourts.cavc.gov/opinion/12345",
        snippet="The Board applied 38 C.F.R. § 3.303 for service connection.",
        holding="The court applied 38 C.F.R. § 3.303 for service connection.",
        decision_date="2024-01-01",
    )

    impacts = extract_provision_impacts(
        "The Board applied 38 C.F.R. § 3.303 for service connection.", official_case
    )
    impact_303 = next((i for i in impacts if "3.303" in i.citation), None)
    assert impact_303 is not None
    # provenance_verified should be True because the retrieved URL (CAVC) is official
    assert impact_303.provenance_verified is True
    # source_url should still be the canonical registry URL
    assert impact_303.source_url == "https://www.ecfr.gov/current/title-38/part-3/section-3.303"

