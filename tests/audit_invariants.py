"""Reusable invariants for per-opinion source-cluster audit rows."""

from __future__ import annotations

import traceback
from collections.abc import Sequence

from va_legal_agent.models import (
    SOURCE_AUDIT_GAP_CODES,
    SOURCE_OPINION_FETCH_ERROR_KINDS,
    SOURCE_OPINION_LIMIT_REASONS,
    LegalAnalysis,
    SourceAuditLedger,
    SourceOpinionAudit,
)


def assert_aud4_domain_invariants(analysis: LegalAnalysis) -> None:
    """Assert the AUD4-002/003/004 lossless-output contract.

    This helper intentionally checks the cross-object invariants a renderer
    could otherwise hide: every legacy display string has a D1 record, every
    surfaced record carries provenance state, every structured abstract is
    meaningfully populated, provision impacts name a stage/effect/anchor and a
    manual citation, and both role playbooks are non-empty.
    """
    assert len(analysis.surfaced_cases) == min(len(analysis.top_cases), 5)
    assert len(analysis.surfaced_cases) == len(analysis.case_abstracts)
    assert analysis.rvsr_guidance.evidence_review_checklist
    assert analysis.rvsr_guidance.exam_development_triggers
    assert analysis.rvsr_guidance.rating_codesheet_actions
    assert analysis.rvsr_guidance.reasons_for_decision_requirements
    assert analysis.senior_rater_guidance.hlr_de_novo_focus
    assert analysis.senior_rater_guidance.duty_to_assist_errors
    assert analysis.senior_rater_guidance.cue_analysis
    assert analysis.senior_rater_guidance.precedent_conflict_resolution
    assert analysis.senior_rater_guidance.quality_review_checkpoints
    # AUD4-005/007: structured role workflows and full impact persistence.
    assert analysis.impact_profile is not None
    assert analysis.impact_profiles
    for profile in analysis.impact_profiles:
        assert profile.issue_tags
        assert profile.nuance
        assert profile.provision_impacts
    guidance = analysis.adjudication_guidance
    assert guidance.rvsr.evidence_review_checklist
    assert guidance.rvsr.lay_evidence_competence
    assert any("5107(b)" in item for item in guidance.rvsr.lay_evidence_competence)
    assert guidance.rvsr.duty_to_assist_exam_actions
    assert guidance.rvsr.rating_codesheet_actions
    assert guidance.rvsr.effective_date_actions
    assert guidance.rvsr.reasons_for_decision_requirements
    assert guidance.senior_rater_dro.hlr_de_novo_actions
    assert any("3.2601" in item for item in guidance.senior_rater_dro.hlr_de_novo_actions)
    assert guidance.senior_rater_dro.dta_error_returns
    assert guidance.senior_rater_dro.cue_analysis
    assert any("3.105(a)" in item for item in guidance.senior_rater_dro.cue_analysis)
    assert any("George" in item for item in guidance.senior_rater_dro.cue_analysis)
    assert guidance.senior_rater_dro.precedent_m21_conflict_resolution
    assert guidance.qrs.second_signature_checkpoints
    assert guidance.qrs.nqr_qrt_error_calls
    assert guidance.qrs.sample_review_focus
    # AUD4-008: every surfaced authority has an explicit tier and the aggregate
    # summary accounts for every record.
    assert analysis.source_reliability_summary.total == len(analysis.surfaced_cases)
    valid_tiers = {
        "official_primary_binding",
        "official_primary_persuasive",
        "official_agency_guidance",
        "secondary_summary",
        "unverified",
    }
    for record in analysis.surfaced_cases:
        assert record.authority_tier in valid_tiers
        assert isinstance(record.is_official_authority, bool)
    valid_sources = {
        "official_court_opinion",
        "official_transcript_archive",
        "secondary_summary",
        "unverified",
    }
    valid_statuses = {
        "precedential_panel",
        "en_banc",
        "single_judge_memorandum",
        "non_precedential_bva",
        "unknown",
    }
    for record in analysis.surfaced_cases:
        assert record.case_title and record.court
        assert record.precedential_status in valid_statuses
        assert record.transcript_source_type in valid_sources
        assert record.panel_status
        assert record.citator_status in {
            "not_assessed",
            "good_law",
            "negative",
            "unknown",
        }
        assert record.citator_confidence in {"none", "low", "medium", "high"}
        assert record.history_status in {
            "not_assessed",
            "superseded_detected",
            "verified_history",
            "good_law_as_of",
            "negative_treatment",
            "unknown",
        }
        assert record.official_transcript_url is None or record.provenance_verified
        if record.transcript_source_type != "official_court_opinion":
            assert any("Transcript source" in warning for warning in record.warnings)
        if record.precedential_status == "single_judge_memorandum":
            assert any("Single-judge" in warning for warning in record.warnings)
        assert record.case_abstract is not None
        assert record.case_abstract.case_title == record.case_title
        assert record.case_abstract.claimant_and_conditions
    for abstract in analysis.case_abstracts:
        assert abstract.case_title
        assert abstract.claimant_and_conditions
        assert abstract.source_text_chars >= 0
        assert abstract.abstract_source in {"full_text", "record_metadata"}
        if abstract.abstract_complete:
            assert any(
                (
                    abstract.procedural_history,
                    abstract.issues_presented,
                    abstract.holding_and_legal_rule,
                    abstract.reasoning_summary,
                    abstract.disposition,
                )
            )
    for impact in analysis.provision_impacts:
        assert impact.citation
        assert impact.stage in {
            "intake_notice",
            "development_exam",
            "rating_dc_evaluation",
            "effective_date",
            "appellate_review",
        }
        assert impact.legal_effect in {
            "narrowing",
            "expanding",
            "invalidating_m21_1_rule",
            "clarifying_evidentiary_threshold",
            "clarifying",
            "procedural",
        }
        assert impact.provenance_anchor
        assert impact.m21_1_citations
        assert impact.registry_version


def _expected_bottlenecks(
    rows: Sequence[SourceOpinionAudit],
) -> dict[str, list[str]]:
    """Derive the expected summary independently of the model helper."""
    expected: dict[str, list[str]] = {}
    for row in rows:
        reasons = list(row.gap_codes)
        if row.candidate_allocation == 0:
            reasons.append("zero_allocation")
        elif row.candidate_pool_limit_reason in {"row", "cursor"}:
            reasons.append(f"candidate_pool_{row.candidate_pool_limit_reason}")
        if (
            row.pages_fetched > 0
            and not row.reported_count_known
            and not row.reported_count_malformed
        ):
            reasons.append("reported_count_unknown")
        if row.invalid_rows_ignored > 0:
            reasons.append("malformed_rows")
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            expected[row.opinion_id] = reasons
    return expected


def assert_exception_chain_redacted(exc: BaseException, secret: str) -> None:
    """Assert that *secret* is absent from an exception and every rendered form.

    This is deliberately independent of the production redaction helper: it
    walks ``__cause__``/``__context__`` directly and formats each node, so a
    regression that only redacts the outer message still fails.
    """
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert secret not in str(current), f"secret in str({type(current).__name__})"
        assert secret not in repr(current), f"secret in repr({type(current).__name__})"
        assert secret not in repr(getattr(current, "args", ())), "secret in args"
        formatted = "".join(
            traceback.format_exception(
                type(current), current, current.__traceback__
            )
        )
        assert secret not in formatted, "secret in formatted traceback"
        for attr in ("__cause__", "__context__"):
            chained = getattr(current, attr, None)
            if isinstance(chained, BaseException):
                pending.append(chained)


def assert_source_opinion_audit_invariants(
    rows: Sequence[SourceOpinionAudit],
    *,
    cluster_opinion_count: int | None = None,
    total_new_to_cluster: int | None = None,
    total_reported: int | None = None,
    total_pages: int | None = None,
) -> None:
    """Assert structural and accounting invariants for one cluster's rows.

    The optional totals let callers check the corresponding cluster fetch
    without requiring every test to build a full ``SourceAuditLedger`` first.
    """
    rows = list(rows)
    if cluster_opinion_count is not None:
        assert cluster_opinion_count == len(rows)
    assert [row.opinion_index for row in rows] == list(range(len(rows)))
    assert all(row.opinion_id for row in rows)
    assert len({row.opinion_id for row in rows}) == len(rows)

    for row in rows:
        assert row.candidate_allocation >= 0
        assert row.citing_ids_collected >= 0
        assert row.citing_ids_new_to_cluster >= 0
        assert row.citing_ids_duplicate_across_cluster >= 0
        assert row.citing_ids_collected == (
            row.citing_ids_new_to_cluster
            + row.citing_ids_duplicate_across_cluster
        )
        assert row.citing_ids_collected <= row.candidate_allocation
        assert row.forward_citations_reported >= 0
        assert row.duplicate_rows_ignored >= 0
        assert row.invalid_rows_ignored >= 0
        assert row.pages_fetched >= 0
        assert row.fetch_error_kind in SOURCE_OPINION_FETCH_ERROR_KINDS
        assert isinstance(row.fetch_retryable, bool)
        assert row.candidate_pool_limit_reason in SOURCE_OPINION_LIMIT_REASONS
        assert len(row.gap_codes) == len(set(row.gap_codes))
        assert set(row.gap_codes) <= SOURCE_AUDIT_GAP_CODES
        if row.pages_fetched == 0:
            assert row.reported_count_known is False
            assert row.forward_citations_reported == 0
        if row.reported_count_known:
            assert row.pages_fetched > 0
            assert row.count_mismatch is (
                row.forward_citations_reported != row.citing_ids_collected
            )
        else:
            assert row.count_mismatch is False
        if row.reported_count_malformed:
            assert row.reported_count_known is False
            assert "count_malformed" in row.gap_codes
            assert row.complete is False
        if "count_malformed" in row.gap_codes:
            assert row.reported_count_malformed is True
        if not row.fetch_error_kind:
            assert row.fetch_retryable is False

        if row.candidate_allocation == 0:
            assert row.fetch_attempted is False
            assert row.pages_fetched == 0
            assert row.citing_ids_collected == 0
            assert row.complete is False
            assert row.truncated is True
            assert row.candidate_pool_limit_reached is True
            assert row.candidate_pool_limit_reason == "allocation"
            assert "source_candidate_pool_truncated" in row.gap_codes
            assert row.forward_citations_reported == 0
        else:
            assert row.fetch_attempted is True

        if row.fetch_error_kind:
            assert row.fetch_attempted is True
            assert row.complete is False
            assert "forward_fetch_failed" in row.gap_codes

        if row.page_limit_reached:
            assert row.complete is False
            assert row.truncated is True
            assert "page_cap" in row.gap_codes

        if row.candidate_pool_limit_reached:
            assert row.truncated is True
            assert row.candidate_pool_limit_reason in {
                "row",
                "cursor",
                "allocation",
            }
        if row.candidate_pool_limit_reason in {"row", "cursor"}:
            assert row.candidate_pool_limit_reached is True
            assert "source_candidate_pool_truncated" in row.gap_codes
        if row.candidate_pool_limit_reason == "allocation":
            assert row.candidate_allocation == 0

        if row.count_mismatch:
            assert row.complete is False
            assert "count_mismatch" in row.gap_codes
            if row.forward_citations_reported > row.citing_ids_collected:
                assert row.truncated is True

        if not row.complete:
            assert row.gap_codes
        if row.complete:
            assert row.truncated is False
            assert row.gap_codes == []

    ledger = SourceAuditLedger(opinion_audits=rows)
    expected = _expected_bottlenecks(rows)
    assert ledger.opinion_audit_bottlenecks() == expected

    if total_new_to_cluster is not None:
        assert sum(row.citing_ids_new_to_cluster for row in rows) == (
            total_new_to_cluster
        )
    if total_reported is not None:
        assert sum(row.forward_citations_reported for row in rows) == total_reported
    if total_pages is not None:
        assert sum(row.pages_fetched for row in rows) == total_pages
