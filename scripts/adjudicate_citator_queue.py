#!/usr/bin/env python3
"""Build and review the evidence-first citator adjudication queue.

Typical workflow:

    python scripts/adjudicate_citator_queue.py build \
        --analysis analysis.json --queue citator-review.json
    python scripts/adjudicate_citator_queue.py list --queue citator-review.json
    python scripts/adjudicate_citator_queue.py show \
        --queue citator-review.json adj-...
    python scripts/adjudicate_citator_queue.py decide \
        --queue citator-review.json adj-... \
        --decision confirmed --reviewer "Initials" \
        --rationale "Excerpt and published role/status checked against the source."

The queue and decisions never change citator status, ranking, or
``superseded_by``. ``show`` puts the verbatim evidence and source before the
automated proposal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from va_legal_agent.adjudication import (
    DECISIONS,
    build_adjudication_queue,
    load_queue,
    record_decision,
    render_queue_text,
    save_queue,
)
from va_legal_agent.models import CitatorCheck

def _checks_from_analysis(path: Path) -> list[CitatorCheck]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("analysis JSON must contain an object")
    raw_checks = payload.get("citator_checks")
    if not isinstance(raw_checks, list):
        raise ValueError("analysis JSON has no citator_checks list")
    return [CitatorCheck.model_validate(item) for item in raw_checks]


def _build(args: argparse.Namespace) -> int:
    try:
        checks = _checks_from_analysis(Path(args.analysis))
        queue = build_adjudication_queue(
            checks, source=args.source or str(args.analysis)
        )
        save_queue(queue, args.queue)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"queue build error: {exc}", file=sys.stderr)
        return 2
    print(f"{len(queue.items)} adjudication item(s) written to {args.queue}")
    return 0


def _list(args: argparse.Namespace) -> int:
    try:
        queue = load_queue(args.queue)
        print(
            render_queue_text(
                queue, include_reviewed=args.all
            )
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"queue read error: {exc}", file=sys.stderr)
        return 2
    return 0


def _show(args: argparse.Namespace) -> int:
    try:
        queue = load_queue(args.queue)
        print(render_queue_text(queue, item_id=args.item_id))
    except KeyError as exc:
        print(f"queue item error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"queue read error: {exc}", file=sys.stderr)
        return 2
    return 0


def _decide(args: argparse.Namespace) -> int:
    try:
        item = record_decision(
            args.queue,
            args.item_id,
            decision=args.decision,
            reviewer=args.reviewer,
            rationale=args.rationale,
        )
    except KeyError as exc:
        print(f"queue item error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"queue decision error: {exc}", file=sys.stderr)
        return 2
    print(
        f"{item.id}: {item.review.status} by {item.review.reviewer} "
        f"at {item.review.reviewed_at}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build a queue from analysis JSON")
    build.add_argument("--analysis", required=True, help="CLI JSON output file")
    build.add_argument("--queue", required=True, help="Destination queue JSON")
    build.add_argument("--source", default="", help="Optional queue provenance label")
    build.set_defaults(func=_build)

    list_parser = subparsers.add_parser("list", help="List pending queue items")
    list_parser.add_argument("--queue", required=True, help="Queue JSON")
    list_parser.add_argument(
        "--all", action="store_true", help="Include reviewed items"
    )
    list_parser.set_defaults(func=_list)

    show = subparsers.add_parser("show", help="Show one item, evidence first")
    show.add_argument("--queue", required=True, help="Queue JSON")
    show.add_argument("item_id", help="Queue item id")
    show.set_defaults(func=_show)

    decide = subparsers.add_parser("decide", help="Record a human decision")
    decide.add_argument("--queue", required=True, help="Queue JSON")
    decide.add_argument("item_id", help="Queue item id")
    decide.add_argument(
        "--decision",
        required=True,
        choices=sorted(DECISIONS),
        help="Human disposition",
    )
    decide.add_argument("--reviewer", required=True, help="Reviewer name or initials")
    decide.add_argument("--rationale", required=True, help="Evidence-based rationale")
    decide.set_defaults(func=_decide)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
