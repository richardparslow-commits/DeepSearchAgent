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

from .config import get_settings
from .llm import interpret_cases
from .models import CaseRecord, ClaimElement, PrincipleFinding
from .topics import PRINCIPLE_PATTERNS, TOPICS

@dataclass(frozen=True)
class ElementSpec:
    name: str
    phrases: tuple[str, ...]
    description: str
    guidance: str
    step: str


# Claimant guidance per topic, keyed by Topic.name. Topic names and detection
# phrases live in topics.TOPICS; this table only adds the interpretation text.
_ELEMENT_DETAILS: dict[str, tuple[str, str, str]] = {
    "service connection": (
        "The Caluza elements: a current diagnosed disability, an in-service "
        "event or aggravation, and a nexus linking the two.",
        "Assemble (1) a current diagnosis, (2) evidence of the in-service "
        "event, and (3) a medical nexus opinion connecting the two.",
        "Map the record to the three Caluza elements (diagnosis, in-service "
        "event, nexus) and identify which element is contested.",
    ),
    "nexus": (
        "A medical or factual link between the current disability and military service.",
        "Obtain a supporting medical nexus opinion that addresses the "
        "'at least as likely as not' standard.",
        "Secure a nexus opinion that explicitly applies the 'at least as likely as not' standard.",
    ),
    "benefit of the doubt": (
        "When the evidence for and against the claim is in relative balance, the "
        "benefit of the doubt is given to the veteran (38 U.S.C. § 5107(b)).",
        "Highlight areas where favorable and unfavorable evidence are roughly in "
        "balance, and request that the benefit of the doubt be applied.",
        "Identify issues where the evidence is in equipoise and argue the benefit of the doubt.",
    ),
    "reasons and bases": (
        "The Board must provide an adequate statement of the reasons and bases "
        "for its decision (38 U.S.C. § 7104(d)(1)).",
        "Point out any findings the Board failed to explain or evidence it failed to address.",
        "Check the decision for findings that lack reasons-and-bases support.",
    ),
    "evidence evaluation": (
        "Evidence must be competent, credible, and properly weighed under VA standards.",
        "Gather competent evidence supporting the claim, and challenge any "
        "improper weighing of lay or medical evidence.",
        "Audit whether VA weighed the competent and lay evidence properly.",
    ),
    "presumption": (
        "Presumptive service connection may apply to certain conditions for qualifying veterans.",
        "Confirm whether the condition and service history qualify for any presumptions.",
        "Check whether presumptive service connection applies.",
    ),
    "rating": (
        "Disability evaluation applies the rating schedule to the symptoms and "
        "severity shown in the record.",
        "Compare the veteran's symptoms against the rating criteria to argue "
        "for the appropriate evaluation.",
        "Compare the symptoms against the rating schedule criteria.",
    ),
    "aggravation": (
        "A pre-existing condition aggravated by service may be service-connected.",
        "Document the pre-service baseline and the worsening during service.",
        "Document the pre-service baseline and the in-service worsening.",
    ),
    "duty to assist": (
        "VA has a duty to assist the claimant in developing evidence relevant to the claim.",
        "Identify records or exams that VA failed to obtain or provide.",
        "Identify any evidence that VA failed to help develop.",
    ),
}


def _build_element_library() -> tuple[ElementSpec, ...]:
    """Join the shared topic names/phrases with the guidance text above.

    Fails loudly (at import, via ``ELEMENT_LIBRARY``) when a topic has no
    guidance entry, naming the missing topics instead of surfacing a bare
    ``KeyError`` from the dict lookup.
    """
    missing = sorted(name for name in (topic.name for topic in TOPICS) if name not in _ELEMENT_DETAILS)
    if missing:
        raise KeyError(
            "Every TOPICS entry needs guidance in _ELEMENT_DETAILS; "
            f"missing: {', '.join(missing)}"
        )
    specs = []
    for topic in TOPICS:
        description, guidance, step = _ELEMENT_DETAILS[topic.name]
        specs.append(ElementSpec(topic.name, topic.phrases, description, guidance, step))
    return tuple(specs)


ELEMENT_LIBRARY: tuple[ElementSpec, ...] = _build_element_library()


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


def uncovered_element_names(issue: str, cases: list[CaseRecord]) -> tuple[str, ...]:
    """Return the detected claim elements no retrieved case covers.

    The observation primitive for the adaptive research loop: it reuses the
    same detection and coverage logic as :func:`build_interpretive_analysis`
    so refinement targets exactly the elements the final report flags as gaps.
    """
    return tuple(
        spec.name for spec in detect_claim_elements(issue) if not _cases_covering_element(spec, cases)
    )


def _template_interpretation(
    issue: str,
    claim_type: str,
    cases: list[CaseRecord],
    elements: list[ElementSpec],
    findings: list[PrincipleFinding],
    interpret_limit: int,
) -> str:
    disclaimer = "This guidance is research support derived from public decisions, not legal advice."
    case_names = ", ".join(case.title for case in cases[:interpret_limit])
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
    settings = get_settings()
    interpret_limit = settings.interpret_case_limit
    scan_limit = settings.principle_scan_limit
    scan_pool = cases[:scan_limit]
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

    template_text = _template_interpretation(
        claim_issue, claim_type, cases, element_specs, findings, interpret_limit
    )
    llm_text = interpret_cases(claim_issue, claim_type, cases[:interpret_limit])

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
