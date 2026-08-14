"""Interpretive analysis layer for VA claims guidance.

Turns ranked cases plus the claim issue into structured, explainable guidance:
legal elements detected in the issue, case-backed principles, strengths and
gaps in the retrieved authority, and actionable next steps. Output is research
support derived from public decisions, not legal advice.

When OPENAI_API_KEY is set, the narrative explanation is enhanced via the
optional LLM layer; otherwise a deterministic template summary is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .llm import interpret_cases
from .models import CaseRecord, ClaimElement, PrincipleFinding

# Narrative interpretation is produced from the strongest cases, while
# principle scanning covers a slightly wider pool.
INTERPRET_CASE_LIMIT = 3
PRINCIPLE_SCAN_LIMIT = 5


@dataclass(frozen=True)
class ElementSpec:
    name: str
    phrases: tuple[str, ...]
    description: str
    guidance: str
    step: str


ELEMENT_LIBRARY: tuple[ElementSpec, ...] = (
    ElementSpec(
        name="service connection",
        phrases=("service connection", "service-connected"),
        description=(
            "The Caluza elements: a current diagnosed disability, an in-service "
            "event or aggravation, and a nexus linking the two."
        ),
        guidance=(
            "Assemble (1) a current diagnosis, (2) evidence of the in-service "
            "event, and (3) a medical nexus opinion connecting the two."
        ),
        step=(
            "Map the record to the three Caluza elements (diagnosis, in-service "
            "event, nexus) and identify which element is contested."
        ),
    ),
    ElementSpec(
        name="nexus",
        phrases=("nexus", "medical opinion", "caused by service", "related to service"),
        description="A medical or factual link between the current disability and military service.",
        guidance=(
            "Obtain a supporting medical nexus opinion that addresses the "
            "'at least as likely as not' standard."
        ),
        step="Secure a nexus opinion that explicitly applies the 'at least as likely as not' standard.",
    ),
    ElementSpec(
        name="benefit of the doubt",
        phrases=("benefit of the doubt", "reasonable doubt", "equipoise"),
        description=(
            "When the evidence for and against the claim is in relative balance, the "
            "benefit of the doubt is given to the veteran (38 U.S.C. § 5107(b))."
        ),
        guidance=(
            "Highlight areas where favorable and unfavorable evidence are roughly in "
            "balance, and request that the benefit of the doubt be applied."
        ),
        step="Identify issues where the evidence is in equipoise and argue the benefit of the doubt.",
    ),
    ElementSpec(
        name="reasons and bases",
        phrases=("reasons and bases", "adequate explanation", "statement of reasons"),
        description=(
            "The Board must provide an adequate statement of the reasons and bases "
            "for its decision (38 U.S.C. § 7104(d)(1))."
        ),
        guidance="Point out any findings the Board failed to explain or evidence it failed to address.",
        step="Check the decision for findings that lack reasons-and-bases support.",
    ),
    ElementSpec(
        name="evidence evaluation",
        phrases=("evidence", "competent evidence", "lay evidence", "medical evidence"),
        description="Evidence must be competent, credible, and properly weighed under VA standards.",
        guidance=(
            "Gather competent evidence supporting the claim, and challenge any "
            "improper weighing of lay or medical evidence."
        ),
        step="Audit whether VA weighed the competent and lay evidence properly.",
    ),
    ElementSpec(
        name="presumption",
        phrases=("presumption", "presumptive"),
        description="Presumptive service connection may apply to certain conditions for qualifying veterans.",
        guidance="Confirm whether the condition and service history qualify for any presumptions.",
        step="Check whether presumptive service connection applies.",
    ),
    ElementSpec(
        name="rating",
        phrases=("rating", "evaluation", "schedular"),
        description=(
            "Disability evaluation applies the rating schedule to the symptoms and "
            "severity shown in the record."
        ),
        guidance=(
            "Compare the veteran's symptoms against the rating criteria to argue "
            "for the appropriate evaluation."
        ),
        step="Compare the symptoms against the rating schedule criteria.",
    ),
    ElementSpec(
        name="aggravation",
        phrases=("aggravation", "aggravated"),
        description="A pre-existing condition aggravated by service may be service-connected.",
        guidance="Document the pre-service baseline and the worsening during service.",
        step="Document the pre-service baseline and the in-service worsening.",
    ),
    ElementSpec(
        name="duty to assist",
        phrases=("duty to assist",),
        description="VA has a duty to assist the claimant in developing evidence relevant to the claim.",
        guidance="Identify records or exams that VA failed to obtain or provide.",
        step="Identify any evidence that VA failed to help develop.",
    ),
)


# Phrase -> principle statement; scanned across retrieved case text with attribution.
PRINCIPLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "benefit of the doubt",
        "When the evidence for and against the claim is in relative equipoise, the "
        "benefit of the doubt is given to the veteran (38 U.S.C. § 5107(b)).",
    ),
    (
        "reasons and bases",
        "The Board must provide an adequate statement of reasons and bases for its "
        "decision (38 U.S.C. § 7104(d)(1)).",
    ),
    (
        "nexus",
        "Service connection requires a nexus linking the current disability to an in-service event.",
    ),
    (
        "competent evidence",
        "Claims must be supported by competent evidence addressing the required elements.",
    ),
    (
        "lay evidence",
        "Competent lay evidence may establish observable symptoms and continuity without medical training.",
    ),
    (
        "medical evidence",
        "Medical evidence is required to establish diagnosis and, where appropriate, etiology.",
    ),
    (
        "presumption",
        "Presumptive service connection may apply for qualifying conditions and service histories.",
    ),
    (
        "duty to assist",
        "VA has a duty to assist the claimant in developing evidence relevant to the claim.",
    ),
)

GENERIC_NEXT_STEPS = [
    "Compare the facts of the current claim to the cited authorities and identify the closest analog.",
    "Treat this output as research support derived from public decisions, not legal advice.",
]


@dataclass
class InterpretiveAnalysis:
    likely_applicable_principles: list[str] = field(default_factory=list)
    how_it_affects_va_claims: str = ""
    next_steps: list[str] = field(default_factory=list)
    detected_elements: list[ClaimElement] = field(default_factory=list)
    principle_findings: list[PrincipleFinding] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    coverage_score: float = 0.0
    interpretation_source: str = "template"


def _case_text(case: CaseRecord) -> str:
    return f"{case.title} {case.snippet} {case.holding} {case.impact}".lower()


def detect_claim_elements(issue: str) -> list[ElementSpec]:
    """Return the element specs whose detection phrases appear in the claim issue."""
    text = issue.lower()
    return [spec for spec in ELEMENT_LIBRARY if any(phrase in text for phrase in spec.phrases)]


def extract_principle_findings(cases: list[CaseRecord]) -> list[PrincipleFinding]:
    """Scan case text for known principle phrases, attributing each to its source cases."""
    findings: list[PrincipleFinding] = []
    for phrase, principle in PRINCIPLE_PATTERNS:
        sources = [case.title for case in cases if phrase in _case_text(case)]
        if sources:
            findings.append(PrincipleFinding(principle=principle, source_cases=sources))
    return findings


def _cases_covering_element(spec: ElementSpec, cases: list[CaseRecord]) -> list[str]:
    return [
        case.title
        for case in cases
        if any(phrase in _case_text(case) for phrase in spec.phrases)
    ]


def _template_interpretation(
    issue: str,
    claim_type: str,
    cases: list[CaseRecord],
    elements: list[ElementSpec],
    findings: list[PrincipleFinding],
) -> str:
    disclaimer = "This guidance is research support derived from public decisions, not legal advice."
    case_names = ", ".join(case.title for case in cases[:INTERPRET_CASE_LIMIT])
    if findings:
        principle_text = " ".join(finding.principle for finding in findings[:3])
        element_text = (
            f"Key elements to establish: {'; '.join(spec.name for spec in elements)}. "
            if elements
            else ""
        )
        return (
            f"For the issue of {issue} under {claim_type}, the retrieved authorities "
            f"({case_names}) indicate the following governing principles: {principle_text} "
            f"{element_text}{disclaimer}"
        )
    return (
        f"No explicit principle could be extracted from the retrieved results for "
        f"{issue} ({claim_type}) — cases reviewed: {case_names}. They may still be "
        f"relevant; review their full text before relying on them. {disclaimer}"
    )


def build_interpretive_analysis(
    claim_issue: str, claim_type: str, cases: list[CaseRecord]
) -> InterpretiveAnalysis:
    """Build structured VA-claims guidance from ranked cases and the claim issue."""
    scan_pool = cases[:PRINCIPLE_SCAN_LIMIT]
    element_specs = detect_claim_elements(claim_issue)
    findings = extract_principle_findings(scan_pool)

    detected: list[ClaimElement] = []
    strengths: list[str] = []
    gaps: list[str] = []
    for spec in element_specs:
        covered_by = _cases_covering_element(spec, scan_pool)
        detected.append(
            ClaimElement(
                name=spec.name,
                description=spec.description,
                guidance=spec.guidance,
                covered_by=covered_by,
            )
        )
        if covered_by:
            strengths.append(
                f"Retrieved authority addresses '{spec.name}': {', '.join(covered_by[:3])}."
            )
        else:
            gaps.append(
                f"No retrieved authority directly addresses '{spec.name}'; "
                f"consider a targeted search for '{spec.name}' precedent."
            )

    if findings:
        strengths.insert(0, f"Retrieved authority articulates {len(findings)} governing principle(s).")
    else:
        gaps.insert(
            0,
            "No explicit legal principle was extracted from the retrieved results; "
            "verify the query terms or broaden the search.",
        )

    coverage_score = (
        sum(1 for element in detected if element.covered_by) / len(detected) if detected else 0.0
    )

    next_steps = [spec.step for spec in element_specs]
    if not element_specs:
        next_steps.append("Identify the precise legal issue and the evidence in the claim file.")
    next_steps.extend(GENERIC_NEXT_STEPS)

    template_text = _template_interpretation(claim_issue, claim_type, cases, element_specs, findings)
    llm_text = interpret_cases(claim_issue, claim_type, cases[:INTERPRET_CASE_LIMIT])

    return InterpretiveAnalysis(
        likely_applicable_principles=[
            f"{finding.principle} (see: {', '.join(finding.source_cases[:3])})"
            for finding in findings
        ],
        how_it_affects_va_claims=llm_text or template_text,
        next_steps=next_steps,
        detected_elements=detected,
        principle_findings=findings,
        strengths=strengths,
        gaps=gaps,
        coverage_score=coverage_score,
        interpretation_source="llm" if llm_text else "template",
    )