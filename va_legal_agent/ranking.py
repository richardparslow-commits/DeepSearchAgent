"""Case ranking layer.

Produces the final case order with an explainable composite score:

1. Court-authority tiers are strictly dominant (Supreme Court > Federal
   Circuit > CAVC > BVA > other), reflecting binding vs. persuasive authority.
2. Within a tier, cases are scored by a weighted blend of:
   - relevance: issue keyword relevance, normalized against the batch maximum
   - recency: decision year scaled between RECENCY_FLOOR_YEAR and today;
     cases without a known date get a neutral score
   - completeness: share of citation/decision date/holding extracted
"""

from __future__ import annotations

from datetime import datetime

from .models import CaseRecord

# Recency scale floor: pre-1985 veterans-law decisions are rare in this corpus.
RECENCY_FLOOR_YEAR = 1985

# Within-tier signal weights (sum to 1.0).
WEIGHT_RELEVANCE = 0.60
WEIGHT_RECENCY = 0.25
WEIGHT_COMPLETENESS = 0.15

# Cases with an unknown decision date are neither rewarded nor penalized.
UNKNOWN_RECENCY = 0.5


def _parse_year(decision_date: str) -> int | None:
    text = (decision_date or "").strip()
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def recency_score(decision_date: str, current_year: int | None = None) -> float:
    """Map an ISO decision date to 0..1 (newer is higher); unknown dates are neutral."""
    year = _parse_year(decision_date)
    if year is None:
        return UNKNOWN_RECENCY
    current_year = current_year or datetime.now().year
    span = max(current_year - RECENCY_FLOOR_YEAR, 1)
    return min(max((year - RECENCY_FLOOR_YEAR) / span, 0.0), 1.0)


def completeness_score(case: CaseRecord) -> float:
    """Share of enrichment fields (citation, decision date, holding) populated."""
    populated = sum(1 for value in (case.citation, case.decision_date, case.holding) if value)
    return populated / 3


def score_case(
    case: CaseRecord, max_relevance: int, current_year: int | None = None
) -> tuple[float, str]:
    """Return the within-tier score (0..1) and a human-readable breakdown for one case."""
    relevance = (case.relevance_score / max_relevance) if max_relevance > 0 else 0.0
    recency = recency_score(case.decision_date, current_year=current_year)
    completeness = completeness_score(case)
    tier_score = (
        WEIGHT_RELEVANCE * relevance
        + WEIGHT_RECENCY * recency
        + WEIGHT_COMPLETENESS * completeness
    )
    explanation = (
        f"tier score {tier_score:.2f} = "
        f"relevance {relevance:.2f} x {WEIGHT_RELEVANCE} + "
        f"recency {recency:.2f} x {WEIGHT_RECENCY} + "
        f"completeness {completeness:.2f} x {WEIGHT_COMPLETENESS}"
    )
    return tier_score, explanation


def rank_cases(cases: list[CaseRecord], current_year: int | None = None) -> list[CaseRecord]:
    """Rank cases best-first.

    Authority tiers are strictly dominant; within a tier the weighted composite
    of relevance, recency, and completeness decides. Sets ``authority_rank``,
    ``composite_score``, and ``ranking_explanation`` on each case and returns
    the ordered list (same case objects).
    """
    if not cases:
        return []

    max_relevance = max(case.relevance_score for case in cases)
    for case in cases:
        tier_score, explanation = score_case(case, max_relevance, current_year=current_year)
        case.composite_score = case.authority_weight + tier_score
        case.ranking_explanation = f"authority tier {case.authority_weight}; {explanation}"

    ordered = sorted(
        cases,
        key=lambda c: (-c.authority_weight, -c.composite_score, c.title, c.url),
    )
    for index, case in enumerate(ordered, start=1):
        case.authority_rank = index
    return ordered
