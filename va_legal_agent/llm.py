"""Optional OpenAI-based interpretation of the top cases for a claim issue.

Enabled only when OPENAI_API_KEY is set and the optional `openai` package is
installed. Any failure falls back to the template-based analysis.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import CaseRecord

logger = logging.getLogger(__name__)


def llm_enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


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

    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL") or None,
    )
    completion = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=messages,
        timeout=60,
        max_tokens=700,
    )
    return (completion.choices[0].message.content or "").strip()


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