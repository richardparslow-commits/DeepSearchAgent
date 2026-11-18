"""Source-reliability and authority-tier classification for search results.

Two related labels are produced for every retrieved authority:

* ``source_reliability`` says whether the *location* is an official court/agency
  domain, a third-party page, or unknown.
* ``authority_tier`` combines that location with the court and precedential
  status so a consumer can distinguish binding primary authority from
  persuasive primary authority, agency guidance, secondary commentary, and
  unverified records.

Neither label establishes that a decision is good law. Citator and appellate
history remain separate, advisory evidence layers.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .models import AuthorityReliabilitySummary
from .topics import BINDING_COURTS, BOARD_COURT, COURT_SUPREME

RELIABILITY_OFFICIAL = "official"
RELIABILITY_SECONDARY = "secondary"
RELIABILITY_UNKNOWN = "unknown"

AUTHORITY_BINDING = "official_primary_binding"
AUTHORITY_PERSUASIVE = "official_primary_persuasive"
AUTHORITY_AGENCY_GUIDANCE = "official_agency_guidance"
AUTHORITY_SECONDARY = "secondary_summary"
AUTHORITY_UNVERIFIED = "unverified"

AUTHORITY_TIERS = (
    AUTHORITY_BINDING,
    AUTHORITY_PERSUASIVE,
    AUTHORITY_AGENCY_GUIDANCE,
    AUTHORITY_SECONDARY,
    AUTHORITY_UNVERIFIED,
)

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

# Official agency guidance lives on va.gov but is not a court decision.
# Entries are hostnames only: "va.gov/vetapp" is a path, never a hostname,
# so it could never match and only obscured the intent.
_AGENCY_GUIDANCE_HOSTS = ("va.gov", "benefits.va.gov")


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


def classify_authority_tier(
    url: str,
    court: str = "",
    precedential: bool | None = None,
) -> tuple[str, bool]:
    """Classify one authority into a tier and an official-source boolean.

    A secondary URL can never become binding merely because its ``court``
    string names a court. An official primary URL is binding only when the
    court is binding and the decision is known to be precedential, except for
    the Supreme Court, whose published decisions are binding without a lower
    court's publication designation. Unknown publication status is treated as
    persuasive, never silently upgraded to binding.
    """
    reliability = classify_source(url)
    if reliability == RELIABILITY_UNKNOWN:
        return AUTHORITY_UNVERIFIED, False
    if reliability == RELIABILITY_SECONDARY:
        return AUTHORITY_SECONDARY, False

    normalized_court = (court or "").strip()
    if normalized_court == COURT_SUPREME:
        return AUTHORITY_BINDING, True
    if normalized_court == BOARD_COURT:
        return AUTHORITY_PERSUASIVE, True
    if normalized_court in BINDING_COURTS:
        if precedential is True:
            return AUTHORITY_BINDING, True
        return AUTHORITY_PERSUASIVE, True

    host = _hostname(url)
    if host == "courtlistener.com" or host.endswith(".courtlistener.com"):
        # CourtListener mirrors court opinions without an official court
        # label here; it is persuasive (official mirror), never agency
        # guidance.
        return AUTHORITY_PERSUASIVE, True
    if host == "va.gov" or host.endswith(".va.gov"):
        # va.gov hosts both BVA decisions and agency guidance. A court label
        # already returned above; everything else is agency material.
        if any(token in host for token in _AGENCY_GUIDANCE_HOSTS):
            return AUTHORITY_AGENCY_GUIDANCE, True
    return AUTHORITY_AGENCY_GUIDANCE, True


def authority_label(tier: str, is_official: bool) -> str:
    """Return the operator-facing Official/Secondary badge for a tier."""
    if tier == AUTHORITY_BINDING:
        return "Official Court Transcript / Primary Authority (binding)"
    if tier == AUTHORITY_PERSUASIVE:
        return "Official Court Transcript / Primary Authority (persuasive)"
    if tier == AUTHORITY_AGENCY_GUIDANCE:
        return "Official Agency Guidance / Primary Authority"
    if is_official:
        return "Official Primary Authority"
    if tier == AUTHORITY_SECONDARY:
        return "Secondary Summary / Non-Binding Source"
    return "Unverified Source"


def summarize_authority_reliability(tiers: list[str]) -> AuthorityReliabilitySummary:
    """Aggregate tier labels into a report-level reliability summary."""
    counts = {tier: 0 for tier in AUTHORITY_TIERS}
    for tier in tiers:
        normalized = tier if tier in counts else AUTHORITY_UNVERIFIED
        counts[normalized] += 1

    official = sum(
        count
        for tier, count in counts.items()
        if tier
        in {AUTHORITY_BINDING, AUTHORITY_PERSUASIVE, AUTHORITY_AGENCY_GUIDANCE}
    )
    secondary = counts[AUTHORITY_SECONDARY]
    unknown = counts[AUTHORITY_UNVERIFIED]
    return AuthorityReliabilitySummary(
        official_count=official,
        secondary_count=secondary,
        unknown_count=unknown,
        official_primary_binding_count=counts[AUTHORITY_BINDING],
        official_primary_persuasive_count=counts[AUTHORITY_PERSUASIVE],
        official_agency_guidance_count=counts[AUTHORITY_AGENCY_GUIDANCE],
        secondary_summary_count=secondary,
        unverified_count=unknown,
        tier_counts=counts,
    )
