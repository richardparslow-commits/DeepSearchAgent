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
from .models import AuthorityCheck, CaseRecord, LegalAnalysis
from .search import SearchError, http_proxy_kwargs
from .topics import COURT_CAVC, COURT_FEDERAL_CIRCUIT

CAVC_PANEL_OPINIONS_URL = "https://www.uscourts.cavc.gov/opinions.php?fullsite=yes"
ECFR_TITLES_URL = "https://www.ecfr.gov/api/versioner/v1/titles.json"
ECFR_FULL_URL = "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-38.xml"
ECFR_SECTION_URL = "https://www.ecfr.gov/current/title-38/section-{section}"
FEDERAL_REGISTER_DOCUMENTS_URL = "https://www.federalregister.gov/api/v1/documents.json"
GOVINFO_SEARCH_URL = "https://api.govinfo.gov/search"

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
        raise SearchError(f"CAVC Panel Opinions list unavailable: {exc}") from exc
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
                detail=str(exc),
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
            detail=str(exc),
            url=ECFR_SECTION_URL.format(section=section),
        )

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        return AuthorityCheck(
            authority=authority,
            source="eCFR",
            status="unavailable",
            detail=f"eCFR returned unparseable XML: {exc}",
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
            detail=str(exc),
            url="https://www.federalregister.gov/",
        )

    results = payload.get("results") or []
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


class GovInfoClient:
    """Minimal client for GovInfo's official search API."""

    def __init__(self, api_key: str | None = None):
        settings = get_settings()
        self.api_key = (api_key if api_key is not None else settings.govinfo_api_key) or ""

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
        try:
            response = requests.post(
                GOVINFO_SEARCH_URL,
                params={"api_key": self.api_key},
                json=body,
                headers=_headers(),
                timeout=_timeout(),
                **http_proxy_kwargs(),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SearchError(f"GovInfo search failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise SearchError("GovInfo search returned an unexpected response.")
        results = payload.get("results") or []
        if not isinstance(results, list):
            raise SearchError("GovInfo search returned an unexpected response.")
        return [row for row in results if isinstance(row, dict)]


GOVINFO_HOME_URL = "https://www.govinfo.gov/"
_GOVINFO_USCODE_PACKAGE_RE = re.compile(r"USCODE-\d{4}-title38", re.IGNORECASE)
# GovInfo files Federal Circuit opinions under the 13th-circuit code, not
# "cafc".
_GOVINFO_FEDERAL_CIRCUIT_PACKAGE_PREFIX = "uscourts-ca13-"


def _govinfo_package_url(package: str) -> str:
    return f"https://www.govinfo.gov/app/details/{package}"


def _title_words(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _titles_match(expected: str, actual: object) -> bool:
    """True when GovInfo's longer title contains the case name we searched."""
    expected_words = _title_words(expected)
    return bool(expected_words and expected_words in _title_words(actual))


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
            detail=str(exc),
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
    return AuthorityCheck(
        authority=authority,
        source="GovInfo",
        status="verified",
        detail=(
            f"GovInfo U.S. Code matched title 38 section {base_section} "
            f"(package {package})."
        ),
        url=_govinfo_package_url(package),
    )


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
            detail=str(exc),
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
        else:
            # verified / not_listed / no_recent_documents are informational.
            continue
        if flag not in seen:
            flags.append(flag)
            seen.add(flag)
    return flags
