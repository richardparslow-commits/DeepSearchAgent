"""Best-effort extraction of case details from source pages.

Extracts citation, decision date, holding, docket number, judge attribution,
cited VA statutes, and procedural outcome from HTML pages and PDF opinions.
"""

from __future__ import annotations

import io
import logging
import os
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    pass


_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec")
_DATE_PATTERN = re.compile(
    r"\b(" + "|".join(_MONTHS + _MONTH_ABBR) + r")\.?\s+(\d{1,2}),?\s+(\d{4})\b"
)
_DATE_HINT = re.compile(r"\b(?:decided|filed|issued|handed down)\b", re.IGNORECASE)


def _citation_patterns() -> list[tuple[re.Pattern[str], object]]:
    return [
        (re.compile(r"\b(\d{1,3})\s+Vet\.?\s?App\.?\s+(\d{1,4})\b"),
         lambda m: f"{m.group(1)} Vet.App. {m.group(2)}"),
        (re.compile(r"\b(\d{1,4})\s+(F\.2d|F\.3d|F\.4th)\s+(\d{1,4})\b"),
         lambda m: f"{m.group(1)} {m.group(2)} {m.group(3)}"),
        (re.compile(r"\b(\d{1,3})\s+S\.?\s?Ct\.?\s+(\d{1,4})\b"),
         lambda m: f"{m.group(1)} S. Ct. {m.group(2)}"),
        (re.compile(r"\b(\d{1,3})\s+U\.S\.\s+(\d{1,4})\b"),
         lambda m: f"{m.group(1)} U.S. {m.group(2)}"),
        (re.compile(r"\b(\d{4})\s+WL\s+(\d+)\b"),
         lambda m: f"{m.group(1)} WL {m.group(2)}"),
        (re.compile(r"Citation\s*Nr?:?\s*(\d{4,})"),
         lambda m: f"BVA Citation Nr. {m.group(1)}"),
    ]


def extract_citation(text: str) -> str:
    """Return the first recognized reporter/docket citation found in the text, or ''."""
    for pattern, formatter in _citation_patterns():
        match = pattern.search(text)
        if match:
            return formatter(match)
    return ""


def _match_to_iso(match: "re.Match[str]") -> str:
    month, day, year = match.group(1), int(match.group(2)), int(match.group(3))
    if month == "Sept":
        month = "Sep"
    for fmt in ("%B", "%b"):
        try:
            return datetime.strptime(f"{month} {day} {year}", f"{fmt} %d %Y").date().isoformat()
        except ValueError:
            continue
    return ""


def extract_decision_date(text: str) -> str:
    """Return an ISO decision date, preferring dates near 'Decided'/'Filed'/'Issued'."""
    for match in _DATE_PATTERN.finditer(text):
        context = text[max(0, match.start() - 40):match.start()]
        if _DATE_HINT.search(context):
            iso = _match_to_iso(match)
            if iso:
                return iso
    for match in _DATE_PATTERN.finditer(text):
        iso = _match_to_iso(match)
        if iso:
            return iso
    return ""


_DOCKET_PATTERNS = (
    re.compile(r"\b(?:Docket|Case|Appeal)\s+No\.?\s*([0-9][0-9\-\s]*[0-9])\b", re.IGNORECASE),
    re.compile(r"\bNo\.\s*([0-9][0-9\-]{3,12})\b"),
)
_JUDGE_PATTERN = re.compile(
    r"\b(?:Chief\s+Judge|Acting\s+Judge|Judge)\s+"
    r"((?:[A-Z]\.\s*)*[A-Z][A-Za-z'\-]+(?:\s+(?:[A-Z]\.\s*)?[A-Z][A-Za-z'\-]+){1,2})\b"
)
_PER_CURIAM_PATTERN = re.compile(r"\bper\s+curiam\b", re.IGNORECASE)
_USC_PATTERN = re.compile(
    r"\b38\s*U\.?\s*S\.?\s*C\.?\s*§*\s*([0-9]{2,4}[A-Za-z]?(?:\s*\([a-zA-Z0-9]+\))*)",
    re.IGNORECASE,
)
_CFR_PATTERN = re.compile(
    r"\b38\s*C\.?\s*F\.?\s*R\.?\s*§*\s*([0-9]{1,2}\.[0-9]{2,4}(?:\s*\([a-zA-Z0-9]+\))*)",
    re.IGNORECASE,
)
# Checked in priority order; most appellate dispositions lead with vacatur/remand.
OUTCOME_SIGNALS: tuple[str, ...] = ("vacated", "remanded", "affirmed", "dismissed", "granted", "denied")
_HOLDING_PATTERN = re.compile(
    r"\b(?:we|this court|the court|the board|this decision)\s+"
    r"(?:hold|holds|held|conclude|concludes|concluded|find|finds|found)\s+that\s+[^.;]{10,400}?[.;]",
    re.IGNORECASE,
)


def extract_docket(text: str) -> str:
    """Return the first docket/case number found (e.g. '19-4433'), or ''."""
    for pattern in _DOCKET_PATTERNS:
        match = pattern.search(text)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


def extract_judge(text: str) -> str:
    """Return the attributed judge's name (or 'Per Curiam'), or ''."""
    match = _JUDGE_PATTERN.search(text)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    if _PER_CURIAM_PATTERN.search(text):
        return "Per Curiam"
    return ""


def extract_statutes(text: str, limit: int = 6) -> list[str]:
    """Return unique cited VA statutes in normalized form (38 U.S.C. / 38 C.F.R.)."""
    statutes: list[str] = []
    for pattern, prefix in ((_USC_PATTERN, "38 U.S.C. § "), (_CFR_PATTERN, "38 C.F.R. § ")):
        for match in pattern.finditer(text):
            entry = prefix + re.sub(r"\s+", "", match.group(1))
            if entry not in statutes:
                statutes.append(entry)
    return statutes[:limit]


def extract_outcome(text: str, limit: int = 2) -> str:
    """Return the detected procedural outcome, e.g. 'vacated and remanded'."""
    lowered = text.lower()
    hits = [signal for signal in OUTCOME_SIGNALS if re.search(rf"\b{signal}\b", lowered)]
    return " and ".join(hits[:limit])


def extract_holding_sentence(text: str) -> str:
    """Return the first explicit holding sentence found in the text, or ''."""
    match = _HOLDING_PATTERN.search(re.sub(r"\s+", " ", text))
    if not match:
        return ""
    sentence = match.group(0).strip().rstrip(";")
    if not sentence.endswith("."):
        sentence += "."
    return sentence[0].upper() + sentence[1:]


def _extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader  # required dependency; lazy import keeps failure local

        reader = PdfReader(io.BytesIO(content))
        chunks: list[str] = []
        for page in reader.pages[:4]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - skip unreadable pages
                continue
        return " ".join(chunks)
    except Exception as exc:  # noqa: BLE001 - unparseable PDF yields no details
        logger.warning("Could not extract PDF text: %s", exc)
        return ""


def fetch_case_details(url: str, timeout: int | None = None) -> dict[str, str | list[str]]:
    """Fetch a case page or PDF and extract structured details.

    Extracts citation, decision date, holding, docket number, judge, cited
    statutes, and procedural outcome where detectable. Network failures raise
    FetchError; parse failures return empty fields.
    """
    timeout = timeout or int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
    headers = {"User-Agent": "Mozilla/5.0 (compatible; VA-Legal-Agent/1.0; +https://example.com)"}
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise FetchError(f"Failed to fetch {url}: {exc}") from exc

    content_type = response.headers.get("Content-Type", "")
    looks_pdf = "pdf" in content_type.lower() or url.lower().split("?")[0].endswith(".pdf")
    if looks_pdf:
        return _details_from_text(_extract_pdf_text(response.content))

    soup = BeautifulSoup(response.text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    page_text = soup.get_text(" ", strip=True)
    details = _details_from_text(f"{title} {page_text}")
    if not details["holding"] and meta:
        details["holding"] = (meta.get("content") or "").strip()
    return details


def _details_from_text(text: str) -> dict[str, str | list[str]]:
    """Extract all structured details from raw decision text."""
    return {
        "citation": extract_citation(text),
        "decision_date": extract_decision_date(text),
        "holding": extract_holding_sentence(text),
        "docket": extract_docket(text),
        "judge": extract_judge(text),
        "statutes": extract_statutes(text),
        "outcome": extract_outcome(text),
    }