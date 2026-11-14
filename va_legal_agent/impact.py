"""Nuanced legal-impact analysis layer.

Turns one case record into a structured impact profile: which claim topics the
ruling touches, its procedural posture, the statutory anchors it engages, and
the weight of its authority. The profile's ``nuance`` narrative backs
``agent.summarize_case_impact``. Output is research support derived from
public decisions, not legal advice.
"""

from __future__ import annotations

import re

from .fetch import extract_outcome, extract_statutes
from .models import CaseRecord, ImpactProfile
from .topics import (
    BINDING_COURTS,
    BOARD_COURT,
    ISSUE_TAG_ORDER,
    STATUTE_HINTS,
    STATUTE_HINT_SUBSECTIONS,
    TOPICS_BY_NAME,
    phrase_matches,
)

# Detection phrase -> reported tag, derived from the shared topic table in
# topics.py, checked in priority order.
ISSUE_TAG_PATTERNS: tuple[tuple[str, str], ...] = tuple(
    (phrase, name)
    for name in ISSUE_TAG_ORDER
    for phrase in TOPICS_BY_NAME[name].phrases
)

# Procedural-posture notes keyed by the leading outcome signal. Phrasing
# deliberately hyphenates doctrine names so synthesized notes do not register
# as case-backed principle phrases downstream.
_FAVORABLE_OUTCOMES = frozenset({"granted", "vacated", "remanded"})
_UNFAVORABLE_OUTCOMES = frozenset({"denied", "affirmed", "dismissed"})

MIXED_OUTCOME_NOTE = (
    "The decision has a mixed disposition, so its effect is not classified as a "
    "single favorable or unfavorable outcome."
)

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

# Party-aware overrides: when the Secretary appealed, every disposition is
# read from the appellant's point of view, so the veteran's direction is the
# opposite of the Secretary's. These notes replace the default phrasing above
# so the impact narrative reflects the actual claimant position.
_SECRETARY_OVERRIDES: dict[str, str] = {
    "affirmed": (
        "The Secretary's cross-appeal was affirmed, meaning the veteran's "
        "favorable outcome below stands."
    ),
    "dismissed": (
        "The Secretary's cross-appeal was dismissed, meaning the veteran's "
        "favorable outcome below stands."
    ),
    "denied": (
        "The Secretary's appeal was denied, meaning the veteran's favorable "
        "outcome below stands."
    ),
    "granted": (
        "The Secretary's appeal was granted, so the favorable outcome below "
        "was reversed."
    ),
    "vacated": (
        "The Secretary's appeal resulted in vacatur of the favorable outcome "
        "below."
    ),
    "remanded": (
        "The Secretary's appeal resulted in a remand, so the favorable "
        "outcome below is not final."
    ),
}

BOILERPLATE = (
    "It underscores that the agency must apply the governing standards carefully, "
    "explain the basis for the decision, and assess the evidence in a way that is "
    "consistent with veterans-law principles and reviewable on appeal."
)


def _impact_text(case: CaseRecord) -> str:
    return f"{case.title} {case.snippet} {case.holding} {case.impact}".lower()


def detect_issue_tags(case: CaseRecord) -> list[str]:
    """Return the claim topics the case touches, in priority order."""
    text = _impact_text(case)
    tags: list[str] = []
    for phrase, tag in ISSUE_TAG_PATTERNS:
        if tag not in tags and phrase_matches(text, phrase):
            tags.append(tag)
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


def _outcome_signals(outcome: str) -> list[str]:
    """Split ``extract_outcome``'s ``" and "`` join into normalized signals."""
    return [
        signal.strip().lower()
        for signal in (outcome or "").split(" and ")
        if signal.strip()
    ]


def outcome_note_for(outcome: str, appellant_role: str = "") -> str:
    """Map an outcome string to its nuance note.

    An explicit appellant role is required for a substantive note: ``affirmed``
    means opposite things when the veteran and the Secretary appeal, so an
    empty or unknown role is not guessed as the veteran. Compound outcomes are
    classified together; a mixed compound gets :data:`MIXED_OUTCOME_NOTE`
    instead of silently using its first signal.
    """
    role = (appellant_role or "").lower()
    if role not in {"veteran", "secretary"}:
        return ""

    signals = _outcome_signals(outcome)
    if not signals:
        return ""

    notes: list[str] = []
    directions: set[int] = set()
    for signal in signals:
        # "granted in part" is not a full grant; classify the compound as mixed.
        if "in part" in signal:
            return MIXED_OUTCOME_NOTE
        name = signal.split(" ", 1)[0]
        if name in _FAVORABLE_OUTCOMES:
            direction = 1
        elif name in _UNFAVORABLE_OUTCOMES:
            direction = -1
        else:
            return ""
        direction = -direction if role == "secretary" else direction
        directions.add(direction)
        note = _SECRETARY_OVERRIDES[name] if role == "secretary" else OUTCOME_NOTES[name]
        if note not in notes:
            notes.append(note)

    if len(directions) != 1:
        return MIXED_OUTCOME_NOTE
    return " ".join(notes)


def detect_statutes(case: CaseRecord) -> list[str]:
    """Return cited statutes, preferring enrichment and scanning text otherwise."""
    return list(case.statutes) or extract_statutes(_impact_text(case))


def _statute_hint_matches(fragment: str, statute: str) -> bool:
    """Match a statute fragment as a section token, not a substring.

    A plain substring check makes ``5103`` match inside ``5103A``; section
    boundaries keep the notice and duty-to-assist provisions distinct. For
    hints tied to a specific subsection, the first parenthetical must match:
    ``5107(a)`` is not the benefit-of-the-doubt rule in ``5107(b)``, and
    ``7104(a)`` is not the reasons-and-bases requirement in ``7104(d)(1)``.
    """
    pattern = (
        rf"(?<![0-9A-Za-z.]){re.escape(fragment)}(?![0-9A-Za-z.])"
        r"(?:\(([0-9A-Za-z]+)\))?"
    )
    matches = list(re.finditer(pattern, statute))
    if not matches:
        return False
    allowed = STATUTE_HINT_SUBSECTIONS.get(fragment)
    if not allowed:
        return True
    return any(
        match.group(1) is not None and match.group(1).lower() in allowed
        for match in matches
    )


def statute_note_for(statutes: list[str]) -> str:
    """Build the statutory-anchor note for the given statutes."""
    hints = [
        hint
        for key, hint in STATUTE_HINTS
        if any(_statute_hint_matches(key, statute) for statute in statutes)
    ]
    if hints:
        joined = hints[0] + (f" and {hints[1]}" if len(hints) > 1 else "")
        return f"The analysis engages {joined}, which can anchor briefing."
    if statutes:
        return f"The ruling cites {', '.join(statutes[:3])}, which may provide statutory anchors."
    return ""


def authority_note_for(court: str, precedential: bool | None = True) -> str:
    """Describe the weight of authority for the issuing body.

    ``precedential=False`` demotes an otherwise binding appellate court to
    persuasive authority. ``None`` means the status could not be verified, so
    the note asks the reader to check it rather than calling the decision
    binding. BVA decisions remain non-binding regardless of the flag.
    """
    if court in BINDING_COURTS:
        if precedential is False:
            return (
                "As a non-precedential appellate decision, this ruling is persuasive only "
                "and is not binding on the Board or VA adjudicators."
            )
        if precedential is None:
            return (
                "The precedential status of this appellate decision is unknown; verify its "
                "publication status before relying on it as binding."
            )
        return "As appellate precedent, this ruling is binding on the Board and VA adjudicators."
    if court == BOARD_COURT:
        return "As a Board-level decision, this ruling is persuasive but not binding precedent."
    return "The issuing body is unidentified, so treat this ruling as persuasive only."


def analyze_case_impact(case: CaseRecord) -> ImpactProfile:
    """Build a structured, nuanced impact profile for one case."""
    tags = detect_issue_tags(case)
    outcome = detect_outcome(case)
    statutes = detect_statutes(case)
    outcome_note = outcome_note_for(outcome, case.appellant_role)
    statute_note = statute_note_for(statutes)
    authority_note = authority_note_for(case.court, case.precedential)

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
