"""Optional OpenAI-based interpretation of the top cases for a claim issue.

Enabled only when OPENAI_API_KEY is set and the optional `openai` package is
installed. Any failure falls back to the template-based analysis.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config import get_settings
from .models import Contradiction

if TYPE_CHECKING:
    from .models import CaseRecord

logger = logging.getLogger(__name__)


def llm_enabled() -> bool:
    return bool(get_settings().openai_api_key)


def _build_messages(issue: str, claim_type: str, cases: "list[CaseRecord]") -> list[dict[str, str]]:
    case_lines = "\n".join(
        f"- {case.title} ({case.court})"
        + (f" [{case.citation}]" if case.citation else "")
        + f": {case.holding or case.snippet}"
        for case in cases
    )
    system = (
        "You are a careful U.S. veterans-law research assistant. Explain how the listed "
        "decisions affect VA compensation claims on the given issue. Refer to the specific "
        "cases, stay under 200 words, and do not invent holdings or citations."
    )
    user = f"Claim issue: {issue}\nBenefit type: {claim_type}\n\nCases:\n{case_lines}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _call_openai(messages: list[dict[str, str]]) -> str:
    from openai import OpenAI  # optional dependency

    settings = get_settings()
    client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    completion = client.chat.completions.create(
        messages=messages,
        model=settings.openai_model,
        timeout=settings.openai_timeout_seconds,
        max_tokens=settings.openai_max_tokens,
    )
    return (completion.choices[0].message.content or "").strip()


@dataclass
class ReasoningResult:
    """Structured LLM reasoning over the retrieved cases.

    ``reconciled_principles`` are legal principles that hold across the cases,
    each phrased with its supporting case(s) cited inline; ``contradictions``
    flag genuine conflicts between two decisions; ``synthesis`` is the
    reconciling narrative for the claim.
    """

    reconciled_principles: list[str] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)
    synthesis: str = ""


def _build_reasoning_messages(
    issue: str, claim_type: str, cases: "list[CaseRecord]", max_tokens: int
) -> list[dict[str, str]]:
    """Build the messages for the reconciling reasoning pass.

    Unlike the single-call narrative (:func:`interpret_cases`), this feeds
    every provided case with its holding, outcome, and statutes so the model
    can reconcile across all of them, flag contradictions, and cite each claim.
    """
    case_lines = "\n\n".join(
        (
            f"{index}. {case.title} ({case.court})"
            + (f" [{case.citation}]" if case.citation else "")
            + (f" -- decided {case.decision_date}" if case.decision_date else "")
            + "\n"
            # Deep-read mode replaces the snippet with a full-text-derived
            # summary; otherwise fall back to the holding, then the snippet.
            + f"   Holding: {case.deep_summary or case.holding or case.snippet or '(no holding extracted)'}"
            + (f"\n   Outcome: {case.outcome}" if case.outcome else "")
            + (f"\n   Statutes: {', '.join(case.statutes)}" if case.statutes else "")
        )
        for index, case in enumerate(cases, start=1)
    )
    system = (
        "You are a meticulous U.S. veterans-law research analyst. Given the claim issue "
        "and the retrieved decisions, reconcile the holdings into coherent guidance. "
        "Respond with ONLY a JSON object with exactly these keys:\n"
        '{"reconciled_principles": ["..."], "contradictions": [{"statement": "...", '
        '"case_a": "...", "case_b": "..."}], "synthesis": "..."}\n'
        "Rules:\n"
        "- reconciled_principles: legal principles that hold across the cases, each a "
        "complete sentence that cites the supporting case(s) inline, e.g. 'Service "
        "connection requires a nexus opinion (Buchanan v. Nicholson).'\n"
        "- contradictions: genuine conflicts between two decisions on the same point "
        "(differing holdings or outcomes). Use the exact case titles for case_a and "
        "case_b. Empty list if the cases agree.\n"
        "- synthesis: a paragraph under 250 words reconciling the holdings into guidance "
        "for the claim, citing the case(s) behind each claim you make.\n"
        "Do not invent cases, holdings, citations, or statutes. Do not make any claim "
        "the listed cases do not support."
    )
    word_budget = max(max_tokens // 4, 50)
    user = (
        f"Claim issue: {issue}\nBenefit type: {claim_type}\n\nCases:\n{case_lines}\n\n"
        f"Keep the synthesis under {word_budget} words. Return only the JSON object."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_reasoning_response(text: str) -> ReasoningResult | None:
    """Parse the LLM's JSON reasoning response into a :class:`ReasoningResult`.

    Tolerates markdown code fences and trailing prose by locating the first
    balanced ``{...}`` block when strict JSON parsing fails. Returns ``None``
    when the text carries no usable structured content.
    """
    if not text or not text.strip():
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        lines = cleaned.splitlines()
        if lines and lines[0].strip().lower() == "json":
            lines = lines[1:]
        cleaned = "\n".join(lines).strip()

    data = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict):
        return None

    principles = [
        str(item).strip()
        for item in (data.get("reconciled_principles") or [])
        if isinstance(item, str) and item.strip()
    ]
    contradictions: list[Contradiction] = []
    for item in data.get("contradictions") or []:
        if not isinstance(item, dict):
            continue
        statement = str(item.get("statement") or "").strip()
        case_a = str(item.get("case_a") or "").strip()
        case_b = str(item.get("case_b") or "").strip()
        if statement and case_a and case_b:
            contradictions.append(Contradiction(statement=statement, case_a=case_a, case_b=case_b))
    synthesis = str(data.get("synthesis") or "").strip()

    if not principles and not contradictions and not synthesis:
        return None
    return ReasoningResult(
        reconciled_principles=principles,
        contradictions=contradictions,
        synthesis=synthesis,
    )


def reason_cases(
    issue: str, claim_type: str, cases: "list[CaseRecord]"
) -> ReasoningResult | None:
    """Return LLM-reconciled reasoning over the cases, or None when unavailable.

    The enhanced reasoning path: reconciles holdings across *all* provided
    cases, flags contradictions between decisions, and cites each claim. Falls
    back to ``None`` (and the caller's deterministic synthesis) when no API key
    is set, ``LLM_REASONING`` is disabled, or the call fails.
    """
    settings = get_settings()
    if not cases or not llm_enabled() or not settings.llm_reasoning:
        return None
    try:
        text = _call_openai(
            _build_reasoning_messages(issue, claim_type, cases, settings.openai_max_tokens)
        )
    except ImportError:
        logger.warning(
            "OPENAI_API_KEY is set but the 'openai' package is not installed "
            "(pip install openai). Skipping LLM reasoning."
        )
        return None
    except Exception as exc:  # noqa: BLE001 - LLM output is an optional enhancement
        logger.warning("LLM reasoning failed; using template synthesis: %s", exc)
        return None
    if not text:
        return None
    return _parse_reasoning_response(text)


def interpret_cases(issue: str, claim_type: str, cases: "list[CaseRecord]") -> str | None:
    """Return an LLM-generated interpretation of the cases, or None when unavailable."""
    if not cases or not llm_enabled():
        return None
    try:
        text = _call_openai(_build_messages(issue, claim_type, cases))
    except ImportError:
        logger.warning(
            "OPENAI_API_KEY is set but the 'openai' package is not installed "
            "(pip install openai). Skipping LLM interpretation."
        )
        return None
    except Exception as exc:  # noqa: BLE001 - LLM output is an optional enhancement
        logger.warning("LLM interpretation failed; using template fallback: %s", exc)
        return None
    return text or None
