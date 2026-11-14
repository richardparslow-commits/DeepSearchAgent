#!/usr/bin/env python3
"""Run the real-document extraction benchmark.

The benchmark feeds committed snapshots of public BVA and CAVC decisions
through the production ``extract_case_details`` function and compares the
results with hand-reviewed gold fields.  It does not download anything and
does not change production extraction behavior.

Known mismatches are recorded in ``baseline.json`` so this can land as a
measurement baseline before the extractor is fixed.  CI fails on any miss that
is not already in the baseline; a baseline miss that starts passing is reported
as an improvement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

from va_legal_agent.fetch import extract_case_details

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "benchmarks" / "extraction"
FIELDS = ("citation", "decision_date", "holding", "legal_standard", "outcome")


def _normalize(value: str) -> str:
    """Normalize whitespace and typographic punctuation for comparisons."""
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u2018", "'").replace("\u2019", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", value).strip().casefold()


def _matches(field: str, expected: str, actual: str) -> bool:
    expected_norm = _normalize(expected)
    actual_norm = _normalize(actual)
    if field == "holding":
        # The extractor returns every holding sentence it finds.  Gold holdings
        # identify the material holding sentence rather than requiring an exact
        # concatenation of every sentence in the opinion.
        return bool(expected_norm) and expected_norm in actual_norm
    return expected_norm == actual_norm


def _preview(value: str, width: int = 88) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= width:
        return value
    return value[: width - 3] + "..."


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

        expected = case.get("expected")
        if not isinstance(expected, dict) or set(expected) != set(FIELDS):
            raise ValueError(f"{case_id}: expected fields must be {', '.join(FIELDS)}")
        if not all(isinstance(expected[field], str) for field in FIELDS):
            raise ValueError(f"{case_id}: every expected field must be a string")
        if not isinstance(case.get("fixture"), str):
            raise ValueError(f"{case_id}: missing fixture path")
        if not isinstance(case.get("sha256"), str):
            raise ValueError(f"{case_id}: missing fixture sha256")
    return cases


def _load_fixture(data_dir: Path, case: dict[str, Any]) -> str:
    fixture_path = (data_dir / case["fixture"]).resolve()
    data_root = data_dir.resolve()
    if data_root not in fixture_path.parents:
        raise ValueError(f"{case['id']}: fixture escapes the benchmark directory")
    raw = fixture_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != case["sha256"]:
        raise ValueError(
            f"{case['id']}: fixture checksum mismatch; expected {case['sha256']}, got {digest}"
        )
    return raw.decode("utf-8")


def _load_baseline(data_dir: Path) -> dict[str, Any]:
    baseline = json.loads((data_dir / "baseline.json").read_text(encoding="utf-8"))
    if not isinstance(baseline, dict):
        raise ValueError("baseline.json must contain a JSON object")
    known_misses = baseline.get("known_misses")
    if not isinstance(known_misses, list) or not all(
        isinstance(item, str) for item in known_misses
    ):
        raise ValueError("baseline.json must contain a 'known_misses' string list")
    if not isinstance(baseline.get("field_count"), int):
        raise ValueError("baseline.json must contain an integer 'field_count'")
    return baseline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="directory containing cases.json, baseline.json, and fixtures",
    )
    args = parser.parse_args(argv)
    data_dir: Path = args.data_dir

    try:
        cases = _load_cases(data_dir)
        baseline = _load_baseline(data_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"benchmark setup error: {exc}", file=sys.stderr)
        return 2

    known_misses = set(baseline["known_misses"])
    expected_field_count = len(cases) * len(FIELDS)
    if baseline["field_count"] != expected_field_count:
        print(
            "benchmark setup error: baseline field_count is "
            f"{baseline['field_count']}, but the corpus has {expected_field_count}; "
            "update benchmarks/extraction/baseline.json",
            file=sys.stderr,
        )
        return 2

    valid_keys = {f"{case['id']}.{field}" for case in cases for field in FIELDS}
    unknown_baseline_keys = known_misses - valid_keys
    if unknown_baseline_keys:
        print(
            "benchmark setup error: baseline names unknown fields: "
            + ", ".join(sorted(unknown_baseline_keys)),
            file=sys.stderr,
        )
        return 2

    actual_misses: set[str] = set()
    resolved: set[str] = set()
    matched_fields = 0
    print(f"{'case':<28} {'field':<16} {'result':<7} expected | actual")
    print("-" * 120)

    try:
        for case in cases:
            text = _load_fixture(data_dir, case)
            details = extract_case_details(text)
            for field in FIELDS:
                key = f"{case['id']}.{field}"
                expected = case["expected"][field]
                actual = details.get(field, "")
                if not isinstance(actual, str):
                    actual = "" if actual is None else str(actual)
                passed = _matches(field, expected, actual)
                if passed:
                    matched_fields += 1
                    if key in known_misses:
                        resolved.add(key)
                        status = "fixed"
                    else:
                        status = "pass"
                else:
                    actual_misses.add(key)
                    status = "known" if key in known_misses else "NEW"
                print(
                    f"{case['id']:<28} {field:<16} {status:<7} "
                    f"{_preview(expected)} | {_preview(actual)}"
                )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"benchmark setup error: {exc}", file=sys.stderr)
        return 2

    new_misses = actual_misses - known_misses
    known_count = len(actual_misses & known_misses)
    total = len(cases) * len(FIELDS)
    print("-" * 120)
    print(
        f"{matched_fields}/{total} fields matched "
        f"({len(cases)} cases x {len(FIELDS)} fields); "
        f"{known_count} known miss(es), {len(new_misses)} new miss(es)"
    )
    if resolved:
        print(
            "Baseline can be tightened; these known misses now pass: "
            + ", ".join(sorted(resolved))
        )
    if new_misses:
        print(
            "Extraction benchmark regression: "
            + ", ".join(sorted(new_misses)),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
