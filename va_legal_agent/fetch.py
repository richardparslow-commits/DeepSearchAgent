"""Best-effort extraction of case details (citation, decision date, holding) from source pages."""

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


def _extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader  # required dependency; lazy import keeps failure local

        reader = PdfReader(io.BytesIO(content))
        chunks: list[str] = []
        for page in reader.pages[:2]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - skip unreadable pages
                continue
        return " ".join(chunks)
    except Exception as exc:  # noqa: BLE001 - unparseable PDF yields no details
        logger.warning("Could not extract PDF text: %s", exc)
        return ""


def fetch_case_details(url: str, timeout: int | None = None) -> dict[str, str]:
    """Fetch a case page or PDF and extract citation, decision date, and a holding summary.

    Network failures raise FetchError; parse failures return empty fields.
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
        text = _extract_pdf_text(response.content)
        return {
            "citation": extract_citation(text),
            "decision_date": extract_decision_date(text),
            "holding": "",
        }

    soup = BeautifulSoup(response.text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    holding = (meta.get("content") or "").strip() if meta else ""
    page_text = soup.get_text(" ", strip=True)
    return {
        "citation": extract_citation(page_text) or extract_citation(title),
        "decision_date": extract_decision_date(page_text),
        "holding": holding,
    }