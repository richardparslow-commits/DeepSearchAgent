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
# Procedural-outcome signals scanned by extract_outcome.
OUTCOME_SIGNALS: tuple[str, ...] = ("vacated", "remanded", "affirmed", "dismissed", "granted", "denied")

# Legal-standard-of-review patterns scanned by extract_legal_standard.
# Veterans-law opinions routinely name the standard in a specific phrase
# ("we review ... for clear error", "de novo review", "abuse of discretion",
# etc.), and the first match is returned because later occurrences are
# usually restatements, not independent standards.  For harmless-error
# variants (no reversible error), both the "harmless" and "reversible"
# forms are captured, but only "harmless error" is returned — the
# next-segment post-processing replaces "no reversible error" and
# "reversible error was not" with "harmless error" so the vocabulary is
# consistent across the corpus.
_STANDARD_REVIEW_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # "We review ... for clear error" / "under the clear error standard"
    (re.compile(r"\b(?:for|under)\s+(?:the\s+)?clear(?:ly)?\s+erroneous\b", re.IGNORECASE), "clear error"),
    (re.compile(r"\bclear(?:ly)?\s+erroneous\s+standard\b", re.IGNORECASE), "clear error"),
    (re.compile(r"\bclear(?:ly)?\s+error\b", re.IGNORECASE), "clear error"),
    # "de novo review" / "review ... de novo"
    (re.compile(r"\bde\s+novo\s+review\b", re.IGNORECASE), "de novo"),
    (re.compile(r"\breview(?:ed|s)?\s+de\s+novo\b", re.IGNORECASE), "de novo"),
    # "abuse of discretion"
    (re.compile(r"\babuse\s+of\s+discretion\b", re.IGNORECASE), "abuse of discretion"),
    # "arbitrary and capricious" / "arbitrary, capricious"
    (re.compile(r"\barbitrary,?\s+(?:and|,)?\s*capricious\b", re.IGNORECASE), "arbitrary and capricious"),
    # "substantial evidence" (the Board's factual findings standard)
    (re.compile(r"\bsubstantial\s+evidence\b", re.IGNORECASE), "substantial evidence"),
    # "harmless error" / "no reversible error"
    (re.compile(r"\bno\s+reversible\s+error\b", re.IGNORECASE), "harmless error"),
    (re.compile(r"\bharmless\s+error\b", re.IGNORECASE), "harmless error"),
    (re.compile(r"\b(?:was|is|be|are)\s+harmless\b", re.IGNORECASE), "harmless error"),
    # "prejudicial error"
    (re.compile(r"\bprejudicial\s+error\b", re.IGNORECASE), "prejudicial error"),
    # "the Board's finding is not clearly erroneous" / "not clearly erroneous"
    (re.compile(r"\bnot\s+clear(?:ly)?\s+erroneous\b", re.IGNORECASE), "clear error"),
    # "we will not disturb" (deferential standard)
    (re.compile(r"\b(?:we\s+)?will\s+not\s+disturb\b", re.IGNORECASE), "deferential (will not disturb)"),
    # "plenary review"
    (re.compile(r"\bplenary\s+review\b", re.IGNORECASE), "plenary review"),
    # "independent review"
    (re.compile(r"\bindependent\s+review\b", re.IGNORECASE), "independent review"),
    # "reasonable mind might accept" (tag for substantial evidence)
    (re.compile(
        r"\breasonable\s+mind\s+(?:might|could|can|would)\s+accept\b",
        re.IGNORECASE,
    ), "substantial evidence"),
    # "so significant as to have affected the outcome" (tag for prejudicial error)
    (re.compile(
        r"\bso\s+significant\s+as\s+to\s+(?:have\s+)?affect(?:ed)?\s+the\s+outcome\b",
        re.IGNORECASE,
    ), "prejudicial error"),
)


def extract_legal_standard(text: str) -> str:
    """Return the legal standard of review the court applied, or ``''``.

    Veterans-law opinions routinely name the standard in a boilerplate
    paragraph near the beginning of the analysis: "We review the Board's
    factual findings for clear error and its legal conclusions de novo."
    The first match is returned because later occurrences are usually
    restatements of the same standard, not independent standards.
    """
    if not text or not text.strip():
        return ""
    for pattern, label in _STANDARD_REVIEW_PATTERNS:
        if pattern.search(text):
            return label
    return ""



# Party-role detection: who appealed? In VA law, "affirmed" means very
# different things depending on the appellant. When the veteran appeals a
# denial and the court affirms, that's unfavorable. When the Secretary
# cross-appeals a grant and the court affirms, that's favorable to the
# veteran. These patterns detect the appellant from the text surrounding
# "appeal" / "appellant" / "cross-appeal".
_SECRETARY_PATTERNS = (
    re.compile(r"\b(?:Secretary|VA|Department of Veterans Affairs)\'s?\s+(?:cross-?)?appeal\b", re.IGNORECASE),
    re.compile(r"\b(?:Secretary|VA)\s+(?:cross-?)?appealed\b", re.IGNORECASE),
    re.compile(r"\bappellant[, ]+(?:the\s+)?Secretary\b", re.IGNORECASE),
    re.compile(r"\b(?:Secretary|VA)\s+is\s+(?:the\s+)?appellant\b", re.IGNORECASE),
)
_VETERAN_PATTERNS = (
    re.compile(r"\b(?:veteran|claimant|appellant)\'s?\s+appeal\b", re.IGNORECASE),
    re.compile(r"\bveteran\s+appealed\b", re.IGNORECASE),
    re.compile(r"\bappellant[, ]+(?:the\s+)?veteran\b", re.IGNORECASE),
    re.compile(r"\bveteran\s+is\s+(?:the\s+)?appellant\b", re.IGNORECASE),
)
# "appellant" alone (without a Secretary/veteran qualifier) is ambiguous; we
# only classify when the text explicitly identifies the appellant.
_APP_ROLE_VETERAN = "veteran"
_APP_ROLE_SECRETARY = "secretary"
_APP_ROLE_UNKNOWN = "unknown"
_HOLDING_PATTERN = re.compile(
    r"\b(?:we|this court|the court|the board|this decision|the panel)\s+"
    r"(?:\w+\s+)?"  # allow one optional adverb ("also", "further", "therefore")
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


def extract_appellant_role(text: str) -> str:
    """Return who appealed: 'veteran', 'secretary', or 'unknown'.

    In VA law, the same outcome signal can mean opposite things depending on
    the appellant. ``"affirmed"`` when the veteran appealed a denial is
    unfavorable, but ``"affirmed"`` when the Secretary cross-appealed a grant
    is favorable to the veteran. Without this, the deterministic
    contradiction detector and the impact-note layer would classify every
    ``"affirmed"`` as unfavorable, missing the Secretary-appeal case.

    Detection is conservative: only explicit textual identification of the
    appellant (``"the veteran's appeal"``, ``"the Secretary's cross-appeal"``,
    ``"appellant, the Secretary"``) triggers a classification. ``"appellant"``
    alone without a qualifier returns ``'unknown'`` so the caller falls back
    to the default outcome classification rather than guessing.
    """
    if any(pat.search(text) for pat in _SECRETARY_PATTERNS):
        return _APP_ROLE_SECRETARY
    if any(pat.search(text) for pat in _VETERAN_PATTERNS):
        return _APP_ROLE_VETERAN
    return _APP_ROLE_UNKNOWN


# --- Citation-treatment labels ---
# Basic (word, standard_label) pairs. The extractor finds each word in the
# text, then scans the surrounding 200-char window for the nearest case name.
_TREATMENT_TERMS: tuple[tuple[str, str], ...] = (
    ("overruled", "overruled"),
    ("overrule", "overruled"),
    ("overruling", "overruled"),
    ("abrogated", "abrogated"),
    ("abrogate", "abrogated"),
    ("abrogating", "abrogated"),
    ("distinguished", "distinguished"),
    ("distinguish", "distinguished"),
    ("distinguishing", "distinguished"),
    ("declined to follow", "declined to follow"),
    ("decline to follow", "declined to follow"),
    ("declining to follow", "declined to follow"),
    ("questioned", "questioned"),
    ("questioning", "questioned"),
    ("criticized", "criticized"),
    ("criticize", "criticized"),
    ("criticizing", "criticized"),
    ("followed", "followed"),
    ("follow", "followed"),
    ("following", "followed"),
    ("reaffirmed", "reaffirmed"),
    ("reaffirm", "reaffirmed"),
    ("reaffirming", "reaffirmed"),
)


_CASE_NAME_PATTERN = re.compile(
    r"(?:^|[\s,;])"
    r"("
    r"[A-Z]\w+(?:,?\s+(?:Inc\.?|LLC|Corp\.?|Ltd\.?|Co\.?))?"
    r"(?:\s+[a-z]+)?"
    r"\s+v\.\s+[A-Z][A-Za-z'0-9]+"
    r"(?:\s+[A-Z][A-Za-z'0-9]+)*"
    r")"
    r"(?:\s*\(\d{4}\))?"
    r"(?=[.,;]|\s+(?:on|in|at|by|for|as|with|without|under|over|which|that|because|and|or|but|the)\b|$)"
)

# Maximum citation treatments extracted per decision.
_MAX_TREATMENTS = 8

# Characters on each side of a treatment-term hit that are scanned for a
# nearby case name.
_TREATMENT_WINDOW = 200


def extract_citation_treatments(text: str) -> list[dict[str, str]]:
    """Return how this decision treated its cited authorities.

    Two-pass approach: first find treatment words ("overruled",
    "distinguished", "followed", etc.) in *text*, then scan a window
    around each hit for a case-name reference ("Word v. Word").
    Returns a list of ``{"cited_case": "Smith v. Wilkie", "treatment":
    "distinguished"}`` dicts in document order. Returns an empty list
    when no treatments are found or *text* is empty.
    """
    if not text or not text.strip():
        return []
    treatments: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    # Tracks character ranges already claimed by a longer treatment term
    # so shorter sub-terms don't double-fire ("follow" within
    # "declined to follow").
    covered_ranges: list[tuple[int, int]] = []

    # Process longer terms first so "declined to follow" claims its range
    # before the single-word "follow" term fires.
    for term, label in sorted(_TREATMENT_TERMS, key=lambda t: -len(t[0])):
        term_pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        for match in term_pattern.finditer(text):
            idx = match.start()
            idx_end = match.end()
            # Skip if this position is already claimed by a longer term.
            if any(s <= idx < e and s < idx_end <= e for s, e in covered_ranges):
                continue
            covered_ranges.append((idx, idx_end))
            # Scan a window around the hit for the nearest case name.
            start = max(0, idx - _TREATMENT_WINDOW)
            end = min(len(text), idx + len(term) + _TREATMENT_WINDOW)
            window = text[start:end + 1]
            # Find ALL case names in the window, then pick the nearest one
            # (smallest distance from the treatment word).
            case_hits = [
                (abs(cm.start() + start - idx), cm.group(1).strip())
                for cm in _CASE_NAME_PATTERN.finditer(window)
            ]
            if not case_hits:
                continue
            # Pick the nearest case name.
            _, raw_name = min(case_hits, key=lambda h: h[0])
            case_name = re.sub(r"\s+", " ", raw_name)
            key = (case_name, label)
            if key not in seen:
                seen.add(key)
                treatments.append({"cited_case": case_name, "treatment": label})
                if len(treatments) >= _MAX_TREATMENTS:
                    return treatments

    return treatments


# ── Precedential-status detection ───────────────────────────────────────────

# Markers that signal a decision is non-precedential (memorandum disposition,
# unpublished order, or expressly designated as not citable).  BVA decisions
# commonly carry "NOT FOR PUBLICATION" at the top; CAVC single-judge orders
# use "NOT PRECEDENTIAL" or "Do not cite as precedent"; some federal circuits
# use "unpublished".
_PRECEDENTIAL_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bnot\s+for\s+publication\b",
        r"\bnon[-\s]?precedential\b",
        r"\bnot\s+precedential\b",
        r"\bunpublished\b",
        r"\bdo\s+not\s+cite\s+(?:this|as)\b",
        r"\bdid\s+not\s+serve\s+as\s+precedent\b",
        r"\bno\s+precedential\s+(?:value|effect)\b",
        r"\b(?:this|the)\s+disposition\s+is\s+not\s+(?:precedential|[a-z]+\s+as\s+precedent)\b",
        r"\bthis\s+order\b.*\bnot\s+(?:to\s+be|citable\s+as)\b",
        r"\bsingle\s+judge\b.*\b(?:designation|disposition)\b",
        r"\bmemorandum\s+decision\b",
        r"\bnon-?citable\b",
    )
)


def extract_precedential_status(text: str) -> bool:
    """Return ``False`` when the text signals a non-precedential decision.

    Veterans-law decisions come in two flavors: precedential (published, can
    be cited as binding authority) and non-precedential (memorandum
    dispositions, unpublished orders, single-judge designations — persuasive
    at best).  A decision is deemed precedential (``True``) unless at least
    one non-precedential marker is found in *text*.  Returns ``True`` for
    empty text (assume precedential when no body is available).

    The result feeds the ranking layer (a 0.25 penalty to ``composite_score``
    for non-precedential decisions within their tier) and the interpretation
    layer (flagging which holdings come from non-binding authority).
    """
    if not text or not text.strip():
        return True
    for regex in _PRECEDENTIAL_MARKERS:
        if regex.search(text):
            return False
    return True
# Veterans-law opinions often articulate multiple holdings (e.g. one on
# nexus analysis and one on benefit-of-the-doubt); the cap bounds the field
# size while still capturing the common case of 2–3 holdings.
_MAX_HOLDING_SENTENCES = 5


def extract_holding_sentences(text: str) -> list[str]:
    """Return all explicit holding sentences found in the text (up to a cap).

    Each holding is cut at the first semicolon (which introduces a separate
    disposition rather than the holding itself), must carry substantive
    content beyond the "We hold" lead-in (``_MIN_HOLDING_WORDS``), and is
    sentence-cased. Fragments like ``"We hold that so."`` are skipped so the
    first *real* holding is returned, not a placeholder. Returns ``[]`` when
    no holding sentence is found.

    Multiple holdings are returned in document order: e.g. a decision that
    says ``"We hold that the Board erred in its nexus analysis. We also hold
    that the benefit-of-the-doubt rule was not applied."`` yields both
    sentences so the interpretation layer sees the full analytical picture
    instead of only the first.
    """
    normalized = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'“])", normalized)
    holdings: list[str] = []
    for sentence in sentences:
        if not _HOLDING_PATTERN.search(sentence):
            continue
        sentence = sentence.split(";", 1)[0]
        cleaned = sentence.strip().rstrip(";")
        if not cleaned.endswith("."):
            cleaned += "."
        if len(cleaned.split()) < _MIN_HOLDING_WORDS:
            continue
        holdings.append(cleaned[0].upper() + cleaned[1:])
        if len(holdings) >= _MAX_HOLDING_SENTENCES:
            break
    return holdings


def extract_holding_sentence(text: str) -> str:
    """Return the first explicit holding sentence found in the text, or ''.

    Backward-compatible wrapper around :func:`extract_holding_sentences` that
    returns only the first holding, preserving the original single-return
    contract for callers that haven't been upgraded yet.
    """
    holdings = extract_holding_sentences(text)
    return holdings[0] if holdings else ""


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
    holdings = extract_holding_sentences(text)
    return {
        "citation": extract_citation(text),
        "decision_date": extract_decision_date(text),
        # Join all holding sentences so downstream consumers (the
        # interpretation layer, the LLM reasoning prompt, and the coverage
        # scan) see every articulated holding, not just the first. The
        # sentences are already individually period-terminated.
        "holding": " ".join(holdings),
        "docket": extract_docket(text),
        "judge": extract_judge(text),
        "statutes": extract_statutes(text),
        "outcome": extract_outcome(text),
        "appellant_role": extract_appellant_role(text),
        "legal_standard": extract_legal_standard(text),
        "citation_treatments": extract_citation_treatments(text),
        "precedential": extract_precedential_status(text),
    }
