from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator


# Stable vocabularies for the public source-invalidator audit. The model keeps
# these as strings for backward-compatible JSON, but producers and tests must
# only emit values from these sets.
SOURCE_INVALIDATOR_REVIEW_STATUSES = frozenset(
    {
        "reviewed",
        "gated",
        "unreviewed",
        "metadata_failed",
        "metadata_unavailable",
        "text_failed",
        "text_empty",
        "target_not_located",
        "out_of_scope",
    }
)
SOURCE_INVALIDATOR_REASON_CODES = frozenset(
    {
        "not_checked",
        "invalidator_accepted",
        "invalidator_authority_lower",
        "invalidator_authority_unknown",
        "invalidator_attribution_ambiguous",
        "invalidator_missing_id",
        "invalidator_gated",
        "invalidator_invalid",
        "invalidator_qualified",
        "invalidator_unverified",
        "invalidator_depth_exceeded",
        "citing_metadata_fetch_failed",
        "citing_metadata_unavailable",
        "citing_text_fetch_failed",
        "citing_text_empty",
        "target_not_located",
        "citing_opinion_cap",
        "post_as_of",
        "undated_as_of",
    }
)
SOURCE_VALIDITY_REASON_CODES = frozenset(
    {
        "not_assessed",
        "source_metadata_unavailable",
        "source_precedence_gate_failed",
        "source_cluster_unresolved",
        "source_cluster_mismatch",
        "source_precedence_defect",
        "source_precedence_unverified",
        "source_no_invalidating_treatment",
        "source_invalidator_accepted_full",
        "source_invalidator_accepted_partial",
        "source_invalidator_rejected",
        "source_invalidator_gated",
        "source_history_incomplete",
        "source_as_of_undated",
    }
)
SOURCE_AUDIT_GAP_CODES = frozenset(
    {
        "source_metadata_unavailable",
        "source_precedence_gate_failed",
        "source_cluster_unresolved",
        "source_cluster_mismatch",
        "source_candidate_pool_truncated",
        "source_as_of_undated",
        "forward_fetch_failed",
        "pagination_cursor_missing",
        "pagination_cursor_repeated",
        "page_cap",
        "count_malformed",
        "count_mismatch",
        "citing_metadata_fetch_failed",
        "citing_metadata_unavailable",
        "citing_text_fetch_failed",
        "citing_text_empty",
        "target_not_located",
        "citing_date_missing",
        "citing_opinion_cap",
    }
)
SOURCE_OPINION_LIMIT_REASONS = frozenset({"", "row", "cursor", "allocation"})
# Bump the logic version when a source-audit change can alter results; bump
# the envelope version when the serialized contract changes.
SOURCE_AUDIT_PROVENANCE_VERSION = 3
SOURCE_AUDIT_LOGIC_VERSION = "1"
SOURCE_AUDIT_ALLOCATION_POLICIES = frozenset({"", "equal", "adaptive"})
SOURCE_AUDIT_CACHE_MODES = frozenset({"", "disabled", "read-through"})


def _fingerprint(payload: dict[str, object]) -> str:
    """Hash a canonical execution-context payload."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# Stable reasons a per-opinion forward-edge fetch can fail. An empty kind means
# the fetch did not fail. Keep these values stable for JSON consumers.
SOURCE_OPINION_FETCH_ERROR_KINDS = frozenset(
    {
        "",
        "rate_limit",
        "authentication",
        "timeout",
        "malformed_response",
        "other",
    }
)


class ExtractionCandidate(BaseModel):
    """A quote-verified extraction proposal that is never authoritative.

    ``quote`` is a verbatim span from the fetched decision and ``value`` is
    derived deterministically from that span. ``status`` stays ``candidate``:
    a candidate never overwrites the deterministic ``CaseRecord`` field and is
    never consumed by ranking, impact, interpretation, or the citator.
    """

    field: str
    quote: str
    value: str = ""
    source: str = "llm_quote"
    status: str = "candidate"
    detail: str = ""


class AppellateHistoryEntry(BaseModel):
    """One verified or unverified step in a decision's appellate history."""

    status: str = ""  # good_law, affirmed, reversed, vacated, overruled, superseded_by_statute, unknown
    case_title: str = ""
    citation: str = ""
    source: str = ""
    verified: bool = False


class CaseAbstract(BaseModel):
    """Structured, non-truncated abstract of a VA decision.

    Unlike the legacy 300-character ``deep_summary`` snippet, every field is
    populated from the full opinion body when available and carries enough
    context for a reviewer to compare a claim to the decision without opening
    the PDF. ``abstract_complete`` is False when only record metadata was
    available (deep-read disabled or fetch failed); callers must not present a
    metadata-only abstract as a full-text summary.
    """

    case_title: str = ""
    court: str = ""
    claimant_and_conditions: str = ""
    procedural_history: str = ""
    issues_presented: str = ""
    holding_and_legal_rule: str = ""
    reasoning_summary: str = ""
    disposition: str = ""
    remand_instructions: str = ""
    source_text_chars: int = 0
    abstract_source: str = "record_metadata"  # full_text or record_metadata
    abstract_complete: bool = False


class ProvisionImpact(BaseModel):
    """One provision-level effect of a decision on VBA adjudication.

    ``provenance_anchor`` is a verbatim sentence from the opinion containing
    the citation. ``provenance_verified`` records only that the source URL is
    an official/primary domain; it is not a claim that the mapping has been
    reviewed by VA or that the decision remains good law.
    """

    citation: str
    authority_type: str = "statute"
    stage: str = "appellate_review"
    legal_effect: str = "clarifying"
    provenance_anchor: str = ""
    m21_1_citations: list[str] = Field(default_factory=list)
    diagnostic_code: str = ""
    staged_rating: bool = False
    medication_effect: bool = False
    source_url: str = ""
    provenance_verified: bool = False
    registry_version: str = "2026.11"


class RvsrGuidance(BaseModel):
    """Role-specific operational playbook for a VA Rating Veterans Service Representative."""

    issue: str = ""
    evidence_review_checklist: list[str] = Field(default_factory=list)
    exam_development_triggers: list[str] = Field(default_factory=list)
    rating_codesheet_actions: list[str] = Field(default_factory=list)
    reasons_for_decision_requirements: list[str] = Field(default_factory=list)
    source_authorities: list[str] = Field(default_factory=list)


class SeniorRaterGuidance(BaseModel):
    """Role-specific operational playbook for a Senior RVSR, DRO, or quality reviewer."""

    issue: str = ""
    hlr_de_novo_focus: list[str] = Field(default_factory=list)
    duty_to_assist_errors: list[str] = Field(default_factory=list)
    cue_analysis: list[str] = Field(default_factory=list)
    precedent_conflict_resolution: list[str] = Field(default_factory=list)
    quality_review_checkpoints: list[str] = Field(default_factory=list)
    source_authorities: list[str] = Field(default_factory=list)


class CaseRecord(BaseModel):
    title: str
    court: str
    citation: str = ""
    url: str = ""
    snippet: str = ""
    decision_date: str = ""
    issue: str = ""
    holding: str = ""
    impact: str = ""
    docket: str = ""
    judge: str = ""
    # CourtListener advisory panel metadata. ``panel`` is a display string of
    # panel member names; it does not establish the author of the majority
    # opinion. Empty when only numeric judge links or no panel data were
    # returned.
    panel: str = ""
    # Public CourtListener docket page, when known. This is a RECAP/PACER
    # reference only: an empty value or a link does not prove that docket
    # documents are available in RECAP, and no docket content is treated as
    # authority or as appellate history.
    recap_docket_url: str = ""
    recap_docket_id: str = ""
    # Public oral-argument recording metadata. Absence is not evidence that no
    # argument occurred; enrichment leaves these empty when no recording is
    # found or the advisory lookup fails.
    oral_argument_url: str = ""
    oral_argument_date: str = ""
    # CourtListener opinion id (from the search result's nested opinion), so
    # deep-read can pull the full text from the opinion detail endpoint
    # instead of the WAF-challenged frontend page.
    courtlistener_opinion_id: str = ""
    deep_summary: str = ""
    # Quote-first LLM proposals for fields the deterministic extractor may
    # have missed or misread. Candidate values are review material only; the
    # authoritative fields above are never replaced by them.
    extraction_candidates: list[ExtractionCandidate] = Field(default_factory=list)
    extraction_flags: list[str] = Field(default_factory=list)
    statutes: list[str] = Field(default_factory=list)
    outcome: str = ""
    # Who appealed: "veteran", "secretary", or "unknown" (empty = not
    # extracted). In VA law, "affirmed" means opposite things depending on
    # the appellant: unfavorable when the veteran appealed, favorable when
    # the Secretary cross-appealed. The contradiction detector and impact
    # note layer use this to make outcome classification party-aware.
    appellant_role: str = ""
    # CAVC Rule 30(a): a single-judge disposition that may not be cited as
    # precedent. Extracted from the decision text, then cross-checked against
    # the court's official Panel Opinions list when that validation is on.
    rule_30a: bool = False
    # Result of the official panel-list check: "panel", "not_listed",
    # "verified_nonprecedential", "conflict", "unavailable", or "" (not a
    # CAVC case / validation did not run).
    cavc_panel_status: str = ""
    # Whether this decision is precedential (published, citable as binding
    # authority). Non-precedential decisions (memorandum dispositions,
    # unpublished orders, single-judge designations) carry a 0.25 composite
    # penalty and are flagged in the report as persuasive rather than
    # binding. ``None`` means the body was unavailable or unreadable, so the
    # status is unknown; callers must neither treat that as binding nor apply
    # the non-precedential penalty.
    precedential: bool | None = None
    # Legal standard of review extracted from the decision text, e.g.
    # "clear error", "de novo", "abuse of discretion", "substantial
    # evidence".  Knowing the standard tells you whether the case is
    # analogous: a "clear error" affirmance is a weaker precedent than a
    # "de novo" reversal for the same statute.
    legal_standard: str = ""
    # How this case treated its cited authorities. Each entry is a
    # {"cited_case": "Smith v. Wilkie", "treatment": "distinguished"}
    # dict. Knowing whether a case follows, distinguishes, overrules, or
    # criticizes prior authority tells you whether that line of precedent
    # is live or dead.
    citation_treatments: list[CitationTreatment] = Field(default_factory=list)
    # When a later decision explicitly overrules or abrogates this one (and
    # the cross-reference is detected in the citation_treatments of the later
    # case), this field carries the title of the superseding decision. The
    # contradiction detector skips superseded pairs and the report flags them
    # as "superseded authority". Empty string means no superseding decision was
    # detected in the retrieved corpus; it is not a finality finding.
    superseded_by: str = ""
    authority_rank: int = 0
    authority_weight: int = 0
    relevance_score: int = 0
    composite_score: float = 0.0
    ranking_explanation: str = ""
    source_reliability: str = ""
    # Structured full-text abstract and provision-level VBA impact. Both are
    # populated by the deep-read/impact layers and preserved by the surfaced
    # case contract instead of being flattened into display strings.
    case_abstract: CaseAbstract | None = None
    provision_impacts: list[ProvisionImpact] = Field(default_factory=list)


class CitationTreatment(BaseModel):
    """How this case treated a cited authority: follow, distinguish, overrule, etc."""

    cited_case: str
    treatment: str


class ClaimElement(BaseModel):
    """A legal element detected in the claim issue, with claimant guidance."""

    name: str
    description: str
    guidance: str
    # Analytical weight for the coverage-score denominator. Higher-weight
    # elements (nexus, service connection) matter more when calculating how
    # well the retrieved authorities cover the claim.
    weight: float = 0.1
    covered_by: list[str] = Field(default_factory=list)


class PrincipleFinding(BaseModel):
    """A legal principle found in the retrieved cases, with attribution."""

    principle: str
    source_cases: list[str] = Field(default_factory=list)


class Contradiction(BaseModel):
    """A conflict between two retrieved authorities on the same point.

    Raised by the always-on deterministic detector (opposite outcomes on a
    shared statute) and/or the LLM reasoning pass when two decisions disagree
    on a holding or outcome, so the report can surface the tension rather than
    silently picking one side.
    """

    statement: str
    case_a: str
    case_b: str


class StatuteOutcomeRow(BaseModel):
    """One cell in the statute × court × outcome matrix."""

    statute: str
    court: str
    favorable: int = 0
    unfavorable: int = 0
    unknown: int = 0


class GovInfoPublicLaw(BaseModel):
    """A GovInfo Public Law record linked to a U.S. Code check.

    ``details_url`` and ``text_url`` are stable GovInfo publication URLs, not
    API-key-bearing search links. ``text_excerpt`` is bounded; the full text is
    available at ``text_url``.
    """

    package_id: str
    citation: str = ""
    title: str = ""
    date_issued: str = ""
    details_url: str = ""
    text_url: str = ""
    text_excerpt: str = ""
    text_chars: int = 0
    # "retrieved", "unavailable", "not_available", or "not_retrieved".
    text_status: str = "not_retrieved"
    detail: str = ""


class GovInfoStatute(BaseModel):
    """A GovInfo Statutes at Large record linked to a U.S. Code check."""

    package_id: str
    granule_id: str = ""
    citation: str = ""
    title: str = ""
    date_issued: str = ""
    details_url: str = ""
    text_url: str = ""
    text_excerpt: str = ""
    text_chars: int = 0
    # "retrieved", "unavailable", "not_available", or "not_retrieved".
    text_status: str = "not_retrieved"
    detail: str = ""


class AuthorityCheck(BaseModel):
    """One external verification result for a cited authority."""

    authority: str
    source: str
    # "verified", "conflict", "unresolved", "no_recent_documents", or
    # "unavailable".
    status: str
    detail: str = ""
    url: str = ""
    # Optional GovInfo rulemaking records attached to a verified U.S. Code
    # check. Empty unless GOVINFO_LEGISLATION_RECORDS is enabled.
    public_laws: list[GovInfoPublicLaw] = Field(default_factory=list)
    statutes_at_large: list[GovInfoStatute] = Field(default_factory=list)
    # "retrieved", "partial", "not_found", or "unavailable". Empty when the
    # optional legislative-record lookup did not run.
    records_status: str = ""
    records_detail: str = ""


class StatutorySupersessionSignal(BaseModel):
    """A separate advisory signal about a cited statute or regulation.

    This is deliberately not ``CaseRecord.superseded_by`` and is never used
    to set citator status. ``candidate`` means the source check found a
    possible repeal/removal/renumbering/supersession; it is not a confirmed
    legal conclusion. ``no_signal`` means the checked source contained the
    current provision or no explicit supersession action, not that the
    provision has never been amended.
    """

    authority: str
    # "regulation" or "statute".
    authority_type: str
    source: str
    # "candidate", "no_signal", "unavailable", or "not_assessed".
    status: str = "not_assessed"
    # e.g. repealed, removed, reserved, renumbered, redesignated, superseded,
    # or not_in_current_source. Empty for no_signal/unavailable/not_assessed.
    signal: str = ""
    detail: str = ""
    url: str = ""
    effective_date: str = ""
    # Only populated when the source explicitly names a replacement.
    replacement: str = ""


class CitatorCoverageGap(BaseModel):
    """One specific reason the citator's coverage is incomplete.

    ``code`` is stable for machine checks; ``detail`` is the human-readable
    explanation, including opinion ids or counts where available.
    """

    code: str
    detail: str


class CitatorCoverage(BaseModel):
    """What the citator actually reviewed for one target case.

    ``complete`` is deliberately conservative: it is true only when every
    forward-citation page the API reported was fetched and every citing
    opinion in that set was successfully read. With an as-of cutoff, the set
    is limited to citing opinions dated on or before the cutoff; later
    opinions are reported separately. An opinion with no usable date cannot
    be placed relative to the cutoff, so it is excluded and makes the as-of
    review incomplete. The count fields let a consumer distinguish a complete
    review from a partially checked one. ``gaps`` enumerates the specific
    missing pieces rather than leaving the consumer to parse flags.
    ``temporal_confidence`` describes how far the latest reviewed citing date
    can bound the review: ``high`` means a complete review with dates for
    every reviewed opinion, ``medium`` means a known date from an incomplete
    review, ``low`` means at least one reviewed or excluded opinion has no
    date, and ``none`` means no usable reviewed date exists.
    """

    forward_citations_found: int = 0
    citing_opinions_reviewed: int = 0
    pages_fetched: int = 0
    complete: bool = False
    truncated: bool = False
    latest_citing_date: str = ""
    gaps: list[CitatorCoverageGap] = Field(default_factory=list)
    temporal_confidence: str = "none"
    temporal_confidence_reason: str = ""


class NegativeTreatment(BaseModel):
    """One target-specific negative-treatment statement in a citing opinion."""

    citing_case: str
    citing_citation: str = ""
    citing_court: str = ""
    citing_date: str = ""
    citing_url: str = ""
    # Stable CourtListener identifiers for the source (citing) opinion. They
    # are extracted from the metadata already used by the citator and are
    # advisory provenance only; they never participate in status or binding.
    source_opinion_id: str = ""
    source_cluster_id: str = ""
    # Canonical term: overruled, abrogated, superseded, questioned,
    # criticized, limited, or declined to follow.
    treatment: str
    # "all", "part", or "unclear". "part" is used when the sentence says
    # "in part"; the other non-overruling terms are unclear by default.
    scope: str = "unclear"
    # None means the relative authority of the two courts could not be
    # established. It must not be read as "not binding".
    binding: bool | None = None
    confidence: str = "high"
    # CourtListener opinion-type code and its normalized role. Only a
    # majority/lead/combined opinion can produce an authoritative finding.
    opinion_type: str = ""
    opinion_role: str = "unknown"
    # CourtListener cluster status and its normalized tier.
    precedential_status: str = ""
    precedential_tier: str = "unknown"
    # True only for a majority-role, Published opinion. Gated findings are
    # kept in CitatorCheck.gated_treatment_findings, never in this list.
    authoritative: bool = True
    gate_reason: str = ""
    # How the target reference was bound in the sentence: "citation" for an
    # explicit reporter citation, "case_name" for a bare full name, or
    # "short_name" for a conservative short-form alias. Bare and short-name
    # matches are retained but flagged because they are weaker attribution.
    attribution_basis: str = "citation"
    # True when the accepted finding needs human attribution review, for
    # example a bare-name match or a passive verb with no explicit actor.
    # This never changes whether the finding counts toward ``status``.
    attribution_ambiguous: bool = False
    attribution_reason: str = ""
    # Verbatim sentence from the citing opinion. It is intentionally not
    # whitespace-normalized so it can be checked against the source text.
    excerpt: str


class SameDocketDecision(BaseModel):
    """Another decision cluster on the target's docket.

    This is an intra-court advisory signal only. It is not a cross-court
    appellate-history link and carries no treatment conclusion.
    """

    case_name: str
    citation: str = ""
    court: str = ""
    decision_date: str = ""
    url: str = ""
    cluster_id: str = ""


class LaterCitingOpinion(BaseModel):
    """A reviewed opinion that explicitly cites the target.

    The list is kept separate from ``negative_treatments`` and from the
    ``good_law`` status. A later citation alone does not establish appellate
    history, finality, or treatment.
    """

    case_name: str
    citation: str = ""
    court: str = ""
    decision_date: str = ""
    url: str = ""
    opinion_id: str = ""
    cluster_id: str = ""
    opinion_type: str = ""
    opinion_role: str = "unknown"
    precedential_status: str = ""
    precedential_tier: str = "unknown"
    # False when the opinion cannot supply an authoritative treatment finding
    # (separate opinion, nonprecedential, or unknown status).
    treatment_eligible: bool = False
    gate_reason: str = ""


class TreatmentTimelineEntry(BaseModel):
    """One chronological negative or reaffirmance signal.

    The timeline is explanatory only. It never changes ``status``,
    ``good_law_as_of``, coverage, ranking, or ``superseded_by``.
    """

    signal: str  # "negative" or "reaffirmed"
    treatment: str
    citing_case: str
    excerpt: str
    scope: str = "unclear"
    citing_citation: str = ""
    citing_court: str = ""
    citing_date: str = ""
    citing_url: str = ""
    # Source identifiers copied from the originating finding for audit.
    source_opinion_id: str = ""
    source_cluster_id: str = ""
    # None means the relative authority could not be established; it is not
    # equivalent to persuasive.
    binding: bool | None = None
    binding_class: str = "unknown"  # "binding", "persuasive", or "unknown"
    authoritative: bool = False
    opinion_type: str = ""
    opinion_role: str = "unknown"
    precedential_status: str = ""
    precedential_tier: str = "unknown"
    gate_reason: str = ""
    # True for a reaffirmance dated after the latest authoritative negative.
    later_than_negative: bool = False


class SourceInvalidatorCheck(BaseModel):
    """One source-history candidate and its invalidator audit outcome.

    ``source_opinion_id`` and the citing fields identify the later opinion
    that could invalidate the source. A candidate that could not be read or
    was excluded by the as-of cutoff has no ``treatment``; ``review_status``
    says which stage it reached, and ``reason_code`` is stable for machine
    consumers. A reviewed finding is accepted only when it binds the source,
    has unambiguous attribution, and the decision's immediate bounded status
    is valid. ``invalidator_status`` is ``not_checked`` for candidates that
    never reached that audit; ``accepted`` is true only for a valid, complete
    invalidator history.
    """

    source_opinion_id: str = ""
    source_cluster_id: str = ""
    citing_case: str = ""
    citing_citation: str = ""
    citing_court: str = ""
    citing_date: str = ""
    citing_url: str = ""
    # reviewed, gated, unreviewed, metadata_failed, metadata_unavailable,
    # text_failed, text_empty, target_not_located, or out_of_scope.
    review_status: str = "reviewed"
    # Only meaningful for a reviewed/gated finding.
    treatment: str = ""
    scope: str = ""
    # None when no finding was obtained (for example, a read failure).
    authoritative: bool | None = None
    gate_reason: str = ""
    binding: bool | None = None
    # not_checked before the audit, or the invalidator's own bounded source
    # status: valid, invalid, qualified, or unverified.
    invalidator_status: str = "not_checked"
    accepted: bool = False
    # Stable reason code; human-readable detail remains in ``reason``.
    reason_code: str = "not_checked"
    reason: str = ""


class SourceOpinionAudit(BaseModel):
    """Coverage accounting for one opinion in a source cluster.

    Rows are ordered like the source cluster's opinion ids. A row describes
    only the forward-edge fetch for that opinion; candidate metadata, review,
    and as-of accounting remain aggregate fields on ``SourceAuditLedger``.
    """

    opinion_id: str
    opinion_index: int = 0
    candidate_allocation: int = 0
    fetch_attempted: bool = False
    reported_count_known: bool = False
    # True when the API count was present but not a non-negative integer.
    reported_count_malformed: bool = False
    # Raw reported count when valid. It can be lower than the number of IDs
    # collected when the provider underreports its own count.
    forward_citations_reported: int = 0
    citing_ids_collected: int = 0
    # IDs contributed to the cluster for the first time by this opinion.
    citing_ids_new_to_cluster: int = 0
    # IDs already collected from an earlier opinion in the same cluster.
    citing_ids_duplicate_across_cluster: int = 0
    # Duplicate usable rows and unusable rows within this opinion's pages.
    duplicate_rows_ignored: int = 0
    invalid_rows_ignored: int = 0
    pages_fetched: int = 0
    complete: bool = False
    truncated: bool = False
    page_limit_reached: bool = False
    candidate_pool_limit_reached: bool = False
    # row, cursor, allocation, or empty.
    candidate_pool_limit_reason: str = ""
    count_mismatch: bool = False
    # Empty unless the fetch itself failed. Values come from
    # SOURCE_OPINION_FETCH_ERROR_KINDS; details stay in ledger.gaps.
    fetch_error_kind: str = ""
    # Whether retrying the same opinion fetch could reasonably succeed.
    fetch_retryable: bool = False
    # Stable row-level incompleteness codes; details stay in ledger.gaps.
    gap_codes: list[str] = Field(default_factory=list)

    @field_validator("candidate_pool_limit_reason")
    @classmethod
    def _known_limit_reason(cls, value: str) -> str:
        if value not in SOURCE_OPINION_LIMIT_REASONS:
            raise ValueError(
                "candidate_pool_limit_reason must be one of "
                f"{sorted(SOURCE_OPINION_LIMIT_REASONS)!r}"
            )
        return value

    @field_validator("fetch_error_kind")
    @classmethod
    def _known_fetch_error_kind(cls, value: str) -> str:
        if value not in SOURCE_OPINION_FETCH_ERROR_KINDS:
            raise ValueError(
                "fetch_error_kind must be one of "
                f"{sorted(SOURCE_OPINION_FETCH_ERROR_KINDS)!r}"
            )
        return value


class SourceAuditProvenance(BaseModel):
    """Versioned execution envelope for one source-history audit.

    The envelope records the exact source-history bounds and execution mode so
    two ledgers can be compared without guessing which caps or cache policy
    produced them. ``provider_version`` is the provider API version, not the
    application package version. ``audit_logic_version`` identifies the
    source-audit implementation. ``execution_fingerprint`` covers the
    timestamp-free execution context and code version; ``configuration_fingerprint``
    excludes source identity so reports can group multi-source audits by
    configuration. ``response_digest`` separately identifies the unique raw
    provider payloads the audit consumed.
    """

    version: int = SOURCE_AUDIT_PROVENANCE_VERSION
    source_opinion_id: str = ""
    source_cluster_id: str = ""
    provider: str = ""
    provider_version: str = ""
    provider_base_url: str = ""
    # Empty means the audit-code version is unknown (legacy envelopes).
    audit_logic_version: str = ""
    generated_at: str = ""
    max_pages: int = 0
    page_size: int = 0
    max_ids: int = 0
    max_citing_opinions: int = 0
    max_invalidator_depth: int = 0
    allocation_policy: str = ""
    cache_mode: str = ""
    cache_version: int = 0
    cache_ttl_seconds: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_entry_age_seconds: int = 0
    response_digest: str = ""

    @property
    def is_recorded(self) -> bool:
        """Whether this envelope carries more than a version placeholder."""
        return any(
            (
                self.source_opinion_id,
                self.source_cluster_id,
                self.provider,
                self.provider_version,
                self.provider_base_url,
                self.audit_logic_version,
                self.generated_at,
                self.max_pages,
                self.page_size,
                self.max_ids,
                self.max_citing_opinions,
                self.max_invalidator_depth,
                self.allocation_policy,
                self.cache_mode,
                self.cache_version,
                self.cache_ttl_seconds,
                self.cache_hits,
                self.cache_misses,
                self.cache_entry_age_seconds,
                self.response_digest,
            )
        )

    @computed_field
    @property
    def execution_fingerprint(self) -> str:
        """Stable SHA-256 over the execution context, excluding time/data."""
        payload = {
            "version": self.version,
            "source_opinion_id": self.source_opinion_id,
            "source_cluster_id": self.source_cluster_id,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "provider_base_url": self.provider_base_url,
            "audit_logic_version": self.audit_logic_version,
            "max_pages": self.max_pages,
            "page_size": self.page_size,
            "max_ids": self.max_ids,
            "max_citing_opinions": self.max_citing_opinions,
            "max_invalidator_depth": self.max_invalidator_depth,
            "allocation_policy": self.allocation_policy,
            "cache_mode": self.cache_mode,
            "cache_version": self.cache_version,
            "cache_ttl_seconds": self.cache_ttl_seconds,
        }
        return _fingerprint(payload)

    @computed_field
    @property
    def configuration_fingerprint(self) -> str:
        """Stable SHA-256 over code/config, excluding source identity/time/data."""
        payload = {
            "version": self.version,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "provider_base_url": self.provider_base_url,
            "audit_logic_version": self.audit_logic_version,
            "max_pages": self.max_pages,
            "page_size": self.page_size,
            "max_ids": self.max_ids,
            "max_citing_opinions": self.max_citing_opinions,
            "max_invalidator_depth": self.max_invalidator_depth,
            "allocation_policy": self.allocation_policy,
            "cache_mode": self.cache_mode,
            "cache_version": self.cache_version,
            "cache_ttl_seconds": self.cache_ttl_seconds,
        }
        return _fingerprint(payload)

    @property
    def comparability_key(self) -> tuple[int, str, str]:
        """Group key for envelope schema, audit logic, and execution context."""
        return (
            self.version,
            self.audit_logic_version,
            self.execution_fingerprint,
        )

    @property
    def configuration_key(self) -> tuple[int, str, str]:
        """Group key for schema, code, and configuration across source IDs."""
        return (
            self.version,
            self.audit_logic_version,
            self.configuration_fingerprint,
        )

    def is_comparable_to(
        self, other: SourceAuditProvenance | None
    ) -> bool:
        """Whether two recorded envelopes share source, configuration, and code."""
        return bool(
            other is not None
            and self.is_recorded
            and other.is_recorded
            and self.audit_logic_version
            and other.audit_logic_version
            and self.comparability_key == other.comparability_key
        )

    def is_configuration_comparable_to(
        self, other: SourceAuditProvenance | None
    ) -> bool:
        """Whether config and code match even when source identities differ."""
        return bool(
            other is not None
            and self.is_recorded
            and other.is_recorded
            and self.audit_logic_version
            and other.audit_logic_version
            and self.configuration_key == other.configuration_key
        )

    @field_validator("allocation_policy")
    @classmethod
    def _known_allocation_policy(cls, value: str) -> str:
        if value not in SOURCE_AUDIT_ALLOCATION_POLICIES:
            raise ValueError(
                "allocation_policy must be one of "
                f"{sorted(SOURCE_AUDIT_ALLOCATION_POLICIES)!r}"
            )
        return value

    @field_validator("cache_mode")
    @classmethod
    def _known_cache_mode(cls, value: str) -> str:
        if value not in SOURCE_AUDIT_CACHE_MODES:
            raise ValueError(
                "cache_mode must be one of "
                f"{sorted(SOURCE_AUDIT_CACHE_MODES)!r}"
            )
        return value


class SourceAuditLedger(BaseModel):
    """Bounded coverage accounting for one source-history audit.

    The ledger describes what the audit could see, not whether the source is
    valid. ``provenance`` records the versioned execution envelope (identity,
    exact bounds, allocation policy, provider version/base URL, audit-logic
    version, cache mode/version/TTL, observed cache hit/miss/age and response
    digest, and timestamp) for reproducibility. ``is_comparable_to`` groups
    ledgers that used the same source and execution context; the
    configuration-only comparison ignores source identity. It is ``None`` for
    legacy ledgers whose envelope was never recorded, including old
    placeholders that serialized only the default version;
    ``provenance_recorded`` makes that distinction explicit in serialized
    output. ``coverage_complete`` means the bounded
    review finished; a reviewed
    but rejected or gated candidate can still have complete coverage while the
    signal's ``history_complete`` remains false. ``forward_citations_reported``
    is the sum of valid per-opinion API counts and may double-count a citing
    opinion linked to more than one cluster opinion; malformed counts are
    flagged separately, and an underreported count can be lower than the
    de-duplicated ids actually collected within the bounded pool. The optional
    ``opinion_audits`` rows retain that breakdown per source opinion.
    """

    assessed: bool = False
    history_attempted: bool = False
    provenance: SourceAuditProvenance | None = None
    as_of_date: str = ""
    coverage_complete: bool = False
    cluster_opinion_count: int = 0
    # Raw sum of API counts across the source cluster's opinions. With multiple
    # opinions, the same citing opinion can be reported more than once.
    forward_citations_reported: int = 0
    # Unique citing ids actually collected within the bounded candidate pool.
    citing_ids_collected: int = 0
    candidates_metadata_loaded: int = 0
    candidates_in_scope: int = 0
    candidates_reviewed: int = 0
    candidates_without_invalidating_finding: int = 0
    candidates_with_invalidating_finding: int = 0
    candidates_gated: int = 0
    candidates_unreviewed: int = 0
    candidates_post_as_of: int = 0
    candidates_undated_as_of: int = 0
    metadata_fetch_failures: int = 0
    metadata_unavailable: int = 0
    text_fetch_failures: int = 0
    text_empty: int = 0
    target_not_located: int = 0
    pages_fetched: int = 0
    page_limit_reached: bool = False
    candidate_pool_limit_reached: bool = False
    # Stable id of the accepted candidate that set invalid/qualified status.
    decisive_candidate_id: str = ""

    @field_validator("provenance", mode="before")
    @classmethod
    def _migrate_legacy_provenance(cls, value: object) -> object:
        """Treat missing or empty placeholder envelopes as unrecorded."""
        if value is None:
            return None
        if isinstance(value, dict):
            value = SourceAuditProvenance.model_validate(value)
        if isinstance(value, SourceAuditProvenance) and not value.is_recorded:
            return None
        return value
    # Concrete incompleteness reasons; deduplicated in capture order.
    gaps: list[CitatorCoverageGap] = Field(default_factory=list)
    # One row per source-cluster opinion, in cluster opinion-id order.
    opinion_audits: list[SourceOpinionAudit] = Field(default_factory=list)

    @computed_field
    @property
    def provenance_recorded(self) -> bool:
        """Whether this ledger carries a recorded provenance envelope."""
        return self.provenance is not None

    def is_comparable_to(self, other: SourceAuditLedger | None) -> bool:
        """Whether two ledgers used the same source and execution context."""
        if (
            other is None
            or self.provenance is None
            or other.provenance is None
        ):
            return False
        return self.provenance.is_comparable_to(other.provenance)

    @computed_field
    @property
    def opinion_bottlenecks(self) -> dict[str, list[str]]:
        """Derived summary serialized for JSON consumers; never stored."""
        return self.opinion_audit_bottlenecks()

    def opinion_audit_bottlenecks(self) -> dict[str, list[str]]:
        """Return constrained opinion IDs mapped to derived reasons.

        Rows are returned in source-cluster order. Gap codes, zero allocation,
        row/cursor caps, unknown reported counts, and malformed rows are
        highlighted. Duplicate-only rows remain data-quality notices rather
        than coverage bottlenecks.

        Raises:
            ValueError: If a row has an empty opinion id or two rows share one.
        """
        bottlenecks: dict[str, list[str]] = {}
        seen_opinion_ids: set[str] = set()
        for row in self.opinion_audits:
            if not row.opinion_id:
                raise ValueError(
                    "opinion_audits rows must have a non-empty opinion_id"
                )
            if row.opinion_id in seen_opinion_ids:
                raise ValueError(
                    "duplicate opinion_id in opinion_audits: "
                    f"{row.opinion_id!r}"
                )
            seen_opinion_ids.add(row.opinion_id)
            reasons = _opinion_bottleneck_reasons(row)
            if reasons:
                bottlenecks[row.opinion_id] = reasons
        return bottlenecks


def _opinion_bottleneck_reasons(row: SourceOpinionAudit) -> list[str]:
    """Return the minimum coverage reasons for one opinion audit row."""
    reasons = list(row.gap_codes)
    if row.candidate_allocation == 0:
        reasons.append("zero_allocation")
    elif row.candidate_pool_limit_reason == "row":
        reasons.append("candidate_pool_row")
    elif row.candidate_pool_limit_reason == "cursor":
        reasons.append("candidate_pool_cursor")
    if (
        row.pages_fetched > 0
        and not row.reported_count_known
        and not row.reported_count_malformed
    ):
        reasons.append("reported_count_unknown")
    if row.invalid_rows_ignored > 0:
        reasons.append("malformed_rows")
    return list(dict.fromkeys(reasons))


class SourceValiditySignal(BaseModel):
    """Advisory validation of one reviewed citing opinion.

    The source opinion's stable CourtListener identifiers are retained so the
    advisory validator can check whether the source still exists in the form
    the finding assumed. ``status`` is ``not_assessed`` until validation runs;
    advisory validation never changes citator status, coverage, ranking, or
    authority-queue order. In ``enforce`` mode these signals can additionally
    withhold support from status and queue demotion. A known lower-court
    source is retained with ``precedence="persuasive"`` and is always withheld
    from enforcement; when its history is otherwise clean its status is
    ``precedence_defect``. Only an authoritative source may be used by
    enforcement. When an as-of cutoff is set, source-history review stops at
    that cutoff. ``invalidator_checks`` retains every source-history candidate
    relevant to invalidation, including gated, unreviewed, failed, and
    as-of-excluded candidates. Reviewed candidates with no invalidating
    treatment are intentionally omitted. ``audit_ledger`` describes bounded
    execution separately: its ``coverage_complete`` may be true for a rejected
    or gated review while ``history_complete`` remains false because the
    source's invalidation status was not established.
    """

    source_opinion_id: str
    source_cluster_id: str = ""
    # not_assessed, valid, invalid, qualified, precedence_defect, or unverified.
    status: str = "not_assessed"
    # Human-readable reason for the status; advisory only.
    reason: str = ""
    # Stable machine-readable companion to ``status``.
    reason_code: str = "not_assessed"
    # authoritative, persuasive, nonprecedential, separate, or unverified.
    precedence: str = ""
    # True when this signal must be withheld from enforcement. This includes
    # known lower-court sources even when their history is invalid, plus any
    # nonprecedential, separate, or unverified source. It also defaults to true
    # before assessment, so an extract-mode signal is never enforceable.
    # Advisory output still shows the signal; only an authoritative, validly
    # assessed source is enforceable.
    withheld: bool = True
    # True only when the bounded one-hop source history review completed.
    # With an as-of cutoff, this covers only history on or before the cutoff.
    history_complete: bool = False
    # Each source-history candidate relevant to invalidation, including
    # gated, unreviewed, failed, and as-of-excluded candidates.
    invalidator_checks: list[SourceInvalidatorCheck] = Field(default_factory=list)
    # Bounded coverage accounting for the source audit.
    audit_ledger: SourceAuditLedger = Field(default_factory=SourceAuditLedger)
    checked_at: str = ""


class AppellateHistoryCandidate(BaseModel):
    """A history signal retained for review, never a history label by itself."""

    signal_type: str = "same_docket"
    signals: list[str] = Field(default_factory=list)
    candidate_case: str
    citation: str = ""
    court: str = ""
    decision_date: str = ""
    url: str = ""
    cluster_id: str = ""
    opinion_id: str = ""
    match_reasons: list[str] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)
    # candidate, verified, rejected, or insufficient_source.
    verification_status: str = "candidate"
    relationship: str = "unknown"
    disposition: str = "unknown"
    scope: str = "unknown"
    source_quality: str = "unknown"
    source_url: str = ""
    reviewer: str = ""
    reviewed_at: str = ""
    rationale: str = ""


class AppellateHistoryRecord(BaseModel):
    """A human verification record for one candidate relationship.

    ``review_status`` is ``confirmed`` only when the reviewer checked the
    relationship, disposition, scope, and an official or licensed source.
    ``rejected`` records are retained for audit but never become history labels.
    """

    target_case: str = ""
    target_cluster_id: str = ""
    target_citation: str = ""
    candidate_case: str
    candidate_cluster_id: str = ""
    candidate_citation: str = ""
    relationship: str
    disposition: str
    scope: str
    source_quality: str
    source_url: str
    reviewer: str
    reviewed_at: str
    rationale: str
    review_status: str = "confirmed"
    coverage_notes: list[str] = Field(default_factory=list)


class AppellateHistoryCheck(BaseModel):
    """Candidate and verified-history state for one target case.

    ``status`` is ``verified`` only when at least one confirmed human record
    exists. With candidates but no verified record it is
    ``insufficient_source``; with no candidates it is ``not_assessed``. It is
    never ``no_history``: absence of a candidate is not proof that no appeal
    exists.
    """

    target_case: str
    target_cluster_id: str = ""
    target_citations: list[str] = Field(default_factory=list)
    status: str = "not_assessed"
    candidates: list[AppellateHistoryCandidate] = Field(default_factory=list)
    verified_history: list[AppellateHistoryRecord] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    source: str = ""
    history_meaning: str = (
        "verified_history contains only human-confirmed records tied to an "
        "official or licensed source; candidates are review signals, not "
        "history findings."
    )


class AuthorityQueueEntry(BaseModel):
    """One selected target in the prioritized authority queue.

    The queue orders cases for advisory review; it does not change their
    ranking, ``superseded_by``, or the citator's status rules. ``base_priority``
    preserves the pre-citator order so a later annotation pass can show whether
    and why an authority moved.
    """

    priority: int = 0
    base_priority: int = 0
    case_title: str
    citation: str = ""
    court: str = ""
    authority_weight: int = 0
    authority_rank: int = 0
    relevance_score: int = 0
    composite_score: float = 0.0
    precedential: bool | None = None
    superseded_by: str = ""
    eligible: bool = True
    reasons: list[str] = Field(default_factory=list)
    # One-way annotations from the citator review. Authoritative negative
    # treatment demotes the entry; gated findings or review-required coverage
    # issues flag it. These fields never feed case ranking or citator status.
    citator_status: str = ""
    citator_demoted: bool = False
    citator_flagged: bool = False
    citator_reasons: list[str] = Field(default_factory=list)
    citator_findings: list[NegativeTreatment] = Field(default_factory=list)


class CitatorCheck(BaseModel):
    """Advisory citator result for one queued authority.

    ``good_law`` means only that no authoritative negative treatment was found
    in the reviewed citing opinions. It does not mean the case is final, has no
    subsequent history, or remains good law. ``good_law_as_of`` is populated
    only for a clean, complete review of that forward-citation set. When
    ``as_of_date`` is set, only citing opinions dated on or before that
    inclusive cutoff affect the status and coverage; later opinions and their
    findings are reported separately and never change the as-of result. An
    authoritative negative finding makes the status ``negative``; a finding
    from a separate or nonprecedential opinion is gated and makes it
    ``unknown``. Missing or truncated coverage also makes it ``unknown``. The
    citator never changes ranking, ``superseded_by``, or contradiction results.
    Confidence and review-required fields are advisory: they explain how much
    trust to place in the review without altering the conservative status rule.
    When opt-in source-validity enforcement runs, it may downgrade an
    unsupported ``negative``/``good_law`` status to ``unknown`` and record its
    actions; it never deletes findings and never upgrades status. Raw treatment
    lists remain available for audit. Blocked sources are omitted from the
    derived binding/persuasive/timeline views but retained as low-priority,
    withheld items in the adjudication queue.
    """

    target_case: str
    target_citation: str = ""
    target_court: str = ""
    target_decision_date: str = ""
    target_opinion_id: str = ""
    target_url: str = ""
    # A case is a CourtListener cluster, not one opinion. These fields expose
    # every opinion in that cluster and every parallel reporter citation so
    # callers can see what was actually resolved and checked.
    target_cluster_id: str = ""
    target_citations: list[str] = Field(default_factory=list)
    target_opinion_ids: list[str] = Field(default_factory=list)
    # Opt-in diagnostic rows for the target cluster's forward-edge fetch.
    # Empty unless target cluster auditing is enabled. These rows reuse the
    # source-audit row shape but never feed status, coverage, confidence,
    # ranking, or authority-queue behavior.
    target_cluster_audit: list[SourceOpinionAudit] = Field(default_factory=list)
    # "negative", "good_law", or "unknown".
    status: str = "unknown"
    # Latest reviewed citing-opinion date, not a finality or history date.
    good_law_as_of: str = ""
    # Optional inclusive cutoff requested by the caller. When set, only
    # citing opinions dated on or before this date can affect ``status``,
    # ``good_law_as_of``, or as-of coverage. An empty value means no cutoff.
    as_of_date: str = ""
    # Stable scope statement carried in JSON/CSV so consumers cannot read
    # ``good_law`` as a finality conclusion.
    good_law_meaning: str = (
        "good_law means only that no authoritative negative treatment was "
        "found in the reviewed citing opinions; it does not assess subsequent "
        "history or finality."
    )
    # False unless a validated human-verified appellate-history record matched.
    # The citator itself never performs a cross-court history lookup.
    subsequent_history_assessed: bool = False
    # Penalty-based confidence in the completeness and attribution of the
    # review. ``confidence`` is ``high``, ``medium``, ``low``, or ``none``;
    # ``confidence_score`` is the corresponding 0..1 score. The score does not
    # feed status or good_law_as_of.
    confidence: str = "none"
    confidence_score: float = 0.0
    confidence_reason: str = ""
    # True when a human should review the check because coverage is incomplete,
    # metadata failed, or an accepted finding has ambiguous attribution.
    review_required: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    # Position and reasons from the prioritized authority queue. These are
    # review-order metadata only and never feed the status calculation.
    authority_priority: int = 0
    authority_reasons: list[str] = Field(default_factory=list)
    checked_at: str = ""
    coverage: CitatorCoverage = Field(default_factory=CitatorCoverage)
    # Candidate and human-verified history for this target. Candidates never
    # change ``status``, ranking, or ``superseded_by``; only a validated
    # reviewer record appears in ``verified_history``.
    appellate_history: AppellateHistoryCheck | None = None
    # Separate advisory signals. Neither is used to derive ``status``.
    same_docket_decisions: list[SameDocketDecision] = Field(default_factory=list)
    later_citing_opinions: list[LaterCitingOpinion] = Field(default_factory=list)
    # Opt-in source-validity signals (``CITATOR_SOURCE_VALIDITY``). Empty when
    # the feature is off. Extract/advisory entries are evidence only. In
    # ``enforce`` mode, withheld or invalid/qualified sources stop supporting
    # status and queue demotion, but the entries and findings remain visible;
    # the adjudication queue marks blocked findings as withheld for audit.
    source_validity: list[SourceValiditySignal] = Field(default_factory=list)
    # True when the opt-in enforcement pass ran. Advisory and extract modes
    # leave this false. Enforcement may change status and queue annotations but
    # never deletes the underlying advisory findings.
    source_validity_enforced: bool = False
    # Human-readable audit trail for enforcement decisions, one entry per
    # withheld or invalid/qualified source. Entries are deduplicated so the
    # pass is idempotent.
    source_validity_actions: list[str] = Field(default_factory=list)
    # As-of-only review material. Opinions dated after the cutoff are reviewed
    # for treatment but never change the as-of status or coverage. Findings
    # include authoritative and gated signals, identified by ``authoritative``.
    post_as_of_citing_opinions: list[LaterCitingOpinion] = Field(
        default_factory=list
    )
    post_as_of_treatment_findings: list[NegativeTreatment] = Field(
        default_factory=list
    )
    # Opinions excluded from an as-of review because their decision date is
    # missing or unusable. They could fall on either side of the cutoff, so
    # their treatment is not used and as-of coverage is incomplete.
    undated_citing_opinions: list[LaterCitingOpinion] = Field(
        default_factory=list
    )
    # True when the separate post-cutoff review is incomplete: its opinion cap
    # was reached, a review failed or was empty, or a target was not located.
    # This never makes the as-of review incomplete.
    post_as_of_incomplete: bool = False
    post_as_of_flags: list[str] = Field(default_factory=list)
    # Only majority-role, Published findings. This is the list that can make
    # the status ``negative``.
    negative_treatments: list[NegativeTreatment] = Field(default_factory=list)
    # Findings from separate, nonprecedential, or unclassified opinions. They
    # are retained for review but never counted toward ``negative`` or
    # ``good_law``.
    gated_treatment_findings: list[NegativeTreatment] = Field(default_factory=list)
    # Authoritative negatives split by the citing court's relationship to the
    # target. ``negative_treatments`` remains the raw union for audit; these
    # derived splits omit sources blocked by enforcement. Unknown binding is
    # intentionally left out of both splits.
    binding_negative_treatments: list[NegativeTreatment] = Field(
        default_factory=list
    )
    persuasive_negative_treatments: list[NegativeTreatment] = Field(
        default_factory=list
    )
    # Chronological negative and reaffirmance signals. Reaffirmance is
    # advisory and never clears or downgrades a negative finding. Enforcement
    # removes blocked sources and recomputes relative-reaffirmance flags.
    treatment_timeline: list[TreatmentTimelineEntry] = Field(default_factory=list)
    later_reaffirmance: bool = False
    flags: list[str] = Field(default_factory=list)

    @staticmethod
    def _source_validity_signal_blocked(signal: SourceValiditySignal) -> bool:
        """Return whether enforcement must not use this source signal."""
        return signal.withheld or signal.status in {"invalid", "qualified"}

    def source_validity_blocked_source_ids(self) -> set[str]:
        """Return source IDs enforcement must not use.

        Withheld sources include known lower courts, nonprecedential and
        separate opinions, and unverified sources. An authoritative source
        whose bounded history is invalid or only partly valid is also blocked
        from supporting enforcement. The advisory findings remain available.
        """
        return {
            signal.source_opinion_id
            for signal in self.source_validity
            if signal.source_opinion_id
            and self._source_validity_signal_blocked(signal)
        }

    def source_validity_blocking_signal(
        self, source_opinion_id: str
    ) -> SourceValiditySignal | None:
        """Return the blocking signal for a source, or ``None``."""
        if not self.source_validity_enforced or not source_opinion_id:
            return None
        return next(
            (
                signal
                for signal in self.source_validity
                if signal.source_opinion_id == source_opinion_id
                and self._source_validity_signal_blocked(signal)
            ),
            None,
        )


def group_source_audit_provenances(
    provenances: list[SourceAuditProvenance],
) -> list[list[SourceAuditProvenance]]:
    """Group envelopes with the same configuration and code across sources."""
    groups: list[list[SourceAuditProvenance]] = []
    for provenance in provenances:
        for group in groups:
            if group[0].is_configuration_comparable_to(provenance):
                group.append(provenance)
                break
        else:
            groups.append([provenance])
    return groups


def source_audit_provenance_warnings(
    citator_checks: list[CitatorCheck],
) -> list[str]:
    """Warn when one analysis mixes source-audit configurations or code."""
    provenances = [
        signal.audit_ledger.provenance
        for check in citator_checks
        for signal in check.source_validity
    ]
    recorded = [
        provenance for provenance in provenances if provenance is not None
    ]
    unrecorded = len(provenances) - len(recorded)
    if not recorded:
        return []
    groups = group_source_audit_provenances(recorded)
    reasons: list[str] = []
    if len(groups) > 1:
        reasons.append(
            f"{len(recorded)} recorded source audits span "
            f"{len(groups)} non-comparable execution configurations"
        )
    if unrecorded:
        reasons.append(
            f"{unrecorded} source audit(s) have no recorded provenance"
        )
    if not reasons:
        return []
    return [
        "Source-audit provenance is mixed: "
        + "; ".join(reasons)
        + "; compare only groups with the same configuration and code."
    ]


class AdjudicationEvidence(BaseModel):
    """Source evidence a human must inspect before accepting a proposal."""

    target_case: str
    target_citation: str = ""
    target_url: str = ""
    citing_case: str = ""
    citing_citation: str = ""
    citing_court: str = ""
    citing_date: str = ""
    citing_url: str = ""
    source_opinion_id: str = ""
    source_cluster_id: str = ""
    opinion_type: str = ""
    opinion_role: str = "unknown"
    precedential_status: str = ""
    # True when source-validity enforcement blocked this finding from
    # authoritative use. The item stays in the queue for audit at low priority.
    source_validity_blocked: bool = False
    # Verbatim source sentence. It is intentionally not normalized or
    # paraphrased; the reviewer must be able to check it against the link.
    excerpt: str = ""


class AdjudicationProposal(BaseModel):
    """Automated disposition shown only after the evidence.

    ``disposition`` is ``negative`` for an authoritative finding, ``gated`` for
    a non-authoritative finding, or ``withheld`` when source-validity
    enforcement blocked the source.
    """

    disposition: str = "gated"
    status: str = "unknown"
    treatment: str = ""
    scope: str = ""
    gate_reason: str = ""


class AdjudicationReview(BaseModel):
    """Human decision and audit fields; never populated automatically."""

    status: str = "pending"
    reviewer: str = ""
    reviewed_at: str = ""
    rationale: str = ""


class AdjudicationItem(BaseModel):
    """One evidence-bearing citator finding awaiting human review."""

    id: str
    kind: str = "treatment_finding"
    # high for authoritative findings, normal for gated findings, and low for
    # enforcement-withheld findings retained for audit.
    priority: str = "normal"
    # Keep evidence before proposal in the model and serialized JSON so the
    # review artifact is evidence-first rather than conclusion-first.
    evidence: AdjudicationEvidence
    proposal: AdjudicationProposal
    queue_reason: str = ""
    review: AdjudicationReview = Field(default_factory=AdjudicationReview)
    # Prior reviews are retained when a decision is corrected or revisited.
    history: list[AdjudicationReview] = Field(default_factory=list)


class AdjudicationQueue(BaseModel):
    """Durable, replayable queue of evidence-first human review items."""

    version: int = 1
    generated_at: str = ""
    source: str = ""
    items: list[AdjudicationItem] = Field(default_factory=list)


class CaseExtractionCandidates(BaseModel):
    """Quote-backed extraction candidates for one surfaced case."""

    case: str
    candidates: list[ExtractionCandidate] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class CongressAction(BaseModel):
    """One Congress.gov action on a bill linked to a public law."""

    date: str = ""
    text: str = ""
    type: str = ""


class CongressPublicLaw(BaseModel):
    """A public law linked to one cited U.S. Code section.

    ``linked`` means Congress.gov returned the source bill; ``public_law_only``
    means GovInfo found the law but its associated bill could not be resolved.
    This is legislative history, not a citator or supersession conclusion.
    """

    citation: str
    title: str = ""
    url: str = ""
    enacted_date: str = ""
    bill_citation: str = ""
    bill_title: str = ""
    bill_url: str = ""
    actions: list[CongressAction] = Field(default_factory=list)
    actions_truncated: bool = False
    status: str = "linked"
    detail: str = ""


class CRSReport(BaseModel):
    """A CRS report retrieved from Congress.gov.

    CRS reports are analyst-written secondary sources. ``authority_class`` is
    fixed to ``secondary`` so consumers cannot mistake them for primary law.
    """

    report_number: str
    title: str = ""
    url: str = ""
    publish_date: str = ""
    authority_class: Literal["secondary"] = "secondary"
    detail: str = "CRS analysis is a secondary source, not primary legal authority."


class CongressLink(BaseModel):
    """Congressional history for one cited U.S. Code section."""

    usc_section: str
    # "linked", "no_public_law", "unavailable", or "not_assessed".
    status: str = "not_assessed"
    public_laws: list[CongressPublicLaw] = Field(default_factory=list)
    detail: str = ""
    source: str = "GovInfo/Congress.gov"


class FederalRegisterDocument(BaseModel):
    """A Federal Register document used to locate a rulemaking docket."""

    document_number: str
    title: str = ""
    publication_date: str = ""
    type: str = ""
    url: str = ""
    abstract: str = ""
    action: str = ""
    docket_ids: list[str] = Field(default_factory=list)


class RegulationsPublicComment(BaseModel):
    """A sampled public comment, never legal authority.

    ``authority_class`` is fixed to ``public_input`` so downstream consumers
    cannot mistake a comment for a rule, statute, or agency interpretation.
    """

    comment_id: str = ""
    title: str = ""
    posted_date: str = ""
    excerpt: str = ""
    authority_class: Literal["public_input"] = "public_input"
    detail: str = "Public comment: public input, not legal authority."


class RegulationsDocument(BaseModel):
    """A Regulations.gov document mapped from a Federal Register document."""

    document_id: str = ""
    docket_id: str = ""
    fr_document_number: str = ""
    title: str = ""
    document_type: str = ""
    posted_date: str = ""
    comment_start_date: str = ""
    comment_end_date: str = ""
    open_for_comment: bool = False
    url: str = ""
    object_id: str = ""
    comment_count: int = 0
    comments_sampled: int = 0
    comments: list[RegulationsPublicComment] = Field(default_factory=list)
    status: str = "mapped"
    detail: str = ""


class RegulationsSupportingDocument(BaseModel):
    """A Supporting & Related Material document in a docket."""

    document_id: str
    title: str = ""
    document_type: str = ""
    posted_date: str = ""
    url: str = ""


class RegulationsDocket(BaseModel):
    """A Regulations.gov docket with its mapped rulemaking record."""

    docket_id: str
    title: str = ""
    agency_id: str = ""
    docket_type: str = ""
    rin: str = ""
    url: str = ""
    documents: list[RegulationsDocument] = Field(default_factory=list)
    supporting_documents: list[RegulationsSupportingDocument] = Field(
        default_factory=list
    )
    status: str = "mapped"
    detail: str = ""


class RegulationsMapping(BaseModel):
    """Rulemaking context for one cited 38 C.F.R. part."""

    cfr_part: str
    # "mapped", "no_docket", "unavailable", or "not_assessed".
    status: str = "not_assessed"
    fr_documents: list[FederalRegisterDocument] = Field(default_factory=list)
    dockets: list[RegulationsDocket] = Field(default_factory=list)
    detail: str = ""
    source: str = "Federal Register/Regulations.gov"


class SurfacedCaseRecord(BaseModel):
    """Deliverable 1 (D1) metadata for one surfaced authority.

    The legacy ``LegalAnalysis.top_cases`` display strings remain for
    compatibility; this record is the lossless contract used by text, CSV, and
    JSON renderers. ``official_transcript_url`` is set only for official or
    archival sources, while ``retrieved_url`` always records what was fetched.
    """

    case_title: str
    court: str
    docket_number: str | None = None
    reporter_citation: str | None = None
    decision_date: str | None = None
    precedential_status: str = "unknown"
    # CourtListener panel metadata and the explicit CAVC panel-list result.
    # ``panel`` is a display string, not proof of authorship.
    panel: str | None = None
    panel_status: str = "not_assessed"
    appellate_history: list[AppellateHistoryEntry] = Field(default_factory=list)
    history_status: str = "not_assessed"
    # Advisory citator state. ``good_law`` means only that no authoritative
    # negative treatment was found in the reviewed citing-opinion set.
    citator_status: str = "not_assessed"
    citator_confidence: str = "none"
    citator_as_of: str | None = None
    official_transcript_url: str | None = None
    retrieved_url: str | None = None
    transcript_source_type: str = "unverified"
    provenance_verified: bool = False
    source_reliability: str = ""
    warnings: list[str] = Field(default_factory=list)
    case_abstract: CaseAbstract | None = None
    provision_impacts: list[ProvisionImpact] = Field(default_factory=list)

    @property
    def display_label(self) -> str:
        """Backward-compatible one-line label for legacy callers."""
        return f"{self.case_title} ({self.court})"


class LegalAnalysis(BaseModel):
    run_id: str = ""
    issue: str
    summary: str
    likely_applicable_principles: list[str] = Field(default_factory=list)
    how_it_affects_va_claims: str
    next_steps: list[str] = Field(default_factory=list)
    top_cases: list[str] = Field(default_factory=list)
    # Lossless D1 case records aligned with ``top_cases``. ``top_cases`` is
    # retained only as a display compatibility layer.
    surfaced_cases: list[SurfacedCaseRecord] = Field(default_factory=list)
    case_abstracts: list[CaseAbstract] = Field(default_factory=list)
    provision_impacts: list[ProvisionImpact] = Field(default_factory=list)
    rvsr_guidance: RvsrGuidance = Field(default_factory=RvsrGuidance)
    senior_rater_guidance: SeniorRaterGuidance = Field(
        default_factory=SeniorRaterGuidance
    )
    # Per-case full-text summaries from deep-read mode, aligned with top_cases:
    # each entry is {"case": "Title (court)", "summary": "..."} (empty summary
    # when deep-read was off or a body could not be fetched).
    deep_summaries: list[dict[str, str]] = Field(default_factory=list)
    # Per-case quote-first extraction candidates. Each candidate carries its
    # verbatim source quote and stays advisory; these entries never replace a
    # case's authoritative fields and never feed ranking or interpretation.
    extraction_candidates: list[CaseExtractionCandidates] = Field(
        default_factory=list
    )
    detected_elements: list[ClaimElement] = Field(default_factory=list)
    principle_findings: list[PrincipleFinding] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    coverage_score: float = Field(default=0.0, ge=0.0, le=1.0)
    coverage_confidence: str = "low"
    interpretation_source: str = "template"
    search_telemetry: dict[str, dict[str, object]] = Field(default_factory=dict)
    search_flags: list[str] = Field(default_factory=list)
    # Best-effort CourtListener citation-lookup guardrail results. Each entry
    # names a citation that could not be resolved, resolved ambiguously, or was
    # not checked because of a failed lookup or exhausted daily quota. Empty
    # when every citation resolved or the check did not run (no API token).
    citation_flags: list[str] = Field(default_factory=list)
    # External authority verification results (CAVC panel list, eCFR, Federal
    # Register, GovInfo). Empty when verification is disabled or did not run.
    authority_checks: list[AuthorityCheck] = Field(default_factory=list)
    # Human-readable problems derived from ``authority_checks`` (conflicts,
    # unresolved citations, or an unavailable verification source).
    authority_flags: list[str] = Field(default_factory=list)
    # Advisory legislative-history links for cited 38 U.S.C. sections. These
    # never change ranking, citator status, or supersession signals.
    congress_links: list[CongressLink] = Field(default_factory=list)
    # CRS reports explicitly cited in the analysis, labeled secondary. The
    # Congress.gov API has no keyword search for CRS reports, so reports are
    # retrieved by report number rather than guessed from a section citation.
    congress_crs_reports: list[CRSReport] = Field(default_factory=list)
    congress_flags: list[str] = Field(default_factory=list)
    # Advisory rulemaking context for cited 38 C.F.R. parts. Public comments
    # are labeled public_input and never treated as authority.
    regulations_mappings: list[RegulationsMapping] = Field(default_factory=list)
    regulations_flags: list[str] = Field(default_factory=list)
    # Separate advisory signals for statutory/regulatory supersession. These
    # never set a case's ``superseded_by`` or change citator status.
    statutory_supersession_signals: list[StatutorySupersessionSignal] = Field(
        default_factory=list
    )
    statutory_supersession_flags: list[str] = Field(default_factory=list)
    # Advisory CourtListener citator results for the prioritized authority
    # queue. Empty when the citator is disabled or was skipped; each check
    # carries its own coverage, so an empty negative-treatment list is not by
    # itself a clean-bill-of-health. This layer never feeds back into ranking.
    citator_checks: list[CitatorCheck] = Field(default_factory=list)
    citator_flags: list[str] = Field(default_factory=list)
    # The selected queue that produced ``citator_checks``, in priority order.
    # Empty when the citator is disabled or skipped.
    authority_queue: list[AuthorityQueueEntry] = Field(default_factory=list)
    # Snapshot of CourtListener's daily request window recorded by the usage
    # guard's final pre-flight check (``used``/``limit``/``remaining``/
    # ``reset_at``), so the output shows how much free-tier budget the run
    # left. ``None`` when CourtListener is not a configured provider or the
    # guard is disabled.
    courtlistener_quota: dict[str, object] | None = None
    # Cross-domain synthesis: a statute × court × outcome matrix showing
    # how the retrieved cases break down. Each row is one (statute, court)
    # pair with counts of favorable, unfavorable, and unknown-direction
    # outcomes. This is the most useful single artifact for assessing the
    # strength of a legal position under a given statute across courts.
    statute_outcome_matrix: list[StatuteOutcomeRow] = Field(default_factory=list)

    @computed_field
    @property
    def source_provenance_warnings(self) -> list[str]:
        """Warn when source audits span non-comparable envelopes."""
        return source_audit_provenance_warnings(self.citator_checks)


class ImpactProfile(BaseModel):
    """Structured, nuanced impact analysis for a single case."""

    issue_tags: list[str] = Field(default_factory=list)
    outcome: str = ""
    outcome_note: str = ""
    statutes: list[str] = Field(default_factory=list)
    statute_note: str = ""
    authority_note: str = ""
    provision_impacts: list[ProvisionImpact] = Field(default_factory=list)
    nuance: str = ""
