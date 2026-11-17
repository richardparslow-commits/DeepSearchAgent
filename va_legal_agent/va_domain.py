"""Deterministic VBA domain contracts for surfaced cases and adjudication impact.

This module is the bridge between the generic case-record pipeline and the
specific vocabulary VA adjudicators use: D1 case metadata, 38 U.S.C. and
38 C.F.R. provisions, rating diagnostic codes, staged evaluations, and the
M21-1 Adjudication Procedures Manual.

The mapping registry is intentionally versioned and provenance-anchored.  A
mapping records where the provision lives and which adjudication stage it
governs; it does not claim that VA has reviewed the mapping, and it does not
turn a quoted sentence into a holding.  ``provenance_verified`` therefore
means only that the cited source URL is on a primary/archival domain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .models import (
    AppellateHistoryEntry,
    CaseAbstract,
    CaseRecord,
    CitatorCheck,
    LegalAnalysis,
    ProvisionImpact,
    RvsrGuidance,
    SeniorRaterGuidance,
    SurfacedCaseRecord,
)
from .reliability import (
    RELIABILITY_OFFICIAL,
    OFFICIAL_DOMAINS,
    classify_authority_tier,
    classify_source,
)
from .topics import BINDING_COURTS, BOARD_COURT, COURT_CAVC

REGISTRY_VERSION = "2026.11"
PRIMARY_SOURCE_DOMAINS = OFFICIAL_DOMAINS + (
    "uscode.house.gov",
    "ecfr.gov",
    "govinfo.gov",
    "federalregister.gov",
    "knowva.ebenefits.va.gov",
)

M21_DUTY_TO_ASSIST = "M21-1, Part III, Subpart i, Chapter 2"
M21_EXAM_SUFFICIENCY = "M21-1, Part IV, Subpart i, Chapter 3, Sections A-C"
M21_RATING = "M21-1, Part V, Subpart ii"
M21_HLR_CUE = "M21-1, Part X, Subpart ii"

STAGE_INTAKE = "intake_notice"
STAGE_DEVELOPMENT = "development_exam"
STAGE_RATING = "rating_dc_evaluation"
STAGE_EFFECTIVE_DATE = "effective_date"
STAGE_APPELLATE = "appellate_review"


@dataclass(frozen=True)
class ProvisionSpec:
    """One curated provision-to-adjudication mapping."""

    citation: str
    authority_type: str
    stage: str
    effect: str
    aliases: tuple[str, ...] = ()
    m21_1: tuple[str, ...] = ()
    source_url: str = ""


def _usc(section: str) -> str:
    return (
        "https://uscode.house.gov/view.xhtml?req=granuleid:"
        f"USC-prelim-title38-section{section}&num=0&edition=prelim"
    )


def _cfr(part: str, section: str) -> str:
    return f"https://www.ecfr.gov/current/title-38/part-{part}/section-{section}"


# The registry is deliberately explicit rather than inferred from a generic
# "statute hint" table.  Each entry names the adjudication stage and the
# M21-1 manual location an RVSR or DRO must check.
PROVISION_REGISTRY: tuple[ProvisionSpec, ...] = (
    # ── 38 U.S.C. ──────────────────────────────────────────────────────────
    ProvisionSpec(
        "38 U.S.C. § 1110",
        "statute",
        STAGE_RATING,
        "clarifying",
        aliases=("1110",),
        m21_1=(M21_RATING,),
        source_url=_usc("1110"),
    ),
    ProvisionSpec(
        "38 U.S.C. § 1131",
        "statute",
        STAGE_RATING,
        "clarifying",
        aliases=("1131",),
        m21_1=(M21_RATING,),
        source_url=_usc("1131"),
    ),
    ProvisionSpec(
        "38 U.S.C. § 1154(b)",
        "statute",
        STAGE_DEVELOPMENT,
        "clarifying_evidentiary_threshold",
        aliases=("1154",),
        m21_1=(M21_DUTY_TO_ASSIST,),
        source_url=_usc("1154"),
    ),
    ProvisionSpec(
        "38 U.S.C. § 1155",
        "statute",
        STAGE_RATING,
        "clarifying",
        aliases=("1155",),
        m21_1=(M21_RATING,),
        source_url=_usc("1155"),
    ),
    ProvisionSpec(
        "38 U.S.C. § 5103",
        "statute",
        STAGE_INTAKE,
        "clarifying",
        aliases=("5103",),
        m21_1=(M21_DUTY_TO_ASSIST,),
        source_url=_usc("5103"),
    ),
    ProvisionSpec(
        "38 U.S.C. § 5103A",
        "statute",
        STAGE_DEVELOPMENT,
        "clarifying_evidentiary_threshold",
        aliases=("5103A",),
        m21_1=(M21_DUTY_TO_ASSIST, M21_EXAM_SUFFICIENCY),
        source_url=_usc("5103A"),
    ),
    ProvisionSpec(
        "38 U.S.C. § 5104(b)",
        "statute",
        STAGE_APPELLATE,
        "clarifying",
        aliases=("5104",),
        m21_1=(M21_RATING,),
        source_url=_usc("5104"),
    ),
    ProvisionSpec(
        "38 U.S.C. § 5107(b)",
        "statute",
        STAGE_RATING,
        "clarifying_evidentiary_threshold",
        aliases=("5107",),
        m21_1=(M21_RATING,),
        source_url=_usc("5107"),
    ),
    ProvisionSpec(
        "38 U.S.C. § 5110",
        "statute",
        STAGE_EFFECTIVE_DATE,
        "clarifying",
        aliases=("5110",),
        m21_1=(M21_RATING,),
        source_url=_usc("5110"),
    ),
    ProvisionSpec(
        "38 U.S.C. § 7104(d)(1)",
        "statute",
        STAGE_APPELLATE,
        "clarifying",
        aliases=("7104",),
        m21_1=(M21_HLR_CUE,),
        source_url=_usc("7104"),
    ),
    ProvisionSpec(
        "38 U.S.C. § 7105",
        "statute",
        STAGE_APPELLATE,
        "clarifying",
        aliases=("7105",),
        m21_1=(M21_HLR_CUE,),
        source_url=_usc("7105"),
    ),
    ProvisionSpec(
        "38 U.S.C. § 7252",
        "statute",
        STAGE_APPELLATE,
        "clarifying",
        aliases=("7252",),
        m21_1=(M21_HLR_CUE,),
        source_url=_usc("7252"),
    ),
    ProvisionSpec(
        "38 U.S.C. § 7261",
        "statute",
        STAGE_APPELLATE,
        "clarifying",
        aliases=("7261",),
        m21_1=(M21_HLR_CUE,),
        source_url=_usc("7261"),
    ),
    # ── 38 C.F.R. Part 3 (Adjudication) ────────────────────────────────────
    ProvisionSpec(
        "38 C.F.R. § 3.102",
        "regulation_part3",
        STAGE_RATING,
        "clarifying_evidentiary_threshold",
        aliases=("3.102",),
        m21_1=(M21_RATING,),
        source_url=_cfr("3", "3.102"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.103",
        "regulation_part3",
        STAGE_INTAKE,
        "clarifying",
        aliases=("3.103",),
        m21_1=(M21_DUTY_TO_ASSIST,),
        source_url=_cfr("3", "3.103"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.105(a)",
        "regulation_part3",
        STAGE_APPELLATE,
        "clarifying",
        aliases=("3.105",),
        m21_1=(M21_HLR_CUE,),
        source_url=_cfr("3", "3.105"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.156",
        "regulation_part3",
        STAGE_DEVELOPMENT,
        "clarifying_evidentiary_threshold",
        aliases=("3.156",),
        m21_1=(M21_DUTY_TO_ASSIST,),
        source_url=_cfr("3", "3.156"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.159",
        "regulation_part3",
        STAGE_DEVELOPMENT,
        "clarifying_evidentiary_threshold",
        aliases=("3.159",),
        m21_1=(M21_DUTY_TO_ASSIST, M21_EXAM_SUFFICIENCY),
        source_url=_cfr("3", "3.159"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.2501",
        "regulation_part3",
        STAGE_APPELLATE,
        "clarifying",
        aliases=("3.2501",),
        m21_1=(M21_HLR_CUE,),
        source_url=_cfr("3", "3.2501"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.2600",
        "regulation_part3",
        STAGE_APPELLATE,
        "clarifying",
        aliases=("3.2600",),
        m21_1=(M21_HLR_CUE,),
        source_url=_cfr("3", "3.2600"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.2601",
        "regulation_part3",
        STAGE_APPELLATE,
        "clarifying",
        aliases=("3.2601",),
        m21_1=(M21_HLR_CUE,),
        source_url=_cfr("3", "3.2601"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.303",
        "regulation_part3",
        STAGE_RATING,
        "clarifying",
        aliases=("3.303",),
        m21_1=(M21_RATING,),
        source_url=_cfr("3", "3.303"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.304(f)",
        "regulation_part3",
        STAGE_DEVELOPMENT,
        "clarifying_evidentiary_threshold",
        aliases=("3.304",),
        m21_1=(M21_DUTY_TO_ASSIST,),
        source_url=_cfr("3", "3.304"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.306",
        "regulation_part3",
        STAGE_RATING,
        "clarifying",
        aliases=("3.306",),
        m21_1=(M21_RATING,),
        source_url=_cfr("3", "3.306"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.307",
        "regulation_part3",
        STAGE_RATING,
        "clarifying",
        aliases=("3.307",),
        m21_1=(M21_RATING,),
        source_url=_cfr("3", "3.307"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.309",
        "regulation_part3",
        STAGE_RATING,
        "clarifying",
        aliases=("3.309",),
        m21_1=(M21_RATING,),
        source_url=_cfr("3", "3.309"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.310",
        "regulation_part3",
        STAGE_RATING,
        "clarifying",
        aliases=("3.310",),
        m21_1=(M21_RATING,),
        source_url=_cfr("3", "3.310"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.320",
        "regulation_part3",
        STAGE_RATING,
        "clarifying",
        aliases=("3.320",),
        m21_1=(M21_RATING,),
        source_url=_cfr("3", "3.320"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.321",
        "regulation_part3",
        STAGE_RATING,
        "clarifying",
        aliases=("3.321",),
        m21_1=(M21_RATING,),
        source_url=_cfr("3", "3.321"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 3.400",
        "regulation_part3",
        STAGE_EFFECTIVE_DATE,
        "clarifying",
        aliases=("3.400",),
        m21_1=(M21_RATING,),
        source_url=_cfr("3", "3.400"),
    ),
    # ── 38 C.F.R. Part 4 (Rating Schedule) ─────────────────────────────────
    ProvisionSpec(
        "38 C.F.R. § 4.1",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.1",),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.1"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.2",
        "regulation_part4",
        STAGE_DEVELOPMENT,
        "clarifying_evidentiary_threshold",
        aliases=("4.2",),
        m21_1=(M21_EXAM_SUFFICIENCY,),
        source_url=_cfr("4", "4.2"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.3",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.3",),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.3"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.7",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.7",),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.7"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.10",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.10",),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.10"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.14",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.14",),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.14"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.16",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.16", "TDIU", "individual unemployability"),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.16"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.20",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.20", "analogous rating"),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.20"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.21",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.21",),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.21"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.25",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.25", "combined rating"),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.25"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.26",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.26", "bilateral factor"),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.26"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.40",
        "regulation_part4",
        STAGE_RATING,
        "clarifying_evidentiary_threshold",
        aliases=("4.40", "functional loss"),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.40"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.45",
        "regulation_part4",
        STAGE_RATING,
        "clarifying_evidentiary_threshold",
        aliases=("4.45", "painful motion", "flare-up"),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.45"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.59",
        "regulation_part4",
        STAGE_RATING,
        "clarifying_evidentiary_threshold",
        aliases=("4.59",),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.59"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.125",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.125",),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.125"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.126",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.126",),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.126"),
    ),
    ProvisionSpec(
        "38 C.F.R. § 4.130",
        "regulation_part4",
        STAGE_RATING,
        "clarifying",
        aliases=("4.130",),
        m21_1=(M21_RATING,),
        source_url=_cfr("4", "4.130"),
    ),
)

# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

_PAGE_NOISE_RE = re.compile(
    r"(?im)^\s*(?:Case:\s*[\w-]+\s+Page:\s*\d+\s+of\s+\d+\s+Filed:\s*[0-9/]+|"
    r"\d+\s*$)\s*$"
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])")
_SECTION_RE = re.compile(
    r"(?im)^[ \t]*(?:[IVXLCDM]+\.(?:[A-Z]\.)?|[A-Z]\.)[ \t]+"
    r"([A-Z][A-Z0-9 ,'’\-]{2,80})[ \t]*$"
)
_USC_RE = re.compile(
    r"38\s*U\.?\s*S\.?\s*C\.?\s*(?:§{1,2}|sections?)\s*"
    r"([0-9]{3,4})(?:\s*\(([a-zA-Z0-9]+)\))?",
    re.IGNORECASE,
)
_CFR_RE = re.compile(
    r"38\s*C\.?\s*F\.?\s*R\.?\s*(?:§{1,2}|sections?)\s*"
    r"([0-9]+\.[0-9]+)(?:\s*\(([a-zA-Z0-9]+)\))?",
    re.IGNORECASE,
)
_DC_RE = re.compile(
    r"\b(?:DC|Diagnostic\s+Code)\s*(\d{4})(?:\s*-\s*(\d{4}))?\b",
    re.IGNORECASE,
)
_M21_RE = re.compile(
    r"M21-1(?:,\s*Part\s+[IVXL]+)?(?:,\s*Subpart\s+[ivxl]+)?"
    r"(?:,\s*Chapter\s+\d+)?(?:,\s*Sections?\s+[A-Za-z0-9\-]+)?",
    re.IGNORECASE,
)
_STAGED_RE = re.compile(
    r"\b(?:staged\s+rat(?:e|ing)|staged\s+evaluation|Fenderson|Hart\s+v\.|"
    r"temporal\s+rating|for\s+the\s+period(?:\s+on\s+appeal)?)\b",
    re.IGNORECASE,
)
_MEDICATION_RE = re.compile(
    r"\b(?:ameliorative|Jones\s+v\.|McCarroll|effect\s+of\s+medication)\b|"
    r"\bmedication\b(?=[^.]{0,100}\b(?:symptom|rating|evaluation|mental|"
    r"disorder|effect)\b)",
    re.IGNORECASE,
)


def clean_text(text: str) -> str:
    """Normalize line endings and strip PDF page headers/footers."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _PAGE_NOISE_RE.sub("", text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def _sentences(text: str) -> list[str]:
    """Return full sentences without applying a character cap."""
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return []
    return [s.strip() for s in _SENTENCE_RE.split(normalized) if s.strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = re.sub(r"\s+", " ", item or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _first_match_sentence(text: str, pattern: re.Pattern[str]) -> str:
    for sentence in _sentences(text):
        if pattern.search(sentence):
            return sentence
    return ""


def _section_body(text: str, target: str) -> str:
    """Return the body of the first all-caps section whose name matches target."""
    cleaned = clean_text(text)
    headings = list(_SECTION_RE.finditer(cleaned))
    for index, heading in enumerate(headings):
        if target.upper() not in heading.group(1).upper():
            continue
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(cleaned)
        return cleaned[start:end].strip()
    return ""


def _intro(text: str) -> str:
    cleaned = clean_text(text)
    first = _SECTION_RE.search(cleaned)
    return cleaned[: first.start()].strip() if first else cleaned


# ---------------------------------------------------------------------------
# Structured case abstract
# ---------------------------------------------------------------------------

_CONDITION_RES = (
    re.compile(
        r"(?:service\s+connection|service-connected|entitlement\s+to)\s+for\s+"
        r"([^.;\n]{2,140})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:rating|evaluation)\s+(?:higher\s+than\s+\d+%\s+)?for\s+"
        r"([^.;\n]{2,140})",
        re.IGNORECASE,
    ),
    re.compile(r"(?:claim|appeal)\s+for\s+([^.;\n]{2,140})", re.IGNORECASE),
)

_PROCEDURAL_CUES = re.compile(
    r"\b(?:regional office|RO|Board|BVA|DRO|decision review officer|"
    r"Notice of Disagreement|NOD|rating decision|remand(?:ed)?|appeal(?:ed)?|"
    r"Court|Federal Circuit|Supreme Court|supplemental claim|higher-level review|HLR)\b",
    re.IGNORECASE,
)
_ISSUE_CUES = re.compile(
    r"\b(?:whether|challenge(?:s|d)?|argue(?:s|d)?|contend(?:s|ed)?|"
    r"question presented|on appeal|issue(?:s)? presented|must decide)\b",
    re.IGNORECASE,
)
_REASONING_CUES = re.compile(
    r"\b(?:because|therefore|thus|accordingly|we find|we conclude|we hold|"
    r"the Court finds|the Court concludes|the regulation|the statute|"
    r"we disagree|we are persuaded|requires|must|is inconsistent|"
    r"impermissible|not persuasive)\b",
    re.IGNORECASE,
)
_DISPOSITION_RE = re.compile(
    r"\b(AFFIRMED|VACATED|REMANDED|DISMISSED|REVERSED|DENIED|GRANTED|"
    r"ORDERED|SET ASIDE)\b",
    re.IGNORECASE,
)
_REMAND_RE = re.compile(r"\b(remand(?:ed|ing)?|on remand|for further proceedings)\b", re.IGNORECASE)


def _claimant_and_conditions(text: str, case: CaseRecord) -> str:
    cleaned = clean_text(text)
    conditions: list[str] = []
    for pattern in _CONDITION_RES:
        for match in pattern.finditer(cleaned):
            value = re.sub(r"\s+", " ", match.group(1)).strip(" ,")
            value = re.split(
                r",\s*(?:both|including|and|as|which|when)\b", value, maxsplit=1
            )[0]
            value = re.split(
                r"\s+(?:as|including|because|but|under|when|if)\s+"
                r"|\s+and\s+(?:assigned|awarded|denied|rated|evaluated)\s+",
                value,
                maxsplit=1,
            )[0]
            if value.lower() in {"these conditions", "those conditions", "this condition"}:
                continue
            if 2 <= len(value) <= 140:
                conditions.append(value)
    if not conditions and case.issue:
        conditions.append(case.issue.strip())
    claimant = case.title.strip()
    if not claimant:
        claimant = "Claimant"
    if conditions:
        return (
            f"{claimant}. Claimed/affected conditions: "
            + "; ".join(_dedupe(conditions)[:12])
            + "."
        )
    return f"{claimant}. No specific condition was extracted from the available text."


def _procedural_history(text: str) -> str:
    background = _section_body(text, "BACKGROUND") or _section_body(text, "PROCEDURAL HISTORY")
    if not background:
        background = _intro(text)
    sentences = [
        sentence
        for sentence in _sentences(background)
        if _PROCEDURAL_CUES.search(sentence)
    ]
    if not sentences:
        sentences = [
            sentence
            for sentence in _sentences(text)
            if _PROCEDURAL_CUES.search(sentence)
        ][:12]
    return " ".join(_dedupe(sentences))


def _issues_presented(text: str, holding: str) -> str:
    intro = _intro(text)
    analysis = _section_body(text, "ANALYSIS") or text
    candidates: list[str] = []
    for sentence in _sentences(f"{intro} {analysis}"):
        if _ISSUE_CUES.search(sentence) or sentence.rstrip().endswith("?"):
            candidates.append(sentence)
    if not candidates and holding:
        candidates.append(holding)
    return " ".join(_dedupe(candidates)[:15])


def _holding_and_rule(text: str) -> str:
    holdings: list[str] = []
    for sentence in _sentences(text):
        lower = sentence.lower()
        if re.search(
            r"\b(?:we|the court)\s+(?:hold|holds|conclude|concludes|find|finds|"
            r"agree|agrees|reject|rejects)\b",
            lower,
        ) or re.search(r"\b(?:is|are)\s+(?:impermissible|unconstitutional|invalid)\b", lower):
            if len(sentence.split()) >= 8:
                holdings.append(sentence)
    # The conclusion/order often states the rule more compactly; retain it.
    conclusion = _section_body(text, "CONCLUSION") or _section_body(text, "ORDER")
    if conclusion:
        for sentence in _sentences(conclusion):
            if re.search(r"\b(?:hold|conclude|remand|vacat|affirm|revers)\b", sentence, re.I):
                holdings.append(sentence)
    return " ".join(_dedupe(holdings))


def _reasoning_summary(text: str) -> str:
    analysis = _section_body(text, "ANALYSIS") or _section_body(text, "DISCUSSION")
    if not analysis:
        analysis = text
    paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n", analysis)
        if paragraph.strip()
    ]
    selected = [
        paragraph
        for paragraph in paragraphs
        if _REASONING_CUES.search(paragraph) or "§" in paragraph
    ]
    # Keep the full analysis when no cue-based paragraph can be isolated: an
    # empty reasoning field is worse than a long, source-grounded one.
    return "\n\n".join(selected or paragraphs[:8])


def _disposition(text: str, case: CaseRecord) -> str:
    conclusion = _section_body(text, "CONCLUSION") or _section_body(text, "ORDER")
    sentences = [
        sentence
        for sentence in _sentences(conclusion or text)
        if _DISPOSITION_RE.search(sentence)
    ]
    if case.outcome:
        sentences.insert(0, f"Record disposition: {case.outcome}.")
    return " ".join(_dedupe(sentences))


def _remand_instructions(text: str) -> str:
    conclusion = _section_body(text, "CONCLUSION") or _section_body(text, "ORDER")
    sentences = [
        sentence
        for sentence in _sentences(conclusion or text)
        if _REMAND_RE.search(sentence)
    ]
    return " ".join(_dedupe(sentences))


def build_case_abstract(
    text: str,
    case: CaseRecord,
    *,
    source: str | None = None,
    source_text_chars: int | None = None,
) -> CaseAbstract:
    """Build a structured abstract from full opinion text or record metadata.

    When *text* is empty this intentionally produces a metadata-only abstract
    and marks ``abstract_complete=False``; it never fabricates facts or presents
    the legacy snippet as a full-text summary.
    """
    cleaned = clean_text(text)
    complete = bool(cleaned.strip())
    if not complete:
        metadata = " ".join(
            part
            for part in (
                case.title,
                case.court,
                case.snippet,
                case.holding,
                case.impact,
                case.deep_summary,
            )
            if part
        )
        cleaned = metadata

    holding = _holding_and_rule(cleaned)
    disposition = _disposition(cleaned, case)
    abstract = CaseAbstract(
        case_title=case.title,
        court=case.court,
        claimant_and_conditions=_claimant_and_conditions(cleaned, case),
        procedural_history=_procedural_history(cleaned),
        issues_presented=_issues_presented(cleaned, holding),
        holding_and_legal_rule=holding,
        reasoning_summary=_reasoning_summary(cleaned),
        disposition=disposition,
        remand_instructions=_remand_instructions(cleaned),
        source_text_chars=source_text_chars if source_text_chars is not None else len(cleaned),
        abstract_source=source or ("full_text" if complete else "record_metadata"),
        abstract_complete=complete,
    )
    # Never emit a field labelled complete when every substantive field is
    # empty; the caller can then show the "abstract unavailable" warning.
    if complete and not any(
        (
            abstract.procedural_history,
            abstract.issues_presented,
            abstract.holding_and_legal_rule,
            abstract.reasoning_summary,
            abstract.disposition,
        )
    ):
        abstract.abstract_complete = False
        abstract.abstract_source = "record_metadata"
    return abstract


# ---------------------------------------------------------------------------
# Provision impact extraction
# ---------------------------------------------------------------------------

def _case_text(case: CaseRecord) -> str:
    abstract = case.case_abstract
    abstract_text = ""
    if abstract is not None:
        abstract_text = " ".join(
            part
            for part in (
                abstract.claimant_and_conditions,
                abstract.procedural_history,
                abstract.issues_presented,
                abstract.holding_and_legal_rule,
                abstract.reasoning_summary,
                abstract.disposition,
                abstract.remand_instructions,
            )
            if part
        )
    return " ".join(
        part
        for part in (
            case.title,
            case.snippet,
            case.holding,
            case.deep_summary,
            abstract_text,
        )
        if part
    )


def _sentence_for(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind(";", 0, start))
    right_candidates = [pos for pos in (text.find(".", end), text.find(";", end)) if pos >= 0]
    right = min(right_candidates) if right_candidates else len(text)
    quote = text[left + 1 : right + 1].strip()
    return re.sub(r"\s+", " ", quote)


def _effect_for(sentence: str, spec: ProvisionSpec, *, m21: bool = False) -> str:
    lower = sentence.lower()
    if re.search(
        r"\b(impermissible|invalid|unlawful|unconstitutional|not persuasive|"
        r"contrary to|reject(?:s|ed)?|cannot|may not|does not permit)\b",
        lower,
    ):
        return "invalidating_m21_1_rule" if m21 else "narrowing"
    if re.search(r"\b(expand|broader|more generous|entitled to|requires? (?:va|the board) to)\b", lower):
        return "expanding"
    if re.search(r"\b(require|requires|must|shall|clarif|threshold|evidentiary)\b", lower):
        if spec.stage in {STAGE_DEVELOPMENT, STAGE_RATING}:
            return "clarifying_evidentiary_threshold"
        return "clarifying"
    if re.search(r"\b(remand|vacat|procedural|remand(?:ed)? for)\b", lower):
        return "procedural"
    return spec.effect


def _impact(
    spec: ProvisionSpec,
    quote: str,
    *,
    citation: str | None = None,
    diagnostic_code: str = "",
    staged: bool = False,
    medication: bool = False,
    m21_1: tuple[str, ...] | None = None,
    case: CaseRecord | None = None,
    m21_direct: bool = False,
) -> ProvisionImpact:
    source_url = spec.source_url or (case.url if case else "")
    host = (urlparse(source_url).hostname or "").lower()
    verified = bool(host) and (
        classify_source(source_url) == RELIABILITY_OFFICIAL
        or any(
            host == domain or host.endswith("." + domain)
            for domain in PRIMARY_SOURCE_DOMAINS
        )
    )
    return ProvisionImpact(
        citation=citation or spec.citation,
        authority_type=spec.authority_type,
        stage=spec.stage,
        legal_effect=_effect_for(quote, spec, m21=m21_direct),
        provenance_anchor=quote,
        m21_1_citations=list(m21_1 if m21_1 is not None else spec.m21_1),
        diagnostic_code=diagnostic_code,
        staged_rating=staged,
        medication_effect=medication,
        source_url=source_url,
        provenance_verified=verified,
        registry_version=REGISTRY_VERSION,
    )


def extract_provision_impacts(
    text: str, case: CaseRecord | None = None
) -> list[ProvisionImpact]:
    """Deterministically map cited provisions and rating doctrines to VBA stages.

    The extractor only emits an impact when the text actually contains the
    provision (or its explicit doctrine trigger).  It does not infer a holding
    from a bare citation; ``provenance_anchor`` carries the sentence in which
    the citation appears.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return []
    impacts: list[ProvisionImpact] = []
    seen: set[tuple[str, str]] = set()

    def add(impact: ProvisionImpact) -> None:
        key = (impact.citation, impact.stage)
        if key not in seen:
            seen.add(key)
            impacts.append(impact)

    for spec in PROVISION_REGISTRY:
        # Match the full canonical citation first, then its numbered alias
        # only when explicitly attached to title 38 (avoid bare "4.2" noise).
        pattern = re.compile(re.escape(spec.citation), re.IGNORECASE)
        match = pattern.search(cleaned)
        if match is None and spec.aliases:
            alias = spec.aliases[0]
            if spec.citation.startswith("38 U.S.C."):
                alias_pattern = re.compile(
                    rf"38\s*U\.?\s*S\.?\s*C\.?\s*(?:§{{1,2}}|sections?)\s*{re.escape(alias)}"
                    r"(?:\s*\(([a-zA-Z0-9]+)\))?",
                    re.IGNORECASE,
                )
            else:
                alias_pattern = re.compile(
                    rf"38\s*C\.?\s*F\.?\s*R\.?\s*(?:§{{1,2}}|sections?)\s*{re.escape(alias)}"
                    r"(?:\s*\(([a-zA-Z0-9]+)\))?",
                    re.IGNORECASE,
                )
            match = alias_pattern.search(cleaned)
        if match is None:
            continue
        quote = _sentence_for(cleaned, match.start(), match.end())
        if not quote:
            quote = spec.citation
        add(_impact(spec, quote, case=case))

    # Diagnostic codes (including analogous hyphenated codes).
    for match in _DC_RE.finditer(cleaned):
        primary = match.group(1)
        analogous = match.group(2)
        code = f"{primary}-{analogous}" if analogous else primary
        quote = _sentence_for(cleaned, match.start(), match.end())
        spec = ProvisionSpec(
            f"Diagnostic Code {code}",
            "diagnostic_code",
            STAGE_RATING,
            "clarifying",
            m21_1=(M21_RATING,),
        )
        add(
            _impact(
                spec,
                quote,
                citation=f"Diagnostic Code {code}",
                diagnostic_code=code,
                case=case,
            )
        )

    # Staged-rating doctrine (Fenderson / Hart), independent of a DC citation.
    for match in _STAGED_RE.finditer(cleaned):
        quote = _sentence_for(cleaned, match.start(), match.end())
        spec = ProvisionSpec(
            "Staged rating (Fenderson/Hart)",
            "doctrine",
            STAGE_RATING,
            "clarifying",
            m21_1=(M21_RATING,),
        )
        add(
            _impact(
                spec,
                quote,
                citation="Staged rating (Fenderson/Hart)",
                staged=True,
                case=case,
            )
        )

    # Ameliorative effect of medication (Jones / McCarroll).
    for match in _MEDICATION_RE.finditer(cleaned):
        quote = _sentence_for(cleaned, match.start(), match.end())
        spec = ProvisionSpec(
            "Ameliorative effect of medication (Jones/McCarroll)",
            "doctrine",
            STAGE_RATING,
            "clarifying_evidentiary_threshold",
            m21_1=(M21_RATING,),
        )
        add(
            _impact(
                spec,
                quote,
                citation="Ameliorative effect of medication (Jones/McCarroll)",
                medication=True,
                case=case,
            )
        )

    # Direct M21-1 citations.  These are map-placement signals, not authority
    # superior to a statute or regulation; the stage is inferred from the
    # manual part named in the sentence.
    for match in _M21_RE.finditer(cleaned):
        citation = re.sub(r"\s+", " ", match.group(0)).strip().rstrip(",")
        quote = _sentence_for(cleaned, match.start(), match.end())
        lower = quote.lower()
        if "part iv" in lower or "examination" in lower or "exam" in lower:
            stage = STAGE_DEVELOPMENT
        elif "part x" in lower or "cue" in lower or "higher-level" in lower:
            stage = STAGE_APPELLATE
        elif "part iii" in lower or "duty to assist" in lower or "notice" in lower:
            stage = STAGE_INTAKE
        else:
            stage = STAGE_RATING
        spec = ProvisionSpec(citation, "m21_1", stage, "clarifying", m21_1=(citation,))
        impact = _impact(spec, quote, citation=citation, m21_1=(citation,), case=case)
        impact.legal_effect = _effect_for(quote, spec, m21=True)
        add(impact)

    return impacts


def impacts_from_cases(cases: list[CaseRecord]) -> list[ProvisionImpact]:
    """Return de-duplicated provision impacts across all cases."""
    seen: set[tuple[str, str]] = set()
    result: list[ProvisionImpact] = []
    for case in cases:
        impacts = case.provision_impacts or extract_provision_impacts(_case_text(case), case)
        for impact in impacts:
            key = (impact.citation, impact.stage)
            if key not in seen:
                seen.add(key)
                result.append(impact)
    return result


# ---------------------------------------------------------------------------
# D1 case metadata
# ---------------------------------------------------------------------------


def _panel_status(case: CaseRecord) -> str:
    """Return the explicit panel/decision-path status for D1."""
    if case.court == BOARD_COURT:
        return "board_panel"
    text = f"{case.title} {case.panel} {case.snippet}".lower()
    if "en banc" in text:
        return "en_banc"
    if case.cavc_panel_status:
        return case.cavc_panel_status
    if case.rule_30a:
        return "single_judge"
    return "not_assessed"


def _precedential_status(case: CaseRecord) -> str:
    if case.court == BOARD_COURT:
        return "non_precedential_bva"
    if case.rule_30a or case.precedential is False:
        return "single_judge_memorandum" if case.court == COURT_CAVC else "unknown"
    if case.court in BINDING_COURTS:
        text = f"{case.title} {case.panel} {case.snippet}".lower()
        if "en banc" in text:
            return "en_banc"
        if case.precedential is True or case.cavc_panel_status == "panel":
            return "precedential_panel"
        return "unknown"
    return "unknown"


def _transcript_source(case: CaseRecord) -> tuple[str | None, str, bool]:
    url = (case.url or "").strip()
    if not url:
        return None, "unverified", False
    reliability = case.source_reliability or classify_source(url)
    host = (urlparse(url).hostname or "").lower()
    is_official = reliability == RELIABILITY_OFFICIAL
    if not is_official and host:
        is_official = any(
            host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS
        )
    if not is_official:
        return None, "secondary_summary", False
    if case.court == BOARD_COURT or "/vetapp" in url.lower() or "bva.va.gov" in host:
        return url, "official_transcript_archive", True
    return url, "official_court_opinion", True


def build_surfaced_case(case: CaseRecord) -> SurfacedCaseRecord:
    """Preserve every D1 field for one surfaced case."""
    status = _precedential_status(case)
    official_url, source_type, verified = _transcript_source(case)
    history: list[AppellateHistoryEntry] = []
    if case.superseded_by:
        history.append(
            AppellateHistoryEntry(
                status="overruled",
                case_title=case.superseded_by,
                source="retrieved corpus cross-reference",
                verified=False,
            )
        )
    warnings: list[str] = []
    if source_type != "official_court_opinion":
        warnings.append(
            f"Transcript source is {source_type}; verify the official opinion before citing."
        )
    if status == "single_judge_memorandum":
        warnings.append(
            "Single-judge memorandum disposition: persuasive only; not citable as precedent."
        )
    if status == "unknown":
        warnings.append("Precedential status is unknown; verify before relying on this decision.")
    if not history:
        warnings.append("Appellate history has not been assessed by the citator.")
    abstract = case.case_abstract or build_case_abstract("", case)
    impacts = case.provision_impacts or extract_provision_impacts(_case_text(case), case)
    tier, official = classify_authority_tier(
        case.url, case.court, case.precedential
    )
    return SurfacedCaseRecord(
        case_title=case.title,
        court=case.court,
        docket_number=case.docket or None,
        reporter_citation=case.citation or None,
        decision_date=case.decision_date or None,
        precedential_status=status,
        panel=case.panel or None,
        panel_status=_panel_status(case),
        appellate_history=history,
        history_status="superseded_detected" if history else "not_assessed",
        official_transcript_url=official_url,
        retrieved_url=case.url or None,
        transcript_source_type=source_type,
        provenance_verified=verified,
        source_reliability=case.source_reliability or classify_source(case.url),
        authority_tier=tier,
        is_official_authority=official,
        warnings=warnings,
        case_abstract=abstract,
        provision_impacts=impacts,
    )


# ---------------------------------------------------------------------------
# RVSR and Senior Rater / DRO guidance
# ---------------------------------------------------------------------------


def apply_citator_checks(
    analysis: LegalAnalysis, checks: list[CitatorCheck]
) -> None:
    """Attach CourtListener citator state and history to surfaced cases.

    The citator is advisory and optional.  This function preserves its exact
    status/confidence/as-of values and copies only verified appellate-history
    records plus authoritative negative-treatment entries into the D1 history.
    It never upgrades ``good_law`` into a finality finding.
    """
    if not checks or not analysis.surfaced_cases:
        return
    for record in analysis.surfaced_cases:
        title = record.case_title.strip().lower()
        citation = (record.reporter_citation or "").strip().lower()
        check = next(
            (
                candidate
                for candidate in checks
                if candidate.target_case.strip().lower() == title
                or (
                    citation
                    and citation
                    in {item.strip().lower() for item in candidate.target_citations}
                )
                or (
                    citation
                    and citation == candidate.target_citation.strip().lower()
                )
            ),
            None,
        )
        if check is None:
            continue

        record.citator_status = check.status
        record.citator_confidence = check.confidence
        record.citator_as_of = check.good_law_as_of or None
        history: list[AppellateHistoryEntry] = list(record.appellate_history)

        if check.appellate_history is not None:
            for item in check.appellate_history.verified_history:
                history.append(
                    AppellateHistoryEntry(
                        status=_history_status(
                            item.disposition, item.relationship
                        ),
                        case_title=item.candidate_case,
                        citation=item.candidate_citation,
                        source=item.source_url,
                        verified=True,
                    )
                )

        for treatment in check.negative_treatments:
            history.append(
                AppellateHistoryEntry(
                    status=_history_status(treatment.treatment),
                    case_title=treatment.citing_case,
                    citation=treatment.citing_citation,
                    source=treatment.citing_url,
                    verified=bool(treatment.binding),
                )
            )

        if check.status == "good_law":
            history.append(
                AppellateHistoryEntry(
                    status="good_law",
                    case_title=check.target_case,
                    citation=check.target_citation,
                    source="CourtListener citator",
                    # good_law is a review-set result, not a finality finding.
                    verified=False,
                )
            )

        if history:
            seen: set[tuple[str, str, str]] = set()
            deduped: list[AppellateHistoryEntry] = []
            for entry in history:
                key = (entry.status, entry.case_title, entry.citation)
                if key not in seen:
                    seen.add(key)
                    deduped.append(entry)
            record.appellate_history = deduped

        if check.appellate_history is not None and check.appellate_history.status == "verified":
            record.history_status = "verified_history"
        elif check.status == "good_law":
            record.history_status = "good_law_as_of"
        elif check.status == "negative":
            record.history_status = "negative_treatment"
        elif check.status == "unknown":
            record.history_status = "unknown"

        record.warnings = [
            warning
            for warning in record.warnings
            if "Appellate history has not been assessed by the citator" not in warning
        ]


def _history_status(*values: str) -> str:
    """Normalize a citator disposition into the D1 history vocabulary."""
    text = " ".join(value.lower() for value in values if value)
    for status in (
        "superseded_by_statute",
        "overruled",
        "reversed",
        "vacated",
        "affirmed",
        "good_law",
    ):
        if status in text:
            return status
    return "unknown"


def _case_brief(case: CaseRecord) -> str:
    citation = f", {case.citation}" if case.citation else ""
    return f"{case.title} ({case.court}{citation})"


def _relevant_cases(cases: list[CaseRecord], *terms: str) -> list[CaseRecord]:
    lowered = [term.lower() for term in terms]
    matches: list[CaseRecord] = []
    for case in cases:
        haystack = _case_text(case).lower()
        if any(term in haystack for term in lowered):
            matches.append(case)
    return matches


def _has(cases: list[CaseRecord], *terms: str) -> bool:
    return bool(_relevant_cases(cases, *terms))


def _authority_refs(cases: list[CaseRecord], limit: int = 4) -> list[str]:
    refs: list[str] = []
    for case in cases:
        ref = _case_brief(case)
        if ref not in refs:
            refs.append(ref)
        if len(refs) >= limit:
            break
    return refs


def build_rvsr_guidance(issue: str, cases: list[CaseRecord]) -> RvsrGuidance:
    """Build a concrete RVSR playbook, not a generic research prompt."""
    refs = _authority_refs(cases)
    ref_text = "; ".join(refs) if refs else "the retrieved authorities"
    mental = _has(cases, "ptsd", "mental", "psychiatric", "4.130", "3.304", "tbi", "traumatic brain")
    musculoskeletal = _has(cases, "musculoskeletal", "range of motion", "4.40", "4.45", "4.59", "knee", "spine")
    tdiu = _has(cases, "tdiu", "individual unemployability", "4.16")
    staged = _has(cases, "staged rating", "fenderson", "hart v.", "temporal rating")
    bilateral = _has(cases, "bilateral", "4.26")
    effective_date = _has(cases, "effective date", "3.400", "5110")
    cue = _has(cases, "clear and unmistakable error", "cue", "3.105")
    exam = _has(cases, "examination", "examiner", "3.159", "4.2", "insufficient exam")
    obesity = _has(cases, "obesity")
    overlap = _has(cases, "pyramiding", "4.14", "overlapping", "same symptoms", "tbi")

    evidence = [
        "Review the complete claims file before rating: service treatment records, "
        "VHA and private treatment records, DD-214/service history, and lay statements; "
        "assess competence and credibility under *Jandreau v. Nicholson* and "
        "*Buchanan v. Nicholson*.",
        "Map each claimed condition to the governing element: current diagnosis, "
        "in-service event or aggravation, and nexus. State the element still in dispute "
        "in the rating decision rather than treating the whole claim as one finding.",
        "For every lay statement, determine whether it is competent to address the "
        "observed symptom, onset, and continuity; do not reject lay evidence solely "
        "because it is not medical evidence.",
    ]
    if mental:
        evidence.append(
            "Mental-disorder claims: verify a current DSM-5 diagnosis, the in-service "
            "stressor, and a nexus opinion; address 38 C.F.R. § 3.304(f) for PTSD "
            "stressors and § 4.130 for the rating criteria."
        )
    if musculoskeletal:
        evidence.append(
            "Musculoskeletal claims: document range of motion, painful motion, "
            "functional loss, flare-ups, and repetitive-use effects under "
            "38 C.F.R. §§ 4.40, 4.45, and 4.59; do not rate on a single motion measurement."
        )
    if obesity:
        evidence.append(
            "Obesity: decide direct and secondary service connection separately. "
            "*Adams v. Collins* (2025) rejects the G.C. opinion's categorical rule for "
            "secondary service connection while leaving the direct-service-connection "
            "disease holding undisturbed; do not apply G.C. Prec. 1-2017 as a blanket bar."
        )

    exam_triggers = [
        "Defer for a VA examination or medical opinion when the record contains a "
        "current diagnosis, evidence of an in-service event, and an open nexus or "
        "severity question; a missing nexus opinion is a development trigger, not a denial.",
        "Ask the examiner the claim-specific questions: etiology (at least as likely as "
        "not), onset and continuity, current severity, functional loss, and whether any "
        "other condition could explain the symptoms.",
        "If an examination is incomplete, request an addendum or a new examination under "
        f"{M21_EXAM_SUFFICIENCY}; do not rely on an insufficient examination (38 C.F.R. "
        "§ 3.159; § 4.2).",
    ]
    if exam:
        exam_triggers.append(
            "The retrieved authority addresses examination adequacy; identify the exact "
            "missing opinion or functional-loss finding and return the examination for "
            "clarification before rating."
        )

    codesheet = [
        "Select the diagnostic code supported by the record; use an analogous code only "
        "under 38 C.F.R. § 4.20 and explain the analogy in the decision.",
        "Apply the rating-schedule criteria symptom-by-symptom and assign the highest "
        "evaluation supported by the evidence; do not average or compromise between tiers.",
        "Before finalizing, check anti-pyramiding under 38 C.F.R. § 4.14, the bilateral "
        "factor under § 4.26, and combined ratings under § 4.25.",
        "Consider extraschedular referral under 38 C.F.R. § 3.321 and TDIU under § 4.16 "
        "when the schedular evaluation does not reflect the veteran's actual impairment.",
    ]
    if staged:
        codesheet.append(
            "Assign staged ratings under *Fenderson v. West* and *Hart* when the evidence "
            "shows distinct periods of severity; state the period and the evidence supporting "
            "each stage instead of applying one rating to the entire appeal period."
        )
    if bilateral:
        codesheet.append(
            "Apply the bilateral factor under 38 C.F.R. § 4.26 only to compensable paired "
            "extremity disabilities and show the computation on the codesheet."
        )
    if tdiu:
        codesheet.append(
            "If TDIU is raised by the record, develop the employment history and address "
            "§ 4.16(a) schedular eligibility and § 4.16(b) extraschedular referral."
        )
    if effective_date:
        codesheet.append(
            "Assign the effective date under 38 C.F.R. § 3.400 and 38 U.S.C. § 5110; "
            "identify the triggering event (claim, receipt of evidence, or decision) and "
            "do not use the rating-decision date by default."
        )
    if overlap:
        codesheet.append(
            "Where symptoms overlap, identify which symptoms are duplicative and which are "
            "distinct before deciding whether separate ratings are permitted; do not combine "
            "conditions solely because they share a diagnosis or body system."
        )

    reasons = [
        "State the evidence considered, the finding on each element, the diagnostic code "
        "and rating criteria applied, and the reason for the evaluation and effective date.",
        "Resolve material conflicts in the evidence and explain why favorable evidence was "
        "accepted or rejected; a recitation of the records is not an adequate statement of reasons.",
        "Address 38 U.S.C. § 5104(b), § 5107(b), and § 7104(d)(1) expressly so the decision "
        "does not create a remandable reasons-or-bases or benefit-of-the-doubt defect.",
    ]
    if cue:
        reasons.append(
            "If CUE is raised, do not treat it as an ordinary disagreement with the prior "
            "weighing; analyze the prior decision under the law and facts extant at the time."
        )

    return RvsrGuidance(
        issue=issue,
        evidence_review_checklist=evidence,
        exam_development_triggers=exam_triggers,
        rating_codesheet_actions=codesheet,
        reasons_for_decision_requirements=reasons,
        source_authorities=refs or [ref_text],
    )


def build_senior_rater_guidance(issue: str, cases: list[CaseRecord]) -> SeniorRaterGuidance:
    """Build a Senior RVSR / DRO / quality-reviewer playbook."""
    refs = _authority_refs(cases)
    cue = _has(cases, "clear and unmistakable error", "cue", "3.105")
    dta = _has(cases, "duty to assist", "3.159", "5103a", "insufficient exam", "examination")
    hlr = _has(cases, "higher-level review", "hlr", "3.2601", "decision review officer", "dro", "3.2600")
    staged = _has(cases, "staged rating", "fenderson", "hart v.")
    precedent_conflict = _has(cases, "m21-1", "general counsel", "precedent opinion", "impermissible")
    overlap = _has(cases, "pyramiding", "4.14", "overlapping", "tbi")
    obesity = _has(cases, "obesity")

    hlr_steps = [
        "Conduct a de novo review of the entire record under 38 C.F.R. § 3.2601 (or the "
        "legacy DRO process under § 3.2600); do not defer to the prior adjudicator's findings.",
        "At the informal conference, identify the dispositive issue, the evidence the prior "
        "decision missed, and the specific regulation or diagnostic-code criterion that "
        "changes the outcome.",
        "Do not accept new evidence in an HLR; if new evidence is necessary, document why the "
        "matter must proceed as a supplemental claim instead.",
    ]
    if hlr:
        hlr_steps.append(
            "The retrieved authority addresses DRO/HLR review: separate each issue and address "
            "the prior decision's CUE finding independently from the merits rating."
        )

    dta_steps = [
        "Identify whether VA failed to obtain relevant records, provide required notice, or "
        "request a necessary examination or opinion under 38 U.S.C. §§ 5103, 5103A and "
        "38 C.F.R. § 3.159.",
        "If a duty-to-assist error is found, return the claim for the missing development "
        "before issuing a merits denial; do not affirm a decision that rests on an incomplete record.",
        f"Audit examination sufficiency under {M21_EXAM_SUFFICIENCY}: the opinion must address "
        "etiology, severity, functional loss, and the relevant diagnostic-code criteria.",
    ]
    if dta:
        dta_steps.append(
            "The retrieved authority flags an examination or development defect; name the "
            "missing record/opinion and the regulation requiring it in the return memorandum."
        )

    cue_steps = [
        "Apply 38 C.F.R. § 3.105(a): the error must be undebatable, must be based on the law "
        "and facts in effect at the time of the prior decision, and must have manifestly "
        "changed the outcome.",
        "Do not find CUE merely because the prior adjudicator weighed the evidence differently; "
        "a disagreement about weighing is not CUE.",
        "Distinguish a later change in judicial interpretation from CUE: under *George v. "
        "McDonough*, a prospective change in law is not a clear and unmistakable error in the "
        "earlier decision.",
    ]
    if cue:
        cue_steps.append(
            "The retrieved authority addresses CUE or DRO revision; document the CUE analysis "
            "in a separate section and preserve the merits analysis for the remanded issue."
        )
    if overlap:
        cue_steps.append(
            "When a prior decision applied anti-pyramiding, determine whether the earlier "
            "record actually contained duplicative symptomatology; a later clarification of "
            "§ 4.14 is not automatically CUE."
        )

    precedent_steps = [
        "Apply binding U.S. Supreme Court, Federal Circuit, and precedential CAVC precedent "
        "over a conflicting M21-1 provision; M21-1 is VA policy, not a statute or regulation.",
        "Document the conflict by citing the binding decision, the M21-1 provision, and the "
        "date of the precedent; do not silently follow an unrevised manual provision.",
        "If the manual has not been updated, follow the binding precedent in the decision, "
        "flag the issue for National Quality Review, and request a manual correction.",
    ]
    if precedent_conflict:
        precedent_steps.append(
            "The retrieved authority invalidates or narrows an agency interpretation; identify "
            "the specific G.C. opinion or M21-1 rule and state that it cannot control the claim."
        )
    if obesity:
        precedent_steps.append(
            "*Adams v. Collins* holds the G.C. opinion's categorical rule against secondary "
            "service connection for obesity is impermissible; do not apply G.C. Prec. 1-2017 "
            "to a secondary-service-connection claim."
        )

    quality = [
        "Second-signature checkpoints: issue characterization, complete evidence list, "
        "examination adequacy, diagnostic-code selection, staged-rating periods, bilateral "
        "factor, TDIU/extraschedular consideration, effective date, and reasons-or-bases.",
        "Confirm every favorable finding and every element denied is supported by an explicit "
        "written finding; a silent omission is a quality defect even if the outcome is correct.",
        "Confirm the decision applies the precedent in effect on the decision date and records "
        "any M21-1 conflict for NQR follow-up.",
    ]
    if staged:
        quality.append(
            "For staged ratings, verify each period has its own evidence, evaluation, and "
            "effective date; a single combined rating across distinct periods is a quality error."
        )

    return SeniorRaterGuidance(
        issue=issue,
        hlr_de_novo_focus=hlr_steps,
        duty_to_assist_errors=dta_steps,
        cue_analysis=cue_steps,
        precedent_conflict_resolution=precedent_steps,
        quality_review_checkpoints=quality,
        source_authorities=refs or ["the retrieved authorities"],
    )
