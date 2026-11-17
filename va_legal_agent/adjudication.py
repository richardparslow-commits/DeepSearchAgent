"""Evidence-first human adjudication queue for citator findings.

The citator is advisory. This module turns each verbatim treatment finding into
a review item whose evidence is serialized and rendered *before* the automated
proposal. A human decision is stored separately from that evidence, requires a
reviewer and rationale, and never changes ``CitatorCheck.status`` or ranking.
Findings whose sources were blocked by enforcement stay on the citator check
and in this queue as low-priority, withheld items for audit.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    AdjudicationEvidence,
    AdjudicationItem,
    AdjudicationProposal,
    AdjudicationQueue,
    AdjudicationReview,
    CaseRecord,
    CitatorCheck,
    NegativeTreatment,
    RvsrGuidance,
    SeniorRaterGuidance,
    SourceValiditySignal,
)

DECISIONS = frozenset({"confirmed", "rejected", "deferred"})


def rvsr_operational_playbook(issue: str, cases: list[CaseRecord]) -> RvsrGuidance:
    """Return the RVSR evidence/exam/codesheet/reasons playbook.

    Kept here alongside the reviewer workflow so the human adjudication queue
    and the role guidance use the same case records; generation stays in the
    deterministic VBA domain registry.
    """
    from .va_domain import build_rvsr_guidance

    return build_rvsr_guidance(issue, cases)


def senior_rater_operational_playbook(
    issue: str, cases: list[CaseRecord]
) -> SeniorRaterGuidance:
    """Return the Senior RVSR/DRO HLR, DTA, CUE, precedent, and NQR playbook."""
    from .va_domain import build_senior_rater_guidance

    return build_senior_rater_guidance(issue, cases)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _item_id(
    finding: NegativeTreatment, target_case: str, target_citation: str, disposition: str
) -> str:
    material = "\x1f".join(
        (
            target_case,
            target_citation,
            finding.citing_url,
            finding.treatment,
            finding.excerpt,
            disposition,
        )
    )
    return "adj-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _blocked_source_reason(signal: SourceValiditySignal) -> str:
    """Describe why enforcement blocked a source without hiding the finding."""
    detail = signal.reason.strip() or f"status {signal.status}"
    if signal.withheld:
        detail += " (source withheld from enforcement)"
    return detail


def _queue_item(
    check: CitatorCheck,
    finding: NegativeTreatment,
    *,
    authoritative: bool,
    blocking_signal: SourceValiditySignal | None = None,
) -> AdjudicationItem:
    if blocking_signal is not None:
        disposition = "withheld"
        priority = "low"
        reason = (
            "Automated source-validity enforcement blocked this finding from "
            f"authoritative use ({_blocked_source_reason(blocking_signal)}); "
            "it is retained for audit and requires source review."
        )
    elif authoritative:
        disposition = "negative"
        priority = "high"
        reason = (
            "Automated citator proposed authoritative negative treatment; "
            "verify the excerpt and the opinion's authority before relying on it."
        )
    else:
        disposition = "gated"
        gate = finding.gate_reason or "opinion role or precedential status"
        priority = "normal"
        reason = f"Automated citator gated this finding: {gate}."
    return AdjudicationItem(
        id=_item_id(
            finding,
            check.target_case,
            check.target_citation,
            disposition,
        ),
        kind="treatment_finding",
        priority=priority,
        queue_reason=reason,
        evidence=AdjudicationEvidence(
            target_case=check.target_case,
            target_citation=check.target_citation,
            target_url=check.target_url,
            citing_case=finding.citing_case,
            citing_citation=finding.citing_citation,
            citing_court=finding.citing_court,
            citing_date=finding.citing_date,
            citing_url=finding.citing_url,
            source_opinion_id=finding.source_opinion_id,
            source_cluster_id=finding.source_cluster_id,
            opinion_type=finding.opinion_type,
            opinion_role=finding.opinion_role,
            precedential_status=finding.precedential_status,
            source_validity_blocked=blocking_signal is not None,
            excerpt=finding.excerpt,
        ),
        proposal=AdjudicationProposal(
            disposition=disposition,
            status=check.status,
            treatment=finding.treatment,
            scope=finding.scope,
            gate_reason=finding.gate_reason,
        ),
    )


def build_adjudication_queue(
    checks: list[CitatorCheck], *, source: str = ""
) -> AdjudicationQueue:
    """Build a de-duplicated evidence-first queue from citator checks.

    Both authoritative and gated treatment findings are queued. Coverage gaps
    without a verbatim finding are left in the citator flags rather than
    presented as evidence they do not contain. Queueing never changes any
    citator status or ranking value. When source-validity enforcement blocks a
    finding's source, the finding stays in this review queue as a low-priority
    item marked ``source_validity_blocked`` and ``withheld`` so it remains
    auditable without being mistaken for authoritative support.
    """
    items: list[AdjudicationItem] = []
    seen: set[str] = set()
    for check in checks:
        for finding in check.negative_treatments:
            item = _queue_item(
                check,
                finding,
                authoritative=True,
                blocking_signal=check.source_validity_blocking_signal(
                    finding.source_opinion_id
                ),
            )
            if item.id not in seen:
                seen.add(item.id)
                items.append(item)
        for finding in check.gated_treatment_findings:
            item = _queue_item(
                check,
                finding,
                authoritative=False,
                blocking_signal=check.source_validity_blocking_signal(
                    finding.source_opinion_id
                ),
            )
            if item.id not in seen:
                seen.add(item.id)
                items.append(item)
    items.sort(
        key=lambda item: (
            item.priority != "high",
            item.priority == "low",
            item.evidence.target_case.casefold(),
            item.evidence.citing_case.casefold(),
            item.id,
        )
    )
    return AdjudicationQueue(
        generated_at=_now(),
        source=source,
        items=items,
    )


def save_queue(queue: AdjudicationQueue, path: str | Path) -> None:
    """Atomically write *queue* as readable, evidence-first JSON."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            json.dump(queue.model_dump(mode="json"), temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def load_queue(path: str | Path) -> AdjudicationQueue:
    """Load and validate a saved adjudication queue."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return AdjudicationQueue.model_validate(payload)


def record_decision(
    path: str | Path,
    item_id: str,
    *,
    decision: str,
    reviewer: str,
    rationale: str,
    reviewed_at: str | None = None,
) -> AdjudicationItem:
    """Record a human decision while preserving the original evidence."""
    if decision not in DECISIONS:
        raise ValueError(
            f"decision must be one of {', '.join(sorted(DECISIONS))}"
        )
    reviewer = reviewer.strip()
    rationale = rationale.strip()
    if not reviewer:
        raise ValueError("reviewer is required")
    if not rationale:
        raise ValueError("rationale is required")

    queue = load_queue(path)
    item = next((candidate for candidate in queue.items if candidate.id == item_id), None)
    if item is None:
        raise KeyError(f"unknown adjudication item: {item_id}")
    if item.review.status != "pending":
        item.history.append(item.review.model_copy(deep=True))
    item.review = AdjudicationReview(
        status=decision,
        reviewer=reviewer,
        reviewed_at=reviewed_at or _now(),
        rationale=rationale,
    )
    save_queue(queue, path)
    return item


def _item_summary(item: AdjudicationItem) -> str:
    evidence = item.evidence
    citation = evidence.citing_citation or "no citation"
    date = evidence.citing_date or "date unknown"
    source = evidence.citing_url or "source unavailable"
    withheld = " [source withheld]" if evidence.source_validity_blocked else ""
    return "\n".join(
        (
            f"[{item.priority}] {item.id} ({item.review.status}){withheld}",
            f'  EVIDENCE: "{evidence.excerpt}"',
            f"  Citing: {evidence.citing_case} ({citation}; {date})",
            f"  Source: {source}",
        )
    )


def _item_detail(item: AdjudicationItem) -> str:
    evidence = item.evidence
    proposal = item.proposal
    review = item.review
    withheld = (
        [
            "",
            "SOURCE-VALIDITY ENFORCEMENT",
            "  Withheld from authoritative use; retained for audit.",
        ]
        if evidence.source_validity_blocked
        else []
    )
    lines = [
        f"ITEM {item.id} [{review.status}] priority={item.priority}",
        "",
        "EVIDENCE (read this before the proposal)",
        f'  Excerpt: "{evidence.excerpt}"',
        (
            f"  Target: {evidence.target_case} "
            f"({evidence.target_citation or 'no citation'})"
        ),
        (
            f"  Citing opinion: {evidence.citing_case} "
            f"({evidence.citing_citation or 'no citation'}; "
            f"{evidence.citing_date or 'date unknown'}; "
            f"{evidence.citing_court or 'court unknown'})"
        ),
        (
            f"  Authority metadata: role={evidence.opinion_role}; "
            f"status={evidence.precedential_status or 'unknown'}; "
            f"type={evidence.opinion_type or 'unknown'}"
        ),
        f"  Source: {evidence.citing_url or 'source unavailable'}",
        f"  Target source: {evidence.target_url or 'source unavailable'}",
        "",
        "WHY QUEUED",
        f"  {item.queue_reason}",
        *withheld,
        "",
        "AUTOMATED PROPOSAL (not a final label)",
        f"  Disposition: {proposal.disposition}",
        f"  Citator status: {proposal.status}",
        f"  Treatment: {proposal.treatment or 'none'}",
        f"  Scope: {proposal.scope or 'unclear'}",
        f"  Gate reason: {proposal.gate_reason or 'none'}",
        "",
        "REVIEW",
        f"  Status: {review.status}",
        f"  Reviewer: {review.reviewer or 'unreviewed'}",
        f"  Reviewed: {review.reviewed_at or 'not reviewed'}",
        f"  Rationale: {review.rationale or 'none'}",
        f"  Prior decisions retained: {len(item.history)}",
    ]
    return "\n".join(lines)


def render_queue_text(
    queue: AdjudicationQueue,
    *,
    item_id: str | None = None,
    include_reviewed: bool = False,
) -> str:
    """Render evidence-first text; proposals appear only after the evidence."""
    if item_id is not None:
        item = next((candidate for candidate in queue.items if candidate.id == item_id), None)
        if item is None:
            raise KeyError(f"unknown adjudication item: {item_id}")
        return _item_detail(item)
    items = [
        item
        for item in queue.items
        if include_reviewed or item.review.status == "pending"
    ]
    if not items:
        return "No pending adjudication items."
    return "\n\n".join(_item_summary(item) for item in items)
