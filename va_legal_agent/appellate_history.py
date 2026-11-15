"""Shadow appellate-history layer built from citator signals.

This module deliberately does **not** infer an appeal from a same-docket
cluster, a later citation, a party name, or a docket number. Those are
candidates for human review. A history label appears only in
``verified_history`` after a reviewer supplies a record tied to an official or
licensed source, a relationship, a disposition, a scope, and a rationale.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import (
    AppellateHistoryCandidate,
    AppellateHistoryCheck,
    AppellateHistoryRecord,
    CitatorCheck,
)

RELATIONSHIPS = frozenset(
    {"direct_appeal", "rehearing", "remand", "collateral", "no_relationship", "other"}
)
DISPOSITIONS = frozenset(
    {
        "affirmed",
        "reversed",
        "vacated",
        "remanded",
        "dismissed",
        "denied",
        "granted",
        "other",
    }
)
SCOPES = frozenset({"all", "part"})
SOURCE_QUALITIES = frozenset({"official", "licensed"})
REVIEW_STATUSES = frozenset({"confirmed", "rejected"})


def _normalize_citation(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _candidate_key(candidate: AppellateHistoryCandidate) -> str:
    if candidate.cluster_id:
        return f"cluster:{candidate.cluster_id}"
    if candidate.citation:
        return f"citation:{_normalize_citation(candidate.citation)}"
    return f"name:{candidate.candidate_case.casefold()}:{candidate.decision_date}"


def _merge_signal(
    candidates: dict[str, AppellateHistoryCandidate],
    candidate: AppellateHistoryCandidate,
) -> None:
    key = _candidate_key(candidate)
    existing = candidates.get(key)
    if existing is None:
        candidates[key] = candidate
        return
    for signal in candidate.signals:
        if signal not in existing.signals:
            existing.signals.append(signal)
    for reason in candidate.match_reasons:
        if reason not in existing.match_reasons:
            existing.match_reasons.append(reason)
    for note in candidate.coverage_notes:
        if note not in existing.coverage_notes:
            existing.coverage_notes.append(note)


def candidates_from_check(check: CitatorCheck) -> list[AppellateHistoryCandidate]:
    """Build review candidates from the citator's advisory signals."""
    candidates: dict[str, AppellateHistoryCandidate] = {}
    for decision in check.same_docket_decisions:
        _merge_signal(
            candidates,
            AppellateHistoryCandidate(
                signal_type="same_docket",
                signals=["same_docket"],
                candidate_case=decision.case_name,
                citation=decision.citation,
                court=decision.court,
                decision_date=decision.decision_date,
                url=decision.url,
                cluster_id=decision.cluster_id,
                match_reasons=[
                    "appears on the target's CourtListener docket",
                ],
                coverage_notes=[
                    "Same-docket clusters are intra-court context only; they do "
                    "not establish an appeal or a later proceeding.",
                ],
            ),
        )
    for cited in check.later_citing_opinions:
        _merge_signal(
            candidates,
            AppellateHistoryCandidate(
                signal_type="later_citing_opinion",
                signals=["later_citing_opinion"],
                candidate_case=cited.case_name,
                citation=cited.citation,
                court=cited.court,
                decision_date=cited.decision_date,
                url=cited.url,
                cluster_id="",
                opinion_id=cited.opinion_id,
                match_reasons=["explicitly cites the target"],
                coverage_notes=[
                    "A later citation is not proof of an appeal, rehearing, or "
                    "treatment.",
                ],
            ),
        )
    return sorted(
        candidates.values(),
        key=lambda item: (
            item.decision_date or "",
            item.candidate_case.casefold(),
        ),
    )


def _target_matches(record: AppellateHistoryRecord, check: CitatorCheck) -> bool:
    """Match a record only by cluster id or normalized citation, never by name."""
    if record.target_cluster_id and check.target_cluster_id:
        if record.target_cluster_id != check.target_cluster_id:
            return False
        return True
    record_citations = {
        _normalize_citation(record.target_citation)
    } if record.target_citation else set()
    target_citations = {
        _normalize_citation(citation)
        for citation in ([check.target_citation, *check.target_citations])
        if citation
    }
    return bool(record_citations & target_citations)


def _candidate_matches(
    candidate: AppellateHistoryCandidate, record: AppellateHistoryRecord
) -> bool:
    if record.candidate_cluster_id and candidate.cluster_id:
        if record.candidate_cluster_id != candidate.cluster_id:
            return False
        return True
    if record.candidate_citation and candidate.citation:
        if _normalize_citation(record.candidate_citation) != _normalize_citation(
            candidate.citation
        ):
            return False
        return True
    return False


def _record_problems(record: AppellateHistoryRecord) -> list[str]:
    problems: list[str] = []
    if not record.candidate_case.strip():
        problems.append("candidate case name is missing")
    if not (record.candidate_cluster_id or record.candidate_citation):
        problems.append("candidate cluster id or citation is missing")
    if not record.reviewer.strip():
        problems.append("reviewer is missing")
    if not record.reviewed_at.strip():
        problems.append("reviewed_at is missing")
    if not record.rationale.strip():
        problems.append("rationale is missing")
    if record.review_status not in REVIEW_STATUSES:
        problems.append("review_status must be confirmed or rejected")
    if record.review_status == "confirmed":
        if record.relationship not in RELATIONSHIPS:
            problems.append("relationship is missing or unsupported")
        if record.disposition not in DISPOSITIONS:
            problems.append("disposition is missing or unsupported")
        if record.scope not in SCOPES:
            problems.append("scope must be all or part")
        if record.source_quality not in SOURCE_QUALITIES:
            problems.append("source_quality must be official or licensed")
        if not record.source_url.startswith(("http://", "https://")):
            problems.append("authoritative source URL is missing")
    return problems


def _candidate_from_record(
    record: AppellateHistoryRecord, *, verified: bool
) -> AppellateHistoryCandidate:
    return AppellateHistoryCandidate(
        signal_type="verified_record",
        signals=["verified_record"],
        candidate_case=record.candidate_case,
        citation=record.candidate_citation,
        cluster_id=record.candidate_cluster_id,
        match_reasons=["human verification record"],
        coverage_notes=[
            "Reviewed against an official or licensed source by a human.",
            *record.coverage_notes,
        ],
        verification_status="verified" if verified else "rejected",
        relationship=record.relationship,
        disposition=record.disposition,
        scope=record.scope,
        source_quality=record.source_quality,
        source_url=record.source_url,
        reviewer=record.reviewer,
        reviewed_at=record.reviewed_at,
        rationale=record.rationale,
    )


def _apply_record(
    candidate: AppellateHistoryCandidate, record: AppellateHistoryRecord
) -> None:
    candidate.verification_status = (
        "verified" if record.review_status == "confirmed" else "rejected"
    )
    candidate.relationship = record.relationship
    candidate.disposition = record.disposition
    candidate.scope = record.scope
    candidate.source_quality = record.source_quality
    candidate.source_url = record.source_url
    candidate.reviewer = record.reviewer
    candidate.reviewed_at = record.reviewed_at
    candidate.rationale = record.rationale
    for note in record.coverage_notes:
        if note not in candidate.coverage_notes:
            candidate.coverage_notes.append(note)


def build_appellate_history(
    check: CitatorCheck,
    records: list[AppellateHistoryRecord] | None = None,
    *,
    source: str = "",
) -> AppellateHistoryCheck:
    """Attach candidate signals and validated human records to *check*.

    Records for other targets are ignored. A record that matches this target
    but lacks required verification fields is surfaced as an
    ``insufficient_source`` candidate; it never becomes a label.
    """
    candidates = candidates_from_check(check)
    flags: list[str] = []
    verified: list[AppellateHistoryRecord] = []
    for record in records or []:
        if not _target_matches(record, check):
            continue
        problems = _record_problems(record)
        if problems:
            flags.append(
                f"Appellate history record for '{record.candidate_case}' is "
                f"insufficient: {'; '.join(problems)}."
            )
            for candidate in candidates:
                if _candidate_matches(candidate, record):
                    candidate.verification_status = "insufficient_source"
                    candidate.coverage_notes.append(
                        "Verification record is incomplete; no history label was exposed."
                    )
            continue
        candidate = next(
            (
                item
                for item in candidates
                if _candidate_matches(item, record)
            ),
            None,
        )
        is_history_label = (
            record.review_status == "confirmed"
            and record.relationship != "no_relationship"
        )
        if candidate is None:
            candidate = _candidate_from_record(
                record, verified=is_history_label
            )
            candidates.append(candidate)
        else:
            _apply_record(candidate, record)
            if not is_history_label:
                candidate.verification_status = "rejected"
        if is_history_label:
            if record not in verified:
                verified.append(record)
        elif record.review_status == "confirmed":
            flags.append(
                f"Appellate history candidate '{record.candidate_case}' was "
                "confirmed to have no appellate relationship; no history label "
                "was exposed."
            )
        else:
            flags.append(
                f"Appellate history candidate '{record.candidate_case}' was "
                f"rejected: {record.rationale}"
            )

    candidates.sort(key=lambda item: (item.decision_date or "", item.candidate_case.casefold()))
    if verified:
        status = "verified"
    elif candidates:
        status = "insufficient_source"
        flags.append(
            f"Appellate history for '{check.target_case}' has "
            f"{len(candidates)} candidate signal(s) but no confirmed human "
            "record; absence of a label is not a finding that no appeal exists."
        )
    else:
        status = "not_assessed"
        flags.append(
            f"Appellate history for '{check.target_case}' was not assessed: no "
            "same-docket or later-citing candidate signals were available; "
            "absence of a candidate is not a finding that no appeal exists."
        )
    return AppellateHistoryCheck(
        target_case=check.target_case,
        target_cluster_id=check.target_cluster_id,
        target_citations=(
            list(check.target_citations)
            if check.target_citations
            else ([check.target_citation] if check.target_citation else [])
        ),
        status=status,
        candidates=candidates,
        verified_history=verified,
        flags=flags,
        source=source,
    )


def load_appellate_history_records(path: str | Path) -> list[AppellateHistoryRecord]:
    """Load a JSON array or ``{"records": [...]}`` verification file."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("records")
    if not isinstance(payload, list):
        raise ValueError("appellate history file must contain a records array")
    return [AppellateHistoryRecord.model_validate(item) for item in payload]
