"""Tests for the evidence-first citator adjudication queue."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from va_legal_agent.adjudication import (
    build_adjudication_queue,
    load_queue,
    record_decision,
    render_queue_text,
    save_queue,
)
from va_legal_agent.models import (
    CitatorCheck,
    CitatorCoverage,
    NegativeTreatment,
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
) -> NegativeTreatment:
    return NegativeTreatment(
        citing_case="Jones v. McDonough",
        citing_citation="33 Vet.App. 10",
        citing_court="Court of Appeals for Veterans Claims",
        citing_date="2021-02-02",
        citing_url=url,
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
) -> CitatorCheck:
    return CitatorCheck(
        target_case=target_case,
        target_citation=target_citation,
        target_url="https://www.courtlistener.com/opinion/111/",
        status=status,
        coverage=CitatorCoverage(complete=True),
        negative_treatments=authoritative or [],
        gated_treatment_findings=gated or [],
    )


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
