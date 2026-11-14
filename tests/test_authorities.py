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


def test_parse_cavc_panel_dockets_ignores_page_dates():
    """A filing date must not become a docket like ``19-2026``."""
    html = (
        '<table><tr>'
        '<td><a href="documents/Case_24-1234 (8-19-2026).pdf">24-1234</a></td>'
        '<td>8-19-2026</td></tr></table>'
    )

    assert auth.parse_cavc_panel_dockets(html) == {"24-1234"}


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
            authority="CAVC docket 24-1234",
            source="CAVC",
            status="conflict",
            detail="panel list contradicts Rule 30(a)",
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
    assert len(flags) == 3
    assert "Authority conflict" in flags[0]
    assert "Unresolved authority" in flags[1]
    assert "unavailable" in flags[2]


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
        return [
            {
                "packageId": "USCOURTS-ca13-24-1234",
                "title": "24-1234 - Veteran v. Secretary",
            }
        ]

    monkeypatch.setattr(client, "search", fake_search)
    case = CaseRecord(
        title="Veteran v. Secretary",
        court=COURT_FEDERAL_CIRCUIT,
    )

    check = auth._verify_federal_circuit_case(case, client)

    assert check.status == "verified"
    assert check.url.endswith("/USCOURTS-ca13-24-1234")
    assert "collection:(USCOURTS)" in seen["query"]
    assert "courtcode:ca13" in seen["query"]


def test_federal_circuit_case_rejects_a_non_ca13_or_wrong_title_match(monkeypatch):
    client = auth.GovInfoClient("demo-key")
    case = CaseRecord(title="Veteran v. Secretary", court=COURT_FEDERAL_CIRCUIT)
    rows = iter(
        [
            [{"packageId": "USCOURTS-ca13-1", "title": "Other v. Secretary"}],
            [{"packageId": "USCOURTS-ca7-1", "title": "Veteran v. Secretary"}],
        ]
    )
    monkeypatch.setattr(client, "search", lambda *args, **kwargs: next(rows))

    assert auth._verify_federal_circuit_case(case, client).status == "unresolved"
    assert auth._verify_federal_circuit_case(case, client).status == "unresolved"


@pytest.fixture(autouse=True)
def _clear_authority_state():
    auth.clear_authority_caches()
    yield
    auth.clear_authority_caches()


def test_normalize_cavc_docket_without_match_returns_empty():
    assert auth._normalize_cavc_docket("not a docket") == ""


def test_fetch_cavc_panel_dockets_caches_a_successful_parse(monkeypatch):
    calls = {"n": 0}

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        return FakeResponse(
            content=(
                b'<a href="/documents/Case_24-1234.pdf">Case [24-1234]</a>'
                b'<a href="/documents/Other_24-5678.pdf">Other [24-5678]</a>'
            )
        )

    monkeypatch.setattr(auth.requests, "get", fake_get)

    assert auth.fetch_cavc_panel_dockets() == {"24-1234", "24-5678"}
    assert auth.fetch_cavc_panel_dockets() == {"24-1234", "24-5678"}
    assert calls["n"] == 1


def test_fetch_cavc_panel_dockets_reports_request_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise auth.requests.ConnectionError("CAVC down")

    monkeypatch.setattr(auth.requests, "get", fail)

    with pytest.raises(SearchError, match="CAVC down"):
        auth.fetch_cavc_panel_dockets()


def test_fetch_cavc_panel_dockets_reports_an_empty_list(monkeypatch):
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(content=b"<html>No decisions</html>"),
    )

    with pytest.raises(SearchError, match="no recognizable docket"):
        auth.fetch_cavc_panel_dockets()


def test_validate_cavc_panel_cases_without_cavc_cases_does_not_fetch(monkeypatch):
    monkeypatch.setattr(
        auth,
        "fetch_cavc_panel_dockets",
        lambda: pytest.fail("should not fetch without a CAVC case"),
    )

    assert auth.validate_cavc_panel_cases([]) == []
    assert auth.validate_cavc_panel_cases([_case(court="BVA")]) == []


def test_validate_cavc_panel_cases_skips_a_case_without_a_docket(monkeypatch):
    monkeypatch.setattr(auth, "fetch_cavc_panel_dockets", lambda: {"24-1234"})

    assert auth.validate_cavc_panel_cases([_case(docket="")]) == []


def test_validate_cavc_panel_cases_panel_with_known_precedential_status(monkeypatch):
    monkeypatch.setattr(auth, "fetch_cavc_panel_dockets", lambda: {"24-1234"})
    case = _case(precedential=True, rule_30a=False)

    checks = auth.validate_cavc_panel_cases([case])

    assert checks[0].status == "verified"
    assert case.cavc_panel_status == "panel"
    assert case.precedential is True


def test_validate_cavc_panel_cases_generic_nonprecedential_flag_is_not_conflict(
    monkeypatch,
):
    """The official list overrides a generic non-precedential body marker."""
    monkeypatch.setattr(auth, "fetch_cavc_panel_dockets", lambda: {"24-1234"})
    case = _case(docket="24-1234", precedential=False, rule_30a=False)

    checks = auth.validate_cavc_panel_cases([case])

    assert case.cavc_panel_status == "panel"
    assert checks[0].status == "verified"
    assert case.precedential is True


def test_validate_cavc_panel_cases_requires_an_explicit_docket(monkeypatch):
    monkeypatch.setattr(auth, "fetch_cavc_panel_dockets", lambda: {"24-1234"})
    case = _case(
        docket="",
        title="Smith v. Collins, 24-1234",
        url="https://example.com/opinions/24-1234",
    )

    assert auth.validate_cavc_panel_cases([case]) == []


def test_validate_cavc_panel_cases_rule_30a_unknown_sets_false(monkeypatch):
    monkeypatch.setattr(auth, "fetch_cavc_panel_dockets", lambda: set())
    case = _case(docket="24-9999", rule_30a=True, precedential=None)

    checks = auth.validate_cavc_panel_cases([case])

    assert checks[0].status == "verified"
    assert case.precedential is False


def test_analysis_text_includes_cases_and_analysis_fragments():
    analysis = LegalAnalysis(
        issue="tinnitus",
        summary="Summary text.",
        how_it_affects_va_claims="Affects claims.",
        likely_applicable_principles=["Nexus principle"],
        next_steps=["Obtain a nexus opinion"],
        strengths=["Strong evidence"],
        gaps=["Missing diagnosis"],
    )
    case = CaseRecord(
        title="Smith",
        court=COURT_CAVC,
        citation="30 Vet.App. 123",
        holding="Holding text.",
        statutes=["38 C.F.R. § 3.303"],
    )

    text = auth._analysis_text(analysis, [case])

    assert "30 Vet.App. 123" in text
    assert "Holding text." in text
    assert "38 C.F.R. § 3.303" in text
    assert "Summary text." in text
    assert "Strong evidence" in text


def test_extract_cfr_sections_honors_limit_and_deduplicates():
    text = "38 C.F.R. § 3.303, 38 C.F.R. § 3.303, 38 C.F.R. § 4.1"
    assert auth.extract_cfr_sections(text, limit=1) == ["3.303"]


def test_extract_usc_sections_is_unique_and_limited():
    text = "38 U.S.C. § 1110 and 38 U.S.C. § 5107; 38 U.S.C. § 1110"
    assert auth._extract_usc_sections(text) == ["1110", "5107"]
    assert auth._extract_usc_sections(text, limit=1) == ["1110"]


def test_ecfr_current_date_prefers_up_to_date_and_caches(monkeypatch):
    calls = {"n": 0}

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        return FakeResponse(
            payload={
                "titles": [
                    {"number": 37, "up_to_date_as_of": "2026-01-01"},
                    {"number": 38, "up_to_date_as_of": "2026-11-12"},
                ]
            }
        )

    monkeypatch.setattr(auth.requests, "get", fake_get)

    assert auth._ecfr_current_date() == "2026-11-12"
    assert auth._ecfr_current_date() == "2026-11-12"
    assert calls["n"] == 1


def test_ecfr_current_date_falls_back_to_issue_and_amended_dates(monkeypatch):
    payloads = iter(
        [
            {"titles": [{"number": 38, "latest_issue_date": "2026-11-11"}]},
            {"titles": [{"number": 38, "latest_amended_on": "2026-11-10"}]},
        ]
    )
    monkeypatch.setattr(
        auth.requests, "get", lambda *args, **kwargs: FakeResponse(payload=next(payloads))
    )

    assert auth._ecfr_current_date() == "2026-11-11"
    auth.clear_authority_caches()
    assert auth._ecfr_current_date() == "2026-11-10"


def test_ecfr_current_date_requires_title_38(monkeypatch):
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            payload={"titles": [{"number": 37}, {"number": 38}]}
        ),
    )

    with pytest.raises(SearchError, match="title 38"):
        auth._ecfr_current_date()


def test_verify_cfr_section_reports_404(monkeypatch):
    monkeypatch.setattr(auth, "_ecfr_current_date", lambda: "2026-11-12")
    monkeypatch.setattr(
        auth.requests, "get", lambda *args, **kwargs: FakeResponse(status_code=404)
    )

    check = auth.verify_cfr_section("3.999")

    assert check.status == "unresolved"
    assert "404" in check.detail


def test_verify_cfr_section_reports_request_failure(monkeypatch):
    monkeypatch.setattr(auth, "_ecfr_current_date", lambda: "2026-11-12")
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            auth.requests.ConnectionError("eCFR down")
        ),
    )

    check = auth.verify_cfr_section("3.303")

    assert check.status == "unavailable"
    assert "eCFR down" in check.detail


def test_verify_cfr_section_reports_unparseable_xml(monkeypatch):
    monkeypatch.setattr(auth, "_ecfr_current_date", lambda: "2026-11-12")
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(content=b"<DIV8 not closed"),
    )

    check = auth.verify_cfr_section("3.303")

    assert check.status == "unavailable"
    assert "unparseable XML" in check.detail


def test_verify_cfr_section_ignores_non_division_elements(monkeypatch):
    monkeypatch.setattr(auth, "_ecfr_current_date", lambda: "2026-11-12")
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(content=b"<ROOT><P/></ROOT>"),
    )

    check = auth.verify_cfr_section("3.303")

    assert check.status == "unresolved"


def test_verify_cfr_section_accepts_hierarchy_metadata(monkeypatch):
    monkeypatch.setattr(auth, "_ecfr_current_date", lambda: "2026-11-12")
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            content=(
                b'<ROOT><DIV8 N="3.304" '
                b'hierarchy_metadata="38 CFR 3.303"></DIV8></ROOT>'
            )
        ),
    )

    check = auth.verify_cfr_section("3.303")

    assert check.status == "verified"


def test_verify_cfr_section_rejects_a_longer_section_prefix(monkeypatch):
    monkeypatch.setattr(auth, "_ecfr_current_date", lambda: "2026-11-12")
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            content=(
                b'<ROOT><DIV8 N="3.304" '
                b'hierarchy_metadata="38 CFR 3.3030"></DIV8></ROOT>'
            )
        ),
    )

    check = auth.verify_cfr_section("3.303")

    assert check.status == "unresolved"


def test_verify_federal_register_part_reports_request_failure(monkeypatch):
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            auth.requests.ConnectionError("FR down")
        ),
    )

    check = auth.verify_federal_register_part("3", today=date(2026, 11, 14))

    assert check.status == "unavailable"
    assert "FR down" in check.detail


def test_govinfo_search_wraps_request_errors(monkeypatch):
    monkeypatch.setenv("GOVINFO_API_KEY", "demo-key")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            auth.requests.ConnectionError("GovInfo down")
        ),
    )

    with pytest.raises(SearchError, match="GovInfo down"):
        auth.GovInfoClient().search("collection:uscode")


def test_govinfo_search_rejects_non_list_results(monkeypatch):
    monkeypatch.setenv("GOVINFO_API_KEY", "demo-key")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(payload={"results": {"bad": 1}}),
    )

    with pytest.raises(SearchError, match="unexpected response"):
        auth.GovInfoClient().search("collection:uscode")


def test_govinfo_search_rejects_a_non_object_payload(monkeypatch):
    monkeypatch.setenv("GOVINFO_API_KEY", "demo-key")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(payload=[{"bad": 1}]),
    )

    with pytest.raises(SearchError, match="unexpected response"):
        auth.GovInfoClient().search("collection:uscode")


def test_govinfo_search_returns_empty_list_for_null_results(monkeypatch):
    monkeypatch.setenv("GOVINFO_API_KEY", "demo-key")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(payload={"results": None}),
    )

    assert auth.GovInfoClient().search("collection:uscode") == []


def test_verify_us_code_requires_the_exact_title_38_granule(monkeypatch):
    client = auth.GovInfoClient("demo-key")
    rows = iter(
        [
            [
                {
                    "packageId": "USCODE-2024-title38",
                    "granuleId": "USCODE-2024-title38-partII-chap11-sec1110",
                }
            ],
            [
                {
                    "packageId": "USCODE-2024-title38",
                    "granuleId": "USCODE-2024-title38-partII-chap11-sec9999",
                }
            ],
            [
                {
                    "packageId": "USCODE-2024-title5",
                    "granuleId": "USCODE-2024-title5-sec1110",
                }
            ],
        ]
    )

    monkeypatch.setattr(client, "search", lambda *args, **kwargs: next(rows))

    verified = auth._verify_us_code("1110", client)
    wrong_section = auth._verify_us_code("1110", client)
    wrong_title = auth._verify_us_code("1110", client)

    assert verified.status == "verified"
    assert "USCODE-2024-title38" in verified.detail
    assert verified.url.endswith("/USCODE-2024-title38")
    assert wrong_section.status == "unresolved"
    assert wrong_title.status == "unresolved"


def test_verify_us_code_reports_unresolved_and_unavailable(monkeypatch):
    client = auth.GovInfoClient("demo-key")
    monkeypatch.setattr(client, "search", lambda *args, **kwargs: [])

    assert auth._verify_us_code("1110", client).status == "unresolved"

    def fail(*args, **kwargs):
        raise SearchError("GovInfo quota")

    monkeypatch.setattr(client, "search", fail)

    assert auth._verify_us_code("1110", client).status == "unavailable"


def test_verify_federal_circuit_case_reports_unresolved_and_unavailable(monkeypatch):
    client = auth.GovInfoClient("demo-key")
    case = CaseRecord(title="Veteran v. Secretary", court=COURT_FEDERAL_CIRCUIT)
    monkeypatch.setattr(client, "search", lambda *args, **kwargs: [])

    assert auth._verify_federal_circuit_case(case, client).status == "unresolved"

    def fail(*args, **kwargs):
        raise SearchError("GovInfo quota")

    monkeypatch.setattr(client, "search", fail)

    assert auth._verify_federal_circuit_case(case, client).status == "unavailable"


def test_verify_analysis_authorities_runs_govinfo_checks(monkeypatch):
    monkeypatch.setenv("AUTHORITY_VERIFICATION", "1")
    monkeypatch.setenv("GOVINFO_API_KEY", "demo-key")
    monkeypatch.setattr(auth, "extract_cfr_sections", lambda text: [])
    monkeypatch.setattr(auth, "_extract_usc_sections", lambda text: ["1110"])
    monkeypatch.setattr(
        auth,
        "_verify_us_code",
        lambda section, client: AuthorityCheck(
            authority=f"38 U.S.C. § {section}",
            source="GovInfo",
            status="verified",
        ),
    )
    monkeypatch.setattr(
        auth,
        "_verify_federal_circuit_case",
        lambda case, client: AuthorityCheck(
            authority=f"Federal Circuit: {case.title}",
            source="GovInfo",
            status="verified",
        ),
    )
    analysis = LegalAnalysis(issue="tinnitus", summary="38 U.S.C. § 1110", how_it_affects_va_claims="")
    cases = [CaseRecord(title="Veteran v. Secretary", court=COURT_FEDERAL_CIRCUIT)]

    checks = auth.verify_analysis_authorities(analysis, cases)

    assert [check.source for check in checks] == ["GovInfo", "GovInfo"]


def test_authority_flags_deduplicates_identical_flags():
    check = AuthorityCheck(
        authority="38 C.F.R. § 3.303",
        source="eCFR",
        status="unresolved",
        detail="not found",
    )

    flags = auth.authority_flags([check, check])

    assert flags == [
        "Unresolved authority '38 C.F.R. § 3.303' (eCFR): not found"
    ]
