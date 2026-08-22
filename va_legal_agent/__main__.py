from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .agent import analyze_cases_for_claim
from .batch import BatchTracker
from .config import get_settings
from .models import ClaimElement, Contradiction, LegalAnalysis, PrincipleFinding, StatuteOutcomeRow
from .providers import (
    recall_flags,
    resolve_search_providers,
    validate_search_providers,
)

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LOG_FORMATS = ("text", "json")
_TRUTHY = {"1", "true", "yes", "on"}

# Dedicated channel for pipeline-relevant events (e.g. analysis completion).
# It pins its own level so the completion marker is emitted even when
# --log-level suppresses INFO diagnostics; automation can rely on it.
_EVENTS_LOGGER = logging.getLogger("va_legal_agent.events")
_EVENTS_LOGGER.setLevel(logging.INFO)


def _resolve_log_format(cli_value: str | None) -> str:
    """Pick the effective log format: CLI flag wins, then LOG_JSON env, then text."""
    if cli_value:
        return cli_value
    if os.getenv("LOG_JSON", "").strip().lower() in _TRUTHY:
        return "json"
    return "text"


class _JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line for machine parsing."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key in (
            "event",
            "run_id",
            "coverage_score",
            "interpretation_source",
            "courtlistener_quota",
            "issue",
            "error",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        for key, value in getattr(record, "data", {}).items():
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def _configure_logging(level: str, log_format: str = "text") -> None:
    """Route package log output to stderr at the given level.

    Log records always go to stderr so stdout stays reserved for the
    analysis output (JSON/text/CSV) and the ``--show-config`` dump.
    """
    handler = logging.StreamHandler(sys.stderr)
    if log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        handlers=[handler],
        force=True,
    )


def _emit_completion_event(analysis: LegalAnalysis, run_id: str) -> None:
    """Emit a structured completion event pipelines can key off of.

    The record goes through ``va_legal_agent.events``, which always emits at
    INFO regardless of the configured ``--log-level``, and carries
    ``coverage_score`` / ``interpretation_source`` as structured fields so
    JSON log consumers can parse them without touching the analysis output.
    ``run_id`` correlates this event with the run that produced it. When the
    run pre-flighted CourtListener's daily window, the remaining quota and
    its reset time ride along so schedulers can pace the next batch without
    a separate api-usage call.
    """
    quota = analysis.courtlistener_quota
    message = "analysis complete (run=%s, coverage=%.2f, source=%s)"
    args: list[object] = [run_id, analysis.coverage_score, analysis.interpretation_source]
    extra: dict[str, object] = {
        "event": "analysis_complete",
        "run_id": run_id,
        "coverage_score": analysis.coverage_score,
        "interpretation_source": analysis.interpretation_source,
    }
    if quota:
        message += ", courtlistener=%s/%s remaining, resets %s"
        args.extend(
            [quota.get("remaining"), quota.get("limit"), quota.get("reset_at") or "unknown"]
        )
        extra["courtlistener_quota"] = quota
    _EVENTS_LOGGER.info(message, *args, extra=extra)


def _emit_failure_event(issue: str, error: BaseException, run_id: str) -> None:
    """Emit a structured failure event pipelines can key off of.

    Mirrors :func:`_emit_completion_event` so automation gets a definitive
    terminal signal on both success and failure; the original exception is
    re-raised by the caller so exit behavior is unchanged.
    """
    _EVENTS_LOGGER.error(
        "analysis failed for %s (run=%s): %s",
        issue,
        run_id,
        error,
        extra={
            "event": "analysis_failed",
            "run_id": run_id,
            "issue": issue,
            "error": str(error),
        },
    )


def _run_id(cli_value: str | None = None) -> str:
    """Return the run id: CLI flag wins, then RUN_ID env, else a fresh id.

    Batch orchestrators can set RUN_ID (once per batch) or pass --run-id so
    all per-issue events share a common correlation id across CLI invocations.
    """
    return cli_value or os.getenv("RUN_ID") or uuid.uuid4().hex


def _emit_batch_summary(summary: dict[str, object]) -> None:
    """Emit a structured batch summary event for fleet monitoring.

    Aggregates the terminal outcomes recorded for a run_id once the batch
    reaches its declared size (--batch-size).
    """
    _EVENTS_LOGGER.info(
        "batch summary (run=%s, total=%s, completed=%s, failed=%s)",
        summary.get("run_id"),
        summary.get("total"),
        summary.get("completed"),
        summary.get("failed"),
        extra={"event": "batch_summary", "run_id": summary.get("run_id"), "data": summary},
    )


def _mask_secret(value: str | None) -> str | None:
    """Mask a secret, keeping first/last 4 characters (or ``***`` for short values)."""
    if not isinstance(value, str) or not value:
        return value
    return f"{value[:4]}...{value[-4:]}" if len(value) > 8 else "***"


def _mask_proxy_url(value: str | None) -> str | None:
    """Mask any credentials embedded in a proxy URL for safe display.

    ``SEARCH_HTTP_PROXY`` may carry a ``user:pass@`` authority (e.g.
    ``http://user:pass@residential-proxy.example:8080``), and ``--show-config``
    must never echo that password. The entire userinfo authority is redacted
    to ``***`` (never partially revealed, since it can hold a password) while
    the ``host:port`` stays visible so operators can still see which proxy is
    configured. A proxy without userinfo is returned unchanged; an unparseable
    value is masked wholesale rather than leaked.
    """
    if not isinstance(value, str) or not value:
        return value
    try:
        parts = urlsplit(value)
    except ValueError:
        return "***"
    if parts.username is None and parts.password is None:
        return value
    # Rebuild the authority with the userinfo fully redacted, re-bracketing an
    # IPv6 host if present.
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"***@{host}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _redacted_settings() -> dict[str, object]:
    """Return the resolved settings as a dict, masking every API key.

    ``effective_search_providers`` shows the post-validation provider list
    (typos removed), alongside the raw ``search_providers`` value, so
    operators see both what they typed and what will actually run. Any field
    that holds a credential (OpenAI, CourtListener) is masked so the dump is
    safe to share in issues or logs.
    """
    data: dict[str, object] = asdict(get_settings())
    data["effective_search_providers"] = resolve_search_providers()
    for key in ("openai_api_key", "courtlistener_api_key"):
        data[key] = _mask_secret(data.get(key))
    # The proxy URL may embed ``user:pass@``, so mask that too.
    data["search_http_proxy"] = _mask_proxy_url(data.get("search_http_proxy"))
    return data


def _maybe_emit_batch_summary(tracker: BatchTracker, batch_size: int) -> None:
    """Emit the batch summary once the declared number of outcomes is recorded."""
    if len(tracker.outcomes()) >= batch_size:
        _emit_batch_summary(tracker.finalize())


def print_settings() -> None:
    """Print the resolved settings as JSON for debugging."""
    print(json.dumps(_redacted_settings(), indent=2))


def _bullets(title: str, items: list[str]) -> list[str]:
    if not items:
        return [f"{title}: (none)"]
    return [f"{title}:"] + [f"- {item}" for item in items]


def _contradiction_label(contradiction: Contradiction) -> str:
    """Render one contradiction with both sides named, e.g. ``A v. B (Smith vs Jones)``."""
    return f"{contradiction.statement} ({contradiction.case_a} vs {contradiction.case_b})"


def _deep_summary_blocks(summaries: list[dict[str, str]]) -> list[str]:
    """Render deep-read summaries as a labeled block per case with content."""
    blocks: list[str] = []
    for entry in summaries:
        label = entry.get("case") or "(unknown case)"
        summary = (entry.get("summary") or "").strip()
        if summary:
            blocks.append(f"{label}:\n{summary}")
    return blocks


def _quota_line(quota: dict[str, object]) -> str:
    """Render the CourtListener daily-window snapshot as one readable line."""
    reset = quota.get("reset_at") or "unknown"
    return (
        f"CourtListener daily quota: {quota.get('remaining', 0)}/{quota.get('limit', 0)} "
        f"remaining (used {quota.get('used', 0)}; resets {reset})."
    )


def _analysis_to_text(analysis: LegalAnalysis) -> str:
    """Render a LegalAnalysis as a readable plain-text report."""
    deep_blocks = _deep_summary_blocks(analysis.deep_summaries)
    blocks = [
        [f"Issue: {analysis.issue}"],
        [f"Run id: {analysis.run_id or '(none)'}"],
        _bullets("Top cases", analysis.top_cases),
        [f"Summary:\n{analysis.summary or '(none)'}"],
        _bullets("Deep summaries", deep_blocks) if deep_blocks else [],
        [f"How this affects VA claims:\n{analysis.how_it_affects_va_claims or '(none)'}"],
        _bullets("Applicable principles", analysis.likely_applicable_principles),
        _bullets("Contradictions", [_contradiction_label(c) for c in analysis.contradictions]),
        _bullets("Next steps", analysis.next_steps),
        _bullets("Strengths", analysis.strengths + _search_strengths(analysis.search_telemetry)),
        _bullets("Gaps", analysis.gaps + _search_gaps(analysis.search_telemetry)),
        ([_matrix_header()] + _matrix_rows(analysis.statute_outcome_matrix))
        if analysis.statute_outcome_matrix else [],
        [f"Coverage score: {analysis.coverage_score:.2f}"],
        [f"Interpretation source: {analysis.interpretation_source}"],
        _bullets("Search telemetry", _telemetry_lines(analysis.search_telemetry)),
        [_quota_line(analysis.courtlistener_quota)] if analysis.courtlistener_quota else [],
    ]
    return "\n\n".join("\n".join(block) for block in blocks)


def _variant_hits(variants: object) -> str:
    """Format the expansion variants that returned results, e.g. ``"5107" (33)``."""
    if not isinstance(variants, dict):
        return ""
    parts: list[str] = []
    for variant, vstat in sorted(variants.items()):
        if isinstance(vstat, dict) and int(vstat.get("results", 0) or 0) > 0:
            parts.append(f"{variant} ({vstat.get('results')})")
    return ", ".join(parts)


def _telemetry_lines(telemetry: dict[str, dict[str, object]]) -> list[str]:
    """Render per-provider search stats as readable lines, naming the expansion
    variants that returned results."""
    lines: list[str] = []
    for provider, row in sorted(telemetry.items()):
        line = (
            f"{provider}: {row.get('queries_issued', 0)} queries, "
            f"{row.get('results', 0)} results, "
            f"{row.get('deduped', 0)} deduped, "
            f"{row.get('failures', 0)} failed"
        )
        hits = _variant_hits(row.get("variants"))
        if hits:
            line += f"; variant hits: {hits}"
        lines.append(line)
    return lines


def _search_strengths(telemetry: dict[str, dict[str, object]]) -> list[str]:
    """Derive positive recall notes from search telemetry for the Strengths block."""
    notes: list[str] = []
    for provider, row in sorted(telemetry.items()):
        results = int(row.get("results", 0) or 0)
        if results > 0:
            notes.append(
                f"Search provider {provider} surfaced {results} results across "
                f"{row.get('queries_issued', 0)} queries."
            )
    return notes


def _search_gaps(telemetry: dict[str, dict[str, int]]) -> list[str]:
    """Flag low-recall / failing providers as gaps (shared with search_flags)."""
    return recall_flags(telemetry)


def _element_label(element: ClaimElement) -> str:
    covered = ", ".join(element.covered_by)
    return element.name + (f" [covered by: {covered}]" if covered else "")


def _finding_label(finding: PrincipleFinding) -> str:
    sources = ", ".join(finding.source_cases)
    return finding.principle + (f" (see: {sources})" if sources else "")


def _matrix_header() -> str:
    """Column headers for the statute × court × outcome matrix."""
    return "Statute-outcome matrix:\n  Statute                                                     Court          Fav  Unf  Unk"


def _matrix_rows(rows: list[StatuteOutcomeRow]) -> list[str]:
    """Format the matrix rows as aligned columns."""
    return [
        f"  {row.statute:<60} {row.court:<15} {row.favorable:>3}  {row.unfavorable:>3}  {row.unknown:>3}"
        for row in rows
    ]


def _analysis_to_csv(analysis: LegalAnalysis) -> str:
    """Render a LegalAnalysis as a single-row CSV with a header."""
    header = [
        "run_id", "issue", "coverage_score", "interpretation_source", "summary",
        "how_it_affects_va_claims", "top_cases", "deep_summaries",
        "applicable_principles", "contradictions", "next_steps", "strengths",
        "gaps", "detected_elements", "principle_findings",
        "statute_outcome_matrix", "search_telemetry", "courtlistener_quota",
    ]
    row = [
        analysis.run_id,
        analysis.issue,
        analysis.coverage_score,
        analysis.interpretation_source,
        analysis.summary,
        analysis.how_it_affects_va_claims,
        " | ".join(analysis.top_cases),
        json.dumps(analysis.deep_summaries, sort_keys=True),
        " | ".join(analysis.likely_applicable_principles),
        " | ".join(_contradiction_label(c) for c in analysis.contradictions),
        " | ".join(analysis.next_steps),
        " | ".join(analysis.strengths),
        " | ".join(analysis.gaps),
        " | ".join(_element_label(e) for e in analysis.detected_elements),
        " | ".join(_finding_label(f) for f in analysis.principle_findings),
        json.dumps(
            [{"statute": r.statute, "court": r.court,
              "favorable": r.favorable, "unfavorable": r.unfavorable,
              "unknown": r.unknown} for r in analysis.statute_outcome_matrix],
            sort_keys=True,
        ),
        json.dumps(analysis.search_telemetry, sort_keys=True),
        json.dumps(analysis.courtlistener_quota, sort_keys=True),
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerow(row)
    return buffer.getvalue().rstrip("\n")


def render_analysis(analysis: LegalAnalysis, output_format: str) -> str:
    """Render a LegalAnalysis in the requested format (json, text, or csv)."""
    if output_format == "text":
        return _analysis_to_text(analysis)
    if output_format == "csv":
        return _analysis_to_csv(analysis)
    return analysis.model_dump_json(indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Search and analyze veterans compensation legal cases.")
    parser.add_argument("issue", nargs="?", help="The legal issue to research, such as service connection for tinnitus.")
    parser.add_argument("--type", dest="claim_type", default="Compensation", help="Benefit type to search for.")
    parser.add_argument(
        "--max-results",
        dest="max_results",
        type=int,
        default=None,
        help="Maximum case results to review (default: SEARCH_MAX_RESULTS env var, else 10).",
    )
    parser.add_argument("--no-enrich", action="store_true", help="Skip fetching case source pages for citation/date details.")
    parser.add_argument(
        "--output-format",
        dest="output_format",
        choices=("json", "text", "csv"),
        default="json",
        help="Output format for the analysis (default: json).",
    )
    parser.add_argument(
        "--output-file",
        dest="output_file",
        default=None,
        help="Write the analysis to this file instead of stdout.",
    )
    parser.add_argument("--show-config", action="store_true", help="Print the resolved settings as JSON and exit.")
    parser.add_argument(
        "--run-id",
        dest="run_id",
        default=None,
        help="Correlation id for this run (default: RUN_ID env var, else a fresh random id).",
    )
    parser.add_argument(
        "--batch-size",
        dest="batch_size",
        type=int,
        default=None,
        help="Declared number of runs sharing this run_id; emit a batch_summary event once that many complete/fail.",
    )
    parser.add_argument(
        "--max-wall-time",
        dest="max_wall_time",
        type=float,
        default=None,
        help=(
            "Cap total wall time (seconds) spent searching for this issue; 0 disables. "
            "Defaults to the SEARCH_MAX_WALL_SECONDS env var."
        ),
    )
    deep_read_group = parser.add_mutually_exclusive_group()
    deep_read_group.add_argument(
        "--deep-read",
        dest="deep_read",
        action="store_true",
        default=None,
        help=(
            "Enable deep-read mode for this run: ingest the full opinion text of "
            "the top cases and summarize it via map-reduce (overrides the "
            "DEEP_READ env var)."
        ),
    )
    deep_read_group.add_argument(
        "--no-deep-read",
        dest="deep_read",
        action="store_false",
        help="Disable deep-read mode for this run (overrides the DEEP_READ env var).",
    )
    parser.add_argument(
        "--deep-read-limit",
        dest="deep_read_limit",
        type=int,
        default=None,
        help=(
            "Top cases ingested in deep-read mode for this run (overrides the "
            "DEEP_READ_LIMIT env var; capped by the number of cases found)."
        ),
    )
    parser.add_argument(
        "--log-level",
        dest="log_level",
        choices=_LOG_LEVELS,
        default="WARNING",
        help="Verbosity of log output on stderr (default: WARNING).",
    )
    parser.add_argument(
        "--log-format",
        dest="log_format",
        choices=_LOG_FORMATS,
        default=None,
        help=(
            "Format of log lines on stderr: text or json (default: text, or "
            "json when LOG_JSON is set in the environment)."
        ),
    )
    args = parser.parse_args()

    _configure_logging(args.log_level, _resolve_log_format(args.log_format))

    # Surface misconfigured SEARCH_PROVIDERS early — including --show-config
    # runs — so typos are visible before any search happens.
    validate_search_providers()

    if args.show_config:
        print_settings()
        return

    if not args.issue:
        parser.error("the following arguments are required: issue")

    max_results = args.max_results if args.max_results is not None else get_settings().search_max_results
    run_id = _run_id(args.run_id)
    tracker = BatchTracker(run_id) if args.batch_size else None
    telemetry: list[dict[str, object]] = []

    try:
        # Always pass the telemetry sink: analyze_cases_for_claim rolls it up
        # into the output's search_telemetry / search_flags fields, so a plain
        # single-issue run carries the same recall picture as a batch run.
        analysis = analyze_cases_for_claim(
            args.issue,
            claim_type=args.claim_type,
            max_results=max(max_results, 1),
            enrich=not args.no_enrich,
            telemetry=telemetry,
            max_wall_seconds=args.max_wall_time,
            deep_read=args.deep_read,
            deep_read_limit=args.deep_read_limit,
        )
    except Exception as exc:  # noqa: BLE001 - emit event, then preserve original behavior
        _emit_failure_event(args.issue, exc, run_id)
        if tracker:
            tracker.record(
                {
                    "event": "analysis_failed",
                    "issue": args.issue,
                    "error": str(exc),
                    "search_telemetry": telemetry,
                }
            )
            _maybe_emit_batch_summary(tracker, args.batch_size)
        raise
    analysis.run_id = run_id
    _emit_completion_event(analysis, run_id)
    if tracker:
        tracker.record(
            {
                "event": "analysis_complete",
                "issue": args.issue,
                "coverage_score": analysis.coverage_score,
                "search_telemetry": telemetry,
            }
        )
        _maybe_emit_batch_summary(tracker, args.batch_size)
    output = render_analysis(analysis, args.output_format)
    if args.output_file:
        try:
            Path(args.output_file).write_text(output + "\n", encoding="utf-8")
        except OSError as exc:
            parser.error(f"could not write output file: {exc}")
        return
    print(output)


if __name__ == "__main__":
    main()
