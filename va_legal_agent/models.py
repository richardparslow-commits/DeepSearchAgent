from __future__ import annotations

from pydantic import BaseModel, Field


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
    opinion in that set was successfully read. The count fields let a
    consumer distinguish a complete review from a partially checked one.
    ``gaps`` enumerates the specific missing pieces rather than leaving the
    consumer to parse flags. ``temporal_confidence`` describes how far the
    latest reviewed citing date can bound the review: ``high`` means a
    complete review with dates for every reviewed opinion, ``medium`` means a
    known date from an incomplete review, ``low`` means at least one reviewed
    opinion has no date, and ``none`` means no usable reviewed date exists.
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
    ranking, ``superseded_by``, or the citator's status rules.
    """

    priority: int = 0
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


class CitatorCheck(BaseModel):
    """Advisory citator result for one queued authority.

    ``good_law`` means only that no authoritative negative treatment was found
    in the reviewed citing opinions. It does not mean the case is final, has no
    subsequent history, or remains good law. ``good_law_as_of`` is populated
    only for a clean, complete review of that forward-citation set. An
    authoritative negative finding makes the status ``negative``; a finding
    from a separate or nonprecedential opinion is gated and makes it
    ``unknown``. Missing or truncated coverage also makes it ``unknown``. The
    citator never changes ranking, ``superseded_by``, or contradiction results.
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
    # Only majority-role, Published findings. This is the list that can make
    # the status ``negative``.
    negative_treatments: list[NegativeTreatment] = Field(default_factory=list)
    # Findings from separate, nonprecedential, or unclassified opinions. They
    # are retained for review but never counted toward ``negative`` or
    # ``good_law``.
    gated_treatment_findings: list[NegativeTreatment] = Field(default_factory=list)
    # Authoritative negatives split by the citing court's relationship to the
    # target. ``negative_treatments`` remains the union and the only status
    # input; unknown binding is intentionally left out of both splits.
    binding_negative_treatments: list[NegativeTreatment] = Field(
        default_factory=list
    )
    persuasive_negative_treatments: list[NegativeTreatment] = Field(
        default_factory=list
    )
    # Chronological negative and reaffirmance signals. Reaffirmance is
    # advisory and never clears or downgrades a negative finding.
    treatment_timeline: list[TreatmentTimelineEntry] = Field(default_factory=list)
    later_reaffirmance: bool = False
    flags: list[str] = Field(default_factory=list)


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
    opinion_type: str = ""
    opinion_role: str = "unknown"
    precedential_status: str = ""
    # Verbatim source sentence. It is intentionally not normalized or
    # paraphrased; the reviewer must be able to check it against the link.
    excerpt: str = ""


class AdjudicationProposal(BaseModel):
    """Automated disposition shown only after the evidence."""

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
