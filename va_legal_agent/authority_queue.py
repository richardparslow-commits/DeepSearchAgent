"""Prioritized authority queue for citator review.

The queue is deliberately separate from case ranking. Ranking answers "which
cases should the report emphasize?"; this module answers "which authorities
should the citator check first, given a bounded request budget?".

Priority is deterministic:

1. Higher court-authority tiers first.
2. Within a tier, cases the citator can actually resolve first.
3. Live authorities before authorities marked superseded in the retrieved
   corpus.
4. Precedential before unknown before non-precedential.
5. Existing authority rank, composite score, and relevance, with input order
   as the final stable tie-break.

After citator review, :func:`apply_citator_findings` performs one separate,
one-way annotation pass. It rebuilds the same base queue, demotes entries with
authoritative negative treatment, and flags entries whose findings were gated
or whose coverage needs review. Annotations never feed back into case ranking,
the base queue, or a second citator run, so applying them twice is idempotent.

Candidates are de-duplicated by CourtListener opinion id, then citation/date,
then title/URL. Superseded or non-precedential cases remain eligible for
review; the queue records why they were deprioritized rather than dropping
them silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import (
    AuthorityQueueEntry,
    CaseRecord,
    CitatorCheck,
    NegativeTreatment,
)
from .topics import authority_weight_for


@dataclass(frozen=True)
class QueuedAuthority:
    """A selected case paired with its serializable queue metadata."""

    case: CaseRecord
    entry: AuthorityQueueEntry


def _identity(case: CaseRecord) -> tuple[str, ...]:
    """Return a stable identity key for duplicate authority records."""
    opinion_id = (case.courtlistener_opinion_id or "").strip()
    if opinion_id:
        # Multi-opinion clusters arrive as "101,102"; the first id names the
        # same cluster as a bare "101", so dedupe on the leading id.
        return ("opinion", opinion_id.split(",")[0])
    citation = " ".join((case.citation or "").split()).casefold()
    date = (case.decision_date or "").strip()
    if citation and date:
        return ("citation_date", citation, date)
    if citation:
        return ("citation", citation)
    return (
        "title_url",
        " ".join((case.title or "").split()).casefold(),
        (case.url or "").strip(),
    )


def _eligible(case: CaseRecord) -> bool:
    """Whether the citator has an identifier it can use without guessing."""
    return bool(
        (case.courtlistener_opinion_id or "").strip()
        or (case.citation or "").strip()
    )


def _authority_weight(case: CaseRecord) -> int:
    """Use the enriched tier, falling back to the court taxonomy."""
    return case.authority_weight or authority_weight_for(case.court)


def _citation_key(value: str) -> str:
    """Normalize a reporter citation for identity matching."""
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def _title_key(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def _check_matches_case(case: CaseRecord, check: CitatorCheck) -> bool:
    """Match a queue case to a citator check without using mutable ranking."""
    case_ids = {
        value.strip()
        for value in (case.courtlistener_opinion_id or "").split(",")
        if value.strip()
    }
    check_ids = {
        value.strip()
        for value in [
            check.target_opinion_id,
            *check.target_opinion_ids,
        ]
        if value.strip()
    }
    if case_ids & check_ids:
        return True

    case_citations = {
        key
        for key in (_citation_key(case.citation),)
        if key
    }
    check_citations = {
        key
        for key in (
            _citation_key(check.target_citation),
            *(_citation_key(value) for value in check.target_citations),
        )
        if key
    }
    if case_citations & check_citations:
        return True

    case_url = (case.url or "").strip().casefold()
    check_url = (check.target_url or "").strip().casefold()
    if case_url and check_url and case_url == check_url:
        return True

    case_title = _title_key(case.title)
    check_title = _title_key(check.target_case)
    courts_match = (
        not case.court
        or not check.target_court
        or _title_key(case.court) == _title_key(check.target_court)
    )
    return bool(case_title and case_title == check_title and courts_match)


def _check_for_case(
    case: CaseRecord, checks: list[CitatorCheck]
) -> CitatorCheck | None:
    """Return the most severe matching check, if any."""
    matches = [check for check in checks if _check_matches_case(case, check)]
    if not matches:
        return None
    return max(
        matches,
        key=lambda check: (
            check.status == "negative",
            bool(check.negative_treatments),
            bool(check.post_as_of_treatment_findings),
            check.review_required,
        ),
    )


def _finding_provenance(prefix: str, finding: NegativeTreatment) -> str:
    """Describe a finding with the source needed to verify it."""
    citing = finding.citing_case or "unknown citing opinion"
    citation = f", {finding.citing_citation}" if finding.citing_citation else ""
    date = finding.citing_date or "date unknown"
    return (
        f"{prefix}: {finding.treatment} by {citing}{citation} ({date}); "
        f"excerpt: \"{finding.excerpt}\""
    )


def _annotate_entry(
    entry: AuthorityQueueEntry, check: CitatorCheck
) -> None:
    """Attach one-way citator annotations to a queue entry."""
    entry.citator_status = check.status
    findings: list[NegativeTreatment] = []
    seen_findings: set[tuple[str, str, str, str, bool]] = set()
    reasons: list[str] = []
    seen_reasons: set[str] = set()
    demoted = check.status == "negative"
    flagged = check.status == "unknown"
    blocked_ids = (
        check.source_validity_blocked_source_ids()
        if check.source_validity_enforced
        else set()
    )
    unblocked_demotion = False

    def add_finding(finding: NegativeTreatment) -> None:
        key = (
            finding.citing_case,
            finding.treatment,
            finding.excerpt,
            finding.citing_date,
            finding.authoritative,
        )
        if key not in seen_findings:
            seen_findings.add(key)
            findings.append(finding)

    def add_reason(reason: str) -> None:
        if reason not in seen_reasons:
            seen_reasons.add(reason)
            reasons.append(reason)

    for finding in check.negative_treatments:
        add_finding(finding)
        if finding.source_opinion_id and finding.source_opinion_id in blocked_ids:
            add_reason(
                _finding_provenance("citator enforcement withheld", finding)
            )
            continue
        demoted = True
        unblocked_demotion = True
        add_reason(_finding_provenance("citator demotion", finding))

    if (
        check.negative_treatments
        or check.post_as_of_treatment_findings
    ) and not unblocked_demotion:
        # A stale negative status must not demote from findings that enforcement
        # filtered out. The normal enforcement pass already downgrades it.
        demoted = False
        flagged = True

    for finding in check.gated_treatment_findings:
        flagged = True
        add_finding(finding)
        reason = _finding_provenance("citator gated finding", finding)
        if finding.gate_reason:
            reason += f"; gate: {finding.gate_reason}"
        add_reason(reason)

    for finding in check.post_as_of_treatment_findings:
        add_finding(finding)
        if finding.source_opinion_id and finding.source_opinion_id in blocked_ids:
            add_reason(
                _finding_provenance("citator enforcement withheld", finding)
            )
        elif finding.authoritative:
            demoted = True
            unblocked_demotion = True
            add_reason(
                _finding_provenance("citator post-as-of demotion", finding)
            )
        else:
            flagged = True
            add_reason(_finding_provenance("citator post-as-of flag", finding))

    if check.review_required:
        flagged = True
        for reason in check.review_reasons:
            add_reason(f"citator review flag: {reason}")
    if check.post_as_of_incomplete:
        flagged = True
        add_reason("citator review flag: post-as-of review was incomplete")

    if demoted and not any(reason.startswith("citator demotion") for reason in reasons):
        add_reason("citator demotion: authoritative negative status")
    if flagged and not reasons:
        add_reason("citator review flag: status requires manual review")

    entry.citator_demoted = demoted
    entry.citator_flagged = flagged
    entry.citator_findings = findings
    entry.citator_reasons = reasons


def _precedent_rank(case: CaseRecord) -> int:
    if case.precedential is True:
        return 0
    if case.precedential is None:
        return 1
    return 2


def _queue_reasons(case: CaseRecord) -> list[str]:
    reasons = [
        f"authority tier {_authority_weight(case)} "
        f"({case.court or 'court unknown'})"
    ]
    if case.superseded_by:
        reasons.append(
            f"superseded in the retrieved corpus by {case.superseded_by}; "
            "deprioritized"
        )
    if case.precedential is True:
        reasons.append("precedential")
    elif case.precedential is None:
        reasons.append("precedential status unknown")
    else:
        reasons.append("non-precedential; deprioritized")

    if (case.courtlistener_opinion_id or "").strip():
        reasons.append("CourtListener opinion id available")
    elif (case.citation or "").strip():
        reasons.append("citation available for resolution")
    else:
        reasons.append(
            "no citation or CourtListener opinion id; resolution may fail"
        )

    if case.authority_rank > 0:
        reasons.append(f"retrieved authority rank {case.authority_rank}")
    if case.relevance_score:
        reasons.append(f"relevance score {case.relevance_score}")
    return reasons


def _sort_key(case: CaseRecord) -> tuple[object, ...]:
    """Return a stable priority key without mutating the case."""
    rank = case.authority_rank if case.authority_rank > 0 else 10**6
    return (
        -_authority_weight(case),
        0 if _eligible(case) else 1,
        1 if case.superseded_by else 0,
        _precedent_rank(case),
        rank,
        -case.composite_score,
        -case.relevance_score,
    )


def prioritize_authorities(
    cases: list[CaseRecord], limit: int | None = None
) -> list[QueuedAuthority]:
    """Return the selected authorities in deterministic priority order.

    ``limit=None`` keeps every unique candidate; ``limit <= 0`` returns an
    empty queue. The input list and its ``CaseRecord`` objects are not mutated.
    """
    if limit is not None and limit <= 0:
        return []

    unique: list[CaseRecord] = []
    seen: set[tuple[str, ...]] = set()
    for case in cases:
        key = _identity(case)
        if key in seen:
            continue
        seen.add(key)
        unique.append(case)

    ordered = sorted(unique, key=_sort_key)
    if limit is not None:
        ordered = ordered[:limit]

    queued: list[QueuedAuthority] = []
    for priority, case in enumerate(ordered, start=1):
        queued.append(
            QueuedAuthority(
                case=case,
                entry=AuthorityQueueEntry(
                    priority=priority,
                    base_priority=priority,
                    case_title=case.title,
                    citation=case.citation,
                    court=case.court,
                    authority_weight=_authority_weight(case),
                    authority_rank=case.authority_rank,
                    relevance_score=case.relevance_score,
                    composite_score=case.composite_score,
                    precedential=case.precedential,
                    superseded_by=case.superseded_by,
                    eligible=_eligible(case),
                    reasons=_queue_reasons(case),
                ),
            )
        )
    return queued


def apply_citator_findings(
    cases: list[CaseRecord],
    checks: list[CitatorCheck],
    limit: int | None = None,
) -> list[QueuedAuthority]:
    """Return the base queue with one-way citator ordering and annotations.

    The function deliberately starts from a freshly rebuilt base queue. It
    never consumes a previously annotated queue, so findings cannot feed back
    into the ordering or produce a self-reinforcing loop. Authoritative
    negative treatment moves an entry behind un-undermined entries; gated or
    incomplete review only flags it. Case ranking fields are untouched.
    """
    queued = prioritize_authorities(cases, limit)
    for item in queued:
        check = _check_for_case(item.case, checks)
        if check is not None:
            _annotate_entry(item.entry, check)

    queued.sort(
        key=lambda item: (
            1 if item.entry.citator_demoted else 0,
            item.entry.base_priority,
        )
    )
    for priority, item in enumerate(queued, start=1):
        item.entry.priority = priority
    return queued


def build_authority_queue(
    cases: list[CaseRecord], limit: int | None = None
) -> list[AuthorityQueueEntry]:
    """Return serializable queue metadata for reports and JSON output."""
    return [item.entry for item in prioritize_authorities(cases, limit)]
