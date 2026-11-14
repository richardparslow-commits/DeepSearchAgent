"""Shared legal vocabulary for the VA claims research agent.

Single source of truth for the claim topics, authority tiers, and
statute/doctrine mappings used by the relevance, impact, and interpretation
layers. Keeping them here stops the keyword tables in those modules from
drifting apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


# ---------------------------------------------------------------------------
# Court authority taxonomy
# ---------------------------------------------------------------------------

COURT_SUPREME = "U.S. Supreme Court"
COURT_FEDERAL_CIRCUIT = "U.S. Court of Appeals for the Federal Circuit"
COURT_CAVC = "Court of Appeals for Veterans Claims"
COURT_BVA = "Board of Veterans' Appeals"
COURT_UNKNOWN = "Veterans law research result"

# Higher weight = higher binding authority over veterans compensation questions.
AUTHORITY_WEIGHTS = {
    COURT_SUPREME: 4,
    COURT_FEDERAL_CIRCUIT: 3,
    COURT_CAVC: 2,
    COURT_BVA: 1,
}

# Courts whose decisions bind the Board and VA adjudicators.
BINDING_COURTS = (COURT_SUPREME, COURT_FEDERAL_CIRCUIT, COURT_CAVC)
BOARD_COURT = COURT_BVA


def authority_weight_for(court: str) -> int:
    return AUTHORITY_WEIGHTS.get(court, 0)


# ---------------------------------------------------------------------------
# Claim topics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Topic:
    """One claim topic: detection phrases plus relevance-scoring synonyms."""

    name: str
    phrases: tuple[str, ...]
    # Issue phrase that triggers this topic's relevance synonym boost.
    # ``None`` for topics that are only detected from case text.
    keyword: str | None
    synonyms: tuple[str, ...]


TOPICS: tuple[Topic, ...] = (
    Topic(
        "service connection",
        ("service connection", "service-connected"),
        "service connection",
        ("service connection", "compensation", "nexus", "diagnosis"),
    ),
    Topic(
        "nexus",
        ("nexus", "medical opinion", "caused by service", "related to service"),
        None,
        (),
    ),
    Topic(
        "benefit of the doubt",
        ("benefit of the doubt", "reasonable doubt", "equipoise"),
        "benefit of the doubt",
        ("benefit of the doubt", "reasonable doubt", "doubtful"),
    ),
    Topic(
        "reasons and bases",
        ("reasons and bases", "adequate explanation", "statement of reasons"),
        "reasons and bases",
        ("reasons and bases", "reasoned decision", "adequate explanation"),
    ),
    Topic(
        "evidence evaluation",
        ("evidence", "competent evidence", "lay evidence", "medical evidence"),
        "evidence",
        ("competent evidence", "medical evidence", "lay evidence", "evidence"),
    ),
    Topic(
        "presumption",
        ("presumption", "presumptive"),
        "presumption",
        ("presumption", "presumptive service connection"),
    ),
    Topic(
        "rating",
        ("rating", "evaluation", "schedular"),
        "rating",
        ("rating", "evaluation", "schedular"),
    ),
    Topic(
        "aggravation",
        ("aggravation", "aggravated"),
        "aggravation",
        ("aggravation", "aggravated"),
    ),
    Topic(
        "duty to assist",
        ("duty to assist",),
        None,
        (),
    ),
)

TOPICS_BY_NAME = {topic.name: topic for topic in TOPICS}


@lru_cache(maxsize=None)
def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Compile a case-insensitive token-boundary matcher for *phrase*.

    A plain substring check makes ``rating`` match inside ``operating`` and
    ``frustrating``; word boundaries keep topic phrases from firing on those
    accidental overlaps. Hyphenated phrases such as ``service-connected``
    still match as a whole.
    """
    return re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE)


def phrase_matches(text: str, phrase: str) -> bool:
    """Return True when *phrase* occurs as a whole token sequence in *text*."""
    if not text or not phrase:
        return False
    return phrase_pattern(phrase).search(text) is not None


def topic_matches_issue(topic: Topic, issue: str) -> bool:
    """Return True when *issue* names *topic* by name, keyword, or phrase."""
    return any(
        phrase_matches(issue, phrase)
        for phrase in (topic.name, topic.keyword, *topic.phrases)
        if phrase
    )


# Priority order for impact issue tags (deliberately distinct from TOPICS
# order, which drives element detection).
ISSUE_TAG_ORDER: tuple[str, ...] = (
    "service connection",
    "benefit of the doubt",
    "reasons and bases",
    "evidence evaluation",
    "nexus",
    "rating",
)


# ---------------------------------------------------------------------------
# Statute / doctrine mappings
# ---------------------------------------------------------------------------

# Statute fragment -> doctrine hint, matched against extracted statute numbers.
STATUTE_HINTS: tuple[tuple[str, str], ...] = (
    ("5107", "the benefit-of-the-doubt rule under 38 U.S.C. § 5107(b)"),
    ("7104", "the reasons-and-bases requirement of 38 U.S.C. § 7104(d)(1)"),
    ("1110", "the core service-connection entitlement under 38 U.S.C. § 1110"),
    ("1131", "peacetime service connection under 38 U.S.C. § 1131"),
    ("5103", "the notice requirements of 38 U.S.C. § 5103"),
    ("5103A", "the duty to assist under 38 U.S.C. § 5103A"),
    ("3.303", "the service-connection framework of 38 C.F.R. § 3.303"),
    ("3.159", "the duty-to-assist regulation at 38 C.F.R. § 3.159"),
    ("4.1", "the rating-schedule requirements of 38 C.F.R. Part 4"),
)

# Doctrine hints that apply only to a specific first subsection. The keys
# remain section fragments for query expansion, but note matching must not
# treat § 5107(a) as § 5107(b) or § 7104(a) as § 7104(d)(1).
STATUTE_HINT_SUBSECTIONS: dict[str, tuple[str, ...]] = {
    "5107": ("b",),
    "7104": ("d",),
}

# Text phrase -> known veterans-law principle, scanned across case text.
PRINCIPLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "benefit of the doubt",
        "When the positive and negative evidence is in approximate balance (nearly "
        "equal, including but not limited to equipoise), the benefit of the doubt "
        "is given to the veteran (38 U.S.C. § 5107(b)).",
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
        "Competent lay evidence may establish observable symptoms and, in the "
        "circumstances Jandreau identifies, a diagnosis; it cannot substitute for "
        "medical evidence beyond lay competence.",
    ),
    (
        "medical evidence",
        "Where a determination requires medical expertise, the Board must rely on "
        "competent medical evidence and may not substitute its own medical judgment.",
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
