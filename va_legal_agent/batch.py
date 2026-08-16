"""Per-run_id batch tracking for fleet monitoring.

Each CLI invocation that participates in a batch appends its terminal outcome
(``analysis_complete`` or ``analysis_failed``) to a JSON Lines state file
keyed by ``run_id``. When the batch reaches its declared size, the outcomes
are aggregated and a ``batch_summary`` event is emitted, then the state file
is removed.

The state directory is configurable via ``BATCH_STATE_DIR`` (defaults to the
system temp dir under ``va_legal_agent_batches/``), keeping the default
footprint small and out of the project tree.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def batch_state_dir() -> Path:
    """Return the directory holding per-run_id batch state files."""
    override = os.getenv("BATCH_STATE_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "va_legal_agent_batches"


class BatchTracker:
    """Append and aggregate terminal outcomes for one run_id.

    The state file is a JSON Lines file at ``<state_dir>/<run_id>.jsonl``;
    each line is one outcome. Files accumulate until the batch reaches its
    declared size and :meth:`finalize` removes them.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        # Sanitize so the id can't escape the state directory or contain slashes.
        safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "._-") or "run"
        self.path = batch_state_dir() / f"{safe}.jsonl"

    def record(self, outcome: dict[str, object]) -> None:
        """Append one terminal outcome line to this run_id's state file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(outcome, ensure_ascii=False) + "\n")

    def outcomes(self) -> list[dict[str, object]]:
        """Read all recorded outcomes, skipping malformed lines."""
        if not self.path.exists():
            return []
        results: list[dict[str, object]] = []
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed batch line in %s", self.path)
        except OSError as exc:
            logger.warning("Could not read batch state %s: %s", self.path, exc)
        return results

    def summary(self) -> dict[str, object]:
        """Aggregate recorded outcomes into a per-run_id summary."""
        outcomes = self.outcomes()
        completed = [o for o in outcomes if o.get("event") == "analysis_complete"]
        failed = [o for o in outcomes if o.get("event") == "analysis_failed"]
        scores = [float(o["coverage_score"]) for o in completed if o.get("coverage_score") is not None]
        return {
            "run_id": self.run_id,
            "total": len(outcomes),
            "completed": len(completed),
            "failed": len(failed),
            "coverage_mean": round(sum(scores) / len(scores), 4) if scores else None,
            "coverage_min": min(scores) if scores else None,
            "coverage_max": max(scores) if scores else None,
            "failed_issues": [str(o.get("issue", "")) for o in failed],
            "search_telemetry": self._search_telemetry(outcomes),
        }

    @staticmethod
    def _search_telemetry(outcomes: list[dict[str, object]]) -> dict[str, object]:
        """Roll per-provider search stats up across all outcomes.

        Each outcome may carry a ``search_telemetry`` list of per-provider
        records (provider, queries_issued, results, deduped, failures, and a
        per-variant breakdown). All records are flattened and summed by
        provider name so the batch summary shows one row per provider
        (reusing the same rollup as the analysis output, including the
        per-variant ``variants`` dict).
        """
        from .providers import rollup_search_telemetry

        records: list[dict[str, object]] = []
        for outcome in outcomes:
            raw = outcome.get("search_telemetry")
            if not isinstance(raw, list):
                continue
            records.extend(r for r in raw if isinstance(r, dict))
        return rollup_search_telemetry(records)

    def finalize(self) -> dict[str, object]:
        """Aggregate the batch summary and delete the state file."""
        summary = self.summary()
        try:
            self.path.unlink()
        except OSError as exc:
            logger.warning("Could not remove batch state %s: %s", self.path, exc)
        return summary
