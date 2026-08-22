from __future__ import annotations

from pydantic import BaseModel, Field


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
    statutes: list[str] = Field(default_factory=list)
    outcome: str = ""
    # Who appealed: "veteran", "secretary", or "unknown" (empty = not
    # extracted). In VA law, "affirmed" means opposite things depending on
    # the appellant: unfavorable when the veteran appealed, favorable when
    # the Secretary cross-appealed. The contradiction detector and impact
    # note layer use this to make outcome classification party-aware.
    appellant_role: str = ""
    # Whether this decision is precedential (published, citable as binding
    # authority). Non-precedential decisions (memorandum dispositions,
    # unpublished orders, single-judge designations) carry a 0.25 composite
    # penalty and are flagged in the report as persuasive rather than
    # binding. Defaults to True (assume precedential when the body cannot
    # be read).
    precedential: bool = True
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
    detected_elements: list[ClaimElement] = Field(default_factory=list)
    principle_findings: list[PrincipleFinding] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    coverage_score: float = 0.0
    interpretation_source: str = "template"
    search_telemetry: dict[str, dict[str, object]] = Field(default_factory=dict)
    search_flags: list[str] = Field(default_factory=list)
    # Snapshot of CourtListener's daily request window recorded by the usage
    # guard's final pre-flight check (``used``/``limit``/``remaining``/
    # ``reset_at``), so the output shows how much free-tier budget the run
    # left. ``None`` when CourtListener is not a configured provider or the
    # guard is disabled.
    courtlistener_quota: dict[str, object] | None = None


class ImpactProfile(BaseModel):
    """Structured, nuanced impact analysis for a single case."""

    issue_tags: list[str] = Field(default_factory=list)
    outcome: str = ""
    outcome_note: str = ""
    statutes: list[str] = Field(default_factory=list)
    statute_note: str = ""
    authority_note: str = ""
    nuance: str = ""
