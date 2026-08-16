"""Shared legal vocabulary for the VA claims research agent.

Single source of truth for the claim topics, authority tiers, and
statute/doctrine mappings used by the relevance, impact, and interpretation
layers. Keeping them here stops the keyword tables in those modules from
drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    # Issue substring that triggers this topic's relevance synonym boost.
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
    ("5103", "the notice-and-development duties of 38 U.S.C. § 5103A"),
    ("3.303", "the service-connection framework of 38 C.F.R. § 3.303"),
    ("3.159", "the duty-to-assist regulation at 38 C.F.R. § 3.159"),
    ("4.1", "the rating-schedule requirements of 38 C.F.R. Part 4"),
)

# Text phrase -> known veterans-law principle, scanned across case text.
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
