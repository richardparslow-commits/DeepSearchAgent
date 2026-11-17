"""Optional Congress.gov legislative-history links for cited U.S. Code sections.

Congress.gov's API does not expose a U.S. Code citation index. To avoid
guessing which bill produced a section, this module discovers enacting public
laws through GovInfo's documented ``uscodecitation`` search, then uses
Congress.gov for the public law's bill metadata and actions. GovInfo is only
the crosswalk; Congress.gov remains the source of bill, action, and CRS data.

The layer is deliberately advisory:

* A cited section is linked to zero or more public laws. A public law is not
  treated as proof that the section is current or that no later amendment
  exists.
* CRS reports are always labeled secondary and are retrieved only when their
  report number is explicitly cited. The API has no keyword search for CRS
  reports, so relevance is never inferred from a section citation.
* Missing keys, source failures, and request-budget exhaustion become
  ``not_assessed`` or ``unavailable`` records. They never abort an analysis
  and never change ranking, ``superseded_by``, or citator status.

The official Congress.gov rate limit is 5,000 requests per hour. See
``CONGRESS_RATE_LIMIT_URL``; the client additionally caps requests per run and
does not count cache hits against that cap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import get_settings
from .courtlistener_cache import CourtListenerCache
from .models import (
    CongressAction,
    CongressLink,
    CongressPublicLaw,
    CRSReport,
    CaseRecord,
    LegalAnalysis,
)
from .search import SearchError, http_proxy_kwargs

CONGRESS_API_BASE = "https://api.congress.gov/v3"
CONGRESS_RATE_LIMIT_PER_HOUR = 5_000
CONGRESS_RATE_LIMIT_URL = (
    "https://github.com/LibraryOfCongress/api.congress.gov#rate-limit"
)
GOVINFO_SEARCH_URL = "https://api.govinfo.gov/search"
GOVINFO_PACKAGE_SUMMARY_URL = "https://api.govinfo.gov/packages/{package}/summary"
GOVINFO_PACKAGE_URL = "https://www.govinfo.gov/app/details/{package}"

_PLAW_PACKAGE_RE = re.compile(
    r"^PLAW-(\d{1,3})publ-?(\d+)$", re.IGNORECASE
)
_BILL_CITATION_RE = re.compile(
    r"^(H\.?\s*R\.?|S\.?|H\.?\s*J\.?\s*Res\.?|S\.?\s*J\.?\s*Res\.?|"
    r"H\.?\s*Con\.?\s*Res\.?|S\.?\s*Con\.?\s*Res\.?)\s*(\d+)$",
    re.IGNORECASE,
)
_CRS_REPORT_RE = re.compile(r"\b(R\d{4,5})\b")
_API_KEY_QUERY_RE = re.compile(r"(api_key=)[^&\s]+", re.IGNORECASE)
_ACTION_LIMIT = 12
_MAX_PUBLIC_LAWS = 3
_MAX_CRS_REPORTS = 2


@dataclass(frozen=True)
class CongressBundle:
    """Congressional records produced for one analysis."""

    links: list[CongressLink] = field(default_factory=list)
    crs_reports: list[CRSReport] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _headers() -> dict[str, str]:
    return {"User-Agent": get_settings().user_agent}


def _as_dict(value: object) -> dict[str, object]:
    """Return a mapping only when *value* is a JSON object."""
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    """Return a list only when *value* is a JSON array."""
    return value if isinstance(value, list) else []


def _bill_type(citation: str) -> tuple[str, str] | None:
    """Parse ``H.R. 1234``-style citations into Congress.gov path parts."""
    match = _BILL_CITATION_RE.match((citation or "").strip())
    if match is None:
        return None
    label = re.sub(r"[^A-Za-z]", "", match.group(1)).upper()
    mapping = {
        "HR": "hr",
        "S": "s",
        "HJRES": "hjres",
        "SJRES": "sjres",
        "HCONRES": "hconres",
        "SCONRES": "sconres",
    }
    return mapping[label], match.group(2)


def _public_law_citation(package: str, summary: dict[str, object]) -> str:
    """Return the official public-law citation, or an empty string."""
    declared = str(summary.get("plawcitation") or "").strip()
    if declared:
        return declared
    match = _PLAW_PACKAGE_RE.fullmatch(package)
    if match is None:
        return ""
    return f"Pub. L. {int(match.group(1))}-{int(match.group(2))}"


def _congress_number(
    package: str, summary: dict[str, object]
) -> int | None:
    try:
        if summary.get("congress"):
            return int(summary["congress"])
    except (TypeError, ValueError):
        pass
    match = _PLAW_PACKAGE_RE.fullmatch(package)
    if match is None:
        return None
    return int(match.group(1))


def _redacted_error(exc: Exception) -> str:
    """Keep request errors useful without echoing credential query values."""
    return _API_KEY_QUERY_RE.sub(r"\1***", str(exc))


def _url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    return f"https://{text.lstrip('/')}"


class CongressClient:
    """Cached, best-effort Congress.gov client for legislative history.

    ``CONGRESS_API_KEY`` is the opt-in switch. ``GOVINFO_API_KEY`` is also
    required because the Congress.gov API has no U.S. Code citation index; the
    official GovInfo public-law search supplies that crosswalk. Neither key is
    written to the cache: Congress.gov uses an ``X-Api-Key`` header, and
    GovInfo's query key is excluded from cache metadata by the shared cache.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        govinfo_api_key: str | None = None,
        cache_dir: str | None = None,
        cache_ttl_seconds: int | None = None,
        max_requests: int | None = None,
    ):
        settings = get_settings()
        self.api_key = (
            api_key if api_key is not None else settings.congress_api_key
        ) or ""
        self.govinfo_api_key = (
            govinfo_api_key
            if govinfo_api_key is not None
            else settings.govinfo_api_key
        ) or ""
        self.max_requests = (
            max_requests
            if max_requests is not None
            else settings.congress_max_requests_per_run
        )
        self.requests_made = 0
        self.errors: list[str] = []
        self._cache: CourtListenerCache | None = None

        directory = (
            cache_dir
            if cache_dir is not None
            else settings.congress_cache_dir
        )
        ttl = (
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else settings.congress_cache_ttl_seconds
        )
        directory = (directory or "").strip()
        if self.api_key and directory and ttl > 0:
            try:
                self._cache = CourtListenerCache(directory, ttl)
            except OSError:
                # A cache failure must not make the optional client unusable.
                self._cache = None

    def _note_error(self, message: str) -> None:
        if message and message not in self.errors:
            self.errors.append(message)

    def _cache_get(self, key: str, params: dict[str, object]) -> Any:
        if self._cache is None:
            return None
        try:
            return self._cache.get("congress", key, params)
        except OSError:
            return None

    def _cache_set(
        self, key: str, params: dict[str, object], payload: object
    ) -> None:
        if self._cache is None:
            return
        try:
            self._cache.set(
                "congress",
                key,
                payload,
                params=params,
            )
        except OSError:
            # The live response is still usable when persistence fails.
            pass

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        cache_key: str,
        cache_params: dict[str, object] | None = None,
        source_name: str = "Congress.gov",
    ) -> dict[str, object]:
        """Return a cached JSON object or make one bounded source request."""
        cache_params = cache_params or {}
        cached = self._cache_get(cache_key, cache_params)
        if cached is not None:
            return cached if isinstance(cached, dict) else {}

        if self.requests_made >= self.max_requests:
            raise SearchError(
                "Congress.gov request cap reached for this analysis "
                f"({self.max_requests}); cached links were retained."
            )
        self.requests_made += 1
        request_headers = _headers()
        request_headers.update(headers or {})
        try:
            if method == "POST":
                response = requests.post(
                    url,
                    params=params,
                    json=json_body,
                    headers=request_headers,
                    timeout=(10, get_settings().request_timeout_seconds),
                    **http_proxy_kwargs(),
                )
            else:
                response = requests.get(
                    url,
                    params=params,
                    headers=request_headers,
                    timeout=(10, get_settings().request_timeout_seconds),
                    **http_proxy_kwargs(),
                )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            detail = _redacted_error(exc)
            raise SearchError(f"{source_name} request failed: {detail}") from exc
        if not isinstance(payload, dict):
            raise SearchError(f"{source_name} returned a non-object JSON response.")
        self._cache_set(cache_key, cache_params, payload)
        return payload

    def _congress_get(
        self, path: str, params: dict[str, object] | None = None
    ) -> dict[str, object]:
        request_params: dict[str, object] = {"format": "json"}
        request_params.update(params or {})
        return self._request_json(
            "GET",
            f"{CONGRESS_API_BASE}/{path.lstrip('/')}",
            params=request_params,
            headers={"X-Api-Key": self.api_key},
            cache_key=f"congress:{path.lstrip('/')}",
            cache_params=request_params,
        )

    def _govinfo_search(self, query: str) -> dict[str, object]:
        body = {
            "query": query,
            "pageSize": _MAX_PUBLIC_LAWS,
            "offsetMark": "*",
            "sorts": [{"field": "score", "sortOrder": "DESC"}],
        }
        return self._request_json(
            "POST",
            GOVINFO_SEARCH_URL,
            params={"api_key": self.govinfo_api_key},
            json_body=body,
            cache_key="govinfo:public-law-search",
            cache_params={"query": query, "page_size": _MAX_PUBLIC_LAWS},
            source_name="GovInfo",
        )

    def _govinfo_summary(self, package: str) -> dict[str, object]:
        return self._request_json(
            "GET",
            GOVINFO_PACKAGE_SUMMARY_URL.format(package=package),
            params={"api_key": self.govinfo_api_key},
            cache_key=f"govinfo:summary:{package}",
            cache_params={"package": package},
            source_name="GovInfo",
        )

    def _public_law_from_row(
        self, row: dict[str, object]
    ) -> CongressPublicLaw | None:
        package = str(row.get("packageId") or "").strip()
        if not _PLAW_PACKAGE_RE.fullmatch(package):
            return None
        summary: dict[str, object] = {}
        summary_error = ""
        try:
            summary = self._govinfo_summary(package)
        except SearchError as exc:
            summary_error = str(exc)
            self._note_error(f"GovInfo public-law summary unavailable for {package}: {exc}")

        citation = _public_law_citation(package, summary)
        bill_citation = str(
            summary.get("billscitation") or row.get("billscitation") or ""
        ).strip()
        law = CongressPublicLaw(
            citation=citation,
            title=str(summary.get("title") or row.get("title") or "").strip(),
            url=GOVINFO_PACKAGE_URL.format(package=package),
            enacted_date=str(
                summary.get("dateIssued") or row.get("dateIssued") or ""
            ).strip(),
            bill_citation=bill_citation,
            status="public_law_only",
            detail=(
                f"GovInfo public-law summary unavailable: {summary_error}"
                if summary_error
                else (
                    "Public law found; no associated bill citation was "
                    "available, so Congress.gov actions were not retrieved."
                )
            ),
        )
        congress = _congress_number(package, summary)
        if bill_citation and congress is not None:
            self._enrich_bill(law, bill_citation, congress)
        return law

    def _enrich_bill(
        self, law: CongressPublicLaw, bill_citation: str, congress: int
    ) -> None:
        parsed = _bill_type(bill_citation)
        if parsed is None:
            law.detail = (
                "Public law found, but its bill citation was not a supported "
                "Congress.gov bill type."
            )
            return
        bill_type, bill_number = parsed
        try:
            payload = self._congress_get(
                f"bill/{congress}/{bill_type}/{bill_number}"
            )
        except SearchError as exc:
            law.detail = f"Congress.gov bill metadata unavailable: {exc}"
            self._note_error(law.detail)
            return

        bill = _as_dict(payload.get("bill"))
        law.bill_title = str(bill.get("title") or "").strip()
        law.bill_url = _url(bill.get("legislationUrl"))
        law.status = "linked"
        law.detail = "Public law linked to its Congress.gov bill record."

        try:
            actions_payload = self._congress_get(
                f"bill/{congress}/{bill_type}/{bill_number}/actions",
                {"limit": 250},
            )
        except SearchError as exc:
            law.detail = (
                "Public law linked to its Congress.gov bill record, but "
                f"actions were unavailable: {exc}"
            )
            self._note_error(law.detail)
            return

        rows = [
            row
            for row in _as_list(actions_payload.get("actions"))
            if isinstance(row, dict)
        ]
        law.actions = [
            CongressAction(
                date=str(row.get("actionDate") or "").strip(),
                text=str(row.get("text") or "").strip(),
                type=str(row.get("type") or "").strip(),
            )
            for row in rows[:_ACTION_LIMIT]
        ]
        law.actions_truncated = len(rows) > _ACTION_LIMIT

    def _find_public_laws(self, section: str) -> list[CongressPublicLaw]:
        base_section = section.split("(", 1)[0]
        query = (
            "collection:PLAW "
            f'uscodecitation:"38 U.S.C. {base_section}"'
        )
        payload = self._govinfo_search(query)
        laws: list[CongressPublicLaw] = []
        seen: set[str] = set()
        for row in _as_list(payload.get("results")):
            if not isinstance(row, dict):
                continue
            law = self._public_law_from_row(row)
            if law is None or law.citation in seen:
                continue
            seen.add(law.citation)
            laws.append(law)
            if len(laws) >= _MAX_PUBLIC_LAWS:
                break
        return laws

    def link_section(self, section: str) -> CongressLink:
        """Link one cited U.S. Code section to public laws and bill actions."""
        authority = f"38 U.S.C. § {section}"
        if not self.api_key:
            return CongressLink(
                usc_section=authority,
                status="not_assessed",
                detail=(
                    "CONGRESS_API_KEY is not set, so congressional history "
                    "was not retrieved."
                ),
            )
        if not self.govinfo_api_key:
            return CongressLink(
                usc_section=authority,
                status="not_assessed",
                detail=(
                    "GOVINFO_API_KEY is not set. Congress.gov has no U.S. Code "
                    "citation search, so the official GovInfo public-law "
                    "crosswalk is required."
                ),
            )
        try:
            laws = self._find_public_laws(section)
        except SearchError as exc:
            return CongressLink(
                usc_section=authority,
                status="unavailable",
                detail=str(exc),
            )
        if not laws:
            return CongressLink(
                usc_section=authority,
                status="no_public_law",
                detail=(
                    "GovInfo's public-law collection returned no law citing "
                    "this section. This is not proof that no enacting law "
                    "exists."
                ),
            )
        return CongressLink(
            usc_section=authority,
            status="linked",
            public_laws=laws,
            detail=(
                f"Found {len(laws)} public law(s) through GovInfo and "
                "enriched with Congress.gov where available."
            ),
        )

    def crs_report(self, report_number: str) -> CRSReport:
        """Retrieve one cited CRS report by its report number."""
        if not self.api_key:
            raise SearchError("CONGRESS_API_KEY is not set.")
        number = report_number.strip().upper()
        payload = self._congress_get(f"crsreport/{number}")
        report = _as_dict(
            payload.get("CRSReport")
            or payload.get("crsReport")
            or payload.get("report")
            or payload
        )
        return CRSReport(
            report_number=str(
                report.get("reportNumber") or report.get("id") or number
            ).strip(),
            title=str(report.get("title") or "").strip(),
            url=_url(report.get("url")),
            publish_date=str(report.get("publishDate") or "").strip(),
        )

    def crs_reports_for_text(self, text: str) -> list[CRSReport]:
        """Retrieve explicitly cited CRS reports, up to a small cap."""
        if not self.api_key:
            return []
        reports: list[CRSReport] = []
        seen: set[str] = set()
        for match in _CRS_REPORT_RE.finditer(text or ""):
            number = match.group(1).upper()
            if number in seen:
                continue
            seen.add(number)
            try:
                reports.append(self.crs_report(number))
            except SearchError as exc:
                self._note_error(f"CRS report {number} unavailable: {exc}")
            if len(reports) >= _MAX_CRS_REPORTS:
                break
        return reports


def congress_flags(
    links: list[CongressLink], errors: list[str] | None = None
) -> list[str]:
    """Turn missing coverage into visible flags without treating it as clean."""
    flags: list[str] = []
    seen: set[str] = set()
    for link in links:
        if link.status not in {"unavailable", "not_assessed"}:
            continue
        flag = (
            f"Congressional history {link.status} for {link.usc_section}: "
            f"{link.detail}"
        )
        if flag not in seen:
            seen.add(flag)
            flags.append(flag)
    for error in errors or []:
        flag = f"Congressional history source error: {error}"
        if flag not in seen:
            seen.add(flag)
            flags.append(flag)
    return flags


def verify_analysis_congress(
    analysis: LegalAnalysis, cases: list[CaseRecord]
) -> CongressBundle:
    """Retrieve congressional links for cited 38 U.S.C. sections.

    The feature is gated by ``AUTHORITY_VERIFICATION`` and by
    ``CONGRESS_API_KEY``. Without either key the result is explicit
    ``not_assessed`` coverage, not a clean/no-history finding.
    """
    settings = get_settings()
    if not settings.authority_verification:
        return CongressBundle()

    # Imported lazily to keep the existing authority module independent of
    # this optional layer.
    from .authorities import _analysis_text, _extract_usc_sections

    text = _analysis_text(analysis, cases)
    sections = _extract_usc_sections(text)
    if not sections:
        return CongressBundle()

    client = CongressClient()
    links = [client.link_section(section) for section in sections]
    reports = client.crs_reports_for_text(text)
    return CongressBundle(
        links=links,
        crs_reports=reports,
        flags=congress_flags(links, client.errors),
    )
