"""Source-reliability classification for search results.

Distinguishes official court/legal-data sources (CourtListener, the federal
appellate courts, the Board) from secondary sources (law-firm summaries, news,
and other third-party pages). The research pipeline stamps each retrieved case
with this tier so the report can prefer primary authority over commentary.
"""

from __future__ import annotations

from urllib.parse import urlparse

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


def _hostname(url: str) -> str:
    """Return the lowercased hostname from *url*, tolerating a missing scheme.

    ``urlparse`` leaves a scheme-less string (e.g. ``uscourts.cavc.gov/1``) in
    the path, so fall back to the leading path token (up to the first ``/``)
    as the host. ``hostname`` already strips userinfo and ports, and a colon
    in a scheme-less token would have been parsed as a scheme, so no further
    port handling is needed here. Returns ``""`` when no host can be found.
    """
    parsed = urlparse((url or "").strip())
    host = parsed.hostname
    if host:
        return host
    token = (parsed.netloc or parsed.path).split("/", 1)[0]
    return token.strip().lower()


def classify_source(url: str) -> str:
    """Return the reliability tier for a result URL.

    ``official`` for primary court/legal-data domains, ``secondary`` for any
    other non-empty URL (third-party commentary relative to the court), and
    ``unknown`` when there is no URL to classify.

    The hostname is matched against the official domains on a dot-boundary
    (exact match, or the host ends with ``.<domain>``), so a lookalike such as
    ``notva.gov`` or ``va.gov.bad.com`` -- which merely contain an official
    domain as a substring -- is never counted as official.
    """
    host = _hostname(url)
    if not host:
        return RELIABILITY_UNKNOWN
    if any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS):
        return RELIABILITY_OFFICIAL
    return RELIABILITY_SECONDARY
