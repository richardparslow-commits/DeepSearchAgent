"""Tests for separate statutory/regulatory supersession signals."""

from __future__ import annotations

from datetime import date

import va_legal_agent.authorities as auth
from va_legal_agent.models import (
    AuthorityCheck,
    LegalAnalysis,
    StatutorySupersessionSignal,
)


class FakeResponse:
    def __init__(self, *, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise auth.requests.HTTPError("HTTP error", response=self)

    def json(self):
        return self._payload


def _check(authority: str, source: str, status: str, detail: str = "") -> AuthorityCheck:
    return AuthorityCheck(
        authority=authority,
        source=source,
        status=status,
        detail=detail,
        url=f"https://example.test/{source}",
    )


def test_verify_cfr_supersession_reports_explicit_removal_candidate(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        captured.update(url=url, params=params)
        return FakeResponse(
            payload={
                "results": [
                    {
                        "document_number": "2026-12345",
                        "title": "Removing superseded service-connection rule",
                        "publication_date": "2026-10-01",
                        "effective_on": "2026-11-01",
                        "html_url": "https://www.federalregister.gov/d/2026-12345",
                        "action": (
                            "Final rule; removal of 38 CFR 3.303 and "
                            "redesignation to 38 CFR 3.304"
                        ),
                        "cfr_references": [
                            {"title": 38, "part": 3, "section": "3.303"}
                        ],
                    }
                ]
            }
        )

    monkeypatch.setattr(auth.requests, "get", fake_get)

    signal = auth.verify_cfr_supersession("3.303", today=date(2026, 11, 14))

    assert signal.status == "candidate"
    assert signal.signal == "removed"
    assert signal.authority == "38 C.F.R. § 3.303"
    assert signal.replacement == "38 C.F.R. § 3.304"
    assert signal.effective_date == "2026-11-01"
    assert captured["params"]["conditions[cfr][section]"] == "3.303"
    assert "action" in captured["params"]["fields[]"]


def test_verify_cfr_supersession_no_action_and_no_results(monkeypatch):
    responses = iter(
        [
            {
                "results": [
                    {
                        "title": "Service Connection Updates",
                        "action": "Final rule",
                        "publication_date": "2026-10-01",
                    }
                ]
            },
            {"results": []},
        ]
    )
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(payload=next(responses)),
    )

    no_action = auth.verify_cfr_supersession("3.303", today=date(2026, 11, 14))
    no_results = auth.verify_cfr_supersession("3.303", today=date(2026, 11, 14))

    assert no_action.status == "no_signal"
    assert "No explicit" in no_action.detail
    assert no_results.status == "no_signal"
    assert "was found" in no_results.detail


def test_verify_cfr_supersession_skips_other_sections_and_handles_failure(monkeypatch):
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            payload={
                "results": [
                    {
                        "action": "Final rule; removal",
                        "title": "Other section",
                        "cfr_references": [{"section": "3.999"}],
                    }
                ]
            }
        ),
    )

    signal = auth.verify_cfr_supersession("3.303", today=date(2026, 11, 14))

    assert signal.status == "no_signal"

    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            auth.requests.ConnectionError("FR down")
        ),
    )

    unavailable = auth.verify_cfr_supersession("3.303", today=date(2026, 11, 14))

    assert unavailable.status == "unavailable"
    assert "FR down" in unavailable.detail


def test_supersession_action_and_replacement_helpers():
    assert auth._supersession_action({"action": "Final rule; repeal"}) == "repealed"
    assert auth._supersession_action({"abstract": "The section is reserved."}) == "reserved"
    assert auth._supersession_action({"title": "Renumbering and redesignation"}) == "renumbered"
    assert auth._supersession_action({"title": "Redesignation of part"}) == "redesignated"
    assert auth._supersession_action({"title": "Rule supersession"}) == "superseded"
    assert auth._supersession_action({"title": "Ordinary amendment"}) == ""
    assert auth._replacement_from_text("renumbered as 3.304") == "38 C.F.R. § 3.304"
    assert auth._replacement_from_text("replaced by 38 CFR 3.304") == "38 C.F.R. § 3.304"
    assert auth._replacement_from_text("superseded with 3.304") == "38 C.F.R. § 3.304"
    assert auth._replacement_from_text("no replacement stated") == ""


def test_build_signals_covers_current_missing_and_not_assessed(monkeypatch):
    cfr_sections = ["3.303", "3.304", "3.305", "3.306", "3.307"]
    usc_sections = ["1110", "1111", "1112", "1113"]
    checks = [
        _check("38 C.F.R. § 3.304", "eCFR", "verified", "present"),
        _check("38 C.F.R. § 3.305", "eCFR", "unresolved", "eCFR 404"),
        _check("38 C.F.R. § 3.306", "eCFR", "unresolved", "eCFR 404"),
        _check("38 C.F.R. § 3.307", "eCFR", "unavailable", "down"),
        _check("38 U.S.C. § 1111", "GovInfo", "verified", "present"),
        _check("38 U.S.C. § 1112", "GovInfo", "unresolved", "not found"),
        _check("38 U.S.C. § 1113", "GovInfo", "unavailable", "no key"),
    ]
    explicit = StatutorySupersessionSignal(
        authority="38 C.F.R. § 3.305",
        authority_type="regulation",
        source="Federal Register",
        status="candidate",
        signal="removed",
        detail="Explicit removal.",
    )
    no_action = StatutorySupersessionSignal(
        authority="38 C.F.R. § 3.306",
        authority_type="regulation",
        source="Federal Register",
        status="no_signal",
        detail="No explicit action.",
    )

    def fake_verify(section):
        return explicit if section == "3.305" else no_action

    monkeypatch.setattr(auth, "verify_cfr_supersession", fake_verify)

    signals = auth.build_statutory_supersession_signals(
        cfr_sections, usc_sections, checks
    )
    by_authority = {signal.authority: signal for signal in signals}

    assert by_authority["38 C.F.R. § 3.303"].status == "not_assessed"
    assert by_authority["38 C.F.R. § 3.304"].status == "no_signal"
    assert by_authority["38 C.F.R. § 3.305"] is explicit
    assert by_authority["38 C.F.R. § 3.306"].status == "candidate"
    assert by_authority["38 C.F.R. § 3.306"].signal == "not_in_current_source"
    assert "No explicit action." in by_authority["38 C.F.R. § 3.306"].detail
    assert by_authority["38 C.F.R. § 3.307"].status == "unavailable"
    assert by_authority["38 U.S.C. § 1110"].status == "not_assessed"
    assert by_authority["38 U.S.C. § 1111"].status == "no_signal"
    assert by_authority["38 U.S.C. § 1112"].status == "candidate"
    assert by_authority["38 U.S.C. § 1112"].signal == "not_in_current_source"
    assert by_authority["38 U.S.C. § 1113"].status == "unavailable"


def test_build_signals_handles_unavailable_federal_register(monkeypatch):
    check = _check("38 C.F.R. § 3.303", "eCFR", "unresolved", "eCFR 404")
    unavailable = StatutorySupersessionSignal(
        authority="38 C.F.R. § 3.303",
        authority_type="regulation",
        source="Federal Register",
        status="unavailable",
        detail="FR down",
    )
    monkeypatch.setattr(auth, "verify_cfr_supersession", lambda section: unavailable)

    signal = auth.build_statutory_supersession_signals(
        ["3.303"], [], [check]
    )[0]

    assert signal.status == "candidate"
    assert signal.signal == "not_in_current_source"
    assert "Federal Register check unavailable" in signal.detail


def test_verify_analysis_supersession_disabled_and_enabled(monkeypatch):
    analysis = LegalAnalysis(
        issue="tinnitus",
        summary="38 C.F.R. § 3.303 and 38 U.S.C. § 1110",
        how_it_affects_va_claims="",
    )
    checks = [
        _check("38 C.F.R. § 3.303", "eCFR", "verified"),
        _check("38 U.S.C. § 1110", "GovInfo", "verified"),
    ]

    monkeypatch.delenv("AUTHORITY_VERIFICATION", raising=False)
    assert auth.verify_analysis_supersession(analysis, [], checks) == []

    monkeypatch.setenv("AUTHORITY_VERIFICATION", "1")
    signals = auth.verify_analysis_supersession(analysis, [], checks)

    assert [signal.status for signal in signals] == ["no_signal", "no_signal"]
    assert [signal.authority_type for signal in signals] == ["regulation", "statute"]


def test_supersession_flags_only_surface_non_clean_states():
    signals = [
        StatutorySupersessionSignal(
            authority="38 C.F.R. § 3.303",
            authority_type="regulation",
            source="eCFR",
            status="no_signal",
        ),
        StatutorySupersessionSignal(
            authority="38 C.F.R. § 3.304",
            authority_type="regulation",
            source="Federal Register",
            status="candidate",
            signal="removed",
            detail="Removed by rule.",
        ),
        StatutorySupersessionSignal(
            authority="38 C.F.R. § 3.305",
            authority_type="regulation",
            source="eCFR",
            status="unavailable",
            detail="eCFR down",
        ),
        StatutorySupersessionSignal(
            authority="38 U.S.C. § 1110",
            authority_type="statute",
            source="GovInfo",
            status="not_assessed",
            detail="No key",
        ),
        StatutorySupersessionSignal(
            authority="38 U.S.C. § 1111",
            authority_type="statute",
            source="GovInfo",
            status="candidate",
            signal="not_in_current_source",
            detail="Not found.",
        ),
    ]

    flags = auth.statutory_supersession_flags(signals + [signals[1]])

    assert len(flags) == 4
    assert flags[0].startswith("Statutory/regulatory supersession signal:")
    assert "unavailable" in flags[1]
    assert "not assessed" in flags[2]
    assert "38 U.S.C. § 1111" in flags[3]


def test_row_references_accept_strings_and_ignore_dicts_without_sections():
    assert auth._row_matches_section({"cfr_references": ["3.303"]}, "3.303") is True
    assert auth._row_matches_section({"cfr_references": [{"title": 38}]}, "3.303") is True
    assert (
        auth._row_matches_section(
            {"cfr_references": [{"section": ""}, "", "3.303"]}, "3.303"
        )
        is True
    )


def test_verify_cfr_supersession_handles_non_list_results(monkeypatch):
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(payload={"results": "not-a-list"}),
    )

    signal = auth.verify_cfr_supersession("3.303", today=date(2026, 11, 14))

    assert signal.status == "no_signal"


def test_not_found_signal_without_explicit_detail(monkeypatch):
    check = _check("38 C.F.R. § 3.303", "eCFR", "unresolved", "eCFR 404")
    no_detail = StatutorySupersessionSignal(
        authority="38 C.F.R. § 3.303",
        authority_type="regulation",
        source="Federal Register",
        status="no_signal",
    )
    monkeypatch.setattr(auth, "verify_cfr_supersession", lambda section: no_detail)

    signal = auth.build_statutory_supersession_signals(["3.303"], [], [check])[0]

    assert signal.status == "candidate"
    assert signal.detail.endswith("confirmed supersession.")
