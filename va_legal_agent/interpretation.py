"""Interpretive analysis layer for VA claims guidance.

Turns ranked cases plus the claim issue into structured, explainable guidance:
legal elements detected in the issue, case-backed principles, strengths and
gaps in the retrieved authority, and actionable next steps. Output is research
support derived from public decisions, not legal advice.

When OPENAI_API_KEY is set, the narrative explanation is enhanced via the
optional LLM layer; otherwise a deterministic template summary is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import get_settings
from .fetch import extract_statutes
from .impact import detect_outcome
from .llm import interpret_cases, reason_cases
from .models import CaseRecord, ClaimElement, Contradiction, PrincipleFinding, StatuteOutcomeRow
from .topics import (
    BINDING_COURTS,
    COURT_SUPREME,
    PRINCIPLE_PATTERNS,
    TOPICS,
    phrase_matches,
    phrase_pattern,
)

@dataclass(frozen=True)
class ElementSpec:
    name: str
    phrases: tuple[str, ...]
    description: str
    guidance: str
    step: str
    # Analytical weight for the coverage-score denominator: a nexus finding
    # is the contested element in most VA claims, so it carries more scoring
    # weight than e.g. the duty-to-assist check.  Weights are relative within
    # whatever subset of elements is detected for a given issue.
    weight: float = 0.1


# Claimant guidance per topic, keyed by Topic.name. Each entry is
# (description, guidance, step, weight). The weight reflects how often this
# element is the *contested* question in VA claims practice: nexus is the
# battleground in ~40% of service-connection appeals, the umbrella
# service-connection topic drives most filings, and the procedural checks
# (duty to assist, reasons and bases) rarely decide cases on their own.
_ELEMENT_DETAILS: dict[str, tuple[str, str, str, float]] = {
    "service connection": (
        "The Caluza elements: a current diagnosed disability, an in-service "
        "event or aggravation, and a nexus linking the two.",
        "Assemble (1) a current diagnosis, (2) evidence of the in-service "
        "event, and (3) a medical nexus opinion connecting the two.",
        "Map the record to the three Caluza elements (diagnosis, in-service "
        "event, nexus) and identify which element is contested.",
        0.30,
    ),
    "nexus": (
        "A medical or factual link between the current disability and military service.",
        "Obtain a supporting medical nexus opinion that addresses the "
        "'at least as likely as not' standard.",
        "Secure a nexus opinion that explicitly applies the 'at least as likely as not' standard.",
        0.40,
    ),
    "benefit of the doubt": (
        "When the positive and negative evidence is in approximate balance (nearly "
        "equal, including but not limited to equipoise), the benefit of the doubt "
        "is given to the veteran (38 U.S.C. § 5107(b)).",
        "Highlight issues where favorable and unfavorable evidence is nearly equal "
        "(an approximate balance), and request that the benefit of the doubt be applied; "
        "do not require exact equipoise.",
        "Identify issues where the evidence is in approximate balance (nearly equal) "
        "and argue the benefit of the doubt.",
        0.10,
    ),
    "reasons and bases": (
        "The Board must provide an adequate statement of the reasons and bases "
        "for its decision (38 U.S.C. § 7104(d)(1)).",
        "Point out any findings the Board failed to explain or evidence it failed to address.",
        "Check the decision for findings that lack reasons-and-bases support.",
        0.05,
    ),
    "evidence evaluation": (
        "Evidence must be competent, credible, and properly weighed under VA standards.",
        "Gather competent evidence supporting the claim, and challenge any "
        "improper weighing of lay or medical evidence.",
        "Audit whether VA weighed the competent and lay evidence properly.",
        0.05,
    ),
    "presumption": (
        "Presumptive service connection may apply to certain conditions for qualifying veterans.",
        "Confirm whether the condition and service history qualify for any presumptions.",
        "Check whether presumptive service connection applies.",
        0.03,
    ),
    "rating": (
        "Disability evaluation applies the rating schedule to the symptoms and "
        "severity shown in the record.",
        "Compare the veteran's symptoms against the rating criteria to argue "
        "for the appropriate evaluation.",
        "Compare the symptoms against the rating schedule criteria.",
        0.02,
    ),
    "aggravation": (
        "A pre-existing condition aggravated by service may be service-connected.",
        "Document the pre-service baseline and the worsening during service.",
        "Document the pre-service baseline and the in-service worsening.",
        0.02,
    ),
    "duty to assist": (
        "VA has a duty to assist the claimant in developing evidence relevant to the claim.",
        "Identify records or exams that VA failed to obtain or provide.",
        "Identify any evidence that VA failed to help develop.",
        0.03,
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
        description, guidance, step, weight = _ELEMENT_DETAILS[topic.name]
        specs.append(ElementSpec(topic.name, topic.phrases, description, guidance, step, weight))
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
    coverage_confidence: str = "low"
    interpretation_source: str = "template"
    statute_outcome_matrix: list[StatuteOutcomeRow] = field(default_factory=list)


def _source_text(case: CaseRecord) -> str:
    """Return source-derived text for deterministic legal scans.

    Generated fields (``impact`` and ``deep_summary``) are deliberately
    excluded: they are derived from the case and must never be treated as
    independent evidence that the case addresses an element or principle.
    """
    return f"{case.title} {case.snippet} {case.holding}".lower()


_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|neither|nor|none|fail(?:ed|s|ing)?|"
    r"cannot|can't|isn't|wasn't|aren't|weren't|doesn't|don't|didn't|"
    r"inapplicable|unwarranted)\b",
    re.IGNORECASE,
)


def _phrase_is_affirmed(text: str, phrase: str) -> bool:
    """Return True when *phrase* occurs outside a negating context.

    This is intentionally conservative: a case that says "no nexus" or
    "the benefit of the doubt does not apply" is not treated as authority
    for the affirmative principle. The check only covers the current
    sentence, so an unrelated negation elsewhere in the holding does not
    erase a positive statement.
    """
    for match in phrase_pattern(phrase).finditer(text):
        index = match.start()
        before = re.split(r"[.;!?]", text[max(0, index - 80) : index])[-1]
        after = re.split(
            r"[.;!?]", text[index + len(phrase) : index + len(phrase) + 80]
        )[0]
        if not _NEGATION_RE.search(before) and not _NEGATION_RE.search(after):
            return True
    return False


# Dispositions that favor the claimant position vs. go against it, used by the
# always-on deterministic contradiction detector. ``vacated``/``remanded``
# reopen a denial and ``granted`` awards relief; ``affirmed``/``dismissed``/
# ``denied`` uphold or reject it. This is deliberately conservative: only
# explicit, single-direction outcomes trigger a flag.
_FAVORABLE_OUTCOMES = frozenset({"granted", "vacated", "remanded"})
_UNFAVORABLE_OUTCOMES = frozenset({"denied", "affirmed", "dismissed"})

# Every disposition is read from the appellant's point of view, so when the
# Secretary appealed, the veteran's direction is the opposite of the
# Secretary's. The common posture is a Secretary appeal of a favorable Board
# decision: "affirmed"/"dismissed"/"denied" then uphold the veteran's grant,
# while "granted"/"vacated"/"remanded" disturb it.


def _outcome_direction(outcome: str, appellant_role: str = "") -> int:
    """Classify an outcome as favorable (+1), unfavorable (-1), or unknown (0).

    An explicit appellant role is required for a substantive classification.
    An empty or unknown role returns 0 rather than assuming the veteran
    appealed: ``affirmed`` means opposite things
    depending on which party appealed, so guessing would silently flip the
    result. When the role is ``"secretary"``, every disposition is read from
    the appellant's point of view, so a denial of the Secretary's appeal
    upholds the veteran's grant, while a grant or vacatur goes against the
    veteran.

    ``extract_outcome`` joins multiple signals with " and " (e.g. "vacated and
    remanded"). A compound that mixes directions ("granted and denied"), a
    partial disposition ("granted in part"), or an unrecognized signal
    resolves to 0 rather than guessing, so the deterministic detector never
    flags on ambiguous postures.
    """
    role = (appellant_role or "").lower()
    if role not in {"veteran", "secretary"}:
        return 0

    signals = [
        signal.strip().lower()
        for signal in (outcome or "").split(" and ")
        if signal.strip()
    ]
    if not signals:
        return 0

    is_secretary = role == "secretary"
    directions: set[int] = set()
    for signal in signals:
        # "granted in part" is not a full grant; do not classify it as one.
        if "in part" in signal:
            return 0
        name = signal.split(" ", 1)[0]
        if name in _FAVORABLE_OUTCOMES:
            direction = 1
        elif name in _UNFAVORABLE_OUTCOMES:
            direction = -1
        else:
            return 0
        directions.add(-direction if is_secretary else direction)
    if len(directions) == 1:
        return directions.pop()
    return 0


def _case_statutes(case: CaseRecord) -> list[str]:
    """Return the case's cited statutes, scanning source text when unenriched."""
    return list(case.statutes) or extract_statutes(_source_text(case))


def _case_element_names(case: CaseRecord) -> set[str]:
    """Return elements affirmatively addressed in the case's source text."""
    text = _source_text(case)
    return {
        spec.name
        for spec in detect_claim_elements(text)
        if any(_phrase_is_affirmed(text, phrase) for phrase in spec.phrases)
    }


def detect_contradictions(cases: list[CaseRecord]) -> list[Contradiction]:
    """Deterministically flag opposite outcomes on a shared statute and element.

    The always-on complement to the LLM reasoning pass: pairs of retrieved
    authorities that cite the same statute and affirmatively address the same
    claim element but reach opposite outcomes (favorable vs. unfavorable to the
    claimant) are surfaced as contradictions so the report never silently picks
    one side. A shared statute alone is not enough: different issues or elements
    under the same statute are not a conflict. The outcome prefers the enriched
    field and falls back to the title/holding text (mirroring
    :func:`impact.detect_outcome`), matching the statute fallback; unknown or
    mixed postures never trigger a flag.
    """
    outcomes = [detect_outcome(case) for case in cases]
    directions = [
        _outcome_direction(outcome, case.appellant_role)
        for outcome, case in zip(outcomes, cases)
    ]
    contradictions: list[Contradiction] = []
    seen: set[tuple[str, str]] = set()
    for i, first in enumerate(cases):
        first_direction = directions[i]
        if first_direction == 0:
            continue
        # A superseded case is never part of a live contradiction — its
        # opposite outcome is expected because a later decision overruled it.
        if first.superseded_by:
            continue
        first_statutes = set(_case_statutes(first))
        if not first_statutes:
            continue
        first_elements = _case_element_names(first)
        if not first_elements:
            continue
        for j, second in enumerate(cases[i + 1 :], start=i + 1):
            second_direction = directions[j]
            if second_direction == 0 or second_direction == first_direction:
                continue
            # Skip pairs where the second case is also superseded.
            if second.superseded_by:
                continue
            shared = first_statutes & set(_case_statutes(second))
            if not shared:
                continue
            shared_elements = first_elements & _case_element_names(second)
            if not shared_elements:
                continue
            key = tuple(sorted((first.title, second.title)))
            if key in seen:
                continue
            seen.add(key)
            statute = sorted(shared)[0]
            element = sorted(shared_elements)[0]
            contradictions.append(
                Contradiction(
                    statement=(
                        f"{first.title} and {second.title} reach opposite outcomes on "
                        f"{statute} regarding {element}: {outcomes[i]} versus {outcomes[j]}."
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
    """Return the element specs whose detection phrases appear in the claim issue.

    No fallback is applied: a bare condition such as "tinnitus" or a
    non-service-connection benefit such as Aid and Attendance must not be
    expanded into every element in the library. When nothing matches, the
    caller should surface an unclassified issue rather than inventing
    Caluza elements that do not govern the claim.
    """
    text = issue.lower()
    return [
        spec
        for spec in ELEMENT_LIBRARY
        if any(phrase_matches(text, phrase) for phrase in spec.phrases)
    ]


def extract_principle_findings(cases: list[CaseRecord]) -> list[PrincipleFinding]:
    """Scan source text for affirmed principle phrases."""
    findings: list[PrincipleFinding] = []
    for phrase, principle in PRINCIPLE_PATTERNS:
        sources = [
            case.title
            for case in cases
            if _phrase_is_affirmed(_source_text(case), phrase)
        ]
        if sources:
            findings.append(PrincipleFinding(principle=principle, source_cases=sources))
    return findings


def _cases_covering_element(spec: ElementSpec, cases: list[CaseRecord]) -> list[str]:
    return [
        case.title
        for case in cases
        if any(_phrase_is_affirmed(_source_text(case), phrase) for phrase in spec.phrases)
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


# ── Statute × Court × Outcome matrix ────────────────────────────────────────

_COURT_ORDER = ["Board of Veterans' Appeals", "Court of Appeals for Veterans Claims",
                "U.S. Court of Appeals for the Federal Circuit", COURT_SUPREME]


def _court_sort_key(court: str) -> int:
    """Map a court name to a sort priority (lower = higher authority)."""
    try:
        return _COURT_ORDER.index(court)
    except ValueError:
        return len(_COURT_ORDER)  # unknown courts sort last


def build_statute_outcome_matrix(cases: list[CaseRecord]) -> list[StatuteOutcomeRow]:
    """Build a statute × court × outcome matrix from the retrieved cases.

    Groups cases by (statute, court) and counts favorable (+1), unfavorable
    (-1), and unknown (0) outcomes using the same party-aware direction
    classifier as the contradiction detector. Statutes are extracted from
    the enriched field first, falling back to text scanning. Unknown or
    mixed-outcome cases count toward the ``unknown`` column.

    The result is sorted: by court in ascending authority order (BVA first,
    SCOTUS last), then by statute name lexicographically, so the matrix
    reads naturally as a hierarchy.
    """
    if not cases:
        return []
    outcomes = [detect_outcome(case) for case in cases]
    directions = [
        _outcome_direction(outcome, case.appellant_role)
        for outcome, case in zip(outcomes, cases)
    ]
    # (statute, court) → [direction_int, ...]
    buckets: dict[tuple[str, str], list[int]] = {}
    for case, direction in zip(cases, directions):
        for statute in _case_statutes(case):
            key = (statute, case.court)
            buckets.setdefault(key, []).append(direction)

    rows: list[StatuteOutcomeRow] = []
    for (statute, court), dirs in sorted(buckets.items(),
                                          key=lambda kv: (_court_sort_key(kv[0][1]), kv[0][0])):
        rows.append(StatuteOutcomeRow(
            statute=statute,
            court=court,
            favorable=sum(1 for d in dirs if d == 1),
            unfavorable=sum(1 for d in dirs if d == -1),
            unknown=sum(1 for d in dirs if d == 0),
        ))
    return rows


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
        # A bare or non-service-connection issue can have no detected elements;
        # say so explicitly instead of falling back to the full Caluza library.
        element_text = (
            f"Key elements to establish: {'; '.join(spec.name for spec in elements)}. "
            if elements
            else "No claim elements were detected from the issue. "
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
                weight=spec.weight,
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

    if not detected:
        gaps.insert(
            0,
            "No claim elements were detected from the issue; specify the benefit type or claim element.",
        )

    # A bare or non-service-connection issue can yield no detected elements;
    # coverage is then 0.0 rather than padded with unrelated claim elements.
    covered_weight = sum(element.weight for element in detected if element.covered_by)
    total_weight = sum(element.weight for element in detected)
    coverage_score = covered_weight / total_weight if total_weight > 0 else 0.0

    # ── Confidence heuristics ───────────────────────────────────────────
    next_steps = [spec.step for spec in element_specs]
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

    # ── Coverage confidence ─────────────────────────────────────────────
    # Volume alone is not confidence. High confidence requires a binding,
    # precedential decision, meaningful element coverage, and at least one
    # element supported by multiple cases. An all-BVA or all-non-precedential
    # corpus cannot be high-confidence authority.
    binding_cases = [
        case
        for case in cases
        if case.precedential is True and case.court in BINDING_COURTS
    ]
    multi_case_support = any(
        len(element.covered_by) >= 2 for element in detected
    )
    if not binding_cases or coverage_score <= 0.0:
        coverage_confidence = "low"
    elif (
        len(cases) >= 10
        and coverage_score >= 0.60
        and multi_case_support
        and any(case.holding for case in cases)
    ):
        coverage_confidence = "high"
    elif coverage_score >= 0.30 and (
        len(cases) >= 5 or (len(cases) >= 3 and binding_cases)
    ):
        coverage_confidence = "medium"
    else:
        coverage_confidence = "low"

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
        coverage_confidence=coverage_confidence,
        interpretation_source=source,
        statute_outcome_matrix=build_statute_outcome_matrix(cases),
    )
