#!/usr/bin/env python3
"""Benchmark source-only adaptive versus equal candidate allocation.

The benchmark is deterministic and offline. A small provider returns a fixed
number of forward citations for each source-cluster opinion; the production
cluster fetcher is run once with the current equal split and once with the
experimental carry-forward allocation. The comparison uses the same per-opinion
allocation and yield rows that source audits expose.

This script does not change production behavior or contact CourtListener.
"""

from __future__ import annotations

import random

from va_legal_agent.citator import _Target, _fetch_cluster_forward_edges
from va_legal_agent.models import CaseRecord

MAX_IDS = 12
MAX_PAGES = 1
SCENARIOS = (
    ("equal-yield", [4, 4, 4]),
    ("front-loaded", [12, 0, 0]),
    ("back-loaded", [0, 0, 12]),
    ("middle-heavy", [0, 12, 0]),
    ("alternating", [12, 0, 12, 0]),
    ("tail-heavy", [0, 0, 8, 8]),
)


class SyntheticProvider:
    """Provider surface with one deterministic forward-citation page per opinion."""

    def __init__(self, yields: list[int]) -> None:
        self._yields = {
            index + 1: list(range((index + 1) * 1000, (index + 1) * 1000 + count))
            for index, count in enumerate(yields)
        }

    def citing_edges(
        self, opinion_id: int, page_size: int = 100, cursor: str | None = None
    ) -> dict[str, object]:
        if cursor is not None:
            return {"count": 0, "results": [], "next": None}
        citing_ids = self._yields.get(opinion_id, [])
        return {
            "count": len(citing_ids),
            "results": [
                {
                    "citing_opinion": (
                        "https://www.courtlistener.com/api/rest/v4/opinions/"
                        f"{citing_id}/"
                    )
                }
                for citing_id in citing_ids[:page_size]
            ],
            "next": None,
        }


def _run(yields: list[int], adaptive: bool) -> tuple[int, list[int], list[int]]:
    opinion_ids = list(range(1, len(yields) + 1))
    case = CaseRecord(
        title="Synthetic v. Benchmark",
        court="CAVC",
        citation="30 Vet.App. 1",
        decision_date="2020-01-01",
        courtlistener_opinion_id="1",
        url="https://www.courtlistener.com/opinion/1/",
    )
    target = _Target(
        case=case,
        opinion_id=1,
        citation=case.citation,
        case_name=case.title,
        cluster_id="synthetic",
        opinion_ids=tuple(opinion_ids),
    )
    edge_fetch = _fetch_cluster_forward_edges(
        SyntheticProvider(yields),
        target,
        MAX_PAGES,
        page_size=MAX_IDS,
        max_ids=MAX_IDS,
        adaptive_allocation=adaptive,
    )
    return (
        len(edge_fetch.ids),
        [row.candidate_allocation for row in edge_fetch.opinion_audits],
        [row.citing_ids_collected for row in edge_fetch.opinion_audits],
    )


def _format(values: list[int]) -> str:
    return "[" + ", ".join(str(value) for value in values) + "]"


def main() -> int:
    print(
        "source-only candidate allocation: equal split vs adaptive carry-forward"
    )
    print(f"budget={MAX_IDS}, pages per opinion={MAX_PAGES}")
    print()
    print(
        f"{'scenario':<14} {'equal':>5} {'adaptive':>8} {'delta':>6} "
        "allocations / collected"
    )
    print("-" * 112)
    for name, yields in SCENARIOS:
        equal_yield, equal_allocations, equal_collected = _run(yields, False)
        adaptive_yield, adaptive_allocations, adaptive_collected = _run(
            yields, True
        )
        print(
            f"{name:<14} {equal_yield:>5} {adaptive_yield:>8} "
            f"{adaptive_yield - equal_yield:>+6} "
            f"equal {_format(equal_allocations)} / {_format(equal_collected)}; "
            f"adaptive {_format(adaptive_allocations)} / "
            f"{_format(adaptive_collected)}"
        )

    rng = random.Random(20261116)
    random_equal = 0
    random_adaptive = 0
    improved = 0
    unchanged = 0
    degraded = 0
    trials = 200
    for _ in range(trials):
        yields = [rng.choice([0, 0, 1, 2, 4, 8, 12]) for _ in range(rng.randint(2, 6))]
        equal_yield, _, _ = _run(yields, False)
        adaptive_yield, _, _ = _run(yields, True)
        random_equal += equal_yield
        random_adaptive += adaptive_yield
        if adaptive_yield > equal_yield:
            improved += 1
        elif adaptive_yield == equal_yield:
            unchanged += 1
        else:
            degraded += 1

    print("-" * 112)
    print(
        f"randomized: {trials} clusters; equal {random_equal} candidates, "
        f"adaptive {random_adaptive} candidates "
        f"({random_adaptive - random_equal:+d}); improved {improved}, "
        f"unchanged {unchanged}, degraded {degraded}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
