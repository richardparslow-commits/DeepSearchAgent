"""Nuanced legal-impact analysis layer.

Turns one case record into a structured impact profile: which claim topics the
ruling touches, its procedural posture, the statutory anchors it engages, and
the weight of its authority. The profile's ``nuance`` narrative backs
``agent.summarize_case_impact``. Output is research support derived from
public decisions, not legal advice.
"""

from __future__ import annotations

from .fetch import extract_outcome, extract_statutes
from .models import CaseRecord, ImpactProfile

# Detection phrase -> reported tag, checked in priority order.
ISSUE_TAG_PATTERNS: tuple[tuple[str, str], ...] = (
    ("service connection", "service connection"),
    ("benefit of the doubt", "benefit of the doubt"),
    ("reasons and bases", "reasons and bases"),
    ("evidence", "evidence evaluation"),
    ("nexus", "nexus"),
    ("rating", "rating"),
)

# Procedural-posture notes keyed by the leading outcome signal. Phrasing
# deliberately hyphenates doctrine names so synthesized notes do not register
# as case-backed principle phrases downstream.
OUTCOME_NOTES: dict[str, str] = {
    "vacated": (
        "The decision below was vacated, signaling a legal or procedural defect "
        "that can support arguments for relief."
    ),
    "remanded": (
        "The matter was remanded for further development, indicating the record or "
        "explanation was insufficient rather than the claim failing outright."
    ),
    "affirmed": (
        "The decision below was affirmed, reinforcing the approach taken by the "
        "earlier adjudicator."
    ),
    "dismissed": (
        "The appeal was dismissed, which may reflect jurisdictional limits rather "
        "than the merits of the claim."
    ),
    "granted": "The request was granted, which can support the theory of entitlement discussed.",
    "denied": "The request was denied, which may counsel caution when relying on the theory discussed.",
}

# Statute fragment -> doctrine hint.
STATUTE_HINTS: tuple[tuple[str, str], ...] = (
    ("5107", "the benefit-of-the-doubt rule under 38 U.S.C. § 5107(b)"),
    ("7104", "the reasons-and-bases requirement of 38 U.S.C. § 7104(d)(1)"),
    ("1110", "the core service-connection entitlement under 38 U.S.C. § 1110"),
    ("1131", "peacetime service connection under 38 U.S.C. § 1131"),
    ("5103", "the notice-and-development duties of 38 U.S.C. § 5103A"),
    ("3.303", "the service-connection framework of 38 C.F.R. § 3.303"),
    ("3.159", "the duty-to-assist regulation at 38 C.F.R. § 3.159"),
    ("4.1", "the rating-schedule requirements of 38 C.F.R. Part 4"),
)

BOILERPLATE = (
    "It underscores that the agency must apply the governing standards carefully, "
    "explain the basis for the decision, and assess the evidence in a way that is "
    "consistent with veterans-law principles and reviewable on appeal."
)

BINDING_COURTS = (
    "U.S. Supreme Court",
    "U.S. Court of Appeals for the Federal Circuit",
    "Court of Appeals for Veterans Claims",
)
BOARD_COURT = "Board of Veterans' Appeals"


def _impact_text(case: CaseRecord) -> str:
    return f"{case.title} {case.snippet} {case.holding} {case.impact}".lower()


def detect_issue_tags(case: CaseRecord) -> list[str]:
    """Return the claim topics the case touches, in priority order."""
    text = _impact_text(case)
    tags = [tag for phrase, tag in ISSUE_TAG_PATTERNS if phrase in text]
    if not tags:
        tags.append(case.issue.lower() or "the factual and legal issue")
    return tags


def detect_outcome(case: CaseRecord) -> str:
    """Return the procedural posture, preferring the enriched outcome field.

    Fallback scanning is limited to title/holding: snippet text often describes
    lower-level actions rather than the ruling's own disposition.
    """
    if case.outcome:
        return case.outcome
    return extract_outcome(f"{case.title} {case.holding}".lower())


def outcome_note_for(outcome: str) -> str:
    """Map an outcome string to its nuance note via the leading signal."""
    first_signal = outcome.split(" ", 1)[0].lower() if outcome else ""
    return OUTCOME_NOTES.get(first_signal, "")


def detect_statutes(case: CaseRecord) -> list[str]:
    """Return cited statutes, preferring enrichment and scanning text otherwise."""
    return list(case.statutes) or extract_statutes(_impact_text(case))


def statute_note_for(statutes: list[str]) -> str:
    """Build the statutory-anchor note for the given statutes."""
    hints = [hint for key, hint in STATUTE_HINTS if any(key in statute for statute in statutes)]
    if hints:
        joined = hints[0] + (f" and {hints[1]}" if len(hints) > 1 else "")
        return f"The analysis engages {joined}, which can anchor briefing."
    if statutes:
        return f"The ruling cites {', '.join(statutes[:3])}, which may provide statutory anchors."
    return ""


def authority_note_for(court: str) -> str:
    """Describe the weight of authority for the issuing body."""
    if court in BINDING_COURTS:
        return "As appellate precedent, this ruling is binding on the Board and VA adjudicators."
    if court == BOARD_COURT:
        return "As a Board-level decision, this ruling is persuasive but not binding precedent."
    return "The issuing body is unidentified, so treat this ruling as persuasive only."


def analyze_case_impact(case: CaseRecord) -> ImpactProfile:
    """Build a structured, nuanced impact profile for one case."""
    tags = detect_issue_tags(case)
    outcome = detect_outcome(case)
    statutes = detect_statutes(case)
    outcome_note = outcome_note_for(outcome)
    statute_note = statute_note_for(statutes)
    authority_note = authority_note_for(case.court)

    parts = [f"This ruling is relevant to {', '.join(tags[:3])} in VA compensation claims."]
    if outcome_note:
        parts.append(f"Procedural posture: {outcome_note}")
    if statute_note:
        parts.append(statute_note)
    parts.append(authority_note)
    parts.append(BOILERPLATE)

    return ImpactProfile(
        issue_tags=tags,
        outcome=outcome,
        outcome_note=outcome_note,
        statutes=statutes,
        statute_note=statute_note,
        authority_note=authority_note,
        nuance=" ".join(parts),
    )