"""Source-reliability classification for search results.

Distinguishes official court/legal-data sources (CourtListener, the federal
appellate courts, the Board) from secondary sources (law-firm summaries, news,
and other third-party pages). The research pipeline stamps each retrieved case
with this tier so the report can prefer primary authority over commentary.
"""

from __future__ import annotations

RELIABILITY_OFFICIAL = "official"
RELIABILITY_SECONDARY = "secondary"
RELIABILITY_UNKNOWN = "unknown"

# Domains that host primary court decisions or structured court data.
OFFICIAL_DOMAINS: tuple[str, ...] = (
    "courtlistener.com",
    "uscourts.cavc.gov",
    "cafc.uscourts.gov",
    "caafc.uscourts.gov",
    "supremecourt.gov",
    "bva.va.gov",
    "va.gov",
)


def classify_source(url: str) -> str:
    """Return the reliability tier for a result URL.

    ``official`` for primary court/legal-data domains, ``secondary`` for any
    other non-empty URL (third-party commentary relative to the court), and
    ``unknown`` when there is no URL to classify.
    """
    normalized = (url or "").strip().lower()
    if not normalized:
        return RELIABILITY_UNKNOWN
    if any(domain in normalized for domain in OFFICIAL_DOMAINS):
        return RELIABILITY_OFFICIAL
    return RELIABILITY_SECONDARY
