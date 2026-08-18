"""Deep-read mode: chunked map-reduce summarization of full opinion text.

The normal pipeline ingests snippets and only the first few PDF pages. Deep
mode (``DEEP_READ=1``) fetches the full body of the top cases, splits it into
chunks, digests each chunk deterministically (map: holding / outcome /
statutes extraction), and synthesizes the digests into one per-case summary
(reduce: LLM when ``OPENAI_API_KEY`` is set, a plain join otherwise). The
reasoning pass then cross-references these deep summaries across the whole
corpus instead of relying on truncated snippets.
"""

from __future__ import annotations

import logging
import re

from .config import get_settings
from .fetch import (
    FetchError,
    extract_holding_sentence,
    extract_outcome,
    extract_statutes,
    fetch_full_text,
)
from .llm import call_openai, llm_enabled
from .models import CaseRecord
from .providers import CourtListenerProvider, SearchError

logger = logging.getLogger(__name__)

_EMPTY_DIGEST = "(nothing relevant)"


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split *text* into chunks of at most *max_chars*, on paragraph boundaries.

    Paragraphs are packed greedily into chunks; a single paragraph longer than
    the cap is hard-split. Empty text yields no chunks.
    """
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(para) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(
                para[start : start + max_chars]
                for start in range(0, len(para), max_chars)
            )
        elif len(current) + len(para) + 2 <= max_chars:
            current = f"{current}\n\n{para}" if current else para
        else:
            # A paragraph that won't fit starts a new chunk; an empty current
            # (e.g. a first paragraph exactly at the cap) must not emit a
            # blank chunk that would later digest to "(nothing relevant)".
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


def _digest_chunk(chunk: str) -> str:
    """Map step: extract the chunk's salient facts deterministically."""
    parts: list[str] = []
    holding = extract_holding_sentence(chunk)
    if holding:
        parts.append(f"Holding: {holding}")
    outcome = extract_outcome(chunk)
    if outcome:
        parts.append(f"Outcome: {outcome}")
    statutes = extract_statutes(chunk)
    if statutes:
        parts.append(f"Statutes: {', '.join(statutes)}")
    if not parts:
        return _EMPTY_DIGEST
    return "; ".join(parts)


def _build_reduce_prompt(
    case_title: str, issue: str, digests: list[str]
) -> list[dict[str, str]]:
    """Build the reduce-step messages that synthesize the chunk digests."""
    digest_lines = "\n".join(f"- {digest}" for digest in digests)
    system = (
        "You are summarizing a U.S. veterans-law decision for legal research. "
        "The section digests below were extracted from one decision. Synthesize "
        "them into a single coherent summary (under 200 words) of what this "
        "decision holds for the issue, preserving the holding, outcome, and "
        "cited statutes. Cite the decision by name. Do not invent facts."
    )
    user = (
        f"Decision: {case_title}\nIssue: {issue}\n\nSection digests:\n{digest_lines}\n\n"
        "Return only the summary."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _synthesize_case(case_title: str, issue: str, digests: list[str]) -> str:
    """Reduce step: combine chunk digests into one per-case deep summary."""
    if not digests:
        return ""
    if llm_enabled():
        try:
            text = call_openai(_build_reduce_prompt(case_title, issue, digests))
            if text and text.strip():
                return text.strip()
        except Exception as exc:  # noqa: BLE001 - LLM is an optional enhancement
            logger.warning("Deep-read synthesis failed; joining digests: %s", exc)
    return "\n".join(f"- {digest}" for digest in digests)


def _fetch_case_text(case: CaseRecord) -> str:
    """Return the full opinion body for *case*, via API when available.

    CourtListener cases carry ``courtlistener_opinion_id`` and their frontend
    page is WAF-challenged, so pull the body from the REST detail endpoint
    instead of scraping the URL. Every other source (BVA ``.txt`` decisions,
    CAVC/CAFC pages, DDG links) falls back to the generic URL fetch.
    """
    if case.courtlistener_opinion_id:
        try:
            text = CourtListenerProvider().fetch_opinion_text(
                int(case.courtlistener_opinion_id)
            )
            if text.strip():
                return text
        except (SearchError, ValueError) as exc:
            logger.warning(
                "CourtListener opinion text fetch failed for %s: %s", case.url, exc
            )
    settings = get_settings()
    return fetch_full_text(case.url, max_pages=settings.deep_read_pages)


def deep_read_case(case: CaseRecord, issue: str) -> str:
    """Deep-read one case: fetch its full text and return its deep summary.

    Returns ``''`` when the body cannot be fetched (network failure, size cap)
    or contains nothing to summarize; the caller then keeps the snippet-based
    path.
    """
    settings = get_settings()
    try:
        text = _fetch_case_text(case)
    except FetchError as exc:
        logger.warning("Deep-read fetch failed for %s: %s", case.url, exc)
        return ""
    if not text or not text.strip():
        return ""
    chunks = chunk_text(text, settings.deep_chunk_chars)
    digests = [_digest_chunk(chunk) for chunk in chunks]
    return _synthesize_case(case.title, issue, digests)


def deep_read_cases(cases: list[CaseRecord], issue: str, limit: int | None = None) -> None:
    """Deep-read the top *limit* cases in place, setting ``case.deep_summary``.

    Best-effort: a case whose body cannot be fetched keeps an empty deep
    summary and the reasoning pass falls back to its holding/snippet.
    """
    settings = get_settings()
    if limit is None:
        limit = settings.deep_read_limit
    for case in cases[:limit]:
        if not case.url:
            continue
        case.deep_summary = deep_read_case(case, issue)
