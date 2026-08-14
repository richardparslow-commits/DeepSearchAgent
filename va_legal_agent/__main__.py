from __future__ import annotations

import argparse

from .agent import analyze_cases_for_claim


def main() -> None:
    parser = argparse.ArgumentParser(description="Search and analyze veterans compensation legal cases.")
    parser.add_argument("issue", help="The legal issue to research, such as service connection for tinnitus.")
    parser.add_argument("--type", dest="claim_type", default="Compensation", help="Benefit type to search for.")
    parser.add_argument("--max-results", dest="max_results", type=int, default=10, help="Maximum case results to review.")
    parser.add_argument("--no-enrich", action="store_true", help="Skip fetching case source pages for citation/date details.")
    args = parser.parse_args()

    analysis = analyze_cases_for_claim(
        args.issue,
        claim_type=args.claim_type,
        max_results=max(args.max_results, 1),
        enrich=not args.no_enrich,
    )
    print(analysis.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
