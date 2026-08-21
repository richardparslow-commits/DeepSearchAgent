"""Best-effort extraction of case details from source pages.

Extracts citation, decision date, holding, docket number, judge attribution,
cited VA statutes, and procedural outcome from HTML pages and PDF opinions.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException as CffiRequestException

from .config import get_settings
from .search import http_proxy_kwargs

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


# Reporter/docket citation patterns, compiled once at import and reused by
# every fetch (extract_citation runs once per fetched page/PDF).
_CITATION_PATTERNS: tuple[tuple[re.Pattern[str], object], ...] = (
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
    # BVA citation numbers look like "2100634" or "A25049742" (letter prefix).
    # The label is usually written "Citation Nr." (with a period), but also
    # appears as "Nr:" or bare "Nr", so the period and colon are each optional.
    (re.compile(r"Citation\s*Nr\.?:?\s*([A-Za-z]?\d{4,})"),
     lambda m: f"BVA Citation Nr. {m.group(1)}"),
)


def extract_citation(text: str) -> str:
    """Return the first recognized reporter/docket citation found in the text, or ''."""
    for pattern, formatter in _CITATION_PATTERNS:
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
    r"\b38\s*(?:U\.\s*S\.\s*C(?:\.\s*A)?\.?|U\.S\.C(?:\.A)?\.?|USC)\s*(?:§|\s+)?\s*([0-9]{2,4}[A-Za-z]?(?:\([A-Za-z0-9]+\))?(?:\([A-Za-z0-9]+\))?(?:\s*\([A-Za-z0-9]+\))?)",
    re.IGNORECASE,
)
_CFR_PATTERN = re.compile(
    r"\b38\s*(?:C\.\s*F\.\s*R\.?|CFR)\s*(?:§|\s+)?\s*([0-9]{1,2}\.[0-9A-Za-z]{1,6}(?:\([A-Za-z0-9]+\))?)",
    re.IGNORECASE,
)
# Detects the "A" annotator inside a matched 38 U.S.C.A. citation header (e.g.
# "38 U.S.C.A. § "), so annotated cites can be labeled apart from "38 U.S.C.".
_USCA_PREFIX = re.compile(r"\b38\s*U\.?\s*S\.?\s*C\.?\s*A\b", re.IGNORECASE)
# Checked in priority order; most appellate dispositions lead with vacatur/remand.
OUTCOME_SIGNALS: tuple[str, ...] = ("vacated", "remanded", "affirmed", "dismissed", "granted", "denied")
_HOLDING_PATTERN = re.compile(
    r"\b(?:we|this court|the court|the board|this decision|the panel)\s+"
    r"(?:hold|holds|held|conclude|concludes|concluded|find|finds|found|erred|errs)\b",
    re.IGNORECASE,
)

# A holding must carry substantive content beyond the "We hold / the Court
# holds" lead-in; this rejects placeholder fragments like "We hold that so.".
_MIN_HOLDING_WORDS = 6


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


def _normalize_statute_value(value: str) -> str:
    """Strip whitespace and trailing parentheticals from a matched section number."""
    value = re.sub(r"\s+", "", value)
    return re.sub(r"\([A-Za-z]{2,}\)$", "", value)


def _format_us_citation(match: "re.Match[str]") -> str:
    """Format one 38 U.S.C. match, keeping an explicit 38 U.S.C.A. annotation."""
    header = match.group(0)[: match.start(1) - match.start(0)]
    prefix = "38 U.S.C.A. § " if _USCA_PREFIX.search(header) else "38 U.S.C. § "
    return prefix + _normalize_statute_value(match.group(1))


def extract_statutes(text: str, limit: int = 6) -> list[str]:
    """Return unique cited VA statutes in normalized form (38 U.S.C. / 38 C.F.R.)."""
    statutes: list[str] = []
    for match in _USC_PATTERN.finditer(text):
        entry = _format_us_citation(match)
        if entry not in statutes:
            statutes.append(entry)
    for match in _CFR_PATTERN.finditer(text):
        entry = "38 C.F.R. § " + _normalize_statute_value(match.group(1))
        if entry not in statutes:
            statutes.append(entry)
    return statutes[:limit]


def extract_outcome(text: str, limit: int = 2) -> str:
    """Return the detected procedural outcome, e.g. 'vacated and remanded'."""
    lowered = text.lower()
    hits = [signal for signal in OUTCOME_SIGNALS if re.search(rf"\b{signal}\b", lowered)]
    return " and ".join(hits[:limit])


def extract_holding_sentence(text: str) -> str:
    """Return the first explicit holding sentence found in the text, or ''.

    The holding is cut at the first semicolon, which introduces a separate
    disposition (e.g. "...; remand follows.") rather than the holding itself.
    Fragments that carry no substantive content ("We hold that so.") are
    skipped so the first real holding is returned.
    """
    normalized = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'“])", normalized)
    for sentence in sentences:
        if not _HOLDING_PATTERN.search(sentence):
            continue
        sentence = sentence.split(";", 1)[0]
        cleaned = sentence.strip().rstrip(";")
        if not cleaned.endswith("."):
            cleaned += "."
        if len(cleaned.split()) < _MIN_HOLDING_WORDS:
            continue
        return cleaned[0].upper() + cleaned[1:]
    return ""


def _extract_pdf_text(content: bytes, max_pages: int = 4) -> str:
    """Extract text from a PDF, reading up to *max_pages* pages (0 = all)."""
    try:
        from pypdf import PdfReader  # required dependency; lazy import keeps failure local

        reader = PdfReader(io.BytesIO(content))
        pages = reader.pages if not max_pages else reader.pages[:max_pages]
        chunks: list[str] = []
        for page in pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - skip unreadable pages
                continue
        return " ".join(chunks)
    except Exception as exc:  # noqa: BLE001 - unparseable PDF yields no details
        logger.warning("Could not extract PDF text: %s", exc)
        return ""


def _read_response_body(response: "requests.Response", max_bytes: int) -> bytes:
    """Read a streamed response body, refusing payloads over *max_bytes*.

    The declared Content-Length is checked first for a cheap early abort; the
    body is then read in bounded chunks so an undeclared oversized payload is
    still rejected before it is buffered in full.
    """
    content_length = response.headers.get("Content-Length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        raise FetchError(
            f"Refusing to download {content_length} bytes from {response.url}: "
            f"exceeds the {max_bytes}-byte cap."
        )
    chunks: list[bytes] = []
    received = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        received += len(chunk)
        if received > max_bytes:
            raise FetchError(f"Response from {response.url} exceeds the {max_bytes}-byte cap.")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_full_text(url: str, timeout: int | None = None, max_pages: int = 0) -> str:
    """Fetch a case page and return its full readable text (deep-read mode).

    Unlike :func:`fetch_case_details`, which extracts a handful of structured
    fields and discards the body, this returns the whole opinion text: every
    PDF page (subject to *max_pages*, 0 = all pages), the full HTML page text,
    or a plain-text decision verbatim. Raises :class:`FetchError` on network
    or size-cap failure.
    """
    settings = get_settings()
    timeout = timeout or settings.request_timeout_seconds
    try:
        # The search layer already bypasses TLS-fingerprint/WAF challenges by
        # impersonating a real Chrome handshake via curl_cffi; the fetch layer
        # must do the same, otherwise enrichment/deep-read of protected pages
        # (CAVC, law-blog mirrors, DDG result targets) gets a 403/challenge
        # body that plain ``requests`` cannot pass. The impersonation also
        # supplies a browser User-Agent, so the app's bot-identifying
        # USER_AGENT is deliberately NOT sent here (overriding the
        # impersonation would re-trigger the block, exactly as with the
        # DuckDuckGo and BVA providers).
        response = cffi_requests.get(
            url,
            timeout=timeout,
            stream=True,
            impersonate="chrome",
            **http_proxy_kwargs(),
        )
        response.raise_for_status()
        content = _read_response_body(response, settings.max_fetch_bytes)
    except (requests.RequestException, CffiRequestException) as exc:
        raise FetchError(f"Failed to fetch {url}: {exc}") from exc

    content_type = response.headers.get("Content-Type", "")
    url_path = url.lower().split("?")[0]
    looks_pdf = "pdf" in content_type.lower() or url_path.endswith(".pdf")
    if looks_pdf:
        return _extract_pdf_text(content, max_pages=max_pages)
    encoding = getattr(response, "encoding", None) or "utf-8"
    text = content.decode(encoding, errors="replace")
    if "text/plain" in content_type.lower() or url_path.endswith(".txt"):
        return text
    soup = BeautifulSoup(text, "html.parser")
    return soup.get_text(" ", strip=True)


def fetch_case_details(url: str, timeout: int | None = None) -> dict[str, str | list[str]]:
    """Fetch a case page or PDF and extract structured details.

    Extracts citation, decision date, holding, docket number, judge, cited
    statutes, and procedural outcome where detectable. Network failures and
    oversized responses raise FetchError; parse failures return empty fields.
    """
    settings = get_settings()
    timeout = timeout or settings.request_timeout_seconds
    try:
        # The search layer already bypasses TLS-fingerprint/WAF challenges by
        # impersonating a real Chrome handshake via curl_cffi; the fetch layer
        # must do the same, otherwise enrichment/deep-read of protected pages
        # (CAVC, law-blog mirrors, DDG result targets) gets a 403/challenge
        # body that plain ``requests`` cannot pass. The impersonation also
        # supplies a browser User-Agent, so the app's bot-identifying
        # USER_AGENT is deliberately NOT sent here (overriding the
        # impersonation would re-trigger the block, exactly as with the
        # DuckDuckGo and BVA providers).
        response = cffi_requests.get(
            url,
            timeout=timeout,
            stream=True,
            impersonate="chrome",
            **http_proxy_kwargs(),
        )
        response.raise_for_status()
        content = _read_response_body(response, settings.max_fetch_bytes)
    except (requests.RequestException, CffiRequestException) as exc:
        raise FetchError(f"Failed to fetch {url}: {exc}") from exc

    content_type = response.headers.get("Content-Type", "")
    url_path = url.lower().split("?")[0]
    looks_pdf = "pdf" in content_type.lower() or url_path.endswith(".pdf")
    if looks_pdf:
        return extract_case_details(_extract_pdf_text(content))

    encoding = getattr(response, "encoding", None) or "utf-8"
    text = content.decode(encoding, errors="replace")
    # BVA decisions are plain-text files; parse them directly rather than as HTML.
    if "text/plain" in content_type.lower() or url_path.endswith(".txt"):
        return extract_case_details(text)

    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    page_text = soup.get_text(" ", strip=True)
    details = extract_case_details(f"{title} {page_text}")
    if not details["holding"] and meta:
        details["holding"] = (meta.get("content") or "").strip()
    return details


def extract_case_details(text: str) -> dict[str, str | list[str]]:
    """Extract all structured details from raw decision text.

    Public counterpart used by the enrichment path and by the CourtListener
    provider when it extracts details from API-delivered opinion text (the
    frontend page is AWS-WAF-challenged and cannot be scraped).
    """
    return {
        "citation": extract_citation(text),
        "decision_date": extract_decision_date(text),
        "holding": extract_holding_sentence(text),
        "docket": extract_docket(text),
        "judge": extract_judge(text),
        "statutes": extract_statutes(text),
        "outcome": extract_outcome(text),
    }
