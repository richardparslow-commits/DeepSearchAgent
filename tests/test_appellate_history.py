"""Tests for the shadow appellate-history layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from va_legal_agent.appellate_history import (
    build_appellate_history,
    candidates_from_check,
    load_appellate_history_records,
)
from va_legal_agent.models import (
    AppellateHistoryRecord,
    CitatorCheck,
    LaterCitingOpinion,
    SameDocketDecision,
)


def _check(
    *,
    same_docket: list[SameDocketDecision] | None = None,
    later_citing: list[LaterCitingOpinion] | None = None,
) -> CitatorCheck:
    return CitatorCheck(
        target_case="Smith v. Wilkie",
        target_citation="30 Vet.App. 123",
        target_cluster_id="777",
        target_citations=["30 Vet.App. 123", "2020 WL 123456"],
        same_docket_decisions=same_docket or [],
        later_citing_opinions=later_citing or [],
    )


def _same_docket(
    *, cluster: str = "999", citation: str = "32 Vet.App. 9"
) -> SameDocketDecision:
    return SameDocketDecision(
        case_name="Smith v. McDonough",
        citation=citation,
        court="Court of Appeals for Veterans Claims",
        decision_date="2019-01-01",
        url="https://www.courtlistener.com/opinion/999/",
        cluster_id=cluster,
    )


def _record(**overrides) -> AppellateHistoryRecord:
    values = {
        "target_cluster_id": "777",
        "candidate_case": "Smith v. McDonough",
        "candidate_cluster_id": "999",
        "relationship": "direct_appeal",
        "disposition": "affirmed",
        "scope": "all",
        "source_quality": "official",
        "source_url": "https://www.cafc.uscourts.gov/opinions/1.pdf",
        "reviewer": "Reviewer",
        "reviewed_at": "2026-01-02T03:04:05+00:00",
        "rationale": "Checked the docket link and disposition.",
    }
    values.update(overrides)
    return AppellateHistoryRecord(**values)


def test_candidates_merge_signals_and_carry_review_reasons():
    check = _check(
        same_docket=[_same_docket(cluster="", citation="32 Vet.App. 9")],
        later_citing=[
            LaterCitingOpinion(
                case_name="Smith v. McDonough",
                citation="32 Vet.App. 9",
                court="Court of Appeals for Veterans Claims",
                decision_date="2019-01-01",
                url="https://www.courtlistener.com/opinion/999/",
                opinion_id="999",
            )
        ],
    )

    candidates = candidates_from_check(check)

    assert len(candidates) == 1
    assert candidates[0].signals == ["same_docket", "later_citing_opinion"]
    assert len(candidates[0].match_reasons) == 2
    assert candidates[0].verification_status == "candidate"
    assert candidates[0].relationship == "unknown"
    assert any("intra-court context" in note for note in candidates[0].coverage_notes)


def test_candidates_without_ids_still_use_a_stable_name_date_key():
    decision = SameDocketDecision(
        case_name="Smith v. McDonough",
        decision_date="2019-01-01",
    )

    candidates = candidates_from_check(_check(same_docket=[decision]))

    assert len(candidates) == 1
    assert candidates[0].candidate_case == "Smith v. McDonough"


def test_duplicate_signals_merge_without_duplicate_reasons_or_notes():
    decision = _same_docket()

    candidates = candidates_from_check(
        _check(same_docket=[decision, decision])
    )

    assert len(candidates) == 1
    assert candidates[0].signals == ["same_docket"]
    assert len(candidates[0].match_reasons) == 1
    assert len(candidates[0].coverage_notes) == 1


def test_no_candidates_is_not_assessed_and_never_no_history():
    history = build_appellate_history(_check())

    assert history.status == "not_assessed"
    assert history.candidates == []
    assert history.verified_history == []
    assert any("absence of a candidate" in flag for flag in history.flags)


def test_candidates_without_records_are_insufficient_source():
    history = build_appellate_history(_check(same_docket=[_same_docket()]))

    assert history.status == "insufficient_source"
    assert len(history.candidates) == 1
    assert history.candidates[0].verification_status == "candidate"
    assert history.verified_history == []
    assert any("no confirmed human record" in flag for flag in history.flags)


def test_confirmed_record_marks_candidate_verified_only_after_validation():
    record = _record(coverage_notes=["Checked the docket entry."])
    history = build_appellate_history(
        _check(same_docket=[_same_docket()]), [record, record]
    )

    assert history.status == "verified"
    assert len(history.verified_history) == 1
    assert history.verified_history[0].disposition == "affirmed"
    candidate = history.candidates[0]
    assert candidate.verification_status == "verified"
    assert candidate.relationship == "direct_appeal"
    assert candidate.source_quality == "official"
    assert candidate.reviewer == "Reviewer"
    assert "Checked the docket entry." in candidate.coverage_notes
    assert len(history.verified_history) == 1


def test_confirmed_record_without_existing_candidate_adds_a_verified_candidate():
    record = _record(candidate_cluster_id="", candidate_citation="32 Vet.App. 9")

    history = build_appellate_history(_check(), [record])

    assert history.status == "verified"
    assert len(history.verified_history) == 1
    assert history.candidates[0].signal_type == "verified_record"
    assert history.candidates[0].verification_status == "verified"


def test_record_can_match_a_candidate_by_citation_without_a_cluster():
    record = _record(candidate_cluster_id="", candidate_citation="32 Vet. App. 9")

    history = build_appellate_history(
        _check(same_docket=[_same_docket()]), [record]
    )

    assert history.status == "verified"
    assert len(history.candidates) == 1
    assert history.candidates[0].verification_status == "verified"


def test_record_with_a_different_candidate_cluster_is_not_attached():
    record = _record(candidate_cluster_id="888")

    history = build_appellate_history(
        _check(same_docket=[_same_docket()]), [record]
    )

    assert history.status == "verified"
    assert len(history.candidates) == 2
    assert {item.verification_status for item in history.candidates} == {
        "candidate",
        "verified",
    }


def test_record_candidate_citation_mismatch_creates_a_separate_candidate():
    record = _record(candidate_cluster_id="", candidate_citation="99 Vet.App. 1")

    history = build_appellate_history(
        _check(same_docket=[_same_docket()]), [record]
    )

    assert history.status == "verified"
    assert len(history.candidates) == 2


def test_record_matches_by_normalized_target_citation():
    record = _record(target_cluster_id="", target_citation="30 Vet. App. 123")

    history = build_appellate_history(
        _check(same_docket=[_same_docket()]), [record]
    )

    assert history.status == "verified"
    assert history.candidates[0].verification_status == "verified"


def test_incomplete_record_never_becomes_a_label():
    record = _record(source_url="", reviewer="", scope="unknown")

    history = build_appellate_history(
        _check(same_docket=[_same_docket()]), [record]
    )

    assert history.status == "insufficient_source"
    assert history.verified_history == []
    assert history.candidates[0].verification_status == "insufficient_source"
    assert any("is insufficient" in flag for flag in history.flags)
    assert any("source URL" in flag for flag in history.flags)


@pytest.mark.parametrize(
    ("updates", "phrase"),
    [
        ({"candidate_case": ""}, "candidate case name"),
        ({"candidate_cluster_id": "", "candidate_citation": ""}, "candidate cluster id"),
        ({"reviewed_at": ""}, "reviewed_at"),
        ({"rationale": ""}, "rationale"),
        ({"review_status": "maybe"}, "review_status"),
        ({"relationship": "unknown"}, "relationship"),
        ({"disposition": "unknown"}, "disposition"),
        ({"source_quality": "unverified"}, "source_quality"),
    ],
)
def test_incomplete_records_report_the_missing_verification_field(updates, phrase):
    record = _record(**updates)

    history = build_appellate_history(
        _check(same_docket=[_same_docket()]), [record]
    )

    assert history.status == "insufficient_source"
    assert history.verified_history == []
    assert any(phrase in flag for flag in history.flags)


def test_incomplete_record_without_matching_candidate_is_only_a_flag():
    record = _record(
        candidate_cluster_id="",
        candidate_citation="",
        source_quality="unverified",
    )

    history = build_appellate_history(_check(), [record])

    assert history.status == "not_assessed"
    assert history.candidates == []
    assert history.verified_history == []
    assert any("source_quality" in flag for flag in history.flags)


def test_confirmed_no_relationship_record_is_not_a_history_label():
    record = _record(relationship="no_relationship")

    history = build_appellate_history(
        _check(same_docket=[_same_docket()]), [record]
    )

    assert history.status == "insufficient_source"
    assert history.verified_history == []
    assert history.candidates[0].verification_status == "rejected"
    assert any("no appellate relationship" in flag for flag in history.flags)


def test_rejected_record_is_retained_without_becoming_a_history_label():
    record = _record(review_status="rejected", rationale="No appellate relationship.")

    history = build_appellate_history(
        _check(same_docket=[_same_docket()]), [record]
    )

    assert history.status == "insufficient_source"
    assert history.verified_history == []
    assert history.candidates[0].verification_status == "rejected"
    assert any("was rejected" in flag for flag in history.flags)


def test_record_for_another_target_is_ignored():
    record = _record(target_cluster_id="888")

    history = build_appellate_history(
        _check(same_docket=[_same_docket()]), [record]
    )

    assert history.status == "insufficient_source"
    assert history.candidates[0].verification_status == "candidate"
    assert history.verified_history == []


def test_load_records_accepts_array_and_records_object(tmp_path: Path):
    record = _record().model_dump(mode="json")
    array_path = tmp_path / "array.json"
    array_path.write_text(json.dumps([record]), encoding="utf-8")
    object_path = tmp_path / "object.json"
    object_path.write_text(json.dumps({"records": [record]}), encoding="utf-8")

    assert load_appellate_history_records(array_path)[0].candidate_case == (
        "Smith v. McDonough"
    )
    assert load_appellate_history_records(object_path)[0].reviewer == "Reviewer"


def test_load_records_rejects_missing_or_non_list_payload(tmp_path: Path):
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({}), encoding="utf-8")
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"records": "not a list"}), encoding="utf-8")

    with pytest.raises(ValueError, match="records array"):
        load_appellate_history_records(missing)
    with pytest.raises(ValueError, match="records array"):
        load_appellate_history_records(wrong)
