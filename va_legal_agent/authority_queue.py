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

Candidates are de-duplicated by CourtListener opinion id, then citation/date,
then title/URL. Superseded or non-precedential cases remain eligible for
review; the queue records why they were deprioritized rather than dropping
them silently.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import AuthorityQueueEntry, CaseRecord
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
        return ("opinion", opinion_id)
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


def build_authority_queue(
    cases: list[CaseRecord], limit: int | None = None
) -> list[AuthorityQueueEntry]:
    """Return serializable queue metadata for reports and JSON output."""
    return [item.entry for item in prioritize_authorities(cases, limit)]
