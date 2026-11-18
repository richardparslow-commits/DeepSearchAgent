"""Official-source authority verification.

The research pipeline can cite three kinds of authority that are cheap to
cross-check against primary government sources:

* CAVC decisions: the court's official **Panel Opinions** list tells us
  whether a docket is a panel/full-court opinion or a Rule 30(a) single-judge
  disposition.
* Regulations: eCFR confirms that a cited 38 C.F.R. section exists, and the
  Federal Register API identifies recent rules affecting the same CFR part.
* Statutes and Federal Circuit opinions: GovInfo's official U.S. Code and
  U.S. Courts Opinions collections provide a second cross-check.

Every network call is best-effort. Failures become ``AuthorityCheck`` records
with an ``unavailable`` status instead of aborting an analysis, and all
verification is opt-in through configuration so the offline path stays
network-free.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from .config import get_settings
from .models import (
    AuthorityCheck,
    CaseRecord,
    GovInfoPublicLaw,
    GovInfoStatute,
    LegalAnalysis,
    StatutorySupersessionSignal,
)
from .redaction import redact_secrets, sanitize_exception
from .search import SearchError, http_proxy_kwargs
from .topics import COURT_CAVC, COURT_FEDERAL_CIRCUIT

CAVC_PANEL_OPINIONS_URL = "https://www.uscourts.cavc.gov/opinions.php?fullsite=yes"
ECFR_TITLES_URL = "https://www.ecfr.gov/api/versioner/v1/titles.json"
ECFR_FULL_URL = "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-38.xml"
ECFR_SECTION_URL = "https://www.ecfr.gov/current/title-38/section-{section}"
FEDERAL_REGISTER_DOCUMENTS_URL = "https://www.federalregister.gov/api/v1/documents.json"
GOVINFO_SEARCH_URL = "https://api.govinfo.gov/search"
GOVINFO_HOME_URL = "https://www.govinfo.gov/"
GOVINFO_DETAILS_URL = "https://www.govinfo.gov/app/details/{package_id}"
GOVINFO_CONTENT_URL = (
    "https://www.govinfo.gov/content/pkg/{package_id}/html/{file_id}.htm"
)
FR_HOME_URL = "https://www.federalregister.gov/"

_MAX_GOVINFO_RECORDS = 2
_GOVINFO_TEXT_EXCERPT_LIMIT = 1_200
_PLAW_PACKAGE_RE = re.compile(r"^PLAW-(\d{1,3})publ-?(\d+)$", re.IGNORECASE)
_STATUTE_PACKAGE_RE = re.compile(r"^STATUTE-(\d+)$", re.IGNORECASE)
_STATUTE_PAGE_RE = re.compile(r"-Pg(\d+)", re.IGNORECASE)

# Explicit actions that can make a cited provision no longer current. These
# are used only to raise a review candidate; an amendment alone is not a
# supersession signal, and no source result is treated as a final label.
_SUPERSESSION_ACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("repealed", re.compile(r"\brepeal(?:e|ed|es|ing)?\b", re.I)),
    ("removed", re.compile(r"\bremov(?:e|ed|es|ing|al)\b", re.I)),
    ("reserved", re.compile(r"\breserv(?:e|ed|es|ing|ation)\b", re.I)),
    ("renumbered", re.compile(r"\brenumber(?:ed|ing|s)?\b", re.I)),
    ("redesignated", re.compile(r"\bredesignat(?:e|ed|es|ing|ion)\b", re.I)),
    ("superseded", re.compile(r"\b(?:supersed(?:e|ed|es|ing)|supersession)\b", re.I)),
)
_REPLACEMENT_RE = re.compile(
    r"(?:redesignat(?:ed|ion)|renumber(?:ed|ing)|moved|transferred|"
    r"replac(?:ed|ement)|supersed(?:ed|es))\s+"
    r"(?:as|to|by|with)\s+"
    r"(?:38\s*C\.?\s*F\.?\s*R\.?\s*(?:§\s*)?)?"
    r"(\d{1,2}\.\d{1,4}[A-Za-z]?)(?!\d)",
    re.I,
)

# Docket numbers in CAVC PDF links are often attached to a filename with an
# underscore (``Jones_20-8342(E).pdf``), so a word boundary is too strict.
_CAVC_DOCKET_RE = re.compile(
    r"(?<!\d)(\d{2})-(\d{3,5})(?:\(E\))?(?!\d)", re.IGNORECASE
)
_CFR_SECTION_RE = re.compile(
    r"\b38\s*(?:C\.\s*F\.\s*R\.?|CFR)\s*(?:§§?|sections?)?\s*"
    r"(\d{1,2}\.\d{1,4}[A-Za-z]?)",
    re.IGNORECASE,
)
_USC_SECTION_RE = re.compile(
    r"\b38\s*U\.?\s*S\.?\s*C\.?\s*(?:§§?|sections?)?\s*"
    r"(\d{1,4}[A-Za-z]?(?:\([A-Za-z0-9]+\))*)",
    re.IGNORECASE,
)

# The panel list changes at most nightly; one successful fetch per process is
# enough for a batch run.
_cavc_panel_cache: set[str] | None = None
_ecfr_date_cache: str | None = None


def clear_authority_caches() -> None:
    """Clear process-level caches (used by tests and long-running web jobs)."""
    global _cavc_panel_cache, _ecfr_date_cache
    _cavc_panel_cache = None
    _ecfr_date_cache = None


def _headers() -> dict[str, str]:
    return {"User-Agent": get_settings().user_agent}


def _timeout() -> tuple[int, int]:
    return (10, get_settings().request_timeout_seconds)


def _normalize_cavc_docket(value: str) -> str:
    """Return ``NN-NNNN`` for a CAVC docket, dropping ``(E)`` and leading zeros."""
    match = _CAVC_DOCKET_RE.search(value or "")
    if match is None:
        return ""
    return f"{match.group(1)}-{int(match.group(2))}"


def parse_cavc_panel_dockets(html: str) -> set[str]:
    """Extract panel-opinion dockets from the official CAVC opinions page.

    The docket lives in the link text of the page's case-number cell. Do not
    scan filenames or the rest of the page: PDF names contain dates such as
    ``8-19-2026``, and the page-wide fallback used to turn those into bogus
    dockets like ``19-2026``.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    dockets: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        # The live page uses relative hrefs such as "documents/Case.pdf".
        if "documents/" not in href.lower():
            continue
        for match in _CAVC_DOCKET_RE.finditer(anchor.get_text(" ", strip=True)):
            dockets.add(_normalize_cavc_docket(match.group(0)))
    dockets.discard("")
    return dockets


def fetch_cavc_panel_dockets() -> set[str]:
    """Fetch and cache the official CAVC Panel Opinions docket set."""
    global _cavc_panel_cache
    if _cavc_panel_cache is not None:
        return _cavc_panel_cache
    try:
        response = requests.get(
            CAVC_PANEL_OPINIONS_URL,
            headers=_headers(),
            timeout=_timeout(),
            **http_proxy_kwargs(),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SearchError(
            f"CAVC Panel Opinions list unavailable: {redact_secrets(str(exc))}"
        ) from exc
    dockets = parse_cavc_panel_dockets(response.text)
    if not dockets:
        raise SearchError(
            "CAVC Panel Opinions list contained no recognizable docket numbers."
        )
    _cavc_panel_cache = dockets
    return dockets


def validate_cavc_panel_cases(cases: list[CaseRecord]) -> list[AuthorityCheck]:
    """Cross-check CAVC decisions against the official Panel Opinions list.

    A docket on the panel list is a panel/full-court opinion. Only a Rule
    30(a) non-citation marker on a panel-listed docket is a conflict worth
    surfacing; a generic non-precedential flag does not contradict the list by
    itself. A Rule 30(a) docket absent from the list is confirmed
    non-precedential. A docket absent without a marker is left ``not_listed``
    rather than assumed non-precedential, because the list is not a complete
    historical docket index.
    """
    cavc_cases = [case for case in cases if case.court == COURT_CAVC]
    if not cavc_cases:
        return []
    try:
        panel_dockets = fetch_cavc_panel_dockets()
    except SearchError as exc:
        return [
            AuthorityCheck(
                authority="CAVC Panel Opinions list",
                source="CAVC",
                status="unavailable",
                detail=redact_secrets(str(exc)),
                url=CAVC_PANEL_OPINIONS_URL,
            )
        ]

    checks: list[AuthorityCheck] = []
    for case in cavc_cases:
        # Only an explicit docket is authoritative. URLs and titles can carry
        # unrelated docket-shaped numbers (including dates), which used to
        # manufacture panel conflicts.
        docket = _normalize_cavc_docket(case.docket)
        if not docket:
            continue
        listed = docket in panel_dockets
        authority = f"CAVC docket {docket}"
        if listed and case.rule_30a:
            case.cavc_panel_status = "conflict"
            checks.append(
                AuthorityCheck(
                    authority=authority,
                    source="CAVC",
                    status="conflict",
                    detail=(
                        "The docket appears on the official Panel Opinions list, "
                        "but the decision carries a Rule 30(a) non-citation "
                        "marker."
                    ),
                    url=CAVC_PANEL_OPINIONS_URL,
                )
            )
        elif listed:
            case.cavc_panel_status = "panel"
            # The official list is authoritative: a panel-list entry overrides
            # a generic non-precedential marker extracted from the body.
            case.precedential = True
            checks.append(
                AuthorityCheck(
                    authority=authority,
                    source="CAVC",
                    status="verified",
                    detail=(
                        "The docket appears on the official Panel Opinions list "
                        "(panel or full-court opinion)."
                    ),
                    url=CAVC_PANEL_OPINIONS_URL,
                )
            )
        elif case.rule_30a:
            case.cavc_panel_status = "verified_nonprecedential"
            if case.precedential is None:
                case.precedential = False
            checks.append(
                AuthorityCheck(
                    authority=authority,
                    source="CAVC",
                    status="verified",
                    detail=(
                        "Rule 30(a) marker confirmed: the docket is not on the "
                        "official Panel Opinions list, so it is a single-judge "
                        "disposition and not citable as precedent."
                    ),
                    url=CAVC_PANEL_OPINIONS_URL,
                )
            )
        else:
            case.cavc_panel_status = "not_listed"
            checks.append(
                AuthorityCheck(
                    authority=authority,
                    source="CAVC",
                    status="not_listed",
                    detail=(
                        "The docket was not found on the official Panel Opinions "
                        "list; panel status was not asserted."
                    ),
                    url=CAVC_PANEL_OPINIONS_URL,
                )
            )
    return checks


def _analysis_text(analysis: LegalAnalysis, cases: list[CaseRecord]) -> str:
    fragments: list[str] = []
    for case in cases:
        fragments.extend(
            [
                case.citation,
                case.title,
                case.holding,
                " ".join(case.statutes),
            ]
        )
    fragments.extend(
        [
            analysis.summary,
            analysis.how_it_affects_va_claims,
            " ".join(analysis.likely_applicable_principles),
            " ".join(analysis.next_steps),
            " ".join(analysis.strengths),
            " ".join(analysis.gaps),
        ]
    )
    return "\n".join(fragment for fragment in fragments if fragment)


def extract_cfr_sections(text: str, limit: int = 8) -> list[str]:
    """Return unique 38 C.F.R. section numbers in first-seen order."""
    sections: list[str] = []
    for match in _CFR_SECTION_RE.finditer(text or ""):
        section = match.group(1)
        if section not in sections:
            sections.append(section)
        if len(sections) >= limit:
            break
    return sections


def _extract_usc_sections(text: str, limit: int = 6) -> list[str]:
    sections: list[str] = []
    for match in _USC_SECTION_RE.finditer(text or ""):
        section = match.group(1)
        if section not in sections:
            sections.append(section)
        if len(sections) >= limit:
            break
    return sections


def _ecfr_current_date() -> str:
    """Return eCFR's current title-38 date, cached for the process."""
    global _ecfr_date_cache
    if _ecfr_date_cache:
        return _ecfr_date_cache
    response = requests.get(
        ECFR_TITLES_URL,
        headers=_headers(),
        timeout=_timeout(),
        **http_proxy_kwargs(),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise SearchError("eCFR titles response was not a JSON object.")
    for title in payload.get("titles", []):
        if int(title.get("number", 0) or 0) == 38:
            current = (
                title.get("up_to_date_as_of")
                or title.get("latest_issue_date")
                or title.get("latest_amended_on")
            )
            if current:
                _ecfr_date_cache = str(current)
                return _ecfr_date_cache
    raise SearchError("eCFR titles response did not contain title 38.")


def verify_cfr_section(section: str) -> AuthorityCheck:
    """Confirm a 38 C.F.R. section exists in the current eCFR."""
    authority = f"38 C.F.R. § {section}"
    part = section.split(".", 1)[0]
    try:
        current_date = _ecfr_current_date()
        response = requests.get(
            ECFR_FULL_URL.format(date=current_date),
            params={"part": part, "section": section},
            headers=_headers(),
            timeout=_timeout(),
            **http_proxy_kwargs(),
        )
        if response.status_code == 404:
            return AuthorityCheck(
                authority=authority,
                source="eCFR",
                status="unresolved",
                detail=f"eCFR returned 404 for title 38, part {part}, section {section}.",
                url=ECFR_SECTION_URL.format(section=section),
            )
        response.raise_for_status()
    except (requests.RequestException, ValueError, SearchError) as exc:
        return AuthorityCheck(
            authority=authority,
            source="eCFR",
            status="unavailable",
            detail=redact_secrets(str(exc)),
            url=ECFR_SECTION_URL.format(section=section),
        )

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        return AuthorityCheck(
            authority=authority,
            source="eCFR",
            status="unavailable",
            detail=f"eCFR returned unparseable XML: {redact_secrets(str(exc))}",
            url=ECFR_SECTION_URL.format(section=section),
        )
    found = False
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "DIV8":
            continue
        if str(element.attrib.get("N", "")).strip() == section:
            found = True
            break
        metadata = str(element.attrib.get("hierarchy_metadata", ""))
        # Guard against a prefix match: section 3.303 must not be satisfied by
        # hierarchy text for 3.3030.
        if re.search(rf"(?<!\d)38 CFR {re.escape(section)}(?!\d)", metadata):
            found = True
            break
    if not found:
        return AuthorityCheck(
            authority=authority,
            source="eCFR",
            status="unresolved",
            detail=(
                f"eCFR returned title 38 part {part}, but no section {section} "
                "element was present."
            ),
            url=ECFR_SECTION_URL.format(section=section),
        )
    return AuthorityCheck(
        authority=authority,
        source="eCFR",
        status="verified",
        detail=f"Section {section} is present in the current eCFR title 38.",
        url=ECFR_SECTION_URL.format(section=section),
    )


def verify_federal_register_part(
    part: str, today: date | None = None
) -> AuthorityCheck:
    """Find the newest Federal Register rule affecting a cited 38 CFR part."""
    lookback = get_settings().federal_register_lookback_days
    today = today or datetime.now(timezone.utc).date()
    since = (today - timedelta(days=lookback)).isoformat()
    authority = f"38 C.F.R. Part {part}"
    params: dict[str, object] = {
        "conditions[cfr][title]": "38",
        "conditions[cfr][part]": part,
        "conditions[type][]": "RULE",
        "conditions[publication_date][gte]": since,
        "order": "newest",
        "per_page": 1,
        "fields[]": [
            "document_number",
            "title",
            "publication_date",
            "html_url",
            "type",
        ],
    }
    try:
        response = requests.get(
            FEDERAL_REGISTER_DOCUMENTS_URL,
            params=params,
            headers=_headers(),
            timeout=_timeout(),
            **http_proxy_kwargs(),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return AuthorityCheck(
            authority=authority,
            source="Federal Register",
            status="unavailable",
            detail=redact_secrets(str(exc)),
            url="https://www.federalregister.gov/",
        )

    raw_results = payload.get("results") or []
    # Non-dict rows carry no document metadata; treating them as documents
    # would fabricate a "verified" finding from malformed data.
    results = [row for row in raw_results if isinstance(row, dict)]
    if not results:
        return AuthorityCheck(
            authority=authority,
            source="Federal Register",
            status="no_recent_documents",
            detail=(
                f"No rule affecting 38 C.F.R. Part {part} was published in "
                f"the last {lookback} days."
            ),
            url="https://www.federalregister.gov/",
        )
    latest = results[0] if isinstance(results[0], dict) else {}
    title = str(latest.get("title") or "Untitled rule")
    published = str(latest.get("publication_date") or "")
    url = str(latest.get("html_url") or "https://www.federalregister.gov/")
    return AuthorityCheck(
        authority=authority,
        source="Federal Register",
        status="verified",
        detail=f"Latest rule ({published}): {title}",
        url=url,
    )


def _supersession_action(row: dict[str, object]) -> str:
    """Return an explicit supersession action named in a Federal Register row."""
    text = " ".join(
        str(row.get(key) or "") for key in ("action", "title", "abstract")
    )
    for signal, pattern in _SUPERSESSION_ACTION_PATTERNS:
        if pattern.search(text):
            return signal
    return ""


def _replacement_from_text(text: str) -> str:
    """Extract a replacement section only when the rule states one explicitly."""
    match = _REPLACEMENT_RE.search(text)
    return f"38 C.F.R. § {match.group(1)}" if match else ""


def _row_matches_section(row: dict[str, object], section: str) -> bool:
    """Require a Federal Register row's CFR references to name the section."""
    references = row.get("cfr_references")
    if not isinstance(references, list) or not references:
        return True
    declared: list[str] = []
    for reference in references:
        if isinstance(reference, dict):
            value = reference.get("section")
            if value:
                declared.append(str(value))
        elif reference:
            declared.append(str(reference))
    return not declared or section in declared


def verify_cfr_supersession(
    section: str, today: date | None = None
) -> StatutorySupersessionSignal:
    """Look for an explicit Federal Register action affecting one CFR section.

    This is a candidate generator, not a legal conclusion. A rule action such
    as removal, repeal, renumbering, redesignation, reservation, or
    supersession is retained with its effective date and source. A current
    eCFR absence without an explicit action is reported separately by
    :func:`build_statutory_supersession_signals` as ``not_in_current_source``.
    """
    authority = f"38 C.F.R. § {section}"
    part = section.split(".", 1)[0]
    lookback = get_settings().federal_register_lookback_days
    today = today or datetime.now(timezone.utc).date()
    since = (today - timedelta(days=lookback)).isoformat()
    params: dict[str, object] = {
        "conditions[cfr][title]": "38",
        "conditions[cfr][part]": part,
        "conditions[cfr][section]": section,
        "conditions[type][]": "RULE",
        "conditions[publication_date][gte]": since,
        "order": "newest",
        "per_page": 10,
        "fields[]": [
            "document_number",
            "title",
            "publication_date",
            "effective_on",
            "html_url",
            "type",
            "action",
            "abstract",
            "cfr_references",
        ],
    }
    try:
        response = requests.get(
            FEDERAL_REGISTER_DOCUMENTS_URL,
            params=params,
            headers=_headers(),
            timeout=_timeout(),
            **http_proxy_kwargs(),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return StatutorySupersessionSignal(
            authority=authority,
            authority_type="regulation",
            source="Federal Register",
            status="unavailable",
            detail=redact_secrets(str(exc)),
            url=FR_HOME_URL,
        )

    results = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(results, list):
        results = []
    for row in results:
        if not isinstance(row, dict) or not _row_matches_section(row, section):
            continue
        action = _supersession_action(row)
        if not action:
            continue
        title = str(row.get("title") or "Untitled rule")
        document = str(row.get("document_number") or "Federal Register rule")
        published = str(row.get("publication_date") or "date unknown")
        text = " ".join(
            str(row.get(key) or "") for key in ("action", "title", "abstract")
        )
        return StatutorySupersessionSignal(
            authority=authority,
            authority_type="regulation",
            source="Federal Register",
            status="candidate",
            signal=action,
            detail=(
                f"{document} ({published}) — {title}: action indicates "
                f"{action} for {authority}. This is a review candidate, not a "
                "confirmed supersession."
            ),
            url=str(row.get("html_url") or FR_HOME_URL),
            effective_date=str(
                row.get("effective_on") or row.get("publication_date") or ""
            ),
            replacement=_replacement_from_text(text),
        )

    if results:
        detail = (
            f"No explicit repeal, removal, reservation, renumbering, "
            f"redesignation, or supersession action for {section} was found "
            f"in Federal Register rules from the last {lookback} days."
        )
    else:
        detail = (
            f"No Federal Register rule referencing {authority} was found in "
            f"the last {lookback} days."
        )
    return StatutorySupersessionSignal(
        authority=authority,
        authority_type="regulation",
        source="Federal Register",
        status="no_signal",
        detail=detail,
        url=FR_HOME_URL,
    )


def _not_found_regulation_signal(
    authority: str, check: AuthorityCheck, explicit: StatutorySupersessionSignal
) -> StatutorySupersessionSignal:
    """Build a current-source-missing candidate, retaining FR coverage detail."""
    detail = (
        f"{check.detail} The section is absent from the current source, which "
        "is a candidate for repeal, renumbering, or a citation error, not a "
        "confirmed supersession."
    )
    if explicit.status == "candidate":
        return explicit
    if explicit.status == "unavailable":
        detail += f" Federal Register check unavailable: {explicit.detail}"
    elif explicit.detail:
        detail += f" {explicit.detail}"
    return StatutorySupersessionSignal(
        authority=authority,
        authority_type="regulation",
        source=check.source,
        status="candidate",
        signal="not_in_current_source",
        detail=detail,
        url=check.url,
    )


def build_statutory_supersession_signals(
    cfr_sections: list[str],
    usc_sections: list[str],
    checks: list[AuthorityCheck],
) -> list[StatutorySupersessionSignal]:
    """Derive separate provision-level signals from authority checks.

    A verified current eCFR/GovInfo section produces ``no_signal``. A missing
    current section produces a review candidate; for regulations, an explicit
    Federal Register action can refine that candidate. An absent GovInfo check
    (for example, no API key) remains ``not_assessed`` rather than looking
    clean.
    """
    by_key = {
        (check.source, check.authority): check for check in checks
    }
    signals: list[StatutorySupersessionSignal] = []
    for section in dict.fromkeys(cfr_sections):
        authority = f"38 C.F.R. § {section}"
        check = by_key.get(("eCFR", authority))
        if check is None:
            signals.append(
                StatutorySupersessionSignal(
                    authority=authority,
                    authority_type="regulation",
                    source="eCFR",
                    status="not_assessed",
                    detail="No eCFR verification result was available.",
                    url=ECFR_SECTION_URL.format(section=section),
                )
            )
        elif check.status == "verified":
            signals.append(
                StatutorySupersessionSignal(
                    authority=authority,
                    authority_type="regulation",
                    source="eCFR",
                    status="no_signal",
                    detail=(
                        "The section is present in the current eCFR; no "
                        "whole-section supersession signal was found."
                    ),
                    url=check.url,
                )
            )
        elif check.status == "unresolved":
            explicit = verify_cfr_supersession(section)
            signals.append(
                _not_found_regulation_signal(authority, check, explicit)
            )
        else:
            signals.append(
                StatutorySupersessionSignal(
                    authority=authority,
                    authority_type="regulation",
                    source=check.source,
                    status="unavailable",
                    detail=check.detail,
                    url=check.url,
                )
            )

    for section in dict.fromkeys(usc_sections):
        authority = f"38 U.S.C. § {section}"
        check = by_key.get(("GovInfo", authority))
        if check is None:
            signals.append(
                StatutorySupersessionSignal(
                    authority=authority,
                    authority_type="statute",
                    source="GovInfo",
                    status="not_assessed",
                    detail=(
                        "GovInfo verification did not run (for example, no "
                        "GOVINFO_API_KEY)."
                    ),
                    url=GOVINFO_HOME_URL,
                )
            )
        elif check.status == "verified":
            signals.append(
                StatutorySupersessionSignal(
                    authority=authority,
                    authority_type="statute",
                    source="GovInfo",
                    status="no_signal",
                    detail=(
                        "The exact section granule is present in the current "
                        "U.S. Code collection; no whole-section supersession "
                        "signal was found."
                    ),
                    url=check.url,
                )
            )
        elif check.status == "unresolved":
            signals.append(
                StatutorySupersessionSignal(
                    authority=authority,
                    authority_type="statute",
                    source="GovInfo",
                    status="candidate",
                    signal="not_in_current_source",
                    detail=(
                        f"{check.detail} The section is absent from the "
                        "current GovInfo U.S. Code collection, which is a "
                        "candidate for repeal, renumbering, or a citation "
                        "error, not a confirmed supersession."
                    ),
                    url=check.url,
                )
            )
        else:
            signals.append(
                StatutorySupersessionSignal(
                    authority=authority,
                    authority_type="statute",
                    source=check.source,
                    status="unavailable",
                    detail=check.detail,
                    url=check.url,
                )
            )
    return signals


def verify_analysis_supersession(
    analysis: LegalAnalysis,
    cases: list[CaseRecord],
    checks: list[AuthorityCheck],
) -> list[StatutorySupersessionSignal]:
    """Build statutory/regulatory supersession signals for an analysis."""
    if not get_settings().authority_verification:
        return []
    text = _analysis_text(analysis, cases)
    return build_statutory_supersession_signals(
        extract_cfr_sections(text),
        _extract_usc_sections(text),
        checks,
    )


def statutory_supersession_flags(
    signals: list[StatutorySupersessionSignal],
) -> list[str]:
    """Turn supersession candidates and coverage gaps into separate flags."""
    flags: list[str] = []
    seen: set[str] = set()
    for signal in signals:
        if signal.status == "candidate":
            flag = (
                f"Statutory/regulatory supersession signal: {signal.authority} "
                f"({signal.source}) — {signal.detail}"
            )
        elif signal.status == "unavailable":
            flag = (
                f"Statutory/regulatory supersession check unavailable for "
                f"{signal.authority} ({signal.source}): {signal.detail}"
            )
        elif signal.status == "not_assessed":
            flag = (
                f"Statutory/regulatory supersession not assessed for "
                f"{signal.authority} ({signal.source}): {signal.detail}"
            )
        else:
            continue
        if flag not in seen:
            seen.add(flag)
            flags.append(flag)
    return flags


class GovInfoClient:
    """Minimal client for GovInfo's official search API."""

    def __init__(
        self,
        api_key: str | None = None,
        include_records: bool | None = None,
    ):
        settings = get_settings()
        self.api_key = (api_key if api_key is not None else settings.govinfo_api_key) or ""
        self.include_records = (
            settings.govinfo_legislation_records
            if include_records is None
            else include_records
        )

    def search(self, query: str, page_size: int = 5) -> list[dict[str, object]]:
        """Run *query* against GovInfo and return its result rows."""
        if not self.api_key:
            raise SearchError(
                "GovInfo requires an api.data.gov key; set GOVINFO_API_KEY."
            )
        body = {
            "query": query,
            "pageSize": max(1, min(page_size, 100)),
            "offsetMark": "*",
            "sorts": [{"field": "score", "sortOrder": "DESC"}],
        }
        headers = _headers()
        # api.data.gov accepts the key in X-Api-Key.  Keeping it out of the
        # query string prevents it from entering HTTPError.url and every
        # request log line that records the URL.
        headers["X-Api-Key"] = self.api_key
        try:
            response = requests.post(
                GOVINFO_SEARCH_URL,
                json=body,
                headers=headers,
                timeout=_timeout(),
                **http_proxy_kwargs(),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            sanitize_exception(exc, (self.api_key,))
            detail = redact_secrets(str(exc), (self.api_key,))
            raise SearchError(f"GovInfo search failed: {detail}") from exc
        if not isinstance(payload, dict):
            raise SearchError("GovInfo search returned an unexpected response.")
        results = payload.get("results") or []
        if not isinstance(results, list):
            raise SearchError("GovInfo search returned an unexpected response.")
        return [row for row in results if isinstance(row, dict)]


_GOVINFO_USCODE_PACKAGE_RE = re.compile(r"USCODE-\d{4}-title38", re.IGNORECASE)
# GovInfo files Federal Circuit opinions under the 13th-circuit code, not
# "cafc".
_GOVINFO_FEDERAL_CIRCUIT_PACKAGE_PREFIX = "uscourts-ca13-"


def _govinfo_package_url(package: str) -> str:
    return f"https://www.govinfo.gov/app/details/{package}"


def _title_words(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


_GOVINFO_DOCKET_PREFIX_RE = re.compile(
    r"^\s*(?:No\.\s*)?\d{2,4}-\d{1,5}\s*(?:-\s*)?", re.IGNORECASE
)


def _titles_match(expected: str, actual: object) -> bool:
    """True when GovInfo's title names exactly the case we searched.

    GovInfo prefixes package titles with the docket (``24-1234 - ...``), so
    remove that prefix before comparing. The remaining case name must match
    exactly: a longer name such as ``Another Veteran v. Secretary`` must not
    satisfy a search for ``Veteran v. Secretary``.
    """
    expected_words = _title_words(expected)
    if not expected_words:
        return False
    actual_without_docket = _GOVINFO_DOCKET_PREFIX_RE.sub("", str(actual or ""))
    return _title_words(actual_without_docket) == expected_words


def _govinfo_details_url(package_id: str, granule_id: str = "") -> str:
    url = GOVINFO_DETAILS_URL.format(package_id=package_id)
    return f"{url}/{granule_id}" if granule_id else url


def _govinfo_text_url(package_id: str, granule_id: str = "") -> str:
    """Return a stable, public HTML URL for a package or granule."""
    return GOVINFO_CONTENT_URL.format(
        package_id=package_id,
        file_id=granule_id or package_id,
    )


def _govinfo_excerpt(text: str) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= _GOVINFO_TEXT_EXCERPT_LIMIT:
        return normalized
    return normalized[: _GOVINFO_TEXT_EXCERPT_LIMIT - 3].rstrip() + "..."


def _fetch_govinfo_text(url: str) -> str:
    """Fetch and normalize the public HTML text at a stable GovInfo URL."""
    try:
        response = requests.get(
            url,
            headers=_headers(),
            timeout=_timeout(),
            **http_proxy_kwargs(),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        sanitize_exception(exc)
        raise SearchError(f"GovInfo text request failed: {exc}") from exc
    soup = BeautifulSoup(getattr(response, "text", "") or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    if not text:
        raise SearchError("GovInfo text response contained no readable text.")
    return text


def _apply_govinfo_text(
    record: GovInfoPublicLaw | GovInfoStatute, text_url: str
) -> str:
    """Attach a bounded excerpt; return an error message or an empty string."""
    if not text_url:
        record.text_status = "not_available"
        record.detail = "No stable GovInfo text URL is available for this record."
        return record.detail
    try:
        text = _fetch_govinfo_text(text_url)
    except SearchError as exc:
        record.text_status = "unavailable"
        record.detail = redact_secrets(str(exc))
        return record.detail
    record.text_excerpt = _govinfo_excerpt(text)
    record.text_chars = len(text)
    record.text_status = "retrieved"
    return ""


def _parse_govinfo_public_law(row: dict[str, object]) -> GovInfoPublicLaw | None:
    package_id = str(row.get("packageId") or "").strip()
    match = _PLAW_PACKAGE_RE.fullmatch(package_id)
    if match is None:
        return None
    congress = int(match.group(1))
    number = int(match.group(2))
    citation = str(row.get("plawcitation") or "").strip()
    if not citation:
        citation = f"Pub. L. {congress}-{number}"
    return GovInfoPublicLaw(
        package_id=package_id,
        citation=citation,
        title=str(row.get("title") or "").strip(),
        date_issued=str(row.get("dateIssued") or "").strip(),
        details_url=_govinfo_details_url(package_id),
        text_url=_govinfo_text_url(package_id),
    )


def _parse_govinfo_statute(row: dict[str, object]) -> GovInfoStatute | None:
    package_id = str(row.get("packageId") or "").strip()
    match = _STATUTE_PACKAGE_RE.fullmatch(package_id)
    if match is None:
        return None
    granule_id = str(row.get("granuleId") or "").strip()
    if granule_id and not granule_id.lower().startswith(
        package_id.lower() + "-"
    ):
        return None
    if granule_id and any(
        marker in granule_id.lower()
        for marker in ("frontmatter", "backmatter")
    ):
        return None
    citation = str(
        row.get("statutecitation") or row.get("citation") or ""
    ).strip()
    if not citation:
        page_match = _STATUTE_PAGE_RE.search(granule_id)
        if page_match:
            citation = f"{int(match.group(1))} Stat. {int(page_match.group(1))}"
        else:
            citation = f"{int(match.group(1))} Stat."
    text_url = (
        _govinfo_text_url(package_id, granule_id) if granule_id else ""
    )
    return GovInfoStatute(
        package_id=package_id,
        granule_id=granule_id,
        citation=citation,
        title=str(row.get("title") or "").strip(),
        date_issued=str(row.get("dateIssued") or "").strip(),
        details_url=_govinfo_details_url(package_id, granule_id),
        text_url=text_url,
        text_status="not_retrieved" if text_url else "not_available",
    )


def _govinfo_legislation_rows(
    client: GovInfoClient, collection: str, section: str
) -> list[dict[str, object]]:
    return client.search(
        f'collection:{collection} uscodecitation:"38 U.S.C. {section}"',
        page_size=5,
    )


def _enrich_govinfo_legislation(
    check: AuthorityCheck, section: str, client: GovInfoClient
) -> None:
    """Attach Public Law and Statutes at Large records to a verified U.S.C. check."""
    if not client.include_records:
        return

    errors: list[str] = []
    public_laws: list[GovInfoPublicLaw] = []
    statutes: list[GovInfoStatute] = []
    searches_succeeded = 0

    try:
        rows = _govinfo_legislation_rows(client, "PLAW", section)
        searches_succeeded += 1
    except SearchError as exc:
        errors.append(f"Public Laws search unavailable: {redact_secrets(str(exc))}")
        rows = []
    seen_packages: set[str] = set()
    for row in rows:
        record = _parse_govinfo_public_law(row)
        if record is None or record.package_id in seen_packages:
            continue
        seen_packages.add(record.package_id)
        public_laws.append(record)
        if len(public_laws) >= _MAX_GOVINFO_RECORDS:
            break

    try:
        rows = _govinfo_legislation_rows(client, "STATUTE", section)
        searches_succeeded += 1
    except SearchError as exc:
        errors.append(f"Statutes at Large search unavailable: {redact_secrets(str(exc))}")
        rows = []
    seen_granules: set[str] = set()
    for row in rows:
        record = _parse_govinfo_statute(row)
        key = f"{record.package_id}:{record.granule_id}" if record else ""
        if record is None or key in seen_granules:
            continue
        seen_granules.add(key)
        statutes.append(record)
        if len(statutes) >= _MAX_GOVINFO_RECORDS:
            break

    for record in public_laws:
        error = _apply_govinfo_text(record, record.text_url)
        if error:
            errors.append(f"{record.citation or record.package_id}: {error}")
    for statute in statutes:
        error = _apply_govinfo_text(statute, statute.text_url)
        if error:
            errors.append(f"{statute.citation or statute.package_id}: {error}")

    check.public_laws = public_laws
    check.statutes_at_large = statutes
    if errors and searches_succeeded:
        check.records_status = "partial"
    elif errors:
        check.records_status = "unavailable"
    elif public_laws or statutes:
        check.records_status = "retrieved"
    else:
        check.records_status = "not_found"

    count_text = (
        f"Retrieved {len(public_laws)} Public Law record(s) and "
        f"{len(statutes)} Statutes at Large record(s)"
    )
    if check.records_status == "not_found":
        check.records_detail = (
            f"No Public Law or Statutes at Large record citing "
            f"38 U.S.C. § {section} was found in GovInfo. This does not "
            "prove that no enacting or amending law exists."
        )
    elif errors:
        check.records_detail = f"{count_text}; {'; '.join(errors)}"
    else:
        check.records_detail = (
            f"{count_text} citing 38 U.S.C. § {section}."
        )


def _verify_us_code(section: str, client: GovInfoClient) -> AuthorityCheck:
    """Verify the exact title-38 section granule, not just any search hit."""
    authority = f"38 U.S.C. § {section}"
    # A subsection citation still lives in the section's granule.
    base_section = section.split("(", 1)[0]
    citation = f"38 U.S.C. {base_section}"
    try:
        results = client.search(
            f'collection:uscode citation:"{citation}"',
            page_size=5,
        )
    except SearchError as exc:
        return AuthorityCheck(
            authority=authority,
            source="GovInfo",
            status="unavailable",
            detail=redact_secrets(str(exc)),
            url=GOVINFO_HOME_URL,
        )
    granule_suffix = f"sec{base_section}".lower()
    match = next(
        (
            row
            for row in results
            if _GOVINFO_USCODE_PACKAGE_RE.fullmatch(
                str(row.get("packageId") or "")
            )
            and str(row.get("granuleId") or "").lower().endswith(granule_suffix)
        ),
        None,
    )
    if match is None:
        return AuthorityCheck(
            authority=authority,
            source="GovInfo",
            status="unresolved",
            detail=(
                "GovInfo's U.S. Code collection returned no exact title 38 "
                f"section {base_section} match."
            ),
            url=GOVINFO_HOME_URL,
        )
    package = str(match.get("packageId") or "")
    check = AuthorityCheck(
        authority=authority,
        source="GovInfo",
        status="verified",
        detail=(
            f"GovInfo U.S. Code matched title 38 section {base_section} "
            f"(package {package})."
        ),
        url=_govinfo_package_url(package),
    )
    _enrich_govinfo_legislation(check, base_section, client)
    return check


def _verify_federal_circuit_case(
    case: CaseRecord, client: GovInfoClient
) -> AuthorityCheck:
    authority = f"Federal Circuit: {case.title}"
    safe_title = re.sub(r'["()]', " ", case.title).strip()
    try:
        results = client.search(
            'collection:(USCOURTS) courtcode:ca13 courttype:appellate '
            f'title:("{safe_title}")',
            page_size=5,
        )
    except SearchError as exc:
        return AuthorityCheck(
            authority=authority,
            source="GovInfo",
            status="unavailable",
            detail=redact_secrets(str(exc)),
            url=GOVINFO_HOME_URL,
        )
    match = next(
        (
            row
            for row in results
            if str(row.get("packageId") or "")
            .lower()
            .startswith(_GOVINFO_FEDERAL_CIRCUIT_PACKAGE_PREFIX)
            and _titles_match(case.title, row.get("title"))
        ),
        None,
    )
    if match is None:
        return AuthorityCheck(
            authority=authority,
            source="GovInfo",
            status="unresolved",
            detail=(
                "GovInfo's U.S. Courts Opinions collection returned no exact "
                "Federal Circuit case match."
            ),
            url=GOVINFO_HOME_URL,
        )
    package = str(match.get("packageId") or "")
    return AuthorityCheck(
        authority=authority,
        source="GovInfo",
        status="verified",
        detail=(
            "GovInfo U.S. Courts Opinions matched the case "
            f"(package {package})."
        ),
        url=_govinfo_package_url(package),
    )


def verify_analysis_authorities(
    analysis: LegalAnalysis, cases: list[CaseRecord]
) -> list[AuthorityCheck]:
    """Verify CFR, U.S. Code, and Federal Circuit authorities when enabled.

    eCFR and Federal Register need no API key. GovInfo checks run only when
    ``GOVINFO_API_KEY`` is configured.
    """
    settings = get_settings()
    if not settings.authority_verification:
        return []

    text = _analysis_text(analysis, cases)
    checks: list[AuthorityCheck] = []

    sections = extract_cfr_sections(text)
    for section in sections:
        checks.append(verify_cfr_section(section))
    for part in dict.fromkeys(section.split(".", 1)[0] for section in sections):
        checks.append(verify_federal_register_part(part))

    client = GovInfoClient()
    if client.api_key:
        for section in _extract_usc_sections(text):
            checks.append(_verify_us_code(section, client))
        federal_circuit_cases = [
            case
            for case in cases
            if case.court == COURT_FEDERAL_CIRCUIT and case.title.strip()
        ][:3]
        for case in federal_circuit_cases:
            checks.append(_verify_federal_circuit_case(case, client))
    return checks


def authority_flags(checks: list[AuthorityCheck]) -> list[str]:
    """Turn authority-check results into actionable output flags."""
    flags: list[str] = []
    seen: set[str] = set()
    for check in checks:
        if check.status == "conflict":
            flag = f"Authority conflict: {check.authority} ({check.source}) — {check.detail}"
        elif check.status == "unresolved":
            flag = f"Unresolved authority '{check.authority}' ({check.source}): {check.detail}"
        elif check.status == "unavailable":
            flag = (
                f"Authority verification unavailable for {check.authority} "
                f"({check.source}): {check.detail}"
            )
        elif check.records_status in {"partial", "unavailable"}:
            flag = (
                f"GovInfo legislative records {check.records_status} for "
                f"{check.authority}: {check.records_detail}"
            )
        else:
            # verified / not_listed / no_recent_documents are informational.
            continue
        if flag not in seen:
            flags.append(flag)
            seen.add(flag)
    return flags
