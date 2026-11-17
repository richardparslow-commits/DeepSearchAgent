from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .adjudication import build_adjudication_queue, save_queue
from .agent import analyze_cases_for_claim
from .batch import BatchTracker
from .config import get_settings
from .interpretation import detect_claim_elements
from .models import (
    AdjudicationRoleGuidance,
    AuthorityCheck,
    AuthorityQueueEntry,
    AuthorityReliabilitySummary,
    CRSReport,
    CaseAbstract,
    CaseExtractionCandidates,
    CitatorCheck,
    ClaimElement,
    CongressLink,
    Contradiction,
    ImpactProfile,
    LegalAnalysis,
    PrincipleFinding,
    ProvisionImpact,
    RegulationsMapping,
    SourceAuditProvenance,
    RvsrGuidance,
    SeniorRaterGuidance,
    SurfacedCaseRecord,
    StatuteOutcomeRow,
    StatutorySupersessionSignal,
    group_source_audit_provenances,
)
from .reliability import authority_label
from .planning import decompose_issue, plan_queries
from .redaction import redact_secrets, sanitize_exception
from .provenance_compare import (
    SIDECAR_VERSION,
    compare_provenance_sidecars,
    load_provenance_sidecar,
    render_comparison_text,
)
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


class _RedactingTextFormatter(logging.Formatter):
    """Text formatter that sanitizes exception chains before rendering."""

    def format(self, record: logging.LogRecord) -> str:
        if record.exc_info and record.exc_info[1] is not None:
            sanitize_exception(record.exc_info[1])
            # Force a fresh render from the now-sanitized exception rather
            # than a cached exc_text produced by another handler.
            record.exc_text = None
        return redact_secrets(super().format(record))


class _JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line for machine parsing."""

    def format(self, record: logging.LogRecord) -> str:
        if record.exc_info and record.exc_info[1] is not None:
            sanitize_exception(record.exc_info[1])
            record.exc_text = None
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
        }
        if record.exc_info:
            payload["exc_info"] = redact_secrets(self.formatException(record.exc_info))
        for key in (
            "event",
            "run_id",
            "coverage_score",
            "interpretation_source",
            "courtlistener_quota",
            "issue",
            "error",
            "error_type",
            "traceback",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        for key, value in getattr(record, "data", {}).items():
            payload[key] = value
        return redact_secrets(json.dumps(payload, ensure_ascii=False))


def _configure_logging(level: str, log_format: str = "text") -> None:
    """Route package log output to stderr at the given level.

    Log records always go to stderr so stdout stays reserved for the
    analysis output (JSON/text/CSV) and the ``--show-config`` dump.
    """
    handler = logging.StreamHandler(sys.stderr)
    if log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            _RedactingTextFormatter("%(levelname)s %(name)s: %(message)s")
        )
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


def _format_exception(error: BaseException) -> str:
    """Return the complete traceback text for *error*, without a trailing newline.

    The exception chain is sanitized in place before formatting.  The final
    string is redacted again as a boundary check for any string captured by a
    third-party traceback formatter.
    """
    sanitize_exception(error)
    return redact_secrets(
        "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ).rstrip()
    )


def _emit_failure_event(issue: str, error: BaseException, run_id: str) -> None:
    """Emit a structured failure event pipelines can key off of.

    Mirrors :func:`_emit_completion_event` so automation gets a definitive
    terminal signal on both success and failure. The event keeps the existing
    ``error`` message field for consumers and adds the exception type and full
    traceback for diagnosis.
    """
    sanitize_exception(error)
    safe_error = redact_secrets(str(error))
    safe_traceback = _format_exception(error)
    _EVENTS_LOGGER.error(
        "analysis failed for %s (run=%s): %s",
        issue,
        run_id,
        safe_error,
        exc_info=(type(error), error, error.__traceback__),
        extra={
            "event": "analysis_failed",
            "run_id": run_id,
            "issue": issue,
            "error": safe_error,
            "error_type": type(error).__name__,
            "traceback": safe_traceback,
        },
    )


def _run_id(cli_value: str | None = None) -> str:
    """Return the run id: CLI flag wins, then RUN_ID env, else a fresh id.

    Batch orchestrators can set RUN_ID (once per batch) or pass --run-id so
    all per-issue events share a common correlation id across CLI invocations.
    """
    return cli_value or os.getenv("RUN_ID") or uuid.uuid4().hex


def _positive_int(value: str) -> int:
    """argparse type for options that must be strictly positive integers."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


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
    """Mask a secret, revealing only a long value's first/last 4 characters.

    Values of 16 characters or fewer are masked completely: revealing four
    characters from each end of an 8- or 9-character API key would expose most
    of the secret in a dump intended to be safe to share.
    """
    if not isinstance(value, str) or not value:
        return value
    return f"{value[:4]}...{value[-4:]}" if len(value) > 16 else "***"


def _mask_proxy_url(value: str | None) -> str | None:
    """Mask any credentials embedded in a proxy URL for safe display.

    ``SEARCH_HTTP_PROXY`` may carry a ``user:pass@`` authority (e.g.
    ``http://user:pass@residential-proxy.example:8080``), and ``--show-config``
    must never echo that password. The entire userinfo authority is redacted
    to ``***`` (never partially revealed, since it can hold a password) while
    the ``host:port`` stays visible so operators can still see which proxy is
    configured. A proxy without userinfo is returned unchanged; an unparseable
    value is masked wholesale rather than leaked.

    ``requests``/``curl_cffi`` also accept a scheme-less proxy such as
    ``user:pass@proxy.example:8080``. Prepend ``//`` before parsing so that
    form is treated as an authority instead of a path. A bad port is omitted
    from the displayed authority rather than raised out of ``--show-config``.
    """
    if not isinstance(value, str) or not value:
        return value

    # urlsplit treats a scheme-less proxy as a path, hiding its userinfo. Add
    # the temporary network-path prefix for parsing, then remove it below.
    has_scheme = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) is not None
    prefixed = not has_scheme and not value.startswith("//")
    parse_value = f"//{value}" if prefixed else value
    try:
        parts = urlsplit(parse_value)
    except ValueError:
        return "***"
    if parts.username is None and parts.password is None:
        return value

    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        # Keep the host visible but omit the malformed port: the config dump
        # is diagnostic output and must not crash on an operator typo.
        port = None

    # Rebuild the authority with the userinfo fully redacted, re-bracketing an
    # IPv6 host if present.
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"***@{host}"
    if port is not None:
        netloc += f":{port}"
    masked = urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )
    if prefixed and masked.startswith("//"):
        masked = masked[2:]
    return masked


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
    for key in (
        "openai_api_key",
        "courtlistener_api_key",
        "govinfo_api_key",
        "congress_api_key",
        "regulations_api_key",
    ):
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


def _authority_lines(checks: list[AuthorityCheck]) -> list[str]:
    """Render authority verifications and any linked GovInfo records."""
    lines: list[str] = []
    for check in checks:
        detail = f": {check.detail}" if check.detail else ""
        lines.append(
            f"[{check.status}] {check.authority} ({check.source}){detail}"
        )
        if check.records_status:
            lines.append(
                f"  GovInfo records [{check.records_status}]: "
                f"{check.records_detail}"
            )
        for law in check.public_laws:
            lines.append(
                f"  Public Law: {law.citation or law.package_id} — "
                f"{law.title or '(untitled)'}"
            )
            if law.details_url:
                lines.append(f"    Record: {law.details_url}")
            if law.text_url:
                lines.append(f"    Text: {law.text_url}")
            if law.text_excerpt:
                lines.append(f"    Excerpt: {law.text_excerpt}")
            elif law.detail:
                lines.append(f"    {law.detail}")
        for statute in check.statutes_at_large:
            lines.append(
                f"  Statutes at Large: "
                f"{statute.citation or statute.granule_id or statute.package_id} — "
                f"{statute.title or '(untitled)'}"
            )
            if statute.details_url:
                lines.append(f"    Record: {statute.details_url}")
            if statute.text_url:
                lines.append(f"    Text: {statute.text_url}")
            if statute.text_excerpt:
                lines.append(f"    Excerpt: {statute.text_excerpt}")
            elif statute.detail:
                lines.append(f"    {statute.detail}")
    return lines


def _congress_lines(links: list[CongressLink]) -> list[str]:
    """Render public-law and bill-action links for cited statutes."""
    lines: list[str] = []
    for link in links:
        detail = f": {link.detail}" if link.detail else ""
        lines.append(f"[{link.status}] {link.usc_section}{detail}")
        for law in link.public_laws:
            line = f"  - {law.citation}"
            if law.title:
                line += f" — {law.title}"
            if law.bill_citation:
                line += f" ({law.bill_citation})"
            lines.append(line)
            if law.bill_url:
                lines.append(f"    Bill: {law.bill_url}")
            if law.url:
                lines.append(f"    Public law: {law.url}")
            for action in law.actions:
                date = action.date or "date unknown"
                lines.append(f"    {date}: {action.text}")
            if law.actions_truncated:
                lines.append(
                    f"    Actions truncated to the first {len(law.actions)}."
                )
    return lines


def _regulations_lines(mappings: list[RegulationsMapping]) -> list[str]:
    """Render rulemaking context and explicitly label public comments."""
    lines: list[str] = []
    for mapping in mappings:
        detail = f": {mapping.detail}" if mapping.detail else ""
        lines.append(
            f"[{mapping.status}] 38 C.F.R. Part {mapping.cfr_part}{detail}"
        )
        for fr_document in mapping.fr_documents:
            date = fr_document.publication_date or "date unknown"
            lines.append(
                f"  FR {fr_document.document_number} ({date}) "
                f"{fr_document.title or '(untitled)'}"
            )
        for docket in mapping.dockets:
            lines.append(
                f"  Docket {docket.docket_id}: {docket.title or '(untitled)'}"
            )
            if docket.url:
                lines.append(f"    {docket.url}")
            for document in docket.documents:
                lines.append(
                    f"    Document {document.document_id}: "
                    f"{document.title or '(untitled)'}"
                )
                lines.append(
                    f"      Public comments: {document.comment_count} "
                    f"(sampled {document.comments_sampled})"
                )
                for comment in document.comments:
                    date = comment.posted_date or "date unknown"
                    lines.append(
                        f"      [public_input] {date}: {comment.excerpt}"
                    )
            if docket.supporting_documents:
                lines.append("    Supporting documents:")
                for supporting in docket.supporting_documents:
                    lines.append(
                        f"      {supporting.document_id}: "
                        f"{supporting.title or '(untitled)'}"
                    )
    return lines


def _crs_report_lines(reports: list[CRSReport]) -> list[str]:
    """Render explicitly cited CRS reports, always marked secondary."""
    return [
        f"[secondary] CRS {report.report_number}: {report.title or '(untitled)'}"
        + (f" — {report.url}" if report.url else "")
        for report in reports
    ]


def _extraction_candidate_lines(
    groups: list[CaseExtractionCandidates],
) -> list[str]:
    """Render quote-backed candidates with their source quotes visible."""
    lines: list[str] = []
    for group in groups:
        lines.append(f"{group.case}:")
        for candidate in group.candidates:
            value = candidate.value or "(no canonical value)"
            quote = candidate.quote
            if len(quote) > 240:
                quote = quote[:237] + "..."
            line = (
                f"  [{candidate.status}] {candidate.field}: {value} "
                f'(quote: "{quote}")'
            )
            if candidate.detail:
                line += f" — {candidate.detail}"
            lines.append(line)
        for flag in group.flags:
            lines.append(f"  [flag] {flag}")
    return lines


def _supersession_lines(
    signals: list[StatutorySupersessionSignal],
) -> list[str]:
    """Render provision-level supersession signals without overstating them."""
    lines: list[str] = []
    for signal in signals:
        label = signal.status
        if signal.signal:
            label += f"; signal={signal.signal}"
        detail = f": {signal.detail}" if signal.detail else ""
        lines.append(
            f"[{label}] {signal.authority} ({signal.source}){detail}"
        )
        if signal.effective_date or signal.replacement:
            lines.append(
                "  "
                + (
                    f"effective: {signal.effective_date}"
                    if signal.effective_date
                    else "effective date unknown"
                )
                + (f"; replacement: {signal.replacement}" if signal.replacement else "")
            )
    return lines


def _authority_queue_lines(queue: list[AuthorityQueueEntry]) -> list[str]:
    """Render the citator's priority order without implying a ranking change."""
    lines = [
        "Review order only; this queue does not change case ranking or "
        "citator status."
    ]
    for item in queue:
        reasons = "; ".join(item.reasons) or "no priority details"
        line = (
            f"  {item.priority}. {item.case_title} "
            f"({item.citation or 'no citation'}; "
            f"{item.court or 'court unknown'}) — {reasons}"
        )
        if item.citator_demoted:
            line += " [citator: demoted]"
        elif item.citator_flagged:
            line += " [citator: flagged]"
        elif item.citator_status:
            line += f" [citator: {item.citator_status}]"
        lines.append(line)
        for citator_reason in item.citator_reasons:
            lines.append(f"      citator: {citator_reason}")
    return lines


def _source_provenance_lines(check: CitatorCheck) -> list[str]:
    """Render each source audit's execution and observed-data envelope."""
    lines = ["  Source-audit provenance:"]
    for signal in check.source_validity:
        provenance = signal.audit_ledger.provenance
        label = (
            f"source {signal.source_opinion_id or 'unknown'}/cluster "
            f"{signal.source_cluster_id or 'unknown'}"
        )
        if provenance is None:
            lines.append(f"    {label}: not recorded")
            continue
        lines.append(
            f"    {label}: envelope v{provenance.version}; audit logic "
            f"{provenance.audit_logic_version or 'unknown'}; provider "
            f"{provenance.provider or 'unknown'} "
            f"{provenance.provider_version or 'unknown'} at "
            f"{provenance.provider_base_url or 'base unknown'}; cache "
            f"{provenance.cache_mode or 'unknown'} v{provenance.cache_version} "
            f"TTL {provenance.cache_ttl_seconds}s; hits "
            f"{provenance.cache_hits}/misses {provenance.cache_misses}; "
            f"oldest hit {provenance.cache_entry_age_seconds}s; execution "
            f"fingerprint {provenance.execution_fingerprint}; configuration "
            f"fingerprint {provenance.configuration_fingerprint}; response "
            f"digest {provenance.response_digest or 'none'}; generated "
            f"{provenance.generated_at or 'unknown'}"
        )
    return lines


def _citator_lines(checks: list[CitatorCheck]) -> list[str]:
    """Render citator results with explicit scope and separate advisory signals."""
    lines: list[str] = [
        "Scope: good_law means only no authoritative negative treatment in "
        "reviewed citing opinions. Only lead/majority/combined/unanimous Published "
        "opinions can set negative; gated findings force unknown. Subsequent history "
        "is not inferred from citation signals: it is reported only as candidates "
        "or as a human-verified record. Same-docket decisions and later citing "
        "opinions are separate advisory signals, not appellate history."
    ]
    for check in checks:
        coverage = check.coverage
        date = check.good_law_as_of or "not established"
        status_detail = {
            "good_law": "no authoritative negative treatment in reviewed citing opinions",
            "negative": "authoritative negative treatment in reviewed citing opinions",
            "unknown": "not established from reviewed citing opinions",
        }.get(check.status, "not established")
        if check.gated_treatment_findings and check.status != "negative":
            status_detail = "only gated negative treatment found; not counted"
        history_state = (
            "human-verified"
            if check.subsequent_history_assessed
            else "not assessed"
        )
        status = (
            f"{check.target_case} ({check.target_citation or 'no citation'}): "
            f"{check.status}; {status_detail}; good-law cutoff: {date}; reviewed "
            f"{coverage.citing_opinions_reviewed}/{coverage.forward_citations_found} "
            f"citing opinions; coverage complete={'yes' if coverage.complete else 'no'}; "
            f"subsequent history: {history_state}"
        )
        lines.append(status)
        if check.as_of_date:
            lines.append(
                f"  As-of cutoff: {check.as_of_date} (inclusive); only citing "
                "opinions through this date affect status. Later treatment is "
                "reported separately."
            )
        if coverage.gaps:
            lines.append("  Coverage gaps:")
            for gap in coverage.gaps:
                lines.append(f"  - {gap.code}: {gap.detail}")
        else:
            lines.append("  Coverage gaps: none")
        lines.append(
            f"  Temporal confidence: {coverage.temporal_confidence} "
            f"({coverage.temporal_confidence_reason or 'no basis recorded'})"
        )
        lines.append(
            f"  Calibrated confidence: {check.confidence} "
            f"({check.confidence_score:.2f}) "
            f"({check.confidence_reason or 'no basis recorded'})"
        )
        if check.review_required:
            lines.append("  Review required: yes")
            for reason in check.review_reasons:
                lines.append(f"  - {reason}")
        else:
            lines.append("  Review required: no")
        history = check.appellate_history
        if history is None:
            lines.append(
                "  Appellate history: not assessed (history layer did not run)"
            )
        else:
            meaning = {
                "verified": "human-verified record available",
                "insufficient_source": (
                    "candidates require authoritative human verification"
                ),
                "not_assessed": (
                    "no candidate signals; absence is not proof of no appeal"
                ),
            }.get(history.status, "not established")
            lines.append(
                f"  Appellate history: {history.status} ({meaning})"
            )
            if history.candidates:
                lines.append(
                    "  History candidates requiring verification "
                    "(not history labels):"
                )
                for candidate in history.candidates:
                    signals = ", ".join(candidate.signals) or candidate.signal_type
                    lines.append(
                        f"  - [{candidate.verification_status}; {signals}] "
                        f"{candidate.candidate_case} "
                        f"({candidate.citation or 'no citation'}; "
                        f"{candidate.decision_date or 'date unknown'}; "
                        f"{candidate.court or 'court unknown'}) "
                        f"{candidate.url}".rstrip()
                    )
                    for reason in candidate.match_reasons:
                        lines.append(f"    signal: {reason}")
                    if candidate.verification_status in {"verified", "rejected"}:
                        lines.append(
                            f"    human review: relationship={candidate.relationship}; "
                            f"disposition={candidate.disposition}; "
                            f"scope={candidate.scope}; "
                            f"reviewer={candidate.reviewer}; "
                            f"reviewed={candidate.reviewed_at}; "
                            f"source={candidate.source_quality} "
                            f"{candidate.source_url}"
                        )
            if history.verified_history:
                lines.append("  Verified appellate history:")
                for record in history.verified_history:
                    lines.append(
                        f"  - {record.candidate_case}: "
                        f"relationship={record.relationship}; "
                        f"disposition={record.disposition}; scope={record.scope}; "
                        f"source={record.source_quality} {record.source_url}; "
                        f"reviewer={record.reviewer}; reviewed={record.reviewed_at}"
                    )
                    lines.append(f"    rationale: {record.rationale}")
            for flag in history.flags:
                lines.append(f"  History note: {flag}")
        if check.target_cluster_id or len(check.target_citations) > 1 or len(
            check.target_opinion_ids
        ) > 1:
            lines.append(
                f"  Cluster: {check.target_cluster_id or 'unknown'}; "
                f"parallel citations: "
                f"{' | '.join(check.target_citations) or 'none'}; "
                f"opinions: {', '.join(check.target_opinion_ids) or 'none'}"
            )
        if check.same_docket_decisions:
            lines.append(
                "  Same-docket decisions (advisory context only; not appellate history):"
            )
            for decision in check.same_docket_decisions:
                lines.append(
                    f"  - {decision.case_name} "
                    f"({decision.citation or 'no citation'}; "
                    f"{decision.decision_date or 'date unknown'}; "
                    f"{decision.court or 'court unknown'}) {decision.url}".rstrip()
                )
        if check.later_citing_opinions:
            lines.append(
                "  Later citing opinions reviewed (advisory only; not appellate "
                "history or treatment):"
            )
            for cited in check.later_citing_opinions:
                eligible = "eligible" if cited.treatment_eligible else "gated"
                lines.append(
                    f"  - {cited.case_name} ({cited.citation or 'no citation'}; "
                    f"{cited.decision_date or 'date unknown'}; "
                    f"{cited.court or 'court unknown'}; "
                    f"{cited.opinion_role}; "
                    f"{cited.precedential_status or 'status unknown'}; "
                    f"{eligible}) {cited.url}".rstrip()
                )
        if check.undated_citing_opinions:
            lines.append(
                "  Undated citing opinions excluded from the as-of result "
                "(they make coverage incomplete):"
            )
            for cited in check.undated_citing_opinions:
                eligible = "eligible" if cited.treatment_eligible else "gated"
                lines.append(
                    f"  - {cited.case_name} ({cited.citation or 'no citation'}; "
                    f"{cited.decision_date or 'date unknown'}; "
                    f"{cited.court or 'court unknown'}; "
                    f"{cited.opinion_role}; "
                    f"{cited.precedential_status or 'status unknown'}; "
                    f"{eligible}) {cited.url}".rstrip()
                )
        if check.post_as_of_citing_opinions:
            lines.append(
                "  Post-as-of citing opinions reviewed (advisory only; not "
                "counted toward as-of status):"
            )
            for cited in check.post_as_of_citing_opinions:
                eligible = "eligible" if cited.treatment_eligible else "gated"
                lines.append(
                    f"  - {cited.case_name} ({cited.citation or 'no citation'}; "
                    f"{cited.decision_date or 'date unknown'}; "
                    f"{cited.court or 'court unknown'}; "
                    f"{cited.opinion_role}; "
                    f"{cited.precedential_status or 'status unknown'}; "
                    f"{eligible}) {cited.url}".rstrip()
                )
        if check.post_as_of_treatment_findings:
            lines.append(
                "  Post-as-of treatment findings (not counted toward as-of "
                "status):"
            )
            for treatment in check.post_as_of_treatment_findings:
                location = treatment.citing_case
                if treatment.citing_url:
                    location += f" {treatment.citing_url}"
                gate = (
                    "authoritative"
                    if treatment.authoritative
                    else f"gated; {treatment.gate_reason}"
                )
                attribution = (
                    f"; attribution ambiguous: {treatment.attribution_reason}"
                    if treatment.attribution_ambiguous
                    else ""
                )
                lines.append(
                    f"  - {treatment.treatment} ({treatment.scope}, "
                    f"{treatment.opinion_role}, {gate}{attribution}): "
                    f"\"{treatment.excerpt}\" — {location} "
                    f"({treatment.citing_date or 'date unknown'})"
                )
        if check.post_as_of_incomplete:
            lines.append(
                "  Post-as-of review incomplete; later treatment may be "
                "missing or unverified."
            )
        for flag in check.post_as_of_flags:
            lines.append(f"  Post-as-of note: {flag}")
        if check.source_validity:
            if check.source_validity_enforced:
                lines.append(
                    "  Source validity (enforcement enabled; non-authoritative "
                    "sources withheld):"
                )
            else:
                lines.append(
                    "  Source validity (advisory; never counted toward status):"
                )
            for signal in check.source_validity:
                detail = f"; {signal.reason}" if signal.reason else ""
                withheld = "; withheld" if signal.withheld else ""
                lines.append(
                    f"  - source opinion {signal.source_opinion_id or 'unknown'}; "
                    f"cluster {signal.source_cluster_id or 'unknown'}; "
                    f"status {signal.status}; code {signal.reason_code}"
                    f"{withheld}{detail}"
                )
                ledger = signal.audit_ledger
                if ledger.assessed:
                    coverage = (
                        "complete"
                        if ledger.coverage_complete
                        else "incomplete"
                    )
                    history = (
                        "attempted"
                        if ledger.history_attempted
                        else "not attempted"
                    )
                    page_cap = "yes" if ledger.page_limit_reached else "no"
                    pool_cap = (
                        "yes" if ledger.candidate_pool_limit_reached else "no"
                    )
                    failures = (
                        ledger.metadata_fetch_failures
                        + ledger.metadata_unavailable
                        + ledger.text_fetch_failures
                        + ledger.text_empty
                        + ledger.target_not_located
                    )
                    gaps = ", ".join(
                        gap.code for gap in ledger.gaps
                    ) or "none"
                    decisive = ledger.decisive_candidate_id or "none"
                    lines.append(
                        f"    audit ledger: coverage {coverage}; "
                        f"history {history}; as of "
                        f"{ledger.as_of_date or 'none'}; cluster opinions "
                        f"{ledger.cluster_opinion_count}; forward citations "
                        f"{ledger.forward_citations_reported} reported/"
                        f"{ledger.citing_ids_collected} collected; candidates "
                        f"{ledger.candidates_metadata_loaded} loaded/"
                        f"{ledger.candidates_in_scope} in scope/"
                        f"{ledger.candidates_reviewed} reviewed; invalidating "
                        f"{ledger.candidates_with_invalidating_finding}/clean "
                        f"{ledger.candidates_without_invalidating_finding}; "
                        f"gated {ledger.candidates_gated}; unreviewed "
                        f"{ledger.candidates_unreviewed}; post-as-of "
                        f"{ledger.candidates_post_as_of}; undated "
                        f"{ledger.candidates_undated_as_of}; failures "
                        f"{failures}; pages {ledger.pages_fetched}; page cap "
                        f"{page_cap}; candidate pool cap {pool_cap}; decisive "
                        f"{decisive}; gaps {gaps}"
                    )
                    bottleneck_summary = "; ".join(
                        f"{opinion_id} ({', '.join(reasons)})"
                        for opinion_id, reasons in (
                            ledger.opinion_audit_bottlenecks().items()
                        )
                    )
                    lines.append(
                        f"    audit bottlenecks: {bottleneck_summary or 'none'}"
                    )
                    for opinion in ledger.opinion_audits:
                        opinion_state = (
                            "complete"
                            if opinion.complete
                            else "incomplete"
                        )
                        if opinion.fetch_attempted:
                            fetched = (
                                f"{opinion.forward_citations_reported} reported/"
                                f"{opinion.citing_ids_collected} collected"
                            )
                        else:
                            fetched = "fetch not attempted"
                        opinion_gaps = ", ".join(
                            opinion.gap_codes
                        ) or "none"
                        lines.append(
                            f"    opinion audit {opinion.opinion_id} "
                            f"(index {opinion.opinion_index}): allocation "
                            f"{opinion.candidate_allocation}; {fetched}; "
                            f"new {opinion.citing_ids_new_to_cluster}/duplicate "
                            f"{opinion.citing_ids_duplicate_across_cluster}; "
                            f"rows ignored duplicate "
                            f"{opinion.duplicate_rows_ignored}/invalid "
                            f"{opinion.invalid_rows_ignored}; pages "
                            f"{opinion.pages_fetched}; {opinion_state}; page "
                            f"cap {'yes' if opinion.page_limit_reached else 'no'}; "
                            f"candidate pool cap "
                            f"{opinion.candidate_pool_limit_reason or 'no'}; "
                            f"gaps {opinion_gaps}"
                        )
                for invalidator in signal.invalidator_checks:
                    if invalidator.accepted:
                        verdict = "accepted"
                    elif invalidator.review_status in {
                        "gated",
                        "unreviewed",
                        "metadata_failed",
                        "metadata_unavailable",
                        "text_failed",
                        "text_empty",
                        "target_not_located",
                        "out_of_scope",
                    }:
                        verdict = invalidator.review_status.replace("_", " ")
                    else:
                        verdict = "rejected"
                    if invalidator.binding is True:
                        binding = "binding"
                    elif invalidator.binding is False:
                        binding = "not binding"
                    else:
                        binding = "binding status unknown"
                    lines.append(
                        f"    invalidator {invalidator.source_opinion_id or 'unknown'} "
                        f"({invalidator.citing_case or 'case unknown'}); "
                        f"{verdict}; {binding}; review "
                        f"{invalidator.review_status}; invalidator status "
                        f"{invalidator.invalidator_status}; "
                        f"code {invalidator.reason_code}; {invalidator.reason}"
                    )
            lines.extend(_source_provenance_lines(check))
            if check.source_validity_actions:
                lines.append("  Source-validity enforcement actions:")
                for action in check.source_validity_actions:
                    lines.append(f"  - {action}")
        if check.negative_treatments:
            lines.append("  Authoritative negative treatment findings:")
        for treatment in check.negative_treatments:
            if treatment.binding is True:
                binding = "binding"
            elif treatment.binding is False:
                binding = "not binding"
            else:
                binding = "binding status unknown"
            location = treatment.citing_case
            if treatment.citing_url:
                location += f" {treatment.citing_url}"
            attribution = (
                f", attribution ambiguous: {treatment.attribution_reason}"
                if treatment.attribution_ambiguous
                else ""
            )
            lines.append(
                f"  - {treatment.treatment} ({treatment.scope}, "
                f"{treatment.confidence}, {treatment.opinion_role}, "
                f"{treatment.precedential_status or 'status unknown'})"
                f"{attribution}: "
                f"\"{treatment.excerpt}\" — {location} "
                f"({treatment.citing_date or 'date unknown'}, {binding})"
            )
        if check.gated_treatment_findings:
            lines.append(
                "  Gated treatment findings (not counted toward status):"
            )
        for treatment in check.gated_treatment_findings:
            location = treatment.citing_case
            if treatment.citing_url:
                location += f" {treatment.citing_url}"
            attribution = (
                f"; attribution ambiguous: {treatment.attribution_reason}"
                if treatment.attribution_ambiguous
                else ""
            )
            lines.append(
                f"  - {treatment.treatment} ({treatment.scope}, "
                f"{treatment.opinion_role}, "
                f"{treatment.precedential_status or 'status unknown'}; "
                f"{treatment.gate_reason}{attribution}): "
                f"\"{treatment.excerpt}\" — "
                f"{location} ({treatment.citing_date or 'date unknown'})"
            )
        if check.treatment_timeline:
            lines.append(
                "  Treatment timeline (chronological; reaffirmance is advisory "
                "and does not change status):"
            )
            for entry in check.treatment_timeline:
                later = (
                    "; after latest negative"
                    if entry.later_than_negative
                    else ""
                )
                gate = "" if entry.authoritative else f"; {entry.gate_reason}"
                authority = (
                    "authoritative" if entry.authoritative else "gated"
                )
                lines.append(
                    f"  - {entry.citing_date or 'date unknown'} "
                    f"{entry.signal}: {entry.treatment} ({entry.scope}, "
                    f"{entry.binding_class}, {authority}{gate}{later}): "
                    f"\"{entry.excerpt}\" — {entry.citing_case}"
                )
        if check.later_reaffirmance:
            lines.append(
                "  Later authoritative reaffirmance noted (advisory; negative "
                "status is unchanged)."
            )
    return lines


def _surfaced_case_lines(records: list[SurfacedCaseRecord]) -> list[str]:
    """Render every D1 field, including explicit provenance warnings."""
    lines: list[str] = []
    for record in records:
        history = "; ".join(
            f"{entry.status}: {entry.case_title or entry.citation or 'unverified'}"
            for entry in record.appellate_history
        ) or record.history_status
        transcript = record.official_transcript_url or record.retrieved_url or "(none)"
        line = (
            f"{record.case_title} ({record.court}) | docket: "
            f"{record.docket_number or '(none)'} | citation: "
            f"{record.reporter_citation or '(none)'} | decided: "
            f"{record.decision_date or '(unknown)'} | status: "
            f"{record.precedential_status} | source: "
            f"{record.source_reliability or 'unknown'} "
            f"[{record.authority_tier}; official: "
            f"{record.is_official_authority}] | panel: "
            f"{record.panel or '(names unavailable)'} ({record.panel_status}) "
            f"| citator: {record.citator_status} "
            f"(confidence: {record.citator_confidence}; as of: "
            f"{record.citator_as_of or '(not assessed)'}) "
            f"| history: {history} | transcript: "
            f"{transcript} [{record.transcript_source_type}; "
            f"provenance verified: {record.provenance_verified}]"
        )
        if record.warnings:
            line += " | WARNING: " + " ".join(record.warnings)
        lines.append(line)
    return lines


def _abstract_lines(abstracts: list[CaseAbstract]) -> list[str]:
    lines: list[str] = []
    for abstract in abstracts:
        label = f"{abstract.case_title} ({abstract.court})"
        if not abstract.abstract_complete:
            label += " [metadata-only abstract; full text unavailable]"
        lines.append(
            f"{label} | claimant/conditions: {abstract.claimant_and_conditions or '(none)'} "
            f"| procedural history: {abstract.procedural_history or '(none)'} "
            f"| issues: {abstract.issues_presented or '(none)'} "
            f"| holding/rule: {abstract.holding_and_legal_rule or '(none)'} "
            f"| reasoning: {abstract.reasoning_summary or '(none)'} "
            f"| disposition: {abstract.disposition or '(none)'} "
            f"| remand: {abstract.remand_instructions or '(none)'}"
        )
    return lines


def _provision_impact_lines(impacts: list[ProvisionImpact]) -> list[str]:
    return [
        f"{impact.citation} [{impact.stage}; {impact.legal_effect}] "
        f"M21-1: {', '.join(impact.m21_1_citations) or '(none)'}; "
        f"DC: {impact.diagnostic_code or '(none)'}; "
        f"staged: {impact.staged_rating}; medication: {impact.medication_effect}; "
        f"anchor: \"{impact.provenance_anchor or '(none)'}\"; source: "
        f"{impact.source_url or '(none)'} (verified: {impact.provenance_verified})"
        for impact in impacts
    ]


def _impact_profile_lines(profiles: list[ImpactProfile]) -> list[str]:
    """Render every field of each full ImpactProfile, not only its nuance."""
    lines: list[str] = []
    for profile in profiles:
        lines.append(
            "issue tags: "
            + (", ".join(profile.issue_tags) or "(none)")
            + f" | outcome: {profile.outcome or '(unknown)'}"
            + f" | outcome note: {profile.outcome_note or '(none)'}"
            + f" | statutes: {', '.join(profile.statutes) or '(none)'}"
            + f" | statute note: {profile.statute_note or '(none)'}"
            + f" | authority: {profile.authority_note or '(none)'}"
            + f" | nuance: {profile.nuance or '(none)'}"
        )
        lines.extend(
            "  impact: " + item
            for item in _provision_impact_lines(profile.provision_impacts)
        )
    return lines


def _source_reliability_lines(
    summary: AuthorityReliabilitySummary,
    records: list[SurfacedCaseRecord],
) -> list[str]:
    """Render aggregate and per-authority Official vs. Secondary labels."""
    lines = [
        f"Official primary: {summary.official_count}; secondary: "
        f"{summary.secondary_count}; unverified: {summary.unknown_count}; "
        f"tiers: {json.dumps(summary.tier_counts, sort_keys=True)}"
    ]
    for record in records:
        lines.append(
            f"{record.case_title} — "
            f"{authority_label(record.authority_tier, record.is_official_authority)} "
            f"[tier: {record.authority_tier}; source: "
            f"{record.source_reliability or 'unknown'}]"
        )
    return lines


def _rvsr_workflow_lines(guidance: AdjudicationRoleGuidance) -> list[str]:
    workflow = guidance.rvsr
    lines: list[str] = []
    for title, items in (
        ("Evidence review", workflow.evidence_review_checklist),
        ("Lay competence / benefit of the doubt", workflow.lay_evidence_competence),
        ("Duty to assist / exam adequacy", workflow.duty_to_assist_exam_actions),
        ("Rating codesheet", workflow.rating_codesheet_actions),
        ("Effective dates", workflow.effective_date_actions),
        ("Reasons for decision", workflow.reasons_for_decision_requirements),
    ):
        lines.extend(f"{title}: {item}" for item in items)
    return lines


def _senior_workflow_lines(guidance: AdjudicationRoleGuidance) -> list[str]:
    workflow = guidance.senior_rater_dro
    lines: list[str] = []
    for title, items in (
        ("HLR de novo (38 C.F.R. § 3.2601)", workflow.hlr_de_novo_actions),
        ("Duty-to-assist error returns", workflow.dta_error_returns),
        ("CUE (38 C.F.R. § 3.105(a))", workflow.cue_analysis),
        ("Precedent vs. M21-1", workflow.precedent_m21_conflict_resolution),
        ("DRO disposition", workflow.dro_disposition_actions),
    ):
        lines.extend(f"{title}: {item}" for item in items)
    return lines


def _qrs_workflow_lines(guidance: AdjudicationRoleGuidance) -> list[str]:
    workflow = guidance.qrs
    lines: list[str] = []
    for title, items in (
        ("Second-signature checkpoints", workflow.second_signature_checkpoints),
        ("NQR/QRT error calls", workflow.nqr_qrt_error_calls),
        ("Sample review focus", workflow.sample_review_focus),
    ):
        lines.extend(f"{title}: {item}" for item in items)
    return lines


def _rvsr_lines(guidance: RvsrGuidance) -> list[str]:
    lines: list[str] = []
    for title, items in (
        ("Evidence review", guidance.evidence_review_checklist),
        ("Exam development", guidance.exam_development_triggers),
        ("Rating codesheet", guidance.rating_codesheet_actions),
        ("Reasons for decision", guidance.reasons_for_decision_requirements),
    ):
        lines.extend(f"{title}: {item}" for item in items)
    return lines


def _senior_lines(guidance: SeniorRaterGuidance) -> list[str]:
    lines: list[str] = []
    for title, items in (
        ("HLR de novo", guidance.hlr_de_novo_focus),
        ("Duty-to-assist error", guidance.duty_to_assist_errors),
        ("CUE analysis", guidance.cue_analysis),
        ("Precedent vs. M21-1", guidance.precedent_conflict_resolution),
        ("Quality review", guidance.quality_review_checkpoints),
    ):
        lines.extend(f"{title}: {item}" for item in items)
    return lines


def _analysis_to_text(analysis: LegalAnalysis) -> str:
    """Render a LegalAnalysis as a readable plain-text report."""
    deep_blocks = _deep_summary_blocks(analysis.deep_summaries)
    provenance_warnings = analysis.source_provenance_warnings
    blocks = [
        [f"Issue: {analysis.issue}"],
        [f"Run id: {analysis.run_id or '(none)'}"],
        _bullets("Top cases", analysis.top_cases),
        _bullets(
            "Surfaced case records (D1)",
            _surfaced_case_lines(analysis.surfaced_cases),
        )
        if analysis.surfaced_cases
        else [],
        _bullets(
            "Source reliability (Official vs. Secondary)",
            _source_reliability_lines(
                analysis.source_reliability_summary, analysis.surfaced_cases
            ),
        )
        if analysis.surfaced_cases
        else [],
        [f"Summary:\n{analysis.summary or '(none)'}"],
        _bullets("Structured case abstracts", _abstract_lines(analysis.case_abstracts))
        if analysis.case_abstracts
        else [],
        _bullets(
            "Provision-level VBA impacts",
            _provision_impact_lines(analysis.provision_impacts),
        )
        if analysis.provision_impacts
        else [],
        _bullets(
            "Full impact profiles (beyond nuance)",
            _impact_profile_lines(analysis.impact_profiles),
        )
        if analysis.impact_profiles
        else [],
        _bullets(
            "VA Rater (RVSR) Application",
            _rvsr_workflow_lines(analysis.adjudication_guidance),
        )
        if analysis.adjudication_guidance.rvsr.evidence_review_checklist
        else [],
        _bullets(
            "Senior VA Rater / DRO Application (HLR & CUE)",
            _senior_workflow_lines(analysis.adjudication_guidance),
        )
        if analysis.adjudication_guidance.senior_rater_dro.hlr_de_novo_actions
        else [],
        _bullets(
            "Quality Review (QRS) Checkpoints",
            _qrs_workflow_lines(analysis.adjudication_guidance),
        )
        if analysis.adjudication_guidance.qrs.nqr_qrt_error_calls
        else [],
        # Backward-compatible headings for callers that parsed the pre-AUD4-007
        # report. The workflow sections above are the authoritative structures.
        _bullets("RVSR operational playbook", _rvsr_lines(analysis.rvsr_guidance))
        if analysis.rvsr_guidance.evidence_review_checklist
        else [],
        _bullets(
            "Senior Rater / DRO operational playbook",
            _senior_lines(analysis.senior_rater_guidance),
        )
        if analysis.senior_rater_guidance.hlr_de_novo_focus
        else [],
        _bullets("LLM provenance warnings", analysis.llm_provenance_warnings)
        if analysis.llm_provenance_warnings
        else [],
        _bullets("Deep summaries", deep_blocks) if deep_blocks else [],
        [f"How this affects VA claims:\n{analysis.how_it_affects_va_claims or '(none)'}"],
        _bullets("Applicable principles", analysis.likely_applicable_principles),
        _bullets("Contradictions", [_contradiction_label(c) for c in analysis.contradictions]),
        _bullets("Next steps", analysis.next_steps),
        _bullets("Strengths", analysis.strengths + _search_strengths(analysis.search_telemetry)),
        _bullets("Gaps", analysis.gaps + _search_gaps(analysis.search_telemetry)),
        _bullets("Citation checks", analysis.citation_flags),
        _bullets("Authority checks", _authority_lines(analysis.authority_checks))
        if analysis.authority_checks
        else [],
        _bullets("Authority flags", analysis.authority_flags)
        if analysis.authority_flags
        else [],
        _bullets("Congressional links", _congress_lines(analysis.congress_links))
        if analysis.congress_links
        else [],
        _bullets(
            "CRS reports (secondary)",
            _crs_report_lines(analysis.congress_crs_reports),
        )
        if analysis.congress_crs_reports
        else [],
        _bullets("Congressional flags", analysis.congress_flags)
        if analysis.congress_flags
        else [],
        _bullets(
            "Regulations.gov rulemaking context",
            _regulations_lines(analysis.regulations_mappings),
        )
        if analysis.regulations_mappings
        else [],
        _bullets("Regulations.gov flags", analysis.regulations_flags)
        if analysis.regulations_flags
        else [],
        _bullets(
            "Statutory/regulatory supersession",
            _supersession_lines(analysis.statutory_supersession_signals),
        )
        if analysis.statutory_supersession_signals
        else [],
        _bullets(
            "Statutory/regulatory supersession flags",
            analysis.statutory_supersession_flags,
        )
        if analysis.statutory_supersession_flags
        else [],
        _bullets(
            "Quote-backed extraction candidates",
            _extraction_candidate_lines(analysis.extraction_candidates),
        )
        if analysis.extraction_candidates
        else [],
        _bullets("Authority queue", _authority_queue_lines(analysis.authority_queue))
        if analysis.authority_queue
        else [],
        _bullets("Citator checks", _citator_lines(analysis.citator_checks))
        if analysis.citator_checks
        else [],
        _bullets("Citator flags", analysis.citator_flags)
        if analysis.citator_flags
        else [],
        _bullets("Source-audit provenance warnings", provenance_warnings)
        if provenance_warnings
        else [],
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
        "gaps", "citation_flags", "authority_checks", "authority_flags",
        "congress_links", "congress_crs_reports", "congress_flags",
        "regulations_mappings", "regulations_flags",
        "extraction_candidates",
        "statutory_supersession_signals", "statutory_supersession_flags",
        "authority_queue", "citator_checks", "citator_flags",
        "detected_elements", "principle_findings", "statute_outcome_matrix",
        "search_telemetry", "courtlistener_quota",
        "source_provenance_warnings", "surfaced_cases", "case_abstracts",
        "provision_impacts", "impact_profile", "impact_profiles",
        "rvsr_guidance", "senior_rater_guidance", "adjudication_guidance",
        "source_reliability_summary", "llm_provenance_warnings",
        "llm_provenance_validated",
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
        " | ".join(analysis.citation_flags),
        json.dumps(
            [check.model_dump() for check in analysis.authority_checks],
            sort_keys=True,
        ),
        " | ".join(analysis.authority_flags),
        json.dumps(
            [link.model_dump() for link in analysis.congress_links],
            sort_keys=True,
        ),
        json.dumps(
            [report.model_dump() for report in analysis.congress_crs_reports],
            sort_keys=True,
        ),
        " | ".join(analysis.congress_flags),
        json.dumps(
            [mapping.model_dump() for mapping in analysis.regulations_mappings],
            sort_keys=True,
        ),
        " | ".join(analysis.regulations_flags),
        json.dumps(
            [
                group.model_dump()
                for group in analysis.extraction_candidates
            ],
            sort_keys=True,
        ),
        json.dumps(
            [
                signal.model_dump()
                for signal in analysis.statutory_supersession_signals
            ],
            sort_keys=True,
        ),
        " | ".join(analysis.statutory_supersession_flags),
        json.dumps(
            [entry.model_dump() for entry in analysis.authority_queue],
            sort_keys=True,
        ),
        json.dumps(
            [check.model_dump() for check in analysis.citator_checks],
            sort_keys=True,
        ),
        " | ".join(analysis.citator_flags),
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
        " | ".join(analysis.source_provenance_warnings),
        json.dumps(
            [record.model_dump() for record in analysis.surfaced_cases],
            sort_keys=True,
        ),
        json.dumps(
            [abstract.model_dump() for abstract in analysis.case_abstracts],
            sort_keys=True,
        ),
        json.dumps(
            [impact.model_dump() for impact in analysis.provision_impacts],
            sort_keys=True,
        ),
        json.dumps(
            analysis.impact_profile.model_dump()
            if analysis.impact_profile is not None
            else None,
            sort_keys=True,
        ),
        json.dumps(
            [profile.model_dump() for profile in analysis.impact_profiles],
            sort_keys=True,
        ),
        json.dumps(analysis.rvsr_guidance.model_dump(), sort_keys=True),
        json.dumps(analysis.senior_rater_guidance.model_dump(), sort_keys=True),
        json.dumps(analysis.adjudication_guidance.model_dump(), sort_keys=True),
        json.dumps(analysis.source_reliability_summary.model_dump(), sort_keys=True),
        " | ".join(analysis.llm_provenance_warnings),
        analysis.llm_provenance_validated,
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


def _provenance_sidecar_payload(analysis: LegalAnalysis) -> dict[str, object]:
    """Build the machine-readable source-audit provenance sidecar."""
    source_audits: list[dict[str, object]] = []
    recorded: list[tuple[dict[str, object], SourceAuditProvenance]] = []
    for check in analysis.citator_checks:
        for signal in check.source_validity:
            provenance = signal.audit_ledger.provenance
            record: dict[str, object] = {
                "target_case": check.target_case,
                "target_citation": check.target_citation,
                "source_opinion_id": signal.source_opinion_id,
                "source_cluster_id": signal.source_cluster_id,
                "provenance_recorded": provenance is not None,
                "provenance": (
                    provenance.model_dump(mode="json")
                    if provenance is not None
                    else None
                ),
            }
            source_audits.append(record)
            if provenance is not None:
                recorded.append((record, provenance))

    groups = group_source_audit_provenances(
        [provenance for _record, provenance in recorded]
    )
    execution_groups: list[dict[str, object]] = []
    for group in groups:
        group_ids = {id(provenance) for provenance in group}
        members = [
            record
            for record, provenance in recorded
            if id(provenance) in group_ids
        ]
        representative = group[0]
        execution_groups.append(
            {
                "provenance_version": representative.version,
                "audit_logic_version": representative.audit_logic_version,
                "configuration_fingerprint": (
                    representative.configuration_fingerprint
                ),
                "source_audit_count": len(group),
                "source_opinion_ids": [
                    member["source_opinion_id"] for member in members
                ],
            }
        )

    warnings = list(analysis.source_provenance_warnings)
    return {
        "version": SIDECAR_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": analysis.run_id,
        "issue": analysis.issue,
        "source_audit_count": len(source_audits),
        "recorded_source_audit_count": len(recorded),
        "unrecorded_source_audit_count": len(source_audits) - len(recorded),
        "source_audits": source_audits,
        "execution_groups": execution_groups,
        "mixed_provenance": bool(warnings),
        "warnings": warnings,
    }


def _sidecar_requested(explicit: str | None) -> bool:
    """Whether an explicit sidecar value requests a file."""
    return bool(
        explicit is not None
        and explicit.strip().lower() not in {"", "off", "none"}
    )


def _resolve_provenance_sidecar_path(
    explicit: str | None, output_file: str | None
) -> Path | None:
    """Resolve an explicit sidecar path or derive one beside report output."""
    if explicit is not None:
        return Path(explicit) if _sidecar_requested(explicit) else None
    if output_file:
        return Path(output_file).with_suffix(".provenance.json")
    return None


def _write_provenance_sidecar(
    analysis: LegalAnalysis, path: Path
) -> None:
    """Write the provenance sidecar as stable, machine-readable JSON."""
    path.write_text(
        json.dumps(
            _provenance_sidecar_payload(analysis),
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


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
    gap_per_round = len(elements)
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
    max_batch_requests: int | None = None,
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
    if max_batch_requests is not None and max_batch_requests < 1:
        raise ValueError("max_batch_requests must be a positive integer")

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
    # An explicit request cap wins; otherwise the batch may use whatever the
    # daily window provides (None disables the packing cap entirely).
    packing_cap = max_batch_requests if max_batch_requests is not None else None
    packed_total = 0
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
        # daily window or an explicit --max-batch-requests cap would be
        # exceeded. A cap-based deferral is marked 'deferred' so scheduling can
        # retry it later without treating it as a quota-blocked abort.
        if packing_cap is not None:
            if packed_total + cl_requests > packing_cap:
                row["verdict"] = "deferred"
            else:
                packed_total += cl_requests
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
        "deferred_issues": sum(1 for r in rows if r["verdict"] == "deferred"),
        "max_batch_requests": packing_cap,
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
    if summary.get("deferred_issues"):
        lines.append(
            f"{summary['deferred_issues']} of {summary['issues']} issue(s) deferred "
            f"by --max-batch-requests {summary.get('max_batch_requests')} (raise the cap "
            "or schedule them for the next window)."
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


# ---------------------------------------------------------------------------
# Auto-run (--auto-run): dry-run the batch first, then actually execute the
# issues that fit (proceed), and loop -- waiting for the CourtListener window
# to reset when needed -- until the deferred/aborted remainder has been run
# or the operator stops it (Ctrl-C). The loop is intentionally bounded so an
# unattended process cannot wait forever on an impossible backlog.
# ---------------------------------------------------------------------------

# A daily quota window can require many passes for a large batch, but an
# unattended process must not spin forever if the quota endpoint is stale or
# the backlog can never fit. One planning pass is made per iteration; each
# wait is capped at one day (the longest useful daily-window wait).
_AUTO_RUN_MAX_ITERATIONS = 100
_AUTO_RUN_MAX_WAIT_SECONDS = 24 * 60 * 60


def _sleep_with_log(seconds: float, reason: str) -> None:
    """Sleep *seconds*, logging the wait so operators can see the pacing."""
    logging.getLogger(__name__).warning(
        "auto-run: sleeping %.0fs before %s (interrupt with Ctrl-C to stop)",
        seconds,
        reason,
    )
    time.sleep(seconds)


def _auto_run(
    issues: list[str],
    claim_type: str,
    max_results: int,
    max_wall_seconds: float | None,
    max_batch_requests: int | None = None,
    run_id: str = "",
    max_iterations: int | None = None,
    max_wait_seconds: float | None = None,
    snapshot_dir: str | None = None,
) -> list[dict[str, object]]:
    """Execute the batch: run the packed issues now, wait for the reset, repeat.

    The first pass uses the same per-issue estimates as the dry run to decide
    which issues fit the current windows (proceed) and executes those through
    :func:`analyze_cases_for_claim`; issues that were deferred or aborted
    stay pending. After the pass, the remainder is re-planned against a
    fresh live usage snapshot: if CourtListener's daily window is exhausted
    the loop waits (sleeps) for its reset time before retrying. Every per-issue
    analysis and terminal event carries *run_id*. Returns per-issue result rows
    with verdicts ``run`` / ``error`` for the executed issues, and ``deferred``
    / ``abort`` for the backlog that could not fit (and would need a later
    window). A failed quota lookup is retried once after a bounded fallback
    wait; a second consecutive lookup failure stops with the pending rows.
    A pending batch that does not use CourtListener is not waited on.

    The planning/wait loop is bounded by *max_iterations* and each sleep by
    *max_wait_seconds*. It stops early when a pass cannot change the pending
    set: a cap-deferred backlog is impossible to schedule without changing
    ``--max-batch-requests``, and a second consecutive quota-stalled pass means
    the reset did not free the window. In either case the pending rows retain
    their verdicts so the caller can report an incomplete run.
    """
    logger = logging.getLogger(__name__)
    results: dict[str, dict[str, object]] = {}
    pending = list(issues)
    iterations = 0
    stalled_waits = 0
    usage_failures = 0
    iteration_limit = (
        _AUTO_RUN_MAX_ITERATIONS if max_iterations is None else max(1, max_iterations)
    )
    wait_limit = (
        _AUTO_RUN_MAX_WAIT_SECONDS if max_wait_seconds is None else max_wait_seconds
    )
    while pending:
        if iterations >= iteration_limit:
            logger.error(
                "auto-run stopped after %d planning pass(es): %d issue(s) remain",
                iterations,
                len(pending),
            )
            break
        iterations += 1
        rows, _summary = batch_dry_run(
            pending,
            claim_type=claim_type,
            max_results=max_results,
            max_batch_requests=max_batch_requests,
        )
        next_pending: list[str] = []
        next_verdicts: dict[str, str] = {}
        ran_this_pass = False
        planned = {str(row["issue"]) for row in rows}
        for row in rows:
            issue = str(row["issue"])
            verdict = str(row["verdict"])
            if verdict == "proceed":
                ran_this_pass = True
                try:
                    analysis = analyze_cases_for_claim(
                        issue,
                        claim_type=claim_type,
                        max_results=max_results,
                        telemetry=None,
                        max_wall_seconds=max_wall_seconds,
                        snapshot_dir=snapshot_dir,
                    )
                    analysis.run_id = run_id
                    _emit_completion_event(analysis, run_id)
                    results[issue] = {"issue": issue, "verdict": "run"}
                except Exception as exc:  # noqa: BLE001 - per-issue failure is recorded
                    sanitize_exception(exc)
                    _emit_failure_event(issue, exc, run_id)
                    results[issue] = {
                        "issue": issue,
                        "verdict": "error",
                        "error": redact_secrets(str(exc)),
                        "error_type": type(exc).__name__,
                        "traceback": _format_exception(exc),
                    }
            else:
                # Keep the first non-run verdict for issues still pending
                # (deferred/abort); a later successful pass overwrites it.
                results.setdefault(issue, {"issue": issue, "verdict": verdict})
                next_pending.append(issue)
                next_verdicts[issue] = verdict
        # Defensive: a planner row is expected for every pending issue. If one
        # is omitted, keep it pending rather than silently dropping it from the
        # summary and exiting successfully.
        for issue in pending:
            if issue not in planned:
                results.setdefault(issue, {"issue": issue, "verdict": "deferred"})
                next_pending.append(issue)
                next_verdicts[issue] = "deferred"
        if not next_pending:
            break
        if not ran_this_pass and set(next_pending) == set(pending):
            if "deferred" in next_verdicts.values():
                logger.error(
                    "auto-run stopped: no progress is possible while %d issue(s) "
                    "remain deferred by --max-batch-requests=%s",
                    len(next_pending),
                    max_batch_requests,
                )
                break
            stalled_waits += 1
            if stalled_waits >= 2:
                logger.error(
                    "auto-run stopped: no progress after waiting for the quota "
                    "reset (%d issue(s) remain)",
                    len(next_pending),
                )
                break
        else:
            stalled_waits = 0
        # Remainders still need the window: wait for the daily reset, but keep
        # each sleep bounded even if the endpoint reports a far-future reset.
        # Without CourtListener there is no quota window to wait for; stop
        # rather than polling a provider that cannot free the backlog.
        if "courtlistener" not in resolve_search_providers():
            logger.error(
                "auto-run stopped: %d issue(s) remain but CourtListener is not "
                "configured, so no quota window can free them",
                len(next_pending),
            )
            break
        try:
            usage = fetch_courtlistener_usage()
            daily = courtlistener_daily_budget(usage)
        except SearchError as exc:
            sanitize_exception(exc)
            usage_failures += 1
            logger.error(
                "auto-run: could not determine the next quota window: %s "
                "(failure %d of 2)",
                exc,
                usage_failures,
                exc_info=True,
            )
            if usage_failures >= 2:
                logger.error(
                    "auto-run stopped: CourtListener usage lookup failed %d "
                    "consecutive time(s); %d issue(s) remain",
                    usage_failures,
                    len(next_pending),
                )
                break
            # A single transient lookup failure should not abandon a
            # resumable batch. Retry after a bounded fallback wait; reset the
            # quota-stall counter so the retry can actually reach the lookup.
            stalled_waits = 0
            wait = max(0.0, min(60.0, wait_limit))
            _sleep_with_log(wait, "retrying the CourtListener usage lookup")
            pending = next_pending
            continue
        usage_failures = 0
        reset = daily.get("reset_at")
        delay = _seconds_until_utc(str(reset)) if reset else 3600.0
        wait = min(max(delay, 60.0), wait_limit)
        _sleep_with_log(wait, "the CourtListener daily window reset")
        pending = next_pending
    return list(results.values())


def _seconds_until_utc(iso: str) -> float:
    """Seconds until the ISO-8601 timestamp *iso* (naive or tz-aware UTC)."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 3600.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    return max(delta, 0.0)


def _auto_run_summary(results: list[dict[str, object]]) -> dict[str, object]:
    """Aggregate the auto-run per-issue result rows into a summary dict."""
    ok = sum(1 for r in results if r["verdict"] == "run")
    err = sum(1 for r in results if r["verdict"] == "error")
    deferred = sum(1 for r in results if r["verdict"] == "deferred")
    aborted = sum(1 for r in results if r["verdict"] == "abort")
    errors = [
        {
            "issue": row["issue"],
            "error_type": row.get("error_type"),
            "error": row.get("error"),
            "traceback": row.get("traceback"),
        }
        for row in results
        if row["verdict"] == "error"
    ]
    return {
        "issues": len(results),
        "successful": ok,
        "failed": err,
        "deferred": deferred,
        "aborted": aborted,
        "errors": errors,
    }


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
    parser.add_argument(
        "--provenance-sidecar",
        dest="provenance_sidecar",
        default=None,
        metavar="PATH",
        help=(
            "Write source-audit provenance as a machine-readable JSON sidecar "
            "for a single-issue analysis. When omitted with --output-file, "
            "defaults to '<output>.provenance.json'; pass 'off' to disable. "
            "Rejected with --show-config, --dry-run, --batch-dry-run, or "
            "--auto-run."
        ),
    )
    parser.add_argument(
        "--compare-provenance",
        dest="compare_provenance",
        nargs=2,
        metavar=("SIDECAR_A", "SIDECAR_B"),
        default=None,
        help=(
            "Compare two source-audit provenance sidecars and report "
            "configuration drift, code drift, data drift, and unrecorded "
            "audits. Writes text unless --comparison-format json is set."
        ),
    )
    parser.add_argument(
        "--comparison-format",
        dest="comparison_format",
        choices=("text", "json"),
        default="text",
        help=(
            "Output format for --compare-provenance (default: text). "
            "Use --output-file to write it to a file."
        ),
    )
    parser.add_argument(
        "--require-comparable",
        action="store_true",
        help=(
            "With --compare-provenance: exit with status 1 after emitting "
            "the report when the analyses are not genuinely comparable."
        ),
    )
    parser.add_argument(
        "--adjudication-queue",
        dest="adjudication_queue",
        default=None,
        help=(
            "After analysis, write evidence-first citator findings to this human "
            "review queue (JSON). The queue does not change citator status or ranking."
        ),
    )
    parser.add_argument(
        "--snapshot-dir",
        dest="snapshot_dir",
        default=None,
        help=(
            "Persist evidence and CourtListener response snapshots under this "
            "directory. Defaults to SNAPSHOT_DIR; empty disables snapshots."
        ),
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
            "window across them (issues from --issues file plus the positional "
            "issue; CSV table with --output-format csv)."
        ),
    )
    parser.add_argument(
        "--auto-run",
        action="store_true",
        help=(
            "Run the batch dry-run, execute the issues that fit the current "
            "windows, then keep re-planning and waiting for the CourtListener "
            "daily reset until every issue has been run (or interrupt with "
            "Ctrl-C). Combine with --max-batch-requests to cap each pass."
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
        "--max-batch-requests",
        dest="max_batch_requests",
        type=_positive_int,
        default=None,
        help=(
            "With --batch-dry-run or --auto-run: cap the CourtListener request "
            "total for each pass; issues that would exceed the cap get a "
            "'deferred' verdict instead of consuming quota. Must be a positive "
            "integer. Defaults to the daily-window remainder when CourtListener "
            "is configured; no cap otherwise."
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

    if args.require_comparable and not args.compare_provenance:
        parser.error("--require-comparable requires --compare-provenance.")

    if args.compare_provenance:
        if _sidecar_requested(args.provenance_sidecar):
            parser.error(
                "--compare-provenance cannot be combined with "
                "--provenance-sidecar."
            )
        if (
            args.show_config
            or args.dry_run
            or args.batch_dry_run
            or args.auto_run
        ):
            parser.error(
                "--compare-provenance cannot be combined with --show-config, "
                "--dry-run, --batch-dry-run, or --auto-run."
            )
        if args.output_file:
            output_path = Path(args.output_file).resolve()
            if output_path in {
                Path(path).resolve()
                for path in args.compare_provenance
            }:
                parser.error(
                    "--output-file must differ from both provenance sidecars."
                )
        try:
            comparison = compare_provenance_sidecars(
                load_provenance_sidecar(args.compare_provenance[0]),
                load_provenance_sidecar(args.compare_provenance[1]),
            )
        except ValueError as exc:
            parser.error(f"could not compare provenance sidecars: {exc}")
        comparison_output = (
            json.dumps(comparison, indent=2, ensure_ascii=False)
            if args.comparison_format == "json"
            else render_comparison_text(comparison)
        )
        try:
            _emit_output(comparison_output, args.output_file)
        except OSError as exc:
            parser.error(f"could not write comparison output: {exc}")
        if args.require_comparable and not comparison.get("comparable"):
            raise SystemExit(1)
        return

    # Surface misconfigured SEARCH_PROVIDERS early — including --show-config
    # runs — so typos are visible before any search happens.
    validate_search_providers()

    if _sidecar_requested(args.provenance_sidecar) and (
        args.show_config
        or args.dry_run
        or args.batch_dry_run
        or args.auto_run
    ):
        parser.error(
            "--provenance-sidecar is only supported for a single-issue analysis; "
            "it cannot be used with --show-config, --dry-run, "
            "--batch-dry-run, or --auto-run. Pass 'off' to disable it."
        )

    if args.show_config:
        print_settings()
        return

    if (args.start_at is not None or args.only_blocked) and not (args.batch_dry_run or args.auto_run):
        parser.error("--start-at and --only-blocked only apply to --batch-dry-run.")

    if not args.issue and not (args.batch_dry_run or args.auto_run):
        parser.error("the following arguments are required: issue")

    max_results = args.max_results if args.max_results is not None else get_settings().search_max_results

    # --auto-run: parse the issues the same way as --batch-dry-run, then run.
    if args.auto_run:
        if args.dry_run:
            parser.error("--dry-run and --auto-run are mutually exclusive.")
        auto_issues = _read_issues_file(args.issues_file) if args.issues_file else []
        auto_issues = [issue for issue, _priority in auto_issues]
        if args.issue:
            auto_issues.append(args.issue)
        if not auto_issues:
            parser.error(
                "--auto-run needs at least one issue (positional or --issues-file)."
            )
        run_id = _run_id(args.run_id)
        results = _auto_run(
            auto_issues,
            claim_type=args.claim_type,
            max_results=max(max_results, 1),
            max_wall_seconds=args.max_wall_time,
            max_batch_requests=args.max_batch_requests,
            run_id=run_id,
            snapshot_dir=args.snapshot_dir,
        )
        summary = _auto_run_summary(results)
        complete = summary["successful"] == summary["issues"]
        status = "complete" if complete else "incomplete"
        print(
            "Auto-run %s: %d issue(s) run, %d failed, %d deferred, %d aborted"
            % (
                status,
                summary["successful"],
                summary["failed"],
                summary["deferred"],
                summary["aborted"],
            )
        )
        if args.output_format == "json":
            print(json.dumps(summary, indent=2))
        if not complete:
            # Cron/systemd and run_batch.sh consume this status; a partial
            # batch must not look successful in an unattended run.
            raise SystemExit(1)
        return

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
            max_batch_requests=args.max_batch_requests,
        )
        for row in rows:
            row["priority"] = priority_by_issue.get(row["issue"])  # type: ignore[typeddict-item]
        if args.only_blocked:
            rows_visible = [row for row in rows if row["verdict"] == "abort"]
        else:
            rows_visible = rows
        # Write the blocked-issues retry file.
        #   * Explicit --retry-file PATH  -> always write (empty if nothing
        #     is blocked, nothing else to do).
        #   * --retry-file "" / none / off -> never write.
        #   * No flag, batch CSV output    -> write to 'issues.retry' (or the
        #     CSV path with a .retry.txt suffix) only when something is
        #     blocked; an all-proceed batch leaves no retry file behind.
        # Abort AND deferred rows are worth retrying later; both go into the
        # retry file so a post-reset (or bigger-budget) re-run can pick them up.
        blocked = [row for row in rows if row["verdict"] in ("abort", "deferred")]
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
            snapshot_dir=args.snapshot_dir,
        )
    except Exception as exc:  # noqa: BLE001 - emit event, then preserve original behavior
        sanitize_exception(exc)
        _emit_failure_event(args.issue, exc, run_id)
        if tracker:
            tracker.record(
                {
                    "event": "analysis_failed",
                    "issue": args.issue,
                    "error": redact_secrets(str(exc)),
                    "search_telemetry": telemetry,
                }
            )
            _maybe_emit_batch_summary(tracker, args.batch_size)
        raise
    analysis.run_id = run_id
    _emit_completion_event(analysis, run_id)
    if args.adjudication_queue:
        try:
            queue = build_adjudication_queue(
                analysis.citator_checks, source=f"run:{run_id}"
            )
            save_queue(queue, args.adjudication_queue)
        except OSError as exc:
            parser.error(f"could not write adjudication queue: {exc}")
        print(
            f"Adjudication queue: {len(queue.items)} item(s) -> "
            f"{args.adjudication_queue}",
            file=sys.stderr,
        )
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
    sidecar_path = _resolve_provenance_sidecar_path(
        args.provenance_sidecar, args.output_file
    )
    if (
        sidecar_path is not None
        and args.output_file
        and sidecar_path.resolve() == Path(args.output_file).resolve()
    ):
        parser.error("--provenance-sidecar must differ from --output-file")
    if args.output_file:
        try:
            Path(args.output_file).write_text(output + "\n", encoding="utf-8")
        except OSError as exc:
            parser.error(f"could not write output file: {exc}")
        if sidecar_path is not None:
            try:
                _write_provenance_sidecar(analysis, sidecar_path)
            except OSError as exc:
                parser.error(f"could not write provenance sidecar: {exc}")
        return
    print(output)
    if sidecar_path is not None:
        try:
            _write_provenance_sidecar(analysis, sidecar_path)
        except OSError as exc:
            parser.error(f"could not write provenance sidecar: {exc}")


if __name__ == "__main__":
    main()
