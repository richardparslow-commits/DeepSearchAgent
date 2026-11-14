"""Tests for external authority verification (CAVC, eCFR, FR, GovInfo)."""

from datetime import date

import pytest

import va_legal_agent.authorities as auth
from va_legal_agent.models import AuthorityCheck, CaseRecord, LegalAnalysis
from va_legal_agent.search import SearchError
from va_legal_agent.topics import COURT_CAVC, COURT_FEDERAL_CIRCUIT


class FakeResponse:
    def __init__(self, *, payload=None, content=b"", status_code=200):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.text = content.decode("utf-8", errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise authe_requests_error(self)

    def json(self):
        return self._payload


def authe_requests_error(response):
    import requests

    return requests.HTTPError(f"HTTP {response.status_code}", response=response)


def _case(**overrides):
    data = {
        "title": "Smith v. Collins",
        "court": COURT_CAVC,
        "docket": "24-1234",
    }
    data.update(overrides)
    return CaseRecord(**data)


def test_parse_cavc_panel_dockets_extracts_links_and_dedupes():
    html = """
    <table>
      <tr><td><a href="/documents/Smith_24-1234.pdf">Smith [24-1234]</a></td></tr>
      <tr><td><a href="/documents/Jones_20-8342(E).pdf">Jones [20-8342(E)]</a></td></tr>
      <tr><td><a href="/documents/Other_24-1234.pdf">Other [24-1234]</a></td></tr>
      <tr><td><a href="/case_archives.php">Archive</a></td></tr>
    </table>
    """
    assert auth.parse_cavc_panel_dockets(html) == {"24-1234", "20-8342"}


def test_validate_cavc_panel_cases_conflict_confirmation_and_not_listed(monkeypatch):
    monkeypatch.setattr(
        auth, "fetch_cavc_panel_dockets", lambda: {"24-1234", "24-5678"}
    )
    conflict = _case(docket="24-1234", rule_30a=True, precedential=False)
    confirmed = _case(docket="24-9999", rule_30a=True, precedential=False)
    unlisted = _case(docket="24-5555", rule_30a=False)

    checks = auth.validate_cavc_panel_cases([conflict, confirmed, unlisted])

    assert conflict.cavc_panel_status == "conflict"
    assert confirmed.cavc_panel_status == "verified_nonprecedential"
    assert unlisted.cavc_panel_status == "not_listed"
    assert [check.status for check in checks] == ["conflict", "verified", "not_listed"]
    assert "Panel Opinions" in checks[0].detail


def test_validate_cavc_panel_cases_panel_listed_sets_unknown_precedential(monkeypatch):
    monkeypatch.setattr(auth, "fetch_cavc_panel_dockets", lambda: {"24-1234"})
    case = _case(precedential=None, rule_30a=False)

    checks = auth.validate_cavc_panel_cases([case])

    assert checks[0].status == "verified"
    assert case.precedential is True
    assert case.cavc_panel_status == "panel"


def test_validate_cavc_panel_cases_unavailable(monkeypatch):
    def fail():
        raise SearchError("list down")

    monkeypatch.setattr(auth, "fetch_cavc_panel_dockets", fail)

    checks = auth.validate_cavc_panel_cases([_case()])

    assert len(checks) == 1
    assert checks[0].status == "unavailable"
    assert "list down" in checks[0].detail


def test_extract_cfr_sections_is_unique_and_ordered():
    text = (
        "38 C.F.R. § 3.303 and 38 CFR 3.303(b); "
        "see also 38 C.F.R. § 4.1 and 38 C.F.R. § 3.159."
    )
    assert auth.extract_cfr_sections(text) == ["3.303", "4.1", "3.159"]


def test_verify_cfr_section_verified(monkeypatch):
    monkeypatch.setattr(auth, "_ecfr_current_date", lambda: "2026-11-09")
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        captured.update(url=url, params=params)
        return FakeResponse(content=b'<DIV8 N="3.303"><HEAD>Service connection</HEAD></DIV8>')

    monkeypatch.setattr(auth.requests, "get", fake_get)

    check = auth.verify_cfr_section("3.303")

    assert check.status == "verified"
    assert check.source == "eCFR"
    assert captured["params"] == {"part": "3", "section": "3.303"}
    assert "title-38.xml" in captured["url"]


def test_verify_cfr_section_missing_is_unresolved(monkeypatch):
    monkeypatch.setattr(auth, "_ecfr_current_date", lambda: "2026-11-09")
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(content=b"<DIV8 N='3.304'/>"),
    )

    check = auth.verify_cfr_section("3.999")

    assert check.status == "unresolved"
    assert "no section 3.999" in check.detail


def test_verify_federal_register_part_reports_latest_rule(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        captured.update(url=url, params=params)
        return FakeResponse(
            payload={
                "results": [
                    {
                        "title": "Service Connection Updates",
                        "publication_date": "2026-10-01",
                        "html_url": "https://www.federalregister.gov/d/2026-12345",
                    }
                ]
            }
        )

    monkeypatch.setattr(auth.requests, "get", fake_get)

    check = auth.verify_federal_register_part("3", today=date(2026, 11, 14))

    assert check.status == "verified"
    assert "Service Connection Updates" in check.detail
    assert captured["params"]["conditions[cfr][part]"] == "3"
    assert captured["params"]["conditions[publication_date][gte]"] == "2025-11-14"


def test_verify_federal_register_part_no_recent_documents(monkeypatch):
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(payload={"results": []}),
    )

    check = auth.verify_federal_register_part("3", today=date(2026, 11, 14))

    assert check.status == "no_recent_documents"


def test_govinfo_search_requires_key(monkeypatch):
    monkeypatch.delenv("GOVINFO_API_KEY", raising=False)
    with pytest.raises(SearchError, match="GOVINFO_API_KEY"):
        auth.GovInfoClient(api_key="").search("collection:uscode")


def test_govinfo_search_posts_query_and_key(monkeypatch):
    monkeypatch.setenv("GOVINFO_API_KEY", "demo-key")
    captured = {}

    def fake_post(url, params=None, json=None, headers=None, timeout=None, **kwargs):
        captured.update(url=url, params=params, body=json)
        return FakeResponse(payload={"results": [{"packageId": "USCODE-2024-title38"}]})

    monkeypatch.setattr(auth.requests, "post", fake_post)

    results = auth.GovInfoClient().search('collection:uscode citation:"38 U.S.C. 1110"')

    assert results[0]["packageId"] == "USCODE-2024-title38"
    assert captured["url"] == auth.GOVINFO_SEARCH_URL
    assert captured["params"] == {"api_key": "demo-key"}
    assert captured["body"]["query"].startswith("collection:uscode")
    assert captured["body"]["offsetMark"] == "*"


def test_verify_analysis_authorities_skips_when_disabled(monkeypatch):
    monkeypatch.delenv("AUTHORITY_VERIFICATION", raising=False)
    analysis = LegalAnalysis(
        issue="tinnitus",
        summary="38 C.F.R. § 3.303 applies.",
        how_it_affects_va_claims="",
    )
    assert auth.verify_analysis_authorities(analysis, []) == []


def test_authority_flags_only_surfaces_problems():
    checks = [
        AuthorityCheck(
            authority="CAVC docket 24-1", source="CAVC", status="verified"
        ),
        AuthorityCheck(
            authority="38 C.F.R. § 3.303",
            source="eCFR",
            status="unresolved",
            detail="not found",
        ),
        AuthorityCheck(
            authority="38 C.F.R. Part 3",
            source="Federal Register",
            status="no_recent_documents",
        ),
        AuthorityCheck(
            authority="38 U.S.C. § 1110",
            source="GovInfo",
            status="unavailable",
            detail="no key",
        ),
    ]
    flags = auth.authority_flags(checks)
    assert len(flags) == 2
    assert "Unresolved authority" in flags[0]
    assert "unavailable" in flags[1]


def test_verify_analysis_authorities_uses_ecfr_and_fr(monkeypatch):
    monkeypatch.setenv("AUTHORITY_VERIFICATION", "1")
    monkeypatch.delenv("GOVINFO_API_KEY", raising=False)
    monkeypatch.setattr(auth, "_ecfr_current_date", lambda: "2026-11-09")
    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        if "federalregister.gov" in url:
            return FakeResponse(payload={"results": []})
        return FakeResponse(content=b'<DIV8 N="3.303"/>')

    monkeypatch.setattr(auth.requests, "get", fake_get)
    analysis = LegalAnalysis(
        issue="tinnitus",
        summary="See 38 C.F.R. § 3.303.",
        how_it_affects_va_claims="",
    )

    checks = auth.verify_analysis_authorities(analysis, [])

    assert [check.source for check in checks] == ["eCFR", "Federal Register"]
    assert all(check.status in {"verified", "no_recent_documents"} for check in checks)


def test_federal_circuit_case_cross_check_uses_govinfo(monkeypatch):
    monkeypatch.setenv("GOVINFO_API_KEY", "demo-key")
    client = auth.GovInfoClient()
    seen = {}

    def fake_search(query, page_size=5):
        seen["query"] = query
        return [{"packageId": "USCOURTS-cafc-1"}]

    monkeypatch.setattr(client, "search", fake_search)
    case = CaseRecord(
        title="Veteran v. Secretary",
        court=COURT_FEDERAL_CIRCUIT,
    )

    check = auth._verify_federal_circuit_case(case, client)

    assert check.status == "verified"
    assert "collection:(USCOURTS)" in seen["query"]
    assert "circuit:federal" in seen["query"]
