"""Regression tests for the Gemini knowledge pack's legal rules."""

import re
from pathlib import Path

from va_legal_agent.interpretation import _outcome_direction

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_PACK = ROOT / "GEMINI-GEM-KNOWLEDGE.md"
AUTHORITY_PROMPTS = (ROOT / "GEMINI-GEM.md", ROOT / "PROGRESSIVE-GEM.md")


def test_secretary_appellant_rule_matches_party_aware_code():
    section = KNOWLEDGE_PACK.read_text(encoding="utf-8").split(
        "### Secretary-appellant flip rule", 1
    )[1].split("\n###", 1)[0]

    assert "denied is always unfavorable" not in section
    assert "granted/vacated/remanded are always favorable" not in section

    expected = {
        "affirmed": ("favorable", 1),
        "dismissed": ("favorable", 1),
        "denied": ("favorable", 1),
        "granted": ("unfavorable", -1),
        "vacated": ("unfavorable", -1),
        "remanded": ("unfavorable", -1),
    }
    for outcome, (label, direction) in expected.items():
        assert re.search(
            rf"^- \*\*{outcome}\*\*:.*= {label}$",
            section,
            re.MULTILINE,
        ), outcome
        assert _outcome_direction(outcome, appellant_role="secretary") == direction


def test_gem_prompts_require_precedential_cavc_authority():
    for path in AUTHORITY_PROMPTS:
        text = path.read_text(encoding="utf-8")
        assert "binding when precedential" in text, path.name
        assert "binding (Federal Circuit, CAVC)" not in text, path.name
