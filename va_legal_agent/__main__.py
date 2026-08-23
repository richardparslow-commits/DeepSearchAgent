from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .agent import analyze_cases_for_claim
from .batch import BatchTracker
from .config import get_settings
from .interpretation import detect_claim_elements
from .models import ClaimElement, Contradiction, LegalAnalysis, PrincipleFinding, StatuteOutcomeRow
from .planning import decompose_issue, plan_queries
from .providers import (
    courtlistener_daily_budget,
    courtlistener_minute_budget,
    fetch_courtlistener_usage,
    recall_flags,
    resolve_search_providers,
    validate_search_providers,
)
from .search import SearchError

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
        [f"Coverage score: {analysis.coverage_score:.2f} (confidence: {analysis.coverage_confidence})"],
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
        "run_id", "issue", "coverage_score", "coverage_confidence", "interpretation_source", "summary",
        "how_it_affects_va_claims", "top_cases", "deep_summaries",
        "applicable_principles", "contradictions", "next_steps", "strengths",
        "gaps", "detected_elements", "principle_findings",
        "statute_outcome_matrix", "search_telemetry", "courtlistener_quota",
    ]
    row = [
        analysis.run_id,
        analysis.issue,
        analysis.coverage_score,
        analysis.coverage_confidence,
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


# ---------------------------------------------------------------------------
# Dry-run estimation (--dry-run): report request cost, quota impact, and
# wall time for an issue without executing any searches. All math is
# deterministic -- no network call unless CourtListener is configured and
# the usage guard is enabled, in which case the live api-usage endpoint is
# read (which has its own throttle and never burns the search budget).
# ---------------------------------------------------------------------------

_HOUR_RATE_RE = re.compile(r"^\d+/hour$")


def _hourly_budget(usage: dict[str, object]) -> dict[str, object] | None:
    """The user-scope hourly rate row from an api-usage payload, or None."""
    for row in usage.get("current_usage") or []:
        if (
            isinstance(row, dict)
            and row.get("scope") == "user"
            and _HOUR_RATE_RE.match(str(row.get("rate", "")))
        ):
            return row
    return None


def _pacing_interval_seconds(provider: str) -> float:
    """The enforced gap between sequential requests to *provider*.

    Mirrors ``_throttle``: the tighter of the global
    ``SEARCH_MIN_INTERVAL_SECONDS`` gap and the per-provider
    ``SEARCH_MAX_RPM_BY_PROVIDER`` budget (60/N seconds).
    """
    settings = get_settings()
    rpm = settings.search_max_rpm_by_provider.get(provider, 0)
    return max(settings.search_min_interval_seconds, 60.0 / rpm if rpm > 0 else 0.0)


def _worst_case_backoff_seconds() -> float:
    """Worst-case retry backoff for a single request (all retries, max jitter)."""
    settings = get_settings()
    total = 0.0
    for attempt in range(1, settings.search_retry_attempts + 1):
        bounded = min(
            settings.search_backoff_base_seconds * (2**attempt),
            settings.search_backoff_max_seconds,
        )
        total += bounded * 1.25  # worst-case jitter multiplier
    return total


def estimate_issue_run(
    issue: str,
    claim_type: str = "Compensation",
    max_results: int = 10,
    deep_read: bool | None = None,
    deep_read_limit: int | None = None,
    usage: dict[str, object] | None = None,
    usage_error: str | None = None,
) -> dict[str, object]:
    """Estimate the request cost, quota impact, and wall time of a real run.

    Reuses the deterministic planner (``decompose_issue`` / ``plan_queries``)
    for the round-1 query list, models worst-case gap-refinement rounds the
    same way the research loop bounds them (one gap query per detected claim
    element per round, capped by ``SEARCH_MAX_REFINEMENT_ROUNDS``), and --
    when CourtListener is configured with the usage guard enabled -- fetches
    live usage (once per call unless the caller passes *usage* or
    *usage_error* for batch reuse) to answer whether the run fits the
    current daily/minute windows. Executes no search requests at all.
    """
    settings = get_settings()
    providers = resolve_search_providers(settings.search_providers)
    queries = plan_queries(decompose_issue(issue, claim_type))
    elements = [spec.name for spec in detect_claim_elements(issue)]
    gap_per_round = len(elements) or 1
    rounds = max(settings.search_max_refinement_rounds, 0)
    worst_queries = len(queries) + rounds * gap_per_round

    resolved_deep_read = bool(deep_read if deep_read is not None else settings.deep_read)
    resolved_dr_limit = deep_read_limit if deep_read_limit is not None else settings.deep_read_limit
    dr_fetches = (
        min(max(resolved_dr_limit, 0), max(max_results, 0)) if resolved_deep_read else 0
    )

    provider_rows: list[dict[str, object]] = []
    total_requests = 0
    nominal = 0.0
    worst_case = 0.0
    retry_chain = _worst_case_backoff_seconds()
    for name in providers:
        variants = max(
            settings.search_query_variants_by_provider.get(name, settings.search_query_variants),
            1,
        )
        pages = max(
            settings.search_pages_per_query_by_provider.get(name, settings.search_pages_per_query),
            1,
        )
        requests = worst_queries * variants * pages
        pacing = _pacing_interval_seconds(name)
        provider_rows.append(
            {
                "name": name,
                "base_queries": len(queries),
                "gap_queries": rounds * gap_per_round,
                "variants": variants,
                "pages": pages,
                "requests": requests,
                "pacing_seconds": round(pacing, 2),
            }
        )
        total_requests += requests
        nominal += requests * pacing
        worst_case += requests * (pacing + retry_chain)

    report: dict[str, object] = {
        "issue": issue,
        "claim_type": claim_type,
        "base_queries": len(queries),
        "refinement_rounds": rounds,
        "gap_queries_per_round": gap_per_round,
        "total_queries_worst_case": worst_queries,
        "providers": provider_rows,
        "deep_read": {
            "enabled": resolved_deep_read,
            "limit": resolved_dr_limit,
            "opinion_fetches": dr_fetches,
        },
        "total_requests_estimate": total_requests + dr_fetches,
        "wall_time_seconds": {
            "nominal": round(nominal, 1),
            "worst_case": round(worst_case, 1),
            "not_modeled": (
                "LLM ingestion/synthesis and deep-read summarization time"
            ),
        },
        "courtlistener": None,
        "verdict": "unchecked",
    }
    if "courtlistener" not in providers:
        return report
    if not settings.courtlistener_usage_guard:
        report["courtlistener"] = {
            "configured": True,
            "guard_enabled": False,
            "note": "COURTLISTENER_USAGE_GUARD=0 disables the pre-flight quota check",
        }
        return report
    if usage_error is not None:
        report["courtlistener"] = {
            "configured": True,
            "guard_enabled": True,
            "error": usage_error,
        }
        return report
    if usage is None:
        try:
            usage = fetch_courtlistener_usage()
        except SearchError as exc:
            report["courtlistener"] = {
                "configured": True,
                "guard_enabled": True,
                "error": str(exc),
            }
            return report
    daily = courtlistener_daily_budget(usage)
    minute = courtlistener_minute_budget(usage)
    hourly = _hourly_budget(usage)
    cl_requests = sum(
        int(row["requests"]) for row in provider_rows if row["name"] == "courtlistener"
    ) + dr_fetches
    abort_reasons: list[str] = []
    if int(daily["remaining"]) < cl_requests:
        abort_reasons.append(
            "daily window: %d of %d remaining, need %d"
            % (int(daily["remaining"]), int(daily["limit"]), cl_requests)
        )
    if dr_fetches and minute and int(minute["remaining"]) < dr_fetches:
        abort_reasons.append(
            "minute window: %d of %d remaining, deep-read needs %d"
            % (int(minute["remaining"]), int(minute["limit"]), dr_fetches)
        )
    report["courtlistener"] = {
        "configured": True,
        "guard_enabled": True,
        "requests_estimated": cl_requests,
        "daily": {key: daily.get(key) for key in ("used", "limit", "remaining", "reset_at")},
        "hourly": {key: hourly.get(key) for key in ("used", "limit", "remaining")}
        if hourly
        else None,
        "minute": {key: minute.get(key) for key in ("used", "limit", "remaining", "reset_at")}
        if minute
        else None,
        "abort_reasons": abort_reasons,
    }
    report["verdict"] = "abort" if abort_reasons else "proceed"
    return report


def _format_wall_seconds(seconds: float) -> str:
    """Render a wall-time figure as a compact human string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.2f} h"


def _dry_run_to_text(report: dict[str, object]) -> str:
    """Render a dry-run estimate as a readable text report."""
    lines = [
        "Dry run - no searches were executed and no analysis was produced.",
        "",
        f"Issue: {report['issue']}",
        f"Claim type: {report['claim_type']}",
        "",
        (
            f"Plan: {report['base_queries']} base queries; worst case "
            f"{report['total_queries_worst_case']} queries "
            f"({report['refinement_rounds']} refinement round(s) x up to "
            f"{report['gap_queries_per_round']} gap query(s) each)."
        ),
        "",
        "Requests per provider (worst-case queries x variants x pages):",
    ]
    for row in report["providers"]:  # type: ignore[union-attr]
        lines.append(
            f"  {row['name']:<14} {row['requests']:>4} requests "
            f"({row['base_queries']}+{row['gap_queries']} queries x "
            f"{row['variants']} variant(s) x {row['pages']} page(s); "
            f"pacing {row['pacing_seconds']}s/request)"
        )
    deep_read = report["deep_read"]  # type: ignore[union-attr]
    if deep_read["enabled"]:
        lines.append("")
        lines.append(
            f"Deep-read: enabled - up to {deep_read['opinion_fetches']} opinion-detail "
            f"fetches (limit {deep_read['limit']}), counted in the request totals above."
        )
    wall = report["wall_time_seconds"]  # type: ignore[union-attr]
    lines += [
        "",
        "Wall-time estimate (requests serialized at enforced pacing; LLM ingestion",
        "and deep-read summarization time is not modeled):",
        f"  nominal:    {_format_wall_seconds(float(wall['nominal']))} (pacing only, no failures)",
        f"  worst-case: {_format_wall_seconds(float(wall['worst_case']))} "
        "(every request exhausts its retry backoff)",
    ]
    cl = report.get("courtlistener")
    if cl is not None:
        lines += ["", "CourtListener quota (live api-usage endpoint):"]
        if not cl.get("guard_enabled"):
            note = cl.get("note")
            if note:
                lines.append(f"  {note}")
        elif cl.get("error"):
            lines.append(f"  (could not read usage: {cl['error']})")
        else:
            daily = cl.get("daily") or {}
            if daily:
                lines.append(
                    f"  daily:  {daily.get('remaining')} / {daily.get('limit')} remaining "
                    f"(used {daily.get('used')})"
                )
            hourly = cl.get("hourly")
            if hourly:
                lines.append(
                    f"  hourly: {hourly.get('remaining')} / {hourly.get('limit')} remaining"
                )
            minute = cl.get("minute")
            if minute:
                lines.append(
                    f"  minute: {minute.get('remaining')} / {minute.get('limit')} remaining"
                )
            reasons = cl.get("abort_reasons") or []
            if reasons:
                lines.append("  would abort:")
                lines.extend(f"    - {reason}" for reason in reasons)
            else:
                lines.append(
                    f"  fits the planned {cl.get('requests_estimated')} request(s)."
                )
    lines.append("")
    verdict = report["verdict"]
    if verdict == "abort":
        lines.append(
            "Verdict: ABORT - the real run would be stopped by the usage guard "
            "before searching."
        )
    elif verdict == "proceed":
        lines.append(
            "Verdict: PROCEED - the current windows cover the planned requests."
        )
    else:
        lines.append(
            "Verdict: unchecked - usage guard off, quota unreadable, or "
            "CourtListener not configured."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Batch dry-run (--batch-dry-run): the same per-issue estimate for many
# issues at once, with the CourtListener daily window allocated cumulatively
# across the batch so operators can schedule work against the quota. The
# api-usage endpoint is fetched at most once for the whole batch.
# ---------------------------------------------------------------------------


def batch_dry_run(
    issues: list[str],
    claim_type: str = "Compensation",
    max_results: int = 10,
    deep_read: bool | None = None,
    deep_read_limit: int | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Estimate each issue and allocate the CourtListener daily window across them.

    Fetches live usage once (when CourtListener is configured and the usage
    guard is on), then runs :func:`estimate_issue_run` per issue with that
    payload shared. Verdicts are re-derived cumulatively: each issue's
    CourtListener requests are consumed from the running daily remainder, so
    later issues abort when the window would run dry. Returns ``(rows,
    summary)`` where every row carries a flat ``courtlistener_requests`` and
    ``courtlistener_after`` field for table rendering.
    """
    settings = get_settings()
    usage_error: str | None = None
    usage: dict[str, object] | None = None
    if (
        "courtlistener" in resolve_search_providers(settings.search_providers)
        and settings.courtlistener_usage_guard
    ):
        try:
            usage = fetch_courtlistener_usage()
        except SearchError as exc:
            usage_error = str(exc)
    daily_before: int | None = None
    daily_limit: int | None = None
    if usage is not None:
        try:
            daily = courtlistener_daily_budget(usage)
            daily_before = int(daily["remaining"])
            daily_limit = int(daily["limit"])
        except SearchError:  # payload shape changed; degrade to no-window info
            pass

    rows: list[dict[str, object]] = []
    remaining = daily_before
    for issue in issues:
        row = estimate_issue_run(
            issue,
            claim_type=claim_type,
            max_results=max_results,
            deep_read=deep_read,
            deep_read_limit=deep_read_limit,
            usage=usage,
            usage_error=usage_error,
        )
        cl_requests = sum(
            int(r["requests"])
            for r in row["providers"]  # type: ignore[union-attr]
            if r["name"] == "courtlistener"
        ) + int(row["deep_read"]["opinion_fetches"])  # type: ignore[union-attr]
        row["courtlistener_requests"] = cl_requests
        # Verdicts from estimate_issue_run (daily/minute checks) stay the source
        # of truth; only escalate a "proceed" to "abort" when the cumulative
        # daily window would run dry -- never downgrade an abort.
        if row["verdict"] == "proceed" and remaining is not None and cl_requests > remaining:
            row["verdict"] = "abort"
        if remaining is not None:
            remaining -= min(cl_requests, remaining)
            row["courtlistener_after"] = remaining
        else:
            row["courtlistener_after"] = None
        rows.append(row)

    summary: dict[str, object] = {
        "issues": len(rows),
        "total_requests": sum(int(r["total_requests_estimate"]) for r in rows),
        "courtlistener_requests": sum(int(r["courtlistener_requests"]) for r in rows),
        "daily_before": daily_before,
        "daily_after": remaining,
        "daily_limit": daily_limit,
        "blocked_issues": sum(1 for r in rows if r["verdict"] == "abort"),
        "usage_error": usage_error,
    }
    return rows, summary


def _batch_dry_run_to_csv(rows: list[dict[str, object]]) -> str:
    """Render batch dry-run rows as a machine-parseable CSV (header + one row each)."""
    header = [
        "issue",
        "priority",
        "base_queries",
        "worst_case_queries",
        "courtlistener_requests",
        "total_requests",
        "nominal_wall_seconds",
        "worst_wall_seconds",
        "courtlistener_daily_after",
        "verdict",
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        wall = row["wall_time_seconds"]  # type: ignore[union-attr]
        writer.writerow(
            [
                row["issue"],
                row["priority"] if row.get("priority") is not None else "",
                row["base_queries"],
                row["total_queries_worst_case"],
                row["courtlistener_requests"],
                row["total_requests_estimate"],
                wall["nominal"],
                wall["worst_case"],
                row["courtlistener_after"],
                row["verdict"],
            ]
        )
    return buffer.getvalue().rstrip("\n")


def _batch_dry_run_to_text(
    rows: list[dict[str, object]],
    summary: dict[str, object],
    only_blocked: bool = False,
) -> str:
    """Render batch dry-run rows as an aligned table with a schedule summary.

    When *only_blocked* is set, the caller has already filtered *rows* down
    to the aborting issues; the report says so and (when nothing is blocked)
    says so explicitly instead of printing an empty table.
    """
    lines = [
        "Batch dry run - no searches were executed and no analysis was produced.",
        "",
        (
            f"Issues: {summary['issues']}   Total requests: {summary['total_requests']}   "
            f"CourtListener requests: {summary['courtlistener_requests']}"
        ),
    ]
    if only_blocked and len(rows) < int(summary["issues"]):
        lines.append(
            f"Showing {len(rows)} of {summary['issues']} issue(s) (--only-blocked)."
        )
    if summary["daily_before"] is not None:
        lines.append(
            f"CourtListener daily window: {summary['daily_before']} remaining "
            f"({summary['daily_limit']}/day) - {summary['daily_after']} left after the batch."
        )
    if summary["blocked_issues"]:
        lines.append(
            f"{summary['blocked_issues']} of {summary['issues']} issue(s) would ABORT "
            "under the current window (wait for the reset to run them)."
        )
    if summary.get("usage_error"):
        lines.append(f"CourtListener usage unavailable: {summary['usage_error']}")
    if not rows:
        lines.append("")
        lines.append("No blocked issues to show.")
        return "\n".join(lines)
    lines.append("")
    lines.append(
        f"{'issue':<30} {'prio':>4} {'worst':>5} {'CL req':>6} {'total req':>9} "
        f"{'nominal':>9} {'worst-case':>10} {'daily after':>11}  verdict"
    )
    for row in rows:
        issue = str(row["issue"])
        if len(issue) > 30:
            issue = issue[:27] + "..."
        daily = row["courtlistener_after"]
        daily_label = "-" if daily is None else str(daily)
        priority = row.get("priority")
        priority_label = "-" if priority is None else str(priority)
        wall = row["wall_time_seconds"]  # type: ignore[union-attr]
        lines.append(
            f"{issue:<30} {priority_label:>4} {row['total_queries_worst_case']:>5} "
            f"{row['courtlistener_requests']:>6} {row['total_requests_estimate']:>9} "
            f"{_format_wall_seconds(float(wall['nominal'])):>9} "
            f"{_format_wall_seconds(float(wall['worst_case'])):>10} "
            f"{daily_label:>11}  {row['verdict']}"
        )
    return "\n".join(lines)


def _read_issues_file(path: str) -> list[tuple[str, int | None]]:
    """Read one issue per line from *path* as ``(issue, priority)`` pairs.

    Lines are ``issue`` alone (priority ``None``) or ``issue<TAB>priority``
    where *priority* is an integer. Lower numbers run first; lines without
    a priority sort after every weighted issue, preserving file order among
    themselves (and among ties). Blank lines and ``#`` comments are skipped;
    a non-integer priority token is ignored (the line becomes unweighted).
    The same issue may appear more than once; callers that want one plan row
    per issue collapse duplicates to the best (lowest-number) explicit
    priority.
    """
    issues: list[tuple[str, int | None]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # The stripped line is non-empty, so the first tab-field is non-empty
        # too; a leading tab only ever splits into an empty field on a line
        # we already skipped above (a trailing tab after the issue is a
        # legitimate empty priority).
        parts = line.split("\t")
        issue = parts[0]
        priority: int | None = None
        if len(parts) >= 2:
            try:
                priority = int(parts[1].strip())
            except ValueError:
                priority = None
        issues.append((issue, priority))
    # Duplicates are collapsed by the caller (the CLI batch branch) to one
    # plan row with the best priority; this reader stays a faithful parser.
    return issues

def _emit_output(text: str, output_file: str | None) -> None:
    """Print *text* to stdout, or write it (plus a newline) to *output_file*."""
    if output_file:
        Path(output_file).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


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
        "--dry-run",
        action="store_true",
        help=(
            "Estimate request cost, quota impact, and wall time for this issue "
            "without executing any searches (text report, or JSON with "
            "--output-format json)."
        ),
    )
    parser.add_argument(
        "--batch-dry-run",
        action="store_true",
        help=(
            "Estimate several issues at once and allocate the CourtListener daily "
            "window across them (issues from --issues-file plus the positional "
            "issue; CSV table with --output-format csv)."
        ),
    )
    parser.add_argument(
        "--issues-file",
        dest="issues_file",
        default=None,
        help=(
            "File with one issue per line for --batch-dry-run (blank lines and "
            "#-prefixed comment lines are skipped)."
        ),
    )
    parser.add_argument(
        "--start-at",
        dest="start_at",
        type=int,
        default=None,
        help=(
            "With --batch-dry-run: 1-based index of the first issue to plan, "
            "skipping the earlier ones (e.g. 4 to resume from the 4th issue)."
        ),
    )
    parser.add_argument(
        "--only-blocked",
        dest="only_blocked",
        action="store_true",
        help=(
            "With --batch-dry-run: show only the issues whose verdict is abort "
            "(those the current window can't cover), so a scheduler can retry "
            "exactly them after the reset."
        ),
    )
    parser.add_argument(
        "--retry-file",
        dest="retry_file",
        default=None,
        help=(
            "With --batch-dry-run: write the issues whose verdict is abort to "
            "this file (one per line), so after the window reset a retry run "
            "can reuse them directly. Defaults to 'issues.retry' next to the "
            "CSV when --output-format csv / --output-file is used; pass "
            "explicitly (or set --retry-file) to always write it, or pass an "
            "empty value to disable."
        ),
    )
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

    if (args.start_at is not None or args.only_blocked) and not args.batch_dry_run:
        parser.error("--start-at and --only-blocked only apply to --batch-dry-run.")

    if not args.issue and not args.batch_dry_run:
        parser.error("the following arguments are required: issue")

    max_results = args.max_results if args.max_results is not None else get_settings().search_max_results
    if args.batch_dry_run:
        if args.dry_run:
            parser.error("--dry-run and --batch-dry-run are mutually exclusive.")
        issues: list[str] = []
        priority_by_issue: dict[str, int] = {}
        seen: set[str] = set()
        if args.issues_file:
            try:
                for parsed_issue, priority in _read_issues_file(args.issues_file):
                    if parsed_issue in seen:
                        # Duplicate issue: keep the best (lowest-number)
                        # explicit priority; an unweighted duplicate never
                        # downgrades an explicit one.
                        if priority is not None:
                            current = priority_by_issue.get(parsed_issue)
                            if current is None or priority < current:
                                priority_by_issue[parsed_issue] = priority
                        continue
                    seen.add(parsed_issue)
                    issues.append(parsed_issue)
                    if priority is not None:
                        priority_by_issue[parsed_issue] = priority
            except OSError as exc:
                parser.error(f"could not read issues file: {exc}")
        if args.issue and args.issue not in seen:
            issues.append(args.issue)
        if not issues:
            parser.error(
                "--batch-dry-run needs at least one issue (positional or --issues-file)."
            )
        if priority_by_issue:
            # Lower numbers run first; unweighted issues sort after every
            # weighted one. ``--start-at`` below then indexes into this
            # priority order.
            issues.sort(key=lambda i: (i not in priority_by_issue, priority_by_issue.get(i, 0)))
        if args.start_at is not None:
            if args.start_at < 1:
                parser.error("--start-at must be >= 1.")
            skipped = args.start_at - 1
            if skipped >= len(issues):
                parser.error(
                    f"--start-at {args.start_at} skips the entire batch "
                    f"(only {len(issues)} issue(s) given)."
                )
            issues = issues[skipped:]
        rows, summary = batch_dry_run(
            issues,
            claim_type=args.claim_type,
            max_results=max(max_results, 1),
            deep_read=args.deep_read,
            deep_read_limit=args.deep_read_limit,
        )
        for row in rows:
            row["priority"] = priority_by_issue.get(row["issue"])  # type: ignore[typeddict-item]
        if args.only_blocked:
            rows_visible = [row for row in rows if row["verdict"] == "abort"]
        else:
            rows_visible = rows
        # Write the blocked-issues retry file.
        #   * Explicit --retry-file PATH  -> always write (empty if nothing
        #     is blocked, so automation can rely on its existence).
        #   * --retry-file "" / none / off -> never write.
        #   * No flag, batch CSV output    -> write to 'issues.retry' (or the
        #     CSV path with a .retry.txt suffix) only when something is
        #     blocked; an all-proceed batch leaves no retry file behind.
        blocked = [row for row in rows if row["verdict"] == "abort"]
        explicit_retry = args.retry_file not in (None, "", "none", "off")
        retry_path: str | None = None
        if args.retry_file in ("", "none", "off"):
            retry_path = None
        elif explicit_retry:
            retry_path = args.retry_file
        elif args.output_format == "csv" and blocked:
            retry_path = (
                str(Path(args.output_file).with_suffix(".retry.txt"))
                if args.output_file
                else "issues.retry"
            )
        if retry_path is not None and (blocked or explicit_retry):
            try:
                Path(retry_path).write_text(
                    "".join(
                        str(row["issue"])
                        + (f"\t{row['priority']}" if row.get("priority") is not None else "")
                        + "\n"
                        for row in blocked
                    ),
                    encoding="utf-8",
                )
            except OSError as exc:
                parser.error(f"could not write retry file {retry_path}: {exc}")
        if args.output_format == "csv":
            text = _batch_dry_run_to_csv(rows_visible)
        else:
            text = _batch_dry_run_to_text(rows_visible, summary, only_blocked=args.only_blocked)
        try:
            _emit_output(text, args.output_file)
        except OSError as exc:
            parser.error(f"could not write output file: {exc}")
        return

    if args.dry_run:
        report = estimate_issue_run(
            args.issue,
            claim_type=args.claim_type,
            max_results=max(max_results, 1),
            deep_read=args.deep_read,
            deep_read_limit=args.deep_read_limit,
        )
        if args.output_format == "json":
            text = json.dumps(report, indent=2, default=str)
        else:
            text = _dry_run_to_text(report)
        try:
            _emit_output(text, args.output_file)
        except OSError as exc:
            parser.error(f"could not write output file: {exc}")
        return

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
