"""Tests for the evidence-first citator adjudication queue."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from va_legal_agent.adjudication import (
    build_adjudication_guidance,
    build_adjudication_queue,
    rvsr_operational_playbook,
    senior_rater_operational_playbook,
    load_queue,
    record_decision,
    render_queue_text,
    save_queue,
)
from va_legal_agent.models import (
    CaseRecord,
    CitatorCheck,
    CitatorCoverage,
    NegativeTreatment,
    SourceValiditySignal,
)


def _treatment(
    *,
    claim: str = "Smith v. Wilkie",
    treatment: str = "overruled",
    scope: str = "all",
    role: str = "majority",
    status: str = "Published",
    authoritative: bool = True,
    gate_reason: str = "",
    url: str = "https://www.courtlistener.com/opinion/201/",
    source_opinion_id: str = "",
    source_cluster_id: str = "",
) -> NegativeTreatment:
    return NegativeTreatment(
        citing_case="Jones v. McDonough",
        citing_citation="33 Vet.App. 10",
        citing_court="Court of Appeals for Veterans Claims",
        citing_date="2021-02-02",
        citing_url=url,
        source_opinion_id=source_opinion_id,
        source_cluster_id=source_cluster_id,
        treatment=treatment,
        scope=scope,
        binding=True,
        confidence="high",
        opinion_type="020lead" if authoritative else "040dissent",
        opinion_role=role,
        precedential_status=status,
        precedential_tier="precedential" if status == "Published" else "unknown",
        authoritative=authoritative,
        gate_reason=gate_reason,
        excerpt=f"The court overruled {claim}, 30 Vet.App. 123.",
    )


def _check(
    *,
    authoritative: list[NegativeTreatment] | None = None,
    gated: list[NegativeTreatment] | None = None,
    status: str = "negative",
    target_case: str = "Smith v. Wilkie",
    target_citation: str = "30 Vet.App. 123",
    source_validity: list[SourceValiditySignal] | None = None,
    source_validity_enforced: bool = False,
) -> CitatorCheck:
    return CitatorCheck(
        target_case=target_case,
        target_citation=target_citation,
        target_url="https://www.courtlistener.com/opinion/111/",
        status=status,
        coverage=CitatorCoverage(complete=True),
        negative_treatments=authoritative or [],
        gated_treatment_findings=gated or [],
        source_validity=source_validity or [],
        source_validity_enforced=source_validity_enforced,
    )


def test_queue_evidence_carries_source_opinion_identifiers():
    queue = build_adjudication_queue(
        [
            _check(
                authoritative=[
                    _treatment(
                        source_opinion_id="201", source_cluster_id="888"
                    )
                ]
            )
        ]
    )

    evidence = queue.items[0].evidence
    assert evidence.source_opinion_id == "201"
    assert evidence.source_cluster_id == "888"


def _enforced_check() -> CitatorCheck:
    return _check(
        authoritative=[
            _treatment(source_opinion_id="201"),
            _treatment(source_opinion_id="203"),
        ],
        gated=[
            _treatment(
                source_opinion_id="202",
                url="https://www.courtlistener.com/opinion/202/",
                authoritative=False,
                role="separate",
                gate_reason="opinion role is separate",
            )
        ],
        source_validity=[
            SourceValiditySignal(
                source_opinion_id="201",
                status="invalid",
                precedence="authoritative",
                withheld=False,
            ),
            SourceValiditySignal(
                source_opinion_id="202",
                status="precedence_defect",
                precedence="persuasive",
                withheld=True,
            ),
            SourceValiditySignal(
                source_opinion_id="203",
                status="valid",
                precedence="authoritative",
                withheld=False,
            ),
        ],
        source_validity_enforced=True,
    )


def test_enforcement_marks_blocked_sources_withheld_in_adjudication_queue():
    check = _enforced_check()

    queue = build_adjudication_queue([check])
    by_source = {
        item.evidence.source_opinion_id: item for item in queue.items
    }

    assert set(by_source) == {"201", "202", "203"}
    assert queue.items[0].evidence.source_opinion_id == "203"
    for source_id in ("201", "202"):
        item = by_source[source_id]
        assert item.priority == "low"
        assert item.evidence.source_validity_blocked is True
        assert item.proposal.disposition == "withheld"
        assert "source-validity enforcement blocked" in item.queue_reason
    assert by_source["203"].priority == "high"
    assert by_source["203"].evidence.source_validity_blocked is False
    assert by_source["203"].proposal.disposition == "negative"
    assert [
        finding.source_opinion_id for finding in check.negative_treatments
    ] == ["201", "203"]
    assert [
        finding.source_opinion_id
        for finding in check.gated_treatment_findings
    ] == ["202"]


def test_advisory_source_validity_does_not_mark_queue_items_withheld():
    blocked = _treatment(source_opinion_id="201")
    check = _check(
        authoritative=[blocked],
        source_validity=[
            SourceValiditySignal(
                source_opinion_id="201",
                status="invalid",
                precedence="authoritative",
                withheld=False,
            )
        ],
    )

    queue = build_adjudication_queue([check])

    assert [item.evidence.source_opinion_id for item in queue.items] == ["201"]
    assert queue.items[0].evidence.source_validity_blocked is False
    assert queue.items[0].proposal.disposition == "negative"


def test_build_queue_is_evidence_first_and_prioritizes_authoritative_findings():
    queue = build_adjudication_queue(
        [
            _check(
                authoritative=[_treatment()],
                gated=[
                    _treatment(
                        treatment="criticized",
                        scope="unclear",
                        role="separate",
                        status="Unpublished",
                        authoritative=False,
                        gate_reason="opinion role is separate",
                        url="https://www.courtlistener.com/opinion/202/",
                    )
                ],
            )
        ],
        source="test-run",
    )

    assert queue.source == "test-run"
    assert len(queue.items) == 2
    authoritative, gated = queue.items
    assert authoritative.priority == "high"
    assert authoritative.proposal.disposition == "negative"
    assert authoritative.evidence.excerpt.startswith("The court overruled")
    assert authoritative.evidence.target_case == "Smith v. Wilkie"
    assert authoritative.review.status == "pending"
    assert gated.priority == "normal"
    assert gated.proposal.disposition == "gated"
    assert gated.proposal.gate_reason == "opinion role is separate"
    assert "gated this finding" in gated.queue_reason
    # The serialized order must keep the evidence before the proposal.
    serialized = authoritative.model_dump(mode="json")
    assert list(serialized).index("evidence") < list(serialized).index("proposal")
    assert list(serialized).index("proposal") < list(serialized).index("queue_reason")


def test_build_queue_deduplicates_identical_findings_and_handles_no_findings():
    duplicate = _treatment()
    gated_duplicate = _treatment(authoritative=False, role="separate")
    queue = build_adjudication_queue(
        [
            _check(authoritative=[duplicate]),
            _check(authoritative=[duplicate]),
            _check(gated=[gated_duplicate]),
            _check(gated=[gated_duplicate]),
        ]
    )

    assert len(queue.items) == 2
    assert build_adjudication_queue([]).items == []


def test_withheld_source_validity_marker_round_trips(tmp_path: Path):
    path = tmp_path / "review.json"
    save_queue(build_adjudication_queue([_enforced_check()]), path)

    queue = load_queue(path)
    withheld = [
        item for item in queue.items if item.evidence.source_validity_blocked
    ]

    assert {item.evidence.source_opinion_id for item in withheld} == {
        "201",
        "202",
    }
    assert all(item.proposal.disposition == "withheld" for item in withheld)
    assert all(item.priority == "low" for item in withheld)
    detail = render_queue_text(
        queue,
        item_id=next(
            item.id
            for item in withheld
            if item.evidence.source_opinion_id == "201"
        ),
    )
    assert "SOURCE-VALIDITY ENFORCEMENT" in detail
    assert "Withheld from authoritative use" in detail


def test_save_queue_cleans_up_a_failed_atomic_write(tmp_path: Path, monkeypatch):
    queue = build_adjudication_queue([])

    def fail_replace(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr("va_legal_agent.adjudication.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        save_queue(queue, tmp_path / "review.json")
    assert list(tmp_path.glob(".review.json.*.tmp")) == []


def test_save_load_and_record_human_decision_preserves_evidence(tmp_path: Path):
    queue = build_adjudication_queue([_check(authoritative=[_treatment()])])
    path = tmp_path / "review.json"
    save_queue(queue, path)
    loaded = load_queue(path)
    item_id = loaded.items[0].id
    original_evidence = loaded.items[0].evidence.model_dump()

    reviewed = record_decision(
        path,
        item_id,
        decision="confirmed",
        reviewer="  A. Reviewer  ",
        rationale="  Checked the excerpt against the opinion.  ",
        reviewed_at="2026-01-02T03:04:05+00:00",
    )

    assert reviewed.review.status == "confirmed"
    assert reviewed.review.reviewer == "A. Reviewer"
    assert reviewed.review.rationale == "Checked the excerpt against the opinion."
    assert reviewed.review.reviewed_at == "2026-01-02T03:04:05+00:00"
    reloaded = load_queue(path)
    assert reloaded.items[0].review.status == "confirmed"
    assert reloaded.items[0].evidence.model_dump() == original_evidence

    revised = record_decision(
        path,
        item_id,
        decision="rejected",
        reviewer="Second Reviewer",
        rationale="The quoted sentence is dicta.",
    )
    assert revised.review.status == "rejected"
    assert [entry.status for entry in revised.history] == ["confirmed"]
    assert load_queue(path).items[0].history[0].reviewer == "A. Reviewer"


def test_record_decision_rejects_invalid_input(tmp_path: Path):
    path = tmp_path / "review.json"
    save_queue(build_adjudication_queue([_check(authoritative=[_treatment()])]), path)
    item_id = load_queue(path).items[0].id

    with pytest.raises(ValueError, match="decision must be"):
        record_decision(path, item_id, decision="maybe", reviewer="R", rationale="x")
    with pytest.raises(ValueError, match="reviewer is required"):
        record_decision(path, item_id, decision="confirmed", reviewer=" ", rationale="x")
    with pytest.raises(ValueError, match="rationale is required"):
        record_decision(path, item_id, decision="confirmed", reviewer="R", rationale=" ")
    with pytest.raises(KeyError, match="unknown adjudication item"):
        record_decision(path, "missing", decision="confirmed", reviewer="R", rationale="x")


def test_queue_text_renders_evidence_before_proposal_and_filters_reviewed(tmp_path: Path):
    path = tmp_path / "review.json"
    save_queue(build_adjudication_queue([_check(authoritative=[_treatment()])]), path)
    queue = load_queue(path)
    item_id = queue.items[0].id

    detail = render_queue_text(queue, item_id=item_id)

    assert detail.index("EVIDENCE") < detail.index("AUTOMATED PROPOSAL")
    assert "The court overruled Smith v. Wilkie" in detail
    assert f"ITEM {item_id} [pending]" in detail
    assert "Target source:" in detail

    summary = render_queue_text(queue)
    assert "EVIDENCE:" in summary
    assert "AUTOMATED PROPOSAL" not in summary

    record_decision(path, item_id, decision="rejected", reviewer="R", rationale="Not adopted.")
    reviewed = load_queue(path)
    assert render_queue_text(reviewed) == "No pending adjudication items."
    assert "rejected" in render_queue_text(reviewed, include_reviewed=True)


def test_queue_text_rejects_unknown_item(tmp_path: Path):
    queue = build_adjudication_queue([])
    path = tmp_path / "empty.json"
    save_queue(queue, path)

    with pytest.raises(KeyError, match="unknown adjudication item"):
        render_queue_text(load_queue(path), item_id="missing")
    assert json.loads(path.read_text(encoding="utf-8"))["items"] == []


def test_aud4_adjudication_exposes_both_operational_playbooks():
    case = CaseRecord(
        title="CUE v. McDonough",
        court="Court of Appeals for Veterans Claims",
        holding=(
            "The Board must address the DRO's CUE finding separately under "
            "38 C.F.R. § 3.105(a) and 38 C.F.R. § 3.2600, and must assess a "
            "staged rating under Fenderson v. West."
        ),
        url="https://uscourts.cavc.gov/opinions/cue",
        source_reliability="official",
    )

    rvsr = rvsr_operational_playbook("CUE and rating", [case])
    senior = senior_rater_operational_playbook("CUE and rating", [case])

    assert rvsr.rating_codesheet_actions
    assert any("staged" in item.lower() for item in rvsr.rating_codesheet_actions)
    assert senior.cue_analysis
    assert any("3.105" in item for item in senior.cue_analysis)
    assert any("CUE finding" in item for item in senior.hlr_de_novo_focus)


def test_aud4_vba_adjudication_workflows_cover_rvsr_hlr_cue_and_qrs():
    case = CaseRecord(
        title="CUE v. McDonough",
        court="Court of Appeals for Veterans Claims",
        holding=(
            "The Board must address an inadequate examination under 38 C.F.R. "
            "§ 3.159, a DRO CUE finding under 38 C.F.R. § 3.105(a), and an HLR "
            "under 38 C.F.R. § 3.2601. A staged rating is required under "
            "Fenderson v. West. George v. McDonough bars treating a later "
            "change in interpretation as CUE."
        ),
        url="https://uscourts.cavc.gov/opinions/cue",
        source_reliability="official",
        precedential=True,
    )

    guidance = build_adjudication_guidance("CUE, HLR, and staged rating", [case])

    # RVSR workflow: evidence, lay competence, exam adequacy, codesheet,
    # effective dates, and reasons for decision are all explicit.
    assert guidance.rvsr.evidence_review_checklist
    assert any("5107(b)" in item for item in guidance.rvsr.lay_evidence_competence)
    assert any("lay" in item.lower() for item in guidance.rvsr.lay_evidence_competence)
    assert guidance.rvsr.duty_to_assist_exam_actions
    assert any("3.159" in item for item in guidance.rvsr.duty_to_assist_exam_actions)
    assert guidance.rvsr.rating_codesheet_actions
    assert any("staged" in item.lower() for item in guidance.rvsr.rating_codesheet_actions)
    assert guidance.rvsr.effective_date_actions
    assert any("3.400" in item for item in guidance.rvsr.effective_date_actions)
    assert guidance.rvsr.reasons_for_decision_requirements

    # Senior Rater/DRO: HLR is de novo, DTA errors return, CUE is time-bound,
    # and binding precedent controls an unrevised M21-1 provision.
    assert any("3.2601" in item for item in guidance.senior_rater_dro.hlr_de_novo_actions)
    assert any("de novo" in item.lower() for item in guidance.senior_rater_dro.hlr_de_novo_actions)
    assert guidance.senior_rater_dro.dta_error_returns
    assert any("return" in item.lower() for item in guidance.senior_rater_dro.dta_error_returns)
    assert any("3.105(a)" in item for item in guidance.senior_rater_dro.cue_analysis)
    assert any("George" in item for item in guidance.senior_rater_dro.cue_analysis)
    assert any("20.1403" in item for item in guidance.senior_rater_dro.cue_analysis)
    assert any(
        "M21-1" in item and "precedent" in item.lower()
        for item in guidance.senior_rater_dro.precedent_m21_conflict_resolution
    )
    assert guidance.senior_rater_dro.dro_disposition_actions

    # QRS workflow: pre-promulgation signatures and NQR/QRT error calls.
    assert guidance.qrs.second_signature_checkpoints
    assert any("diagnostic" in item.lower() for item in guidance.qrs.second_signature_checkpoints)
    assert guidance.qrs.nqr_qrt_error_calls
    assert any("duty-to-assist" in item.lower() for item in guidance.qrs.nqr_qrt_error_calls)
    assert guidance.qrs.sample_review_focus
    assert guidance.source_authorities


def test_aud4_citator_queue_api_is_unchanged_by_vba_workflow_engine():
    """The queue remains an independent evidence-review process."""
    queue = build_adjudication_queue([_check(authoritative=[_treatment()])])
    assert len(queue.items) == 1
    assert queue.items[0].kind == "treatment_finding"
    assert queue.items[0].proposal is not None
