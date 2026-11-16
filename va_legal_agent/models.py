from __future__ import annotations

from pydantic import BaseModel, Field


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


class AuthorityCheck(BaseModel):
    """One external verification result for a cited authority."""

    authority: str
    source: str
    # "verified", "conflict", "unresolved", "no_recent_documents", or
    # "unavailable".
    status: str
    detail: str = ""
    url: str = ""


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


class SourceAuditLedger(BaseModel):
    """Bounded coverage accounting for one source-history audit.

    The ledger describes what the audit could see, not whether the source is
    valid. ``coverage_complete`` means the bounded review finished; a reviewed
    but rejected or gated candidate can still have complete coverage while the
    signal's ``history_complete`` remains false. ``forward_citations_reported``
    is the sum of per-opinion API counts and may double-count a citing opinion
    linked to more than one cluster opinion; ``citing_ids_collected`` is the
    de-duplicated count actually collected within the bounded pool.
    """

    assessed: bool = False
    history_attempted: bool = False
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
    # Concrete incompleteness reasons; deduplicated in capture order.
    gaps: list[CitatorCoverageGap] = Field(default_factory=list)


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


class LegalAnalysis(BaseModel):
    run_id: str = ""
    issue: str
    summary: str
    likely_applicable_principles: list[str] = Field(default_factory=list)
    how_it_affects_va_claims: str
    next_steps: list[str] = Field(default_factory=list)
    top_cases: list[str] = Field(default_factory=list)
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


class ImpactProfile(BaseModel):
    """Structured, nuanced impact analysis for a single case."""

    issue_tags: list[str] = Field(default_factory=list)
    outcome: str = ""
    outcome_note: str = ""
    statutes: list[str] = Field(default_factory=list)
    statute_note: str = ""
    authority_note: str = ""
    nuance: str = ""
