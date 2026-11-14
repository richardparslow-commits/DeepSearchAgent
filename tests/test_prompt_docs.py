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


def test_gem_prompts_do_not_expand_bare_issues_to_all_nine_elements():
    for path in AUTHORITY_PROMPTS:
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        assert "assume all nine elements apply" not in lowered, path.name
        assert "list all nine elements" not in lowered, path.name
        assert "all nine elements apply" not in lowered, path.name
        assert "do not assume all nine" in lowered, path.name
        assert "Aid and Attendance" in text, path.name


def test_gem_prompts_require_same_element_for_contradictions():
    gem = (ROOT / "GEMINI-GEM.md").read_text(encoding="utf-8")
    progressive = (ROOT / "PROGRESSIVE-GEM.md").read_text(encoding="utf-8")

    assert "same claim element or legal question" in gem
    assert "share a statute and a claim element or legal question" in progressive
    assert "share only a broad statute" in gem
    assert "share only a broad statute" in progressive


def test_knowledge_pack_requires_source_coverage_and_authority_for_confidence():
    text = KNOWLEDGE_PACK.read_text(encoding="utf-8")

    assert "supported by at least one cited case holding" in text
    assert "coverage score at least 0.60" in text
    assert "all cases from a non-binding" in text


def test_knowledge_pack_requires_explicit_appellant_role():
    text = KNOWLEDGE_PACK.read_text(encoding="utf-8")

    assert "require an explicit appellant role" in text
    assert "do not assume the veteran appealed" in text
    assert "empty or unknown appellant role" in text
    for path in AUTHORITY_PROMPTS:
        assert "do not assume the veteran appealed" in path.read_text(encoding="utf-8")


def test_knowledge_pack_aligns_benefit_of_doubt_with_lynch():
    text = KNOWLEDGE_PACK.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "Lynch v. McDonough, 21 F.4th 776" in text
    assert "approximate balance" in lowered
    assert "nearly equal" in lowered
    assert "not limited to equipoise" in lowered
    assert "relative equipoise" not in lowered
    assert "when evidence is in equipoise, the veteran wins" not in lowered


def test_knowledge_pack_jandreau_has_all_three_diagnosis_paths():
    text = KNOWLEDGE_PACK.read_text(encoding="utf-8")

    assert "(1) a layperson is competent to identify the condition" in text
    assert "(2) the layperson is reporting a contemporaneous medical diagnosis" in text
    assert "(3) lay testimony describing symptoms at the time supports a later diagnosis" in text
    assert "fact issue for the Board" in text
    assert "diagnosis or causation is beyond lay competence" in text
    assert "tinnitus, a limp, a rash" not in text


def test_knowledge_pack_does_not_require_medical_evidence_for_every_diagnosis():
    text = KNOWLEDGE_PACK.read_text(encoding="utf-8")

    assert "Medical evidence is required to establish diagnosis" not in text
    assert "may not reject independent medical evidence" in text
    assert "where a determination requires medical expertise" in text


def test_knowledge_pack_distinguishes_precedential_cavc_decisions():
    text = KNOWLEDGE_PACK.read_text(encoding="utf-8")

    assert "### CAVC (binding on the Board" not in text
    assert "### CAVC (precedential decisions bind the Board; persuasive on the Federal Circuit)" in text
    assert "Precedential CAVC decisions (three-judge panel or en banc) bind the Board" in text
    assert "Non-precedential memorandum decisions and single-judge actions are persuasive only" in text
    assert "U.S. Vet. App. R. 30(a)" in text


def test_gem_prompt_uses_lynch_for_benefit_of_doubt():
    gem = (ROOT / "GEMINI-GEM.md").read_text(encoding="utf-8")
    progressive = (ROOT / "PROGRESSIVE-GEM.md").read_text(encoding="utf-8")

    assert "Shedden" not in gem
    assert "Lynch" in gem
    assert "CAVC decisions bind the Board only when precedential" in gem
    assert "CAVC decisions bind the Board only when precedential" in progressive
