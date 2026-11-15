#!/usr/bin/env python3
"""Run the hand-labeled real-opinion citator attribution benchmark.

The benchmark is deterministic and offline. Each case points at a fixture that
contains the CourtListener-shaped target metadata, one recorded citing-opinion
passage set, and a partial forward-citation page. The production ``check_case``
path is driven exactly as in the synthetic benchmark; nothing here changes
production citator behavior.

The score is passage-level target and speaker attribution, not full citator
coverage. A fixture deliberately records a partial citation page, so coverage
stays incomplete and a missed extraction can never be mistaken for a clean
``good_law`` result. Known misses and a metric floor are recorded in
``baseline.json``; CI fails on an unlisted miss or a metric regression.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from va_legal_agent.citator import check_case
from va_legal_agent.models import CaseRecord

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "benchmarks" / "citator_real"
ATTRIBUTIONS = frozenset({"authoritative", "gated", "none"})
STATUSES = frozenset({"negative", "good_law", "unknown"})
METRIC_NAMES = (
    "target_precision",
    "target_recall",
    "authoritative_precision",
    "authoritative_recall",
)


@dataclass(frozen=True)
class Prediction:
    """One negative-treatment finding normalized for label matching."""

    excerpt: str
    treatment: str
    attribution: str
    opinion_role: str
    precedential_status: str


class FixtureProvider:
    """In-memory CourtListener surface built from one recorded case fixture."""

    def __init__(self, fixture: dict[str, Any]) -> None:
        self._metadata = {
            str(key): value for key, value in (fixture.get("metadata") or {}).items()
        }
        self._texts = {
            str(key): value for key, value in (fixture.get("texts") or {}).items()
        }
        self._pages = {
            (int(page["opinion_id"]), page.get("cursor") or None): page
            for page in (fixture.get("edges") or [])
        }

    def lookup_citations(self, text: str) -> list[dict[str, object]]:
        return []

    def citing_edges(
        self, opinion_id: int, page_size: int = 100, cursor: str | None = None
    ) -> dict[str, object]:
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
                        f"{citing_id}/"
                    )
                }
                for citing_id in citing_ids
            ],
            "next": page.get("next") or None,
        }

    def opinion_metadata(self, opinion_id: int) -> dict[str, object] | None:
        return self._metadata.get(str(opinion_id))

    def fetch_opinion_text(self, opinion_id: int) -> str:
        return str(self._texts.get(str(opinion_id)) or "")

    def same_docket_decisions(
        self, opinion_id: int, max_results: int = 5
    ) -> list[dict[str, str]]:
        return []


def _normalize(value: str) -> str:
    """Normalize whitespace and typographic punctuation for label matching."""
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u2018", "'").replace("\u2019", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"')
    value = value.replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", value).strip().casefold()


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
        if not isinstance(case.get("fixture"), str):
            raise ValueError(f"{case_id}: missing fixture path")
        if case.get("expected_status") not in STATUSES:
            raise ValueError(f"{case_id}: invalid expected_status")
        gold = case.get("gold")
        if not isinstance(gold, list) or not gold:
            raise ValueError(f"{case_id}: gold must be a non-empty list")

        label_ids: set[str] = set()
        for label in gold:
            if not isinstance(label, dict):
                raise ValueError(f"{case_id}: every gold entry must be an object")
            label_id = label.get("id")
            if not isinstance(label_id, str) or not label_id:
                raise ValueError(f"{case_id}: every gold entry needs an id")
            if label_id in label_ids:
                raise ValueError(f"{case_id}: duplicate gold id: {label_id}")
            label_ids.add(label_id)
            for field in ("citing_id", "excerpt", "treatment", "kind"):
                if not isinstance(label.get(field), str):
                    raise ValueError(f"{case_id}: {label_id} has invalid {field}")
            if not label["excerpt"].strip():
                raise ValueError(f"{case_id}: {label_id} has an empty excerpt")
            if label.get("attribution") not in ATTRIBUTIONS:
                raise ValueError(f"{case_id}: {label_id} has invalid attribution")
            if label["attribution"] != "none" and not label["treatment"]:
                raise ValueError(
                    f"{case_id}: {label_id} needs a treatment for a positive label"
                )
    return cases


def _load_baseline(data_dir: Path) -> tuple[set[str], dict[str, float]]:
    path = data_dir / "baseline.json"
    if not path.exists():
        return set(), {}
    baseline = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(baseline, dict):
        raise ValueError("baseline.json must contain a JSON object")
    known_misses = baseline.get("known_misses")
    if not isinstance(known_misses, list) or not all(
        isinstance(item, str) for item in known_misses
    ):
        raise ValueError("baseline.json must contain a 'known_misses' string list")
    floor = baseline.get("metric_floor", {})
    if not isinstance(floor, dict):
        raise ValueError("baseline.json metric_floor must be an object")
    unknown = set(floor) - set(METRIC_NAMES)
    if unknown:
        raise ValueError(
            "baseline.json names unknown metric floors: " + ", ".join(sorted(unknown))
        )
    for name, value in floor.items():
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise ValueError(f"baseline.json metric floor {name} must be in [0, 1]")
    return set(known_misses), {name: float(value) for name, value in floor.items()}


def _load_fixture(data_dir: Path, case: dict[str, Any]) -> dict[str, Any]:
    fixture_path = (data_dir / case["fixture"]).resolve()
    data_root = data_dir.resolve()
    if data_root not in fixture_path.parents:
        raise ValueError(f"{case['id']}: fixture escapes the benchmark directory")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(fixture, dict):
        raise ValueError(f"{case['id']}: fixture must be a JSON object")
    if fixture.get("id") != case["id"]:
        raise ValueError(f"{case['id']}: fixture id does not match")
    capture = fixture.get("capture")
    if not isinstance(capture, dict):
        raise ValueError(f"{case['id']}: fixture needs a capture object")
    for field in ("retrieved", "method", "source_urls", "api_endpoints"):
        if field not in capture:
            raise ValueError(f"{case['id']}: fixture capture is missing {field}")
    target = fixture.get("target_case")
    if not isinstance(target, dict):
        raise ValueError(f"{case['id']}: fixture needs a target_case object")
    try:
        CaseRecord(**target)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{case['id']}: invalid target_case: {exc}") from exc

    metadata = fixture.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError(f"{case['id']}: fixture needs a metadata object")
    texts = fixture.get("texts")
    if not isinstance(texts, dict) or not texts:
        raise ValueError(f"{case['id']}: fixture needs a non-empty texts object")
    for key, value in texts.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            raise ValueError(f"{case['id']}: invalid text entry")
    target_id = str(target.get("courtlistener_opinion_id") or "")
    target_metadata = metadata.get(target_id)
    if not isinstance(target_metadata, dict):
        raise ValueError(f"{case['id']}: target metadata is missing")
    if target_metadata.get("cluster_resolved") is not True:
        raise ValueError(f"{case['id']}: target cluster must be resolved")

    edges = fixture.get("edges")
    if not isinstance(edges, list) or not edges:
        raise ValueError(f"{case['id']}: fixture needs a non-empty edges list")
    edge_keys: set[tuple[int, str | None]] = set()
    citing_ids: set[str] = set()
    for page in edges:
        if not isinstance(page, dict):
            raise ValueError(f"{case['id']}: every edge page must be an object")
        key = (int(page.get("opinion_id", 0)), page.get("cursor") or None)
        if key in edge_keys:
            raise ValueError(f"{case['id']}: duplicate edge page {key}")
        edge_keys.add(key)
        for citing_id in page.get("citing_ids") or []:
            citing_ids.add(str(citing_id))
        if int(page.get("count", 0)) < len(page.get("citing_ids") or []):
            raise ValueError(f"{case['id']}: edge count is below its candidate set")

    excerpts: set[str] = set()
    for label in case["gold"]:
        citing_id = str(label["citing_id"])
        if citing_id not in texts:
            raise ValueError(
                f"{case['id']}: {label['id']} cites an absent text {citing_id}"
            )
        if citing_id not in citing_ids:
            raise ValueError(
                f"{case['id']}: {label['id']} cites an opinion absent from edges"
            )
        normalized = _normalize(label["excerpt"])
        if normalized not in _normalize(texts[citing_id]):
            raise ValueError(
                f"{case['id']}: {label['id']} excerpt is not verbatim in the fixture"
            )
        if normalized in excerpts:
            raise ValueError(
                f"{case['id']}: {label['id']} duplicates another gold excerpt"
            )
        excerpts.add(normalized)
    return fixture


def _predictions(check: object) -> list[Prediction]:
    findings: list[tuple[object, str]] = [
        *((finding, "authoritative") for finding in check.negative_treatments),
        *((finding, "gated") for finding in check.gated_treatment_findings),
    ]
    return [
        Prediction(
            excerpt=str(getattr(finding, "excerpt", "")),
            treatment=str(getattr(finding, "treatment", "")).casefold(),
            attribution=attribution,
            opinion_role=str(getattr(finding, "opinion_role", "")),
            precedential_status=str(getattr(finding, "precedential_status", "")),
        )
        for finding, attribution in findings
    ]


def _evaluate(case: dict[str, Any], check: object) -> dict[str, Any]:
    positives = [
        label for label in case["gold"] if label["attribution"] != "none"
    ]
    controls = [label for label in case["gold"] if label["attribution"] == "none"]
    positives_by_excerpt = {
        _normalize(label["excerpt"]): label for label in positives
    }
    controls_by_excerpt = {
        _normalize(label["excerpt"]): label for label in controls
    }
    predictions = _predictions(check)
    covered: set[str] = set()
    details: list[str] = []
    fp = 0
    speaker_correct = 0
    treatment_correct = 0
    strict_tp = 0
    control_fp = 0

    for prediction in predictions:
        key = _normalize(prediction.excerpt)
        label = positives_by_excerpt.get(key)
        if label is not None and key not in covered:
            covered.add(key)
            bucket_ok = prediction.attribution == label["attribution"]
            treatment_ok = prediction.treatment == _normalize(label["treatment"])
            speaker_correct += int(bucket_ok)
            treatment_correct += int(treatment_ok)
            if bucket_ok and treatment_ok:
                strict_tp += 1
            else:
                details.append(
                    f"{label['id']}: expected {label['attribution']}/"
                    f"{label['treatment']}, got {prediction.attribution}/"
                    f"{prediction.treatment}"
                )
            continue
        fp += 1
        if key in controls_by_excerpt:
            control_fp += 1
            details.append(
                f"{controls_by_excerpt[key]['id']}: unexpected "
                f"{prediction.attribution}/{prediction.treatment}"
            )
        elif key in positives_by_excerpt:
            details.append(
                f"{positives_by_excerpt[key]['id']}: duplicate prediction "
                f"{prediction.attribution}/{prediction.treatment}"
            )
        else:
            details.append(
                "unmatched prediction: "
                f"{prediction.attribution}/{prediction.treatment}: "
                f"{prediction.excerpt}"
            )

    fn = 0
    for label in positives:
        if _normalize(label["excerpt"]) not in covered:
            fn += 1
            details.append(f"{label['id']}: no target-bound finding")

    tp = len(covered)
    authoritative_predictions = [
        prediction
        for prediction in predictions
        if prediction.attribution == "authoritative"
    ]
    authoritative_labels = [
        label for label in positives if label["attribution"] == "authoritative"
    ]
    authoritative_keys = {_normalize(label["excerpt"]) for label in authoritative_labels}
    authoritative_covered: set[str] = set()
    authoritative_correct = 0
    for prediction in authoritative_predictions:
        key = _normalize(prediction.excerpt)
        if key in authoritative_keys and key not in authoritative_covered:
            authoritative_covered.add(key)
            authoritative_correct += 1
    authoritative_fp = len(authoritative_predictions) - authoritative_correct

    gated_predictions = [
        prediction for prediction in predictions if prediction.attribution == "gated"
    ]
    gated_labels = [label for label in positives if label["attribution"] == "gated"]
    gated_keys = {_normalize(label["excerpt"]) for label in gated_labels}
    gated_covered: set[str] = set()
    gated_correct = 0
    for prediction in gated_predictions:
        key = _normalize(prediction.excerpt)
        if key in gated_keys and key not in gated_covered:
            gated_covered.add(key)
            gated_correct += 1
    gated_fp = len(gated_predictions) - gated_correct

    status_safe = not (check.status == "good_law" and bool(positives))
    case_pass = (
        fp == 0
        and fn == 0
        and strict_tp == tp
        and status_safe
    )
    return {
        "id": case["id"],
        "gold": len(positives),
        "pred": len(predictions),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "strict_tp": strict_tp,
        "speaker_correct": speaker_correct,
        "treatment_correct": treatment_correct,
        "matched": tp,
        "control_count": len(controls),
        "control_fp": control_fp,
        "authoritative_predictions": len(authoritative_predictions),
        "authoritative_correct": authoritative_correct,
        "authoritative_gold": len(authoritative_labels),
        "authoritative_covered": len(authoritative_covered),
        "authoritative_fp": authoritative_fp,
        "gated_predictions": len(gated_predictions),
        "gated_correct": gated_correct,
        "gated_gold": len(gated_labels),
        "gated_covered": len(gated_covered),
        "gated_fp": gated_fp,
        "status": check.status,
        "expected_status": case["expected_status"],
        "status_match": check.status == case["expected_status"],
        "status_safe": status_safe,
        "case_pass": case_pass,
        "details": details,
    }


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _summary(results: list[dict[str, Any]]) -> dict[str, float]:
    tp = sum(result["tp"] for result in results)
    fp = sum(result["fp"] for result in results)
    fn = sum(result["fn"] for result in results)
    matched = sum(result["matched"] for result in results)
    authoritative_predictions = sum(
        result["authoritative_predictions"] for result in results
    )
    authoritative_correct = sum(
        result["authoritative_correct"] for result in results
    )
    authoritative_gold = sum(result["authoritative_gold"] for result in results)
    authoritative_covered = sum(
        result["authoritative_covered"] for result in results
    )
    return {
        "target_precision": _ratio(tp, tp + fp),
        "target_recall": _ratio(tp, tp + fn),
        "authoritative_precision": _ratio(
            authoritative_correct, authoritative_predictions
        ),
        "authoritative_recall": _ratio(
            authoritative_covered, authoritative_gold
        ),
        "speaker_accuracy": _ratio(
            sum(result["speaker_correct"] for result in results), matched
        ),
        "treatment_accuracy": _ratio(
            sum(result["treatment_correct"] for result in results), matched
        ),
        "tp": float(tp),
        "fp": float(fp),
        "fn": float(fn),
        "matched": float(matched),
        "control_count": float(sum(result["control_count"] for result in results)),
        "control_fp": float(sum(result["control_fp"] for result in results)),
        "status_matches": float(sum(result["status_match"] for result in results)),
        "status_safe": float(all(result["status_safe"] for result in results)),
    }


def _preview(value: str, width: int = 120) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= width:
        return value
    return value[: width - 3] + "..."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="directory containing cases.json, fixtures/, and baseline.json",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every unmatched prediction and missing label",
    )
    args = parser.parse_args(argv)
    data_dir: Path = args.data_dir

    try:
        cases = _load_cases(data_dir)
        known_misses, metric_floor = _load_baseline(data_dir)
        if known_misses - {case["id"] for case in cases}:
            raise ValueError(
                "baseline names unknown cases: "
                + ", ".join(sorted(known_misses - {case["id"] for case in cases}))
            )
        fixtures = {case["id"]: _load_fixture(data_dir, case) for case in cases}
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"real citator benchmark setup error: {exc}", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    new_misses: list[str] = []
    fixed: list[str] = []
    print(
        f"{'case':<24} {'gold':>4} {'pred':>4} {'TP':>3} {'FP':>3} "
        f"{'FN':>3} {'P':>7} {'R':>7} {'status':<9} result"
    )
    print("-" * 108)

    for case in cases:
        try:
            check = check_case(
                CaseRecord(**fixtures[case["id"]]["target_case"]),
                FixtureProvider(fixtures[case["id"]]),
                max_citing_opinions=5,
                max_pages=1,
            )
            result = _evaluate(case, check)
        except (KeyError, TypeError, ValueError) as exc:
            result = {
                "id": case["id"],
                "gold": 0,
                "pred": 0,
                "tp": 0,
                "fp": 0,
                "fn": 0,
                "strict_tp": 0,
                "speaker_correct": 0,
                "treatment_correct": 0,
                "matched": 0,
                "control_count": 0,
                "control_fp": 0,
                "authoritative_predictions": 0,
                "authoritative_correct": 0,
                "authoritative_gold": 0,
                "authoritative_covered": 0,
                "authoritative_fp": 0,
                "gated_predictions": 0,
                "gated_correct": 0,
                "gated_gold": 0,
                "gated_covered": 0,
                "gated_fp": 0,
                "status": "unknown",
                "expected_status": case["expected_status"],
                "status_match": False,
                "status_safe": True,
                "case_pass": False,
                "details": [f"runtime error: {exc}"],
            }
        results.append(result)

        if result["case_pass"]:
            outcome = "fixed" if case["id"] in known_misses else "pass"
            if outcome == "fixed":
                fixed.append(case["id"])
        elif case["id"] in known_misses:
            outcome = "known"
        else:
            outcome = "NEW"
            new_misses.append(case["id"])

        precision = _ratio(result["tp"], result["tp"] + result["fp"])
        recall = _ratio(result["tp"], result["tp"] + result["fn"])
        status = result["status"] + ("" if result["status_safe"] else "!")
        print(
            f"{case['id']:<24} {result['gold']:>4} {result['pred']:>4} "
            f"{result['tp']:>3} {result['fp']:>3} {result['fn']:>3} "
            f"{_percent(precision):>7} {_percent(recall):>7} "
            f"{status:<9} {outcome}"
        )
        if args.verbose:
            for detail in result["details"]:
                print(f"  {case['id']}: {_preview(detail)}")

    summary = _summary(results)
    total = len(cases)
    passed = sum(1 for result in results if result["case_pass"])
    known_count = sum(
        1
        for result in results
        if not result["case_pass"] and result["id"] in known_misses
    )
    print("-" * 108)
    print(
        f"cases: {passed}/{total} exact; {known_count} known miss(es), "
        f"{len(new_misses)} new miss(es)"
    )
    print(
        "target attribution: "
        f"precision {_percent(summary['target_precision'])} "
        f"({int(summary['tp'])} TP / {int(summary['fp'])} FP); "
        f"recall {_percent(summary['target_recall'])} "
        f"({int(summary['tp'])} TP / {int(summary['fn'])} FN)"
    )
    print(
        "authoritative finding: "
        f"precision {_percent(summary['authoritative_precision'])}; "
        f"recall {_percent(summary['authoritative_recall'])}"
    )
    print(
        "matched-passage attribution: "
        f"speaker/role {_percent(summary['speaker_accuracy'])}, "
        f"treatment {_percent(summary['treatment_accuracy'])} "
        f"({int(summary['matched'])} matched)"
    )
    print(
        f"controls: {int(summary['control_count'])} passages; "
        f"{int(summary['control_fp'])} predicted as treatment"
    )
    print(
        f"status: {int(summary['status_matches'])}/{total} match expected; "
        f"unsafe good_law: {'yes' if not summary['status_safe'] else 'no'}"
    )

    floor_breaches: list[str] = []
    for name in METRIC_NAMES:
        if name in metric_floor and summary[name] + 1e-9 < metric_floor[name]:
            floor_breaches.append(
                f"{name} {_percent(summary[name])} < "
                f"{_percent(metric_floor[name])}"
            )
    if metric_floor:
        print("metric floor: " + ("ok" if not floor_breaches else "; ".join(floor_breaches)))
    if fixed:
        print(
            "Baseline can be tightened; these known misses now pass: "
            + ", ".join(fixed)
        )

    if new_misses:
        print(
            "Real citator benchmark regression: " + ", ".join(new_misses),
            file=sys.stderr,
        )
    if floor_breaches:
        print(
            "Real citator metric regression: " + "; ".join(floor_breaches),
            file=sys.stderr,
        )
    if not summary["status_safe"]:
        print(
            "Real citator benchmark safety failure: good_law returned for a case "
            "with a gold negative passage",
            file=sys.stderr,
        )
    if new_misses or floor_breaches or not summary["status_safe"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
