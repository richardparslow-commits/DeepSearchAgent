"""Focused tests for optional GovInfo Public Law and Statutes at Large records."""

from __future__ import annotations

import pytest
import requests

import va_legal_agent.authorities as auth
from va_legal_agent.__main__ import render_analysis
from va_legal_agent.config import Settings
from va_legal_agent.models import AuthorityCheck, LegalAnalysis
from va_legal_agent.search import SearchError


class FakeResponse:
    def __init__(self, *, payload=None, text="", status_code=200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


def _client(include_records=True):
    return auth.GovInfoClient("demo-key", include_records=include_records)


def _check():
    return AuthorityCheck(
        authority="38 U.S.C. § 1110",
        source="GovInfo",
        status="verified",
        detail="GovInfo U.S. Code matched title 38 section 1110.",
    )


def _rows():
    return {
        "PLAW": [
            {"packageId": "PLAW-118pvtl-1", "title": "Private law"},
            {"packageId": "BAD-PLAW", "title": "Invalid package"},
            {
                "packageId": "PLAW-118publ-123",
                "title": "Veterans Benefits Act",
                "dateIssued": "2024-06-01",
            },
            {
                "packageId": "PLAW-118publ-123",
                "title": "Duplicate law",
            },
            {
                "packageId": "PLAW-118publ-124",
                "plawcitation": "Public Law 118-124",
                "title": "Second Veterans Act",
                "dateIssued": "2024-07-01",
            },
            {
                "packageId": "PLAW-118publ-125",
                "title": "Third law should be capped",
            },
        ],
        "STATUTE": [
            {
                "packageId": "STATUTE-118",
                "granuleId": "STATUTE-118-FrontMatter-Pgi",
                "title": "Front matter",
            },
            {
                "packageId": "STATUTE-118",
                "granuleId": "OTHER-118-Pg1",
                "title": "Wrong package granule",
            },
            {
                "packageId": "STATUTE-118",
                "granuleId": "STATUTE-118-Pg3754",
                "title": "Veterans Benefits Act",
                "dateIssued": "2024-06-01",
            },
            {
                "packageId": "STATUTE-118",
                "granuleId": "STATUTE-118-Pg3754",
                "title": "Duplicate statute",
            },
            {
                "packageId": "STATUTE-118",
                "granuleId": "STATUTE-118-Pg4000",
                "title": "Second Veterans Act",
                "dateIssued": "2024-07-01",
            },
            {
                "packageId": "STATUTE-118",
                "granuleId": "STATUTE-118-Pg5000",
                "title": "Third volume record should be capped",
            },
        ],
    }


def _fake_search(monkeypatch):
    queries = []
    rows = _rows()

    def search(query, page_size=5):
        queries.append((query, page_size))
        if "collection:PLAW" in query:
            return list(rows["PLAW"])
        if "collection:STATUTE" in query:
            return list(rows["STATUTE"])
        return [
            {
                "packageId": "USCODE-2024-title38",
                "granuleId": "USCODE-2024-title38-partII-chap11-sec1110",
            }
        ]

    monkeypatch.setattr(auth.GovInfoClient, "search", staticmethod(search))
    return queries


def _fake_text(monkeypatch, *, fail=False):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if fail:
            raise requests.ConnectionError("text host down")
        return FakeResponse(
            text=(
                "<html><body><script>ignore()</script>"
                "<p>Public law text for veterans.</p></body></html>"
            )
        )

    monkeypatch.setattr(auth.requests, "get", get)
    return calls


def test_govinfo_legislation_records_are_optional_by_default():
    assert auth.GovInfoClient("demo-key").include_records is False


def test_enrichment_attaches_public_laws_statutes_and_stable_links(
    monkeypatch,
):
    queries = _fake_search(monkeypatch)
    text_calls = _fake_text(monkeypatch)
    check = _check()

    auth._enrich_govinfo_legislation(check, "1110", _client())

    assert check.records_status == "retrieved"
    assert len(check.public_laws) == 2
    assert len(check.statutes_at_large) == 2
    assert check.public_laws[0].citation == "Pub. L. 118-123"
    assert check.public_laws[1].citation == "Public Law 118-124"
    assert check.public_laws[0].details_url == (
        "https://www.govinfo.gov/app/details/PLAW-118publ-123"
    )
    assert check.public_laws[0].text_url == (
        "https://www.govinfo.gov/content/pkg/PLAW-118publ-123/html/"
        "PLAW-118publ-123.htm"
    )
    assert check.statutes_at_large[0].citation == "118 Stat. 3754"
    assert check.statutes_at_large[0].details_url == (
        "https://www.govinfo.gov/app/details/STATUTE-118/STATUTE-118-Pg3754"
    )
    assert check.statutes_at_large[0].text_url == (
        "https://www.govinfo.gov/content/pkg/STATUTE-118/html/"
        "STATUTE-118-Pg3754.htm"
    )
    assert check.public_laws[0].text_status == "retrieved"
    assert check.public_laws[0].text_excerpt == (
        "Public law text for veterans."
    )
    assert len(text_calls) == 4
    assert queries[0][0].startswith("collection:PLAW")
    assert queries[0][0].endswith('uscodecitation:"38 U.S.C. 1110"')
    assert queries[1][0].startswith("collection:STATUTE")


def test_enrichment_reports_not_found_without_claiming_no_law_exists(
    monkeypatch,
):
    monkeypatch.setattr(
        auth.GovInfoClient,
        "search",
        staticmethod(lambda query, page_size=5: []),
    )
    check = _check()

    auth._enrich_govinfo_legislation(check, "1110", _client())

    assert check.records_status == "not_found"
    assert "does not prove" in check.records_detail
    assert check.public_laws == []
    assert check.statutes_at_large == []


def test_enrichment_is_partial_when_one_search_fails(monkeypatch):
    def search(query, page_size=5):
        if "collection:STATUTE" in query:
            raise SearchError("Statutes search failed")
        return [_rows()["PLAW"][2]]

    monkeypatch.setattr(auth.GovInfoClient, "search", staticmethod(search))
    _fake_text(monkeypatch)
    check = _check()

    auth._enrich_govinfo_legislation(check, "1110", _client())

    assert check.records_status == "partial"
    assert len(check.public_laws) == 1
    assert "Statutes search failed" in check.records_detail
    assert auth.authority_flags([check]) == [
        "GovInfo legislative records partial for 38 U.S.C. § 1110: "
        + check.records_detail
    ]


def test_enrichment_is_unavailable_when_all_searches_fail(monkeypatch):
    def search(query, page_size=5):
        raise SearchError("GovInfo unavailable")

    monkeypatch.setattr(auth.GovInfoClient, "search", staticmethod(search))
    check = _check()

    auth._enrich_govinfo_legislation(check, "1110", _client())

    assert check.records_status == "unavailable"
    assert "Public Laws search unavailable" in check.records_detail
    assert "Statutes at Large search unavailable" in check.records_detail


def test_text_failure_keeps_record_and_marks_enrichment_partial(
    monkeypatch,
):
    def search(query, page_size=5):
        if "collection:PLAW" in query:
            return [_rows()["PLAW"][2]]
        return []

    monkeypatch.setattr(auth.GovInfoClient, "search", staticmethod(search))
    _fake_text(monkeypatch, fail=True)
    check = _check()

    auth._enrich_govinfo_legislation(check, "1110", _client())

    assert check.records_status == "partial"
    law = check.public_laws[0]
    assert law.text_status == "unavailable"
    assert law.text_excerpt == ""
    assert "text host down" in law.detail


def test_missing_statute_granule_keeps_details_link_without_text(
    monkeypatch,
):
    def search(query, page_size=5):
        if "collection:PLAW" in query:
            return []
        return [{"packageId": "STATUTE-118", "title": "Volume only"}]

    monkeypatch.setattr(auth.GovInfoClient, "search", staticmethod(search))
    check = _check()

    auth._enrich_govinfo_legislation(check, "1110", _client())

    statute = check.statutes_at_large[0]
    assert statute.granule_id == ""
    assert statute.details_url == (
        "https://www.govinfo.gov/app/details/STATUTE-118"
    )
    assert statute.text_url == ""
    assert statute.text_status == "not_available"
    assert check.records_status == "partial"


def test_verify_us_code_does_not_fetch_records_when_disabled(monkeypatch):
    calls = []

    def search(query, page_size=5):
        calls.append(query)
        return [
            {
                "packageId": "USCODE-2024-title38",
                "granuleId": "USCODE-2024-title38-partII-chap11-sec1110",
            }
        ]

    monkeypatch.setattr(auth.GovInfoClient, "search", staticmethod(search))
    check = auth._verify_us_code("1110", _client(include_records=False))

    assert check.status == "verified"
    assert len(calls) == 1
    assert check.records_status == ""
    assert check.public_laws == []


def test_verify_us_code_attaches_records_when_enabled(monkeypatch):
    _fake_search(monkeypatch)
    _fake_text(monkeypatch)
    check = auth._verify_us_code("1110", _client(include_records=True))

    assert check.status == "verified"
    assert check.records_status == "retrieved"
    assert check.public_laws[0].package_id == "PLAW-118publ-123"
    assert check.statutes_at_large[0].granule_id == "STATUTE-118-Pg3754"


def test_public_law_parser_rejects_private_laws_and_builds_citation():
    assert auth._parse_govinfo_public_law(
        {"packageId": "PLAW-118pvtl-2"}
    ) is None
    assert auth._parse_govinfo_public_law({"packageId": "BAD"}) is None
    record = auth._parse_govinfo_public_law(
        {
            "packageId": "PLAW-118publ-123",
            "plawcitation": "Public Law 118-123",
            "title": "A law",
            "dateIssued": "2024-01-01",
        }
    )
    assert record is not None
    assert record.citation == "Public Law 118-123"


def test_statute_parser_rejects_front_matter_and_uses_page_citation():
    assert auth._parse_govinfo_statute(
        {
            "packageId": "STATUTE-118",
            "granuleId": "STATUTE-118-FrontMatter-Pgi",
        }
    ) is None
    assert auth._parse_govinfo_statute({"packageId": "BAD"}) is None
    record = auth._parse_govinfo_statute(
        {
            "packageId": "STATUTE-118",
            "granuleId": "STATUTE-118-Pg3754",
            "title": "A law",
        }
    )
    assert record is not None
    assert record.citation == "118 Stat. 3754"
    with_citation = auth._parse_govinfo_statute(
        {
            "packageId": "STATUTE-118",
            "granuleId": "STATUTE-118-Pg3754",
            "statutecitation": "118 Stat. 3754",
        }
    )
    assert with_citation is not None
    assert with_citation.citation == "118 Stat. 3754"


def test_govinfo_text_extraction_strips_markup_and_wraps_errors(
    monkeypatch,
):
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda url, **kwargs: FakeResponse(
            text="<html><style>x</style><body>Hello <b>veterans</b>.</body></html>"
        ),
    )
    assert auth._fetch_govinfo_text("https://example.test") == "Hello veterans ."

    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda url, **kwargs: FakeResponse(text="<script>x</script>"),
    )
    with pytest.raises(SearchError, match="no readable text"):
        auth._fetch_govinfo_text("https://example.test")

    def fail(url, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(auth.requests, "get", fail)
    with pytest.raises(SearchError, match="offline"):
        auth._fetch_govinfo_text("https://example.test")


def test_excerpt_is_bounded():
    excerpt = auth._govinfo_excerpt("word " * 400)
    assert len(excerpt) == auth._GOVINFO_TEXT_EXCERPT_LIMIT
    assert excerpt.endswith("...")


def test_optional_setting_is_parsed_and_defaults_off(monkeypatch):
    monkeypatch.delenv("GOVINFO_LEGISLATION_RECORDS", raising=False)
    assert Settings().govinfo_legislation_records is False
    monkeypatch.setenv("GOVINFO_LEGISLATION_RECORDS", "1")
    assert Settings.from_env().govinfo_legislation_records is True


def test_text_rendering_shows_records_links_and_excerpts(monkeypatch):
    _fake_search(monkeypatch)
    _fake_text(monkeypatch)
    check = _check()
    auth._enrich_govinfo_legislation(check, "1110", _client())
    analysis = LegalAnalysis(
        issue="service connection",
        summary="",
        how_it_affects_va_claims="",
        authority_checks=[check],
    )

    text = render_analysis(analysis, "text")

    assert "GovInfo records [retrieved]" in text
    assert "Public Law: Pub. L. 118-123" in text
    assert "Record: https://www.govinfo.gov/app/details/PLAW-118publ-123" in text
    assert "Text: https://www.govinfo.gov/content/pkg/PLAW-118publ-123/html/" in text
    assert "Excerpt: Public law text for veterans." in text
    assert "Statutes at Large: 118 Stat. 3754" in text
