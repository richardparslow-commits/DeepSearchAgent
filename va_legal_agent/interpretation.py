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
from .fetch import extract_statutes
from .impact import detect_outcome
from .llm import interpret_cases, reason_cases
from .models import CaseRecord, ClaimElement, Contradiction, PrincipleFinding
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
    contradictions: list[Contradiction] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    coverage_score: float = 0.0
    interpretation_source: str = "template"


def _case_text(case: CaseRecord) -> str:
    # ``deep_summary`` is included so full-text ingestion feeds the
    # deterministic element-coverage, principle, and statute scans, not just
    # the LLM reasoning pass. Without it, deep-read mode could produce a
    # substantive holding while the coverage score stays 0.0 because the
    # snippet/holding fields were too thin to match an element phrase.
    return (
        f"{case.title} {case.snippet} {case.holding} "
        f"{case.impact} {case.deep_summary}"
    ).lower()


# Dispositions that favor the claimant position vs. go against it, used by the
# always-on deterministic contradiction detector. ``vacated``/``remanded``
# reopen a denial and ``granted`` awards relief; ``affirmed``/``dismissed``/
# ``denied`` uphold or reject it. This is deliberately conservative: only
# explicit, single-direction outcomes trigger a flag.
_FAVORABLE_OUTCOMES = frozenset({"granted", "vacated", "remanded"})
_UNFAVORABLE_OUTCOMES = frozenset({"denied", "affirmed", "dismissed"})


def _outcome_direction(outcome: str) -> int:
    """Classify an outcome as favorable (+1), unfavorable (-1), or unknown (0).

    ``extract_outcome`` joins multiple signals with " and " (e.g. "vacated and
    remanded"). A compound that mixes directions ("granted and denied") or an
    unrecognized signal resolves to 0 rather than guessing, so the deterministic
    detector never flags on ambiguous postures.
    """
    signals = outcome.lower().split(" and ")
    if not signals or any(not signal for signal in signals):
        return 0
    directions: set[int] = set()
    for signal in signals:
        if signal in _FAVORABLE_OUTCOMES:
            directions.add(1)
        elif signal in _UNFAVORABLE_OUTCOMES:
            directions.add(-1)
        else:
            return 0
    if len(directions) == 1:
        return directions.pop()
    return 0


def _case_statutes(case: CaseRecord) -> list[str]:
    """Return the case's cited statutes, scanning text when unenriched."""
    return list(case.statutes) or extract_statutes(_case_text(case))


def detect_contradictions(cases: list[CaseRecord]) -> list[Contradiction]:
    """Deterministically flag opposite outcomes on a shared statute.

    The always-on complement to the LLM reasoning pass: pairs of retrieved
    authorities that cite the same statute but reach opposite outcomes
    (favorable vs. unfavorable to the claimant) are surfaced as contradictions
    so the report never silently picks one side. The outcome prefers the
    enriched field and falls back to the title/holding text (mirroring
    :func:`impact.detect_outcome`), matching the statute fallback; unknown or
    mixed postures never trigger a flag.
    """
    outcomes = [detect_outcome(case) for case in cases]
    directions = [_outcome_direction(outcome) for outcome in outcomes]
    contradictions: list[Contradiction] = []
    seen: set[tuple[str, str]] = set()
    for i, first in enumerate(cases):
        first_direction = directions[i]
        if first_direction == 0:
            continue
        first_statutes = set(_case_statutes(first))
        if not first_statutes:
            continue
        for j, second in enumerate(cases[i + 1 :], start=i + 1):
            second_direction = directions[j]
            if second_direction == 0 or second_direction == first_direction:
                continue
            shared = first_statutes & set(_case_statutes(second))
            if not shared:
                continue
            key = tuple(sorted((first.title, second.title)))
            if key in seen:
                continue
            seen.add(key)
            statute = sorted(shared)[0]
            contradictions.append(
                Contradiction(
                    statement=(
                        f"{first.title} and {second.title} reach opposite outcomes on "
                        f"{statute}: {outcomes[i]} versus {outcomes[j]}."
                    ),
                    case_a=first.title,
                    case_b=second.title,
                )
            )
    return contradictions


def _merge_contradictions(
    primary: list[Contradiction], secondary: list[Contradiction]
) -> list[Contradiction]:
    """Combine two contradiction lists, deduping by the case pair.

    ``primary`` (the LLM reasoning pass) wins on a collision; deterministic
    findings fill in any pair the LLM did not report.
    """
    merged = list(primary)
    seen = {tuple(sorted((c.case_a, c.case_b))) for c in primary}
    for contradiction in secondary:
        key = tuple(sorted((contradiction.case_a, contradiction.case_b)))
        if key not in seen:
            seen.add(key)
            merged.append(contradiction)
    return merged


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
    # Always-on deterministic conflict detection: opposite outcomes on a shared
    # statute surface even without the LLM, so the report flags tension between
    # authorities on the template path too.
    deterministic_contradictions = detect_contradictions(cases)
    reasoning = reason_cases(claim_issue, claim_type, cases[: settings.llm_reasoning_limit])
    # When the reconciling pass provides a synthesis it replaces the lighter
    # narrative call entirely (one LLM call instead of two); otherwise the
    # single-call narrative is still tried.
    llm_text = (
        None
        if reasoning and reasoning.synthesis
        else interpret_cases(claim_issue, claim_type, cases[:interpret_limit])
    )

    # The deterministic principles are the always-on baseline; the reasoning
    # pass overrides them (its principles carry inline citations) when the LLM
    # is available and returns content.
    template_principles = [
        f"{finding.principle} (see: {', '.join(finding.source_cases[:3])})"
        for finding in findings
    ]
    if reasoning:
        principles = list(reasoning.reconciled_principles) or template_principles
        narrative = reasoning.synthesis or llm_text or template_text
        contradictions = _merge_contradictions(
            list(reasoning.contradictions), deterministic_contradictions
        )
        source = "llm"
    else:
        principles = template_principles
        narrative = llm_text or template_text
        contradictions = deterministic_contradictions
        source = "llm" if llm_text else "template"

    return InterpretiveAnalysis(
        likely_applicable_principles=principles,
        how_it_affects_va_claims=narrative,
        next_steps=next_steps,
        detected_elements=detected,
        principle_findings=findings,
        contradictions=contradictions,
        strengths=strengths,
        gaps=gaps,
        coverage_score=coverage_score,
        interpretation_source=source,
    )
