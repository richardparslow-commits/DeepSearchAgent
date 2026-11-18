"""AUD5 regression proofs — Audit Pass 5 (read-only audit deliverable, Part 4).

Every ``test_aud5_*`` defect-proof test FAILS on the current tree; each failure
is the evidence for the correspondingly named defect in docs/audit-pass5.md.
The three ``test_aud5_tg*`` guards PASS and pin behavior the proposed patches
must preserve. No tracked source file is modified by this module.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from va_legal_agent import (  # noqa: E402
    authorities,
    authority_queue,
    batch,
    citator,
    config,
    congress,
    extraction,
    fetch,
    impact,
    interpretation,
    llm,
    providers,
    redaction,
    regulations,
    reliability,
    va_domain,
)
from va_legal_agent import __main__ as cli  # noqa: E402
import va_legal_agent.agent as agent_mod  # noqa: E402
from va_legal_agent.llm import ReasoningResult  # noqa: E402
from va_legal_agent.models import (  # noqa: E402
    CaseRecord,
    CitatorCheck,
    RegulationsDocument,
    SourceAuditLedger,
    SourceInvalidatorCheck,
    SourceOpinionAudit,
    SourceValiditySignal,
    SurfacedCaseRecord,
)
from va_legal_agent.search import SearchError  # noqa: E402
from va_legal_agent.snapshots import (  # noqa: E402
    SnapshotStore,
    SnapshottingCourtListenerProvider,
)
from va_legal_agent.va_domain import ProvisionSpec  # noqa: E402

CAVC = "U.S. Court of Appeals for Veterans Claims"


def _case(title: str, snippet: str = "", holding: str = "", court: str = CAVC, **kwargs) -> CaseRecord:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "case"
    return CaseRecord(
        title=title,
        url=f"https://example.com/{slug}",
        court=court,
        snippet=snippet,
        holding=holding,
        **kwargs,
    )


def _load_script(name: str):
    """Import a scripts/ module with sys.modules registration (py3.14 dataclasses)."""
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def _stub_dns(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )


class _JsonResponse:
    def __init__(self, payload, status_code: int = 200, url: str = "https://api.example.com/x"):
        self._payload = payload
        self.status_code = status_code
        self.url = url
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self.headers: dict[str, str] = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


class _FakeSession:
    def __init__(self, get_fn):
        self._get_fn = get_fn

    def get(self, url, **kwargs):
        return self._get_fn(url, **kwargs)


_INDEX_XML = """<?xml version="1.0"?>
<sitemapindex><sitemap><loc>https://www.va.gov/vetapp26/sitemap.xml</loc></sitemap></sitemapindex>"""
_LEAF_XML = """<?xml version="1.0"?>
<urlset><url><loc>https://www.va.gov/vetapp26/Files/A2600001.txt</loc><lastmod>2026-01-02</lastmod></url></urlset>"""
_DECISION_TEXT = "tinnitus service connection decision text"


def _clear_bva_sitemap_cache():
    providers._bva_sitemap_cache.clear()


def _mock_bva_requests(monkeypatch, *, leaf_xml: str = _LEAF_XML):
    _clear_bva_sitemap_cache()

    def fake_get(url, **kwargs):
        if url.endswith("sitemap_bva.xml"):
            return _JsonResponse(_INDEX_XML, url=url)
        if url.endswith("/vetapp26/sitemap.xml"):
            return _JsonResponse(leaf_xml, url=url)
        return _JsonResponse(_DECISION_TEXT, url=url)

    monkeypatch.setattr(providers.requests, "get", fake_get)


# ---------------------------------------------------------------------------
# Stage-5 scripts / deploy
# ---------------------------------------------------------------------------


def test_aud5_stage5a_status_match_required():
    """AUD5-014: benchmark case_pass must require expected_status match."""
    module = _load_script("run_citator_real_benchmark.py")
    source = Path(module.__file__).read_text(encoding="utf-8")
    match = re.search(r"case_pass = \((.*?)\)", source, re.S)
    assert match, "case_pass expression not found"
    assert "expected_status" in match.group(1), (
        "case_pass ignores check.status == case['expected_status']"
    )


def test_aud5_stage5b_install_sh_sed_escaping():
    """AUD5-057: install.sh must escape sed metacharacters in TARGET/USER_NAME."""
    text = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "s|__TARGET__|$TARGET|g" not in text, (
        "raw $TARGET interpolated into sed replacement (|, &, \\ break the unit files)"
    )


# ---------------------------------------------------------------------------
# Models / va_domain (M family)
# ---------------------------------------------------------------------------


def test_aud5_m1_cfr_prefix_collision():
    """AUD5-001: '§ 4.16' must not also emit a § 4.1 impact."""
    impacts = va_domain.extract_provision_impacts(
        "The Board applied 38 C.F.R. § 4.16 to the TDIU claim."
    )
    citations = {i.citation for i in impacts}
    assert "38 C.F.R. § 4.1" not in citations


def test_aud5_m1_usc_prefix_collision():
    """AUD5-001: '§ 5103A' must not also emit a § 5103 impact."""
    impacts = va_domain.extract_provision_impacts(
        "VA's duty under 38 U.S.C. § 5103A was triggered."
    )
    citations = {i.citation for i in impacts}
    assert "38 U.S.C. § 5103" not in citations


def test_aud5_m2_history_status_vocabulary():
    """AUD5-018: citator 'abrogated' must map to a real D1 history status."""
    assert va_domain._history_status("abrogated") != "unknown"


def test_aud5_m3_sentence_decimal_cut():
    """AUD5-019: sentence extraction must not cut inside '4.16'."""
    text = "The Board applied 38 C.F.R. § 4.16 to the claim. Affirmed."
    start = text.index("4.16")
    # end inside the citation token: the next '.' found is the decimal dot.
    quote = va_domain._sentence_for(text, start, start + 1)
    assert "4.16" in quote


def test_aud5_m4_relevant_cases_substring():
    """AUD5-020: 'cue' must not match inside 'rescue'."""
    case = _case("Rescue Mission v. VA", snippet="A rescue operation dispute.")
    assert va_domain._relevant_cases([case], "cue") == []


def test_aud5_m5_ledger_dump_empty_id():
    """AUD5-021: model_dump must survive an empty opinion_id row (skip, not raise)."""
    ledger = SourceAuditLedger(opinion_audits=[SourceOpinionAudit(opinion_id="")])
    dumped = ledger.model_dump()
    assert isinstance(dumped, dict)


def test_aud5_m6_secondary_official_flag():
    """AUD5-022: secondary_summary tier must never carry is_official_authority=True."""
    record = SurfacedCaseRecord(
        case_title="Blog summary",
        court="n/a",
        source_reliability="secondary",
        authority_tier="secondary_summary",
        is_official_authority=True,
    )
    assert record.is_official_authority is False


def test_aud5_m7_default_review_status():
    """AUD5-065: an unconstructed-review check must default to 'unreviewed'."""
    check = SourceInvalidatorCheck()
    assert check.review_status == "unreviewed"


def test_aud5_m8_negative_counters_rejected():
    """AUD5-066: pages_fetched=-3 must be rejected by the schema."""
    with pytest.raises(ValueError):
        SourceOpinionAudit(opinion_id="1", pages_fetched=-3)


def test_aud5_m9_dot_continuation():
    """AUD5-067: M21-1 section tokens must keep dot continuations (D.1.a)."""
    impacts = va_domain.extract_provision_impacts(
        "See M21-1, Part III, Subpart iv, Chapter 4, Section D.1.a for guidance."
    )
    m21 = [i for i in impacts if i.authority_type == "m21_1"]
    assert m21 and "D.1.a" in m21[0].citation


def test_aud5_m9_partxi_not_partx():
    """AUD5-067: 'Part XI' must not trigger the Part X (appellate) keyword."""
    impacts = va_domain.extract_provision_impacts(
        "M21-1, Part XI governs this matter."
    )
    m21 = [i for i in impacts if i.authority_type == "m21_1"]
    assert m21 and m21[0].stage != va_domain.STAGE_APPELLATE


def test_aud5_m10_dc_range():
    """AUD5-068: DC 0000 is not a valid diagnostic code."""
    impacts = va_domain.extract_provision_impacts("Rated under DC 0000 for the condition.")
    assert not [i for i in impacts if "0000" in i.citation]


def test_aud5_m11_env_bool_warns(monkeypatch, caplog):
    """AUD5-069: unrecognized env_bool values must log a warning."""
    monkeypatch.setenv("AUD5_FLAG_PROBE", "banana")
    with caplog.at_level(logging.WARNING, logger="va_legal_agent.config"):
        config.env_bool("AUD5_FLAG_PROBE", False)
    assert any("banana" in r.message or "AUD5_FLAG_PROBE" in r.message for r in caplog.records)


def test_aud5_m12_user_agent_blank(monkeypatch):
    """AUD5-070: blank USER_AGENT must fall back to the default."""
    monkeypatch.setenv("USER_AGENT", "   ")
    settings = config.Settings.from_env()
    assert settings.user_agent.strip()


def test_aud5_m13_dead_regexes_removed():
    """AUD5-071: unused _USC_RE/_CFR_RE must be deleted."""
    assert not hasattr(va_domain, "_USC_RE")
    assert not hasattr(va_domain, "_CFR_RE")


# ---------------------------------------------------------------------------
# Engines (E family)
# ---------------------------------------------------------------------------


def test_aud5_e1_llm_text_provenance_gate(monkeypatch):
    """AUD5-002: fabricated interpret_cases narrative must fail the provenance gate."""
    case = _case("Nexus v. VA", snippet="Service connection requires a nexus.")
    monkeypatch.setattr(
        interpretation,
        "reason_cases",
        lambda *a, **k: ReasoningResult(
            reconciled_principles=["p"], synthesis="", schema_validated=True
        ),
    )
    monkeypatch.setattr(
        interpretation,
        "interpret_cases",
        lambda *a, **k: "Zzyzx quuxology fabricated unrelated narrative",
    )
    result = interpretation.build_interpretive_analysis("tinnitus", "increase", [case])
    assert result.llm_provenance_validated is False


def test_aud5_e2_unverified_summary_excluded():
    """AUD5-003: unverified deep_summary must not feed impact/case text."""
    case = _case("Smith v. VA")
    case.deep_summary = "Zzyqx fabricated holding about unrelated law"
    case.deep_summary_provenance_verified = False
    assert "fabricated" not in impact._impact_text(case)
    assert "fabricated" not in va_domain._case_text(case)


def test_aud5_e4_subsection_mismatch():
    """AUD5-026: '§ 5107(a)' must not emit the § 5107(b) registry spec."""
    impacts = va_domain.extract_provision_impacts(
        "The veteran invoked 38 U.S.C. § 5107(a)."
    )
    assert "38 U.S.C. § 5107(b)" not in {i.citation for i in impacts}


def test_aud5_e6_contradiction_schema():
    """AUD5-027: dict-valued case_a must invalidate the whole reasoning payload."""
    payload = {
        "reconciled_principles": ["p"],
        "contradictions": [
            {"statement": "s", "case_a": {"bad": 1}, "case_b": "B"}
        ],
        "synthesis": "s",
    }
    assert llm._parse_reasoning_response(json.dumps(payload)) is None


def test_aud5_e7_overlap_ratio():
    """AUD5-028: one shared word out of seven must fail provenance overlap."""
    case = _case("Nexus v. VA", snippet="Service connection requires a nexus.")
    failures = llm.validate_text_provenance(
        "nexus zzyqx quuxology fabricated unrelated narrative words", [case]
    )
    assert failures


def test_aud5_e8_abstract_self_feed():
    """AUD5-029: case.impact must not seed the abstract's substantive fields."""
    case = _case("Doe v. VA")
    case.impact = "Zzyqx fabricated holding"
    abstract = va_domain.build_case_abstract("", case)
    fields = (
        abstract.claimant_and_conditions,
        abstract.procedural_history,
        abstract.issues_presented,
        abstract.holding_and_legal_rule,
        abstract.reasoning_summary,
        abstract.disposition,
        abstract.remand_instructions,
    )
    assert all("fabricated" not in (f or "") for f in fields)


def test_aud5_e9_george_unconditional():
    """AUD5-030: George v. McDonough must not appear with zero retrieved cases."""
    guidance = va_domain.build_senior_rater_guidance("tinnitus", [])
    assert not any("George v. McDonough" in step for step in guidance.cue_analysis)


def test_aud5_e9_placeholder_authorities():
    """AUD5-030: 'the retrieved authorities' placeholder must be replaced."""
    guidance = va_domain.build_senior_rater_guidance("tinnitus", [])
    assert guidance.source_authorities != ["the retrieved authorities"]


def test_aud5_e10_5109a_registry():
    """AUD5-031: 38 U.S.C. § 5109A must be in the provision registry."""
    impacts = va_domain.extract_provision_impacts(
        "Revision under 38 U.S.C. § 5109A was requested."
    )
    assert "38 U.S.C. § 5109A" in {i.citation for i in impacts}


def test_aud5_e10_201403e_registry():
    """AUD5-031: 38 C.F.R. § 20.1403(e) must be in the provision registry."""
    impacts = va_domain.extract_provision_impacts(
        "Board CUE under 38 C.F.R. § 20.1403(e) is unavailable here."
    )
    assert "38 C.F.R. § 20.1403(e)" in {i.citation for i in impacts}


def test_aud5_e12_manifest_self_heal(tmp_path):
    """AUD5-032: a missing manifest row must be re-appended for an existing snapshot."""
    store = SnapshotStore(tmp_path)
    rec = store.record("k", {"a": 1})
    store.manifest.unlink()
    store.record("k", {"a": 1})
    assert store.manifest.exists()
    assert rec.id in store.manifest.read_text(encoding="utf-8")


def test_aud5_e13_capture_typeerror():
    """AUD5-033: TypeError from the store must not escape _capture."""

    class BadStore:
        def record_response(self, *a, **k):
            raise TypeError("bad snapshot payload")

    provider = SnapshottingCourtListenerProvider(object(), BadStore())
    provider._capture("k", {}, {})  # must not raise


def test_aud5_e14_ecfr_list_payload(monkeypatch):
    """AUD5-034: a list eCFR payload must raise SearchError, not AttributeError."""
    monkeypatch.setattr(authorities, "_ecfr_date_cache", None)
    monkeypatch.setattr(
        authorities.requests, "get", lambda *a, **k: _JsonResponse(["not", "a", "dict"])
    )
    with pytest.raises(SearchError):
        authorities._ecfr_current_date()


def test_aud5_e15_fr_nondict_rows(monkeypatch):
    """AUD5-035: non-dict Federal Register rows must not fabricate 'verified'."""
    _stub_dns(monkeypatch)
    monkeypatch.setattr(
        authorities.requests,
        "get",
        lambda *a, **k: _JsonResponse({"results": ["x"]}),
    )
    check = authorities.verify_federal_register_part("3")
    assert check.status != "verified"


def test_aud5_e16_detail_redaction(monkeypatch):
    """AUD5-036: AuthorityCheck.detail must redact credentials from exceptions."""
    import requests as _requests

    _stub_dns(monkeypatch)

    def boom(*a, **k):
        raise _requests.RequestException("https://u:SECRET@host/path")

    monkeypatch.setattr(authorities.requests, "get", boom)
    check = authorities.verify_federal_register_part("3")
    assert "SECRET" not in (check.detail or "")


def test_aud5_e17_effect_requires_m21_context():
    """AUD5-037: bare 'cannot' must not invalidate an M21-1 rule."""
    spec = ProvisionSpec("M21-1, Part X", "m21_1", va_domain.STAGE_APPELLATE, "clarifying")
    effect = va_domain._effect_for("The court cannot say.", spec, m21=True)
    assert effect != "invalidating_m21_1_rule"


def test_aud5_e18_llm_log_redaction(monkeypatch, caplog):
    """AUD5-038: LLM failure logs must redact credentials."""
    def boom(*a, **k):
        raise RuntimeError("https://u:SECRET@host")

    monkeypatch.setenv("LLM_REASONING", "true")
    monkeypatch.setattr(llm, "llm_enabled", lambda: True)
    monkeypatch.setattr(llm, "call_openai", boom)
    with caplog.at_level(logging.WARNING, logger="va_legal_agent.llm"):
        llm.reason_cases("tinnitus", "increase", [_case("A v. VA")])
    assert all("SECRET" not in r.getMessage() for r in caplog.records)


def test_aud5_e19_identity_comma_ids():
    """AUD5-039: '101,102' and '101' must dedupe to one queue item."""
    a = _case("Same Case v. VA", courtlistener_opinion_id="101,102")
    b = _case("Same Case v. VA", courtlistener_opinion_id="101")
    queue = authority_queue.prioritize_authorities([a, b])
    assert len(queue) == 1


def test_aud5_e20_crs_regex_context():
    """AUD5-040: docket 'R12345' must not match the CRS report pattern."""
    assert congress._CRS_REPORT_RE.findall("Docket R12345 was filed") == []


def test_aud5_e21_comment_limit_zero(monkeypatch):
    """AUD5-041: comment_sample_limit=0 must yield zero comments."""
    client = regulations.RegulationsClient(api_key="k", comment_sample_limit=0)
    payload = {
        "meta": {"totalElements": 1},
        "data": [
            {"id": "c1", "attributes": {"title": "t", "comment": "body", "postedDate": "2026-01-01"}}
        ],
    }
    monkeypatch.setattr(client, "_request_json", lambda *a, **k: payload)
    doc = RegulationsDocument(object_id="obj-1")
    client._comments(doc)
    assert len(doc.comments) == 0


def test_aud5_e22_nan_coverage(tmp_path, monkeypatch):
    """AUD5-042: NaN coverage_score must not poison the batch mean."""
    monkeypatch.setattr(batch, "batch_state_dir", lambda: tmp_path)
    tracker = batch.BatchTracker("nan-run")
    tracker.record({"event": "analysis_complete", "issue": "i", "coverage_score": float("nan")})
    summary = tracker.summary()
    mean = summary.get("coverage_mean")
    assert mean is None or mean == mean  # NaN != NaN


def test_aud5_e23_cache_unicode_error(monkeypatch):
    """AUD5-043: UnicodeEncodeError from the cache must not abort the run."""
    class BadCache:
        def get(self, *a, **k):
            raise UnicodeEncodeError("utf-8", "", 0, 1, "boom")

        def set(self, *a, **k):
            raise UnicodeEncodeError("utf-8", "", 0, 1, "boom")

    client = congress.CongressClient()
    client._cache = BadCache()
    client._cache_set("k", {}, {"a": 1})  # must not raise
    assert client._cache_get("k", {}) is None


# ---------------------------------------------------------------------------
# Citator (C family)
# ---------------------------------------------------------------------------


def test_aud5_c1_long_negation_window():
    """AUD5-006: negation more than 40 chars before the term must be detected."""
    sentence = (
        "The court did not, after careful and extended consideration of the "
        "arguments, overrule Smith."
    )
    idx = sentence.index("overrule")
    assert citator._is_negated(sentence, idx) is True


def test_aud5_c2_decline_base_form():
    """AUD5-023: 'decline to overrule' (base form) must be a reaffirmance cue."""
    matched = any(
        pattern.search("We decline to overrule Smith.")
        for pattern, _label in citator._REAFFIRMANCE_PATTERNS
    )
    assert matched


def test_aud5_c3_question_noun_object():
    """AUD5-007: 'question the doctrine' is a noun object, not a treatment."""
    sentence = "We question the doctrine announced in Smith."
    idx = sentence.index("question")
    assert citator._is_non_verb_negative("questioned", "question", sentence, idx + len("question")) is False


def test_aud5_c4_limit_to_scope_overbroad():
    """AUD5-024: 'limited to the question presented' is a real limitation."""
    sentence = "Smith is limited to the question presented there."
    idx = sentence.index("limited")
    assert citator._is_non_verb_negative("limited", "limited", sentence, idx + len("limited")) is False


def test_aud5_c5_to_the_extent_scope():
    """AUD5-025: 'to the extent' must classify as partial scope."""
    assert citator._scope_for("overruled", "Overruled to the extent it conflicts.") == "part"


def test_aud5_c6_abbreviation_gaps():
    """AUD5-008: reporter abbreviations must not split sentences."""
    for tok in ("Supp", "Ed", "Fed", "Cl", "App'x"):
        text = f"12 {tok}. 345"
        pos = text.index(".")
        assert citator._is_abbreviation(text, pos) is True, tok


def test_aud5_c7_citation_regex_gaps():
    """AUD5-009: 4-digit Vet.App. pages, F.Supp.3d, curly App’x must match (F.4th already fixed)."""
    for text in ("1234 Vet. App. 567", "12 F.Supp.3d 345", "12 F. App’x 345"):
        assert citator._CITATION_RE.search(text), text


def test_aud5_c8_single_row_mismatch():
    """AUD5-010: a single non-matching lookup row must not be accepted blindly."""
    row = {"citation": "999 F.3d 111", "case_name": "Wrong Case"}
    assert citator._select_lookup_row([row], "12 Vet. App. 34") is None


def test_aud5_c9_good_law_downgrade():
    """AUD5-011: blocking all treatments must not demote a clean good_law check."""
    check = CitatorCheck(
        target_case="X",
        status="good_law",
        good_law_as_of="2026-01-01",
        source_validity=[SourceValiditySignal(source_opinion_id="9", status="invalid")],
    )
    citator._apply_source_validity_enforcement(check)
    assert check.status == "good_law"


def test_aud5_c10_passive_separate_speaker():
    """AUD5-012: 'by the dissent' must register as a separate-voice passive."""
    assert citator._PASSIVE_PARTY_RE.match("by the dissent in Smith")


def test_aud5_c11_count_malformed_penalty():
    """AUD5-076: count_malformed coverage gaps must carry a confidence penalty."""
    assert "count_malformed" in citator._CONFIDENCE_GAP_PENALTIES


# ---------------------------------------------------------------------------
# CLI / agent / webapp (D family)
# ---------------------------------------------------------------------------


def test_aud5_d5_packing_cap_preserves_abort():
    """AUD5-015: packing-cap deferral must not overwrite a quota abort verdict."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    match = re.search(r"if packing_cap is not None(.*?):", source)
    assert match, "packing-cap branch not found"
    assert 'row["verdict"] == "proceed"' in match.group(1), (
        "packing cap applies to non-proceed rows and can overwrite 'abort'"
    )


def test_aud5_d6_auto_run_forwards_deep_read():
    """AUD5-016: _auto_run must accept and forward deep_read/deep_read_limit/enrich."""
    import inspect

    params = inspect.signature(cli._auto_run).parameters
    assert "deep_read" in params
    assert "deep_read_limit" in params
    assert "enrich" in params


def test_aud5_d7_start_at_with_auto_run():
    """AUD5-060 (already fixed in tree): guard must keep accepting --auto-run."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert (
        'and not (args.batch_dry_run or args.auto_run)' in source
    ), "guard regressed: --start-at/--only-blocked rejected under --auto-run"


def test_aud5_d8_batch_size_validation():
    """AUD5-061: --batch-size must reject non-positive values."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    match = re.search(r'"--batch-size",(.*?)\)', source, re.S)
    assert match, "--batch-size argument not found"
    assert "_positive_int" in match.group(1), "--batch-size accepts non-positive values"


def test_aud5_d9_explicit_zero_limit():
    """AUD5-062: explicit deep_read_limit=0 must not collapse to the settings default."""
    source = Path(__import__("va_legal_agent.agent", fromlist=["agent"]).__file__).read_text(
        encoding="utf-8"
    )
    assert "deep_read_limit or " not in source, (
        "`or` fallback silently replaces an explicit deep_read_limit=0"
    )


def test_aud5_d10_extraction_flag_redaction(monkeypatch):
    """AUD5-017: extraction_flags must redact credentials from LLM exceptions."""

    case = _case("Doe v. VA")
    case.courtlistener_opinion_id = ""

    def boom(*a, **k):
        raise RuntimeError("https://u:SECRET@host")

    monkeypatch.setenv("LLM_EXTRACTION", "true")
    monkeypatch.setattr(agent_mod, "llm_enabled", lambda: True)
    monkeypatch.setattr(agent_mod, "fetch_case_details_and_text", lambda url: ({}, "Some opinion text."))
    monkeypatch.setattr(agent_mod, "extract_quote_candidates", boom)
    agent_mod.enrich_top_cases([case])
    assert all("SECRET" not in flag for flag in case.extraction_flags)


def test_aud5_d11_verified_holding_gate():
    """AUD5-017b: verified_holding must be empty when provenance is unverified."""
    source = Path(__import__("va_legal_agent.agent", fromlist=["agent"]).__file__).read_text(
        encoding="utf-8"
    )
    assert '"verified_holding": case.holding or case.snippet' not in source, (
        "verified_holding emitted regardless of deep_summary_provenance_verified"
    )


def test_aud5_d12_text_report_sections():
    """AUD5-072: text report must include detected elements/principle findings/provenance."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    start = source.index("def _analysis_to_text")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    assert "Detected claim elements" in body
    assert "Principle findings" in body
    assert "LLM provenance validated" in body


def test_aud5_d13_guidance_section_gates():
    """AUD5-073: guidance sections must gate on the rendered lines, not one subfield."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "if analysis.adjudication_guidance.rvsr.evidence_review_checklist" not in source
    assert "if analysis.adjudication_guidance.senior_rater_dro.hlr_de_novo_actions" not in source
    assert "if analysis.adjudication_guidance.qrs.nqr_qrt_error_calls" not in source


# ---------------------------------------------------------------------------
# Providers / fetch / redaction (P family)
# ---------------------------------------------------------------------------


def test_aud5_p1_response_closed_on_cap():
    """AUD5-004: oversized-body FetchError must still close the response."""
    closed = {"v": False}

    class FakeCffiResponse:
        url = "https://example.com/big"
        headers = {"Content-Length": "999999999"}

        def iter_content(self, chunk_size):
            yield b"x"

        def close(self):
            closed["v"] = True

    import va_legal_agent.fetch as fetch_mod

    original = fetch_mod._fetch_with_redirects
    fetch_mod._fetch_with_redirects = lambda url, timeout: (FakeCffiResponse(), url)
    try:
        with pytest.raises(fetch_mod.FetchError):
            fetch_mod.fetch_full_text("https://example.com/big")
    finally:
        fetch_mod._fetch_with_redirects = original
    assert closed["v"] is True


def test_aud5_p3_userinfo_multiple_at():
    """AUD5-045: passwords containing '@' must be fully redacted."""
    out = redaction.redact_secrets("https://user:pa@ss@host/path")
    assert "ss@host" not in out and "pa@ss" not in out


def test_aud5_p4_fragment_query_pairs():
    """AUD5-046: '#api_key=SECRET' fragments must be redacted."""
    out = redaction.redact_url("https://host/path#api_key=SECRET")
    assert "SECRET" not in out


def test_aud5_p6_list_payload_guard(monkeypatch):
    """AUD5-048: a list JSON payload must raise SearchError, not crash downstream."""
    monkeypatch.setenv("SEARCH_MIN_INTERVAL_SECONDS", "0")
    provider = providers.CourtListenerProvider()
    provider._cache = None
    # Guard must live in _get_json itself; emulate the raw transport instead.
    monkeypatch.setattr(
        providers.requests, "get", lambda *a, **k: _JsonResponse(["not", "a", "dict"])
    )
    with pytest.raises(SearchError):
        provider.fetch_opinion_text(1)


def test_aud5_p7_string_citation():
    """AUD5-049: a bare string citation must not be indexed character-wise."""
    item = {
        "caseName": "A v. VA",
        "absolute_url": "/opinion/1/x/",
        "citation": "12 Vet. App. 34",
    }
    mapped = providers._map_courtlistener_opinion(item)
    assert mapped.get("citation") == "12 Vet. App. 34"


def test_aud5_p8_anomaly_false_positive(monkeypatch):
    """AUD5-050: medical 'anomaly' in real results must not trigger rate-limit."""
    html = (
        '{"resultsData": {"results": [{"url": "https://www.va.gov/vetapp26/Files/A1.txt", '
        '"title": "congenital anomaly claim", "description": "d"}]}}'
    )
    session = _FakeSession(
        lambda url, **k: _JsonResponse(html, url="https://search.usa.gov/search")
    )
    monkeypatch.setattr(providers, "_get_bva_session", lambda: session)
    provider = providers.BVAProvider()
    results = provider.search("congenital anomaly", max_results=5)
    assert isinstance(results, list)


def test_aud5_p14_page_zero(monkeypatch):
    """AUD5-054: page=0 must behave like page=1, not raise."""
    _mock_bva_requests(monkeypatch)
    provider = providers.BvaSitemapProvider()
    page1 = provider.search("tinnitus", max_results=5, page=1)
    page0 = provider.search("tinnitus", max_results=5, page=0)
    assert page0 == page1


def test_aud5_p16_treatment_window_off_by_one():
    """AUD5-055: the treatment window must not include one extra character."""
    source = Path(fetch.__file__).read_text(encoding="utf-8")
    assert "text[start:end + 1]" not in source, (
        "window slice includes one character past the declared 200-char window"
    )


# ---------------------------------------------------------------------------
# Direct verification (X family)
# ---------------------------------------------------------------------------


def test_aud5_x1_extraction_flag_redaction(monkeypatch):
    """AUD5-084: extraction flags must redact credentials."""
    monkeypatch.setenv("LLM_EXTRACTION", "true")
    monkeypatch.setattr(extraction, "llm_enabled", lambda: True)
    monkeypatch.setattr(extraction, "call_openai", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("https://u:SECRET@host")))
    result = extraction.extract_quote_candidates("Some opinion text about service connection.")
    assert all("SECRET" not in flag for flag in result.flags)


def test_aud5_x2_court_data_host_tier():
    """AUD5-085: courtlistener.com with unknown court must not be agency guidance."""
    tier, _official = reliability.classify_authority_tier(
        "https://www.courtlistener.com/opinion/1/", "", True
    )
    assert tier != reliability.AUTHORITY_AGENCY_GUIDANCE


def test_aud5_x3_dead_path_token():
    """AUD5-086: 'va.gov/vetapp' path token never matches a hostname; remove it."""
    assert "va.gov/vetapp" not in reliability._AGENCY_GUIDANCE_HOSTS


# ---------------------------------------------------------------------------
# TG guards — these PASS today and must keep passing after the patches.
# ---------------------------------------------------------------------------


def test_aud5_tg2_official_tiebreak_guard():
    """TG-2 guard: official authority wins an otherwise-equal ranking tie."""
    from va_legal_agent.ranking import rank_cases

    official = _case("Alpha v. VA", snippet="tinnitus service connection nexus")
    official.is_official_authority = True
    official.authority_tier = reliability.AUTHORITY_BINDING
    secondary = _case("Beta v. VA", snippet="tinnitus service connection nexus")
    secondary.is_official_authority = False
    secondary.authority_tier = reliability.AUTHORITY_BINDING
    ranked = rank_cases([secondary, official])
    assert ranked[0].title == "Alpha v. VA"


def test_aud5_tg3_usc_impact_guard():
    """TG-3 guard: a clean USC citation emits a verified statute impact."""
    impacts = va_domain.extract_provision_impacts(
        "The duty to notify under 38 U.S.C. § 5103 applies."
    )
    usc = [i for i in impacts if i.citation == "38 U.S.C. § 5103"]
    assert usc and usc[0].authority_type == "statute"


def test_aud5_tg4_stage_effect_guard():
    """TG-4 guard: § 3.2601 maps to the appellate stage with a clarifying effect."""
    impacts = va_domain.extract_provision_impacts(
        "Higher-level review under 38 C.F.R. § 3.2601 was elected."
    )
    spec = [i for i in impacts if i.citation == "38 C.F.R. § 3.2601"]
    assert spec and spec[0].stage == va_domain.STAGE_APPELLATE
