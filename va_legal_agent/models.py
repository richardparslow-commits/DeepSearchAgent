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
    authority_rank: int = 0
    authority_weight: int = 0
    relevance_score: int = 0
    composite_score: float = 0.0
    ranking_explanation: str = ""


class ResearchRequest(BaseModel):
    claimant_issue: str
    benefit_type: str = "Compensation"
    jurisdiction: str = "United States"
    max_results: int = 10
    include_board_cases: bool = True
    include_cavc_cases: bool = True
    include_federal_circuit_cases: bool = True
    include_supreme_court_cases: bool = True


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


class LegalAnalysis(BaseModel):
    issue: str
    summary: str
    likely_applicable_principles: list[str] = Field(default_factory=list)
    how_it_affects_va_claims: str
    next_steps: list[str] = Field(default_factory=list)
    top_cases: list[str] = Field(default_factory=list)
    detected_elements: list[ClaimElement] = Field(default_factory=list)
    principle_findings: list[PrincipleFinding] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    coverage_score: float = 0.0
    interpretation_source: str = "template"
