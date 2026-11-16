#!/usr/bin/env python3
"""Run the labeled, adversarial citator evaluation set.

The benchmark is deterministic and offline. Each case supplies a synthetic
target, citing-opinion metadata/text, forward-citation pages, and gold labels.
A fixture provider drives the production ``check_case`` path; nothing here
changes production citator behavior or contacts CourtListener.

Known misses can be listed by case id in ``baseline.json`` so the benchmark can
land as a measurement baseline. CI fails on any new miss. A case whose known
miss starts passing is reported as ``fixed``; remove it from the baseline when
the production behavior is corrected.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from va_legal_agent.citator import check_case
from va_legal_agent.models import CaseRecord
from va_legal_agent.search import SearchError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "benchmarks" / "citator"
FINDING_FIELDS = (
    "citing_url",
    "treatment",
    "scope",
    "opinion_role",
    "precedential_status",
)
TIMELINE_FIELDS = (
    "signal",
    "treatment",
    "citing_url",
    "citing_court",
    "citing_date",
    "scope",
    "opinion_role",
    "precedential_status",
    "binding_class",
    "authoritative",
    "later_than_negative",
)
TIMELINE_BOOLEAN_FIELDS = frozenset({"authoritative", "later_than_negative"})
EXPECTED_FIELDS = (
    "status",
    "coverage_complete",
    "target_cluster_id",
    "target_citations",
    "target_opinion_ids",
    "authoritative",
    "gated",
    "flags_contain",
    "same_docket_count",
)
SIGNATURE_ACTUALS = {
    "authoritative": "negative_treatments",
    "gated": "gated_treatment_findings",
    "binding": "binding_negative_treatments",
    "persuasive": "persuasive_negative_treatments",
}
SIGNATURE_FIELDS = {
    **{key: FINDING_FIELDS for key in SIGNATURE_ACTUALS},
    "timeline": TIMELINE_FIELDS,
}
AS_OF_EXPECTED_FIELDS = (
    "as_of_date",
    "good_law_as_of",
    "post_as_of_citing_opinion_ids",
    "undated_citing_opinion_ids",
    "post_as_of_treatments",
    "post_as_of_incomplete",
    "truncated",
    "gap_codes_contain",
)
OPTIONAL_EXPECTED_FIELDS = tuple(
    key
    for key in (
        *SIGNATURE_FIELDS,
        "later_reaffirmance",
        *AS_OF_EXPECTED_FIELDS,
    )
    if key not in {"authoritative", "gated"}
)
ALLOWED_EXPECTED_FIELDS = frozenset(EXPECTED_FIELDS) | frozenset(
    OPTIONAL_EXPECTED_FIELDS
)


class FixtureProvider:
    """In-memory CourtListener surface built from one evaluation case."""

    def __init__(self, case: dict[str, Any]) -> None:
        self._case = case
        self._metadata = {
            str(key): value for key, value in (case.get("metadata") or {}).items()
        }
        self._texts = {
            str(key): value for key, value in (case.get("texts") or {}).items()
        }
        self._errors = case.get("errors") or {}
        self._pages = {
            (int(page["opinion_id"]), page.get("cursor") or None): page
            for page in (case.get("edges") or [])
        }
        self._same_docket = case.get("same_docket") or []

    def _error(self, kind: str, key: object | None = None) -> str:
        value = self._errors.get(kind)
        if isinstance(value, dict):
            value = value.get(str(key))
        return str(value) if value else ""

    def lookup_citations(self, text: str) -> list[dict[str, object]]:
        error = self._error("lookup")
        if error:
            raise SearchError(error)
        return list(self._case.get("lookup") or [])

    def citing_edges(
        self, opinion_id: int, page_size: int = 100, cursor: str | None = None
    ) -> dict[str, object]:
        error = self._error("edges", opinion_id)
        if error:
            raise SearchError(error)
        page = self._pages.get((opinion_id, cursor))
        if page is None:
            return {"count": 0, "results": [], "next": None}
        citing_ids = [int(value) for value in page.get("citing_ids") or []]
        return {
            "count": int(page.get("count", len(citing_ids))),
            "results": [
                {
                    "citing_opinion": (
                        "https://www.courtlistener.com/api/rest/v4/opinions/"
                        f"{opinion_id}/"
                    )
                }
                for opinion_id in citing_ids
            ],
            "next": page.get("next") or None,
        }

    def opinion_metadata(self, opinion_id: int) -> dict[str, object] | None:
        error = self._error("metadata", opinion_id)
        if error:
            raise SearchError(error)
        return self._metadata.get(str(opinion_id))

    def fetch_opinion_text(self, opinion_id: int) -> str:
        error = self._error("text", opinion_id)
        if error:
            raise SearchError(error)
        return str(self._texts.get(str(opinion_id)) or "")

    def same_docket_decisions(
        self, opinion_id: int, max_results: int = 5
    ) -> list[dict[str, str]]:
        error = self._error("same_docket", opinion_id)
        if error:
            raise SearchError(error)
        return list(self._same_docket)[:max(0, max_results)]


def _validate_signature_list(
    case_id: str,
    key: str,
    values: object,
    fields: tuple[str, ...],
) -> None:
    if not isinstance(values, list):
        raise ValueError(f"{case_id}: {key} must be a list")
    for finding in values:
        if not isinstance(finding, dict) or set(finding) != set(fields):
            raise ValueError(
                f"{case_id}: {key} finding fields must be {', '.join(fields)}"
            )


def _load_cases(data_dir: Path) -> list[dict[str, Any]]:
    payload = json.loads((data_dir / "cases.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("cases.json must contain a JSON object")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases.json must contain a non-empty 'cases' list")

    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("every case must be a JSON object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("every case needs a non-empty string id")
        if case_id in seen:
            raise ValueError(f"duplicate case id: {case_id}")
        seen.add(case_id)

        target = case.get("target")
        if not isinstance(target, dict) or not isinstance(target.get("title"), str):
            raise ValueError(f"{case_id}: target must be an object with a title")
        if not isinstance(target.get("court"), str):
            raise ValueError(f"{case_id}: target must include a court")

        expected = case.get("expected")
        if not isinstance(expected, dict):
            raise ValueError(f"{case_id}: expected must be an object")
        expected_fields = set(expected)
        if (
            not set(EXPECTED_FIELDS) <= expected_fields
            or not expected_fields <= ALLOWED_EXPECTED_FIELDS
        ):
            raise ValueError(
                f"{case_id}: expected fields must include "
                f"{', '.join(EXPECTED_FIELDS)} and may add "
                f"{', '.join(OPTIONAL_EXPECTED_FIELDS)}"
            )
        if expected["status"] not in {"negative", "good_law", "unknown"}:
            raise ValueError(f"{case_id}: invalid expected status")
        if not isinstance(expected["coverage_complete"], bool):
            raise ValueError(f"{case_id}: coverage_complete must be a boolean")
        for key in (
            "target_cluster_id",
            "target_citations",
            "target_opinion_ids",
            "flags_contain",
        ):
            if not isinstance(expected[key], (str, list)):
                raise ValueError(f"{case_id}: {key} has the wrong type")
        for key, fields in SIGNATURE_FIELDS.items():
            if key in expected:
                _validate_signature_list(case_id, key, expected[key], fields)
        if "later_reaffirmance" in expected and not isinstance(
            expected["later_reaffirmance"], bool
        ):
            raise ValueError(f"{case_id}: later_reaffirmance must be a boolean")
        for key in ("as_of_date", "good_law_as_of"):
            if key in expected and not isinstance(expected[key], str):
                raise ValueError(f"{case_id}: {key} must be a string")
        for key in (
            "post_as_of_citing_opinion_ids",
            "undated_citing_opinion_ids",
            "gap_codes_contain",
        ):
            if key in expected and (
                not isinstance(expected[key], list)
                or not all(isinstance(item, str) for item in expected[key])
            ):
                raise ValueError(f"{case_id}: {key} must be a string list")
        if "post_as_of_treatments" in expected:
            _validate_signature_list(
                case_id,
                "post_as_of_treatments",
                expected["post_as_of_treatments"],
                FINDING_FIELDS,
            )
        for key in (
            "post_as_of_incomplete",
            "truncated",
        ):
            if key in expected and not isinstance(expected[key], bool):
                raise ValueError(f"{case_id}: {key} must be a boolean")
        if not isinstance(expected["same_docket_count"], int):
            raise ValueError(f"{case_id}: same_docket_count must be an integer")
    return cases


def _load_baseline(data_dir: Path) -> set[str]:
    path = data_dir / "baseline.json"
    if not path.exists():
        return set()
    baseline = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(baseline, dict):
        raise ValueError("baseline.json must contain a JSON object")
    known_misses = baseline.get("known_misses")
    if not isinstance(known_misses, list) or not all(
        isinstance(item, str) for item in known_misses
    ):
        raise ValueError("baseline.json must contain a 'known_misses' string list")
    return set(known_misses)


def _signature(finding: object) -> dict[str, str]:
    return {
        field: str(getattr(finding, field) or "") for field in FINDING_FIELDS
    }


def _sorted_signatures(findings: list[object]) -> list[dict[str, str]]:
    return sorted(
        (_signature(finding) for finding in findings),
        key=lambda item: json.dumps(item, sort_keys=True),
    )


def _timeline_signature(entry: object) -> dict[str, object]:
    return {
        field: (
            bool(getattr(entry, field))
            if field in TIMELINE_BOOLEAN_FIELDS
            else str(getattr(entry, field) or "")
        )
        for field in TIMELINE_FIELDS
    }


def _timeline_signatures(entries: list[object]) -> list[dict[str, object]]:
    return [_timeline_signature(entry) for entry in entries]


def _compare_case(
    case: dict[str, Any], check: object
) -> list[str]:
    expected = case["expected"]
    mismatches: list[str] = []

    def compare(name: str, actual: object) -> None:
        if actual != expected[name]:
            mismatches.append(f"{name}: expected {expected[name]!r}, got {actual!r}")

    compare("status", check.status)
    compare("coverage_complete", check.coverage.complete)
    compare("target_cluster_id", check.target_cluster_id)
    compare("target_citations", check.target_citations)
    compare("target_opinion_ids", check.target_opinion_ids)
    for name, attribute in SIGNATURE_ACTUALS.items():
        if name in expected:
            compare(name, _sorted_signatures(getattr(check, attribute)))
    if "timeline" in expected:
        compare("timeline", _timeline_signatures(check.treatment_timeline))
    if "later_reaffirmance" in expected:
        compare("later_reaffirmance", check.later_reaffirmance)
    compare("same_docket_count", len(check.same_docket_decisions))
    if "as_of_date" in expected:
        compare("as_of_date", check.as_of_date)
    if "good_law_as_of" in expected:
        compare("good_law_as_of", check.good_law_as_of)
    if "post_as_of_citing_opinion_ids" in expected:
        compare(
            "post_as_of_citing_opinion_ids",
            [cited.opinion_id for cited in check.post_as_of_citing_opinions],
        )
    if "undated_citing_opinion_ids" in expected:
        compare(
            "undated_citing_opinion_ids",
            [cited.opinion_id for cited in check.undated_citing_opinions],
        )
    if "post_as_of_treatments" in expected:
        compare(
            "post_as_of_treatments",
            _sorted_signatures(check.post_as_of_treatment_findings),
        )
    if "post_as_of_incomplete" in expected:
        compare("post_as_of_incomplete", check.post_as_of_incomplete)
    if "truncated" in expected:
        compare("truncated", check.coverage.truncated)
    if "gap_codes_contain" in expected:
        actual_codes = [gap.code for gap in check.coverage.gaps]
        for code in expected["gap_codes_contain"]:
            if code not in actual_codes:
                mismatches.append(f"gap_codes_contain: {code!r} not found")
    flags_text = "\n".join(check.flags)
    for phrase in expected["flags_contain"]:
        if phrase not in flags_text:
            mismatches.append(f"flags_contain: {phrase!r} not found")
    return mismatches


def _preview(value: object, width: int = 100) -> str:
    text = str(value)
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="directory containing cases.json and optional baseline.json",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every mismatch detail instead of only case status",
    )
    args = parser.parse_args(argv)
    data_dir: Path = args.data_dir

    try:
        cases = _load_cases(data_dir)
        known_misses = _load_baseline(data_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"citator benchmark setup error: {exc}", file=sys.stderr)
        return 2

    known_ids = {case["id"] for case in cases}
    unknown_known = known_misses - known_ids
    if unknown_known:
        print(
            "citator benchmark setup error: baseline names unknown cases: "
            + ", ".join(sorted(unknown_known)),
            file=sys.stderr,
        )
        return 2

    new_misses: list[str] = []
    known_count = 0
    fixed: list[str] = []
    print(f"{'case':<34} {'result':<7} category")
    print("-" * 100)

    for case in cases:
        provider = FixtureProvider(case)
        limits = case.get("limits") or {}
        try:
            check = check_case(
                CaseRecord(**case["target"]),
                provider,
                max_citing_opinions=int(limits.get("max_citing_opinions", 5)),
                max_pages=int(limits.get("max_pages", 1)),
                as_of_date=str(limits.get("as_of_date") or ""),
            )
            mismatches = _compare_case(case, check)
        except (KeyError, TypeError, ValueError) as exc:
            mismatches = [f"runtime error: {exc}"]

        if not mismatches:
            result = "fixed" if case["id"] in known_misses else "pass"
            if result == "fixed":
                fixed.append(case["id"])
        elif case["id"] in known_misses:
            result = "known"
            known_count += 1
        else:
            result = "NEW"
            new_misses.append(case["id"])
            if args.verbose:
                for mismatch in mismatches:
                    print(f"  {case['id']}: {_preview(mismatch, 160)}")
        print(f"{case['id']:<34} {result:<7} {case.get('category', '')}")

    total = len(cases)
    passed = total - known_count - len(new_misses)
    print("-" * 100)
    print(
        f"{passed}/{total} cases pass; {known_count} known miss(es), "
        f"{len(new_misses)} new miss(es)"
    )
    if fixed:
        print(
            "Baseline can be tightened; these known misses now pass: "
            + ", ".join(fixed)
        )
    if new_misses:
        print(
            "Citator benchmark regression: " + ", ".join(new_misses),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
