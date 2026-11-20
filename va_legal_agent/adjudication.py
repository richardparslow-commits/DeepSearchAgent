"""Evidence-first citator queue and VBA compensation adjudication workflows.

The first half of this module is the citator human-review queue: the citator is
advisory, each verbatim treatment finding is serialized and rendered *before*
the automated proposal, and a human decision is stored separately from that
evidence, requires a reviewer and rationale, and never changes
``CitatorCheck.status`` or ranking. Findings whose sources were blocked by
enforcement stay on the citator check and in the queue as low-priority,
withheld items for audit.

The second half builds structured VBA workflows for Rating Veterans Service
Representatives (RVSR), Senior Raters/Decision Review Officers (DRO), and
Quality Review Specialists (QRS): HLR under 38 C.F.R. § 3.2601, duty-to-assist
returns, CUE under 38 C.F.R. § 3.105(a)/38 U.S.C. § 5109A, precedent-versus-
M21-1 conflicts, and pre-promulgation/NQR quality checkpoints. The queue and
the workflows share case records but never alter each other's status.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    AdjudicationEvidence,
    AdjudicationItem,
    AdjudicationProposal,
    AdjudicationQueue,
    AdjudicationReview,
    AdjudicationRoleGuidance,
    CaseRecord,
    CitatorCheck,
    NegativeTreatment,
    QrsAuditWorkflow,
    RvsrAdjudicationWorkflow,
    RvsrGuidance,
    SeniorRaterDroWorkflow,
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


def build_adjudication_guidance(
    issue: str, cases: list[CaseRecord]
) -> AdjudicationRoleGuidance:
    """Build structured RVSR, Senior Rater/DRO, and QRS VBA workflows.

    The citator queue above is a separate evidence-review process.  This
    function is the compensation-claims adjudication engine: it converts the
    deterministic role playbooks into explicit RVSR, HLR/CUE, and quality-
    review actions that the report and web UI can render and persist.
    """
    rvsr = rvsr_operational_playbook(issue, cases)
    senior = senior_rater_operational_playbook(issue, cases)

    lay_competence = [
        item
        for item in rvsr.evidence_review_checklist
        if any(
            phrase in item.lower()
            for phrase in ("lay", "competent", "credib", "jandreau", "buchanan")
        )
    ]
    lay_competence.insert(
        0,
        "Apply 38 U.S.C. § 5107(b): resolve every material, approximate-balance "
        "question in favor of the veteran after weighing the competent evidence; "
        "do not use benefit-of-the-doubt to supply an absent element.",
    )

    effective_dates = [
        "Assign the effective date under 38 C.F.R. § 3.400 and 38 U.S.C. § 5110 "
        "from the claim, the date the evidence establishes entitlement, or the "
        "controlling statutory trigger -- not automatically the rating date.",
        "For a granted appeal or returned HLR, preserve the earliest proper "
        "effective date and explain any staged or partial-period rating.",
    ]
    effective_dates.extend(
        item for item in rvsr.rating_codesheet_actions if "effective date" in item.lower()
    )

    dro_dispositions = [
        "Issue a new decision on the merits after de novo review, or identify the "
        "specific pre-decisional duty-to-assist error that requires a return for "
        "supplemental development; do not affirm on an incomplete record.",
        "If the prior decision contains multiple issues, dispose of each issue "
        "separately and state the evidence and regulation supporting the change.",
        "Preserve the veteran's appeal rights and the date of the HLR decision in "
        "the written disposition.",
    ]

    qrs_second_signature = list(senior.quality_review_checkpoints)
    qrs_second_signature.extend(
        [
            "Confirm the RVSR applied the court's holding to the correct "
            "diagnostic code and did not treat an M21-1 provision as superior "
            "to binding precedent.",
            "Confirm every benefit-of-the-doubt finding identifies the "
            "approximate-balance evidence and does not conceal a missing element.",
        ]
    )
    nqr_error_calls = [
        "Call a duty-to-assist error when a required record, notice, examination, "
        "or medical opinion was omitted and the omission could change the outcome.",
        "Call a reasons-or-bases error when the decision fails to state the evidence, "
        "the element findings, the diagnostic-code criteria, or the effective-date basis.",
        "Call a CUE-processing error when the decision applies a later judicial "
        "interpretation retroactively instead of the law extant at the prior decision.",
        "Call a precedent-conflict error when an unrevised M21-1 provision is followed "
        "over a binding CAVC/Federal Circuit/Supreme Court holding.",
    ]
    sample_focus = [
        "Sample grants, denials, staged ratings, TDIU/extraschedular referrals, "
        "bilateral-factor computations, and HLR returns for the issue in this report.",
        "Verify the decision's stated authority actually supports the proposition for "
        "which it is cited; do not accept a citation-only quality check.",
    ]

    refs = list(
        dict.fromkeys(
            [*rvsr.source_authorities, *senior.source_authorities]
        )
    )
    return AdjudicationRoleGuidance(
        issue=issue,
        rvsr=RvsrAdjudicationWorkflow(
            issue=issue,
            evidence_review_checklist=list(rvsr.evidence_review_checklist),
            lay_evidence_competence=lay_competence,
            duty_to_assist_exam_actions=list(rvsr.exam_development_triggers),
            rating_codesheet_actions=list(rvsr.rating_codesheet_actions),
            effective_date_actions=effective_dates,
            reasons_for_decision_requirements=list(
                rvsr.reasons_for_decision_requirements
            ),
            source_authorities=list(rvsr.source_authorities),
        ),
        senior_rater_dro=SeniorRaterDroWorkflow(
            issue=issue,
            hlr_de_novo_actions=list(senior.hlr_de_novo_focus),
            dta_error_returns=list(senior.duty_to_assist_errors),
            cue_analysis=[
                *senior.cue_analysis,
                "Apply 38 U.S.C. § 5109A and 38 C.F.R. § 3.105(a): CUE is a "
                "request to revise a final rating decision, not a new claim or "
                "an ordinary disagreement with the prior weighing.",
                "Apply 38 C.F.R. § 20.1403(e): a later judicial reinterpretation "
                "cannot supply CUE unless the court holds the statute's plain "
                "meaning was always unambiguous; the error must have been "
                "undebatable on the law and facts extant at the prior decision.",
            ],
            precedent_m21_conflict_resolution=list(
                senior.precedent_conflict_resolution
            ),
            dro_disposition_actions=dro_dispositions,
            source_authorities=list(senior.source_authorities),
        ),
        qrs=QrsAuditWorkflow(
            issue=issue,
            second_signature_checkpoints=qrs_second_signature,
            nqr_qrt_error_calls=nqr_error_calls,
            sample_review_focus=sample_focus,
            source_authorities=refs or ["the retrieved authorities"],
        ),
        source_authorities=refs or ["the retrieved authorities"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _item_id(
    finding: NegativeTreatment,
    target_case: str,
    target_citation: str,
    disposition: str = "",
) -> str:
    material = "\x1f".join(
        (
            target_case,
            target_citation,
            finding.source_opinion_id or "",
            finding.citing_case or "",
            finding.citing_citation or "",
            finding.citing_date or "",
            finding.citing_url or "",
            finding.opinion_role or "",
            finding.treatment or "",
            finding.excerpt or "",
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
    checks: list[CitatorCheck],
    *,
    source: str = "",
    prior: AdjudicationQueue | list[AdjudicationItem] | None = None,
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
    prior_map: dict[str, AdjudicationItem] = {}
    if prior is not None:
        prior_list = (
            prior.items if isinstance(prior, AdjudicationQueue) else prior
        )
        for p_item in prior_list:
            prior_map[p_item.id] = p_item

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
                if item.id in prior_map:
                    item.review = prior_map[item.id].review
                    item.history = list(prior_map[item.id].history)
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
                if item.id in prior_map:
                    item.review = prior_map[item.id].review
                    item.history = list(prior_map[item.id].history)
                items.append(item)

    for p_id, p_item in prior_map.items():
        if p_id not in seen and p_item.review.status != "pending":
            seen.add(p_id)
            items.append(p_item)

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


@contextmanager
def _exclusive_lock(path: str | Path) -> Iterator[None]:
    """Hold an advisory lock across a queue read-modify-write operation.

    The lock file is intentionally persistent. Removing it while another
    process has it open would let a third process lock a different inode
    and bypass the lock.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_suffix(target.suffix + ".lock").open(
        "a", encoding="utf-8"
    ) as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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

    with _exclusive_lock(path):
        queue = load_queue(path)
        item = next(
            (candidate for candidate in queue.items if candidate.id == item_id),
            None,
        )
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
