"""Tests for optional Congress.gov legislative-history links."""

from __future__ import annotations

import pytest
import requests

import va_legal_agent.congress as congress
from audit_invariants import assert_exception_chain_redacted
from va_legal_agent.__main__ import render_analysis
from va_legal_agent.config import Settings
from va_legal_agent.models import CRSReport, LegalAnalysis


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


def _client(tmp_path, **overrides):
    data = {
        "api_key": "congress-key",
        "govinfo_api_key": "govinfo-key",
        "cache_dir": str(tmp_path / "cache"),
        "cache_ttl_seconds": 3600,
    }
    data.update(overrides)
    return congress.CongressClient(**data)


def _source_payloads():
    return {
        "summary": {
            "title": "Veterans Benefits Act",
            "billscitation": "H.R. 1234",
            "congress": 118,
            "plawcitation": "Public Law 118-123",
            "dateIssued": "2024-01-02",
        },
        "bill": {
            "bill": {
                "title": "Veterans Benefits Act",
                "legislationUrl": (
                    "https://www.congress.gov/bill/118th-congress/"
                    "house-bill/1234"
                ),
            }
        },
        "actions": {
            "actions": [
                {
                    "actionDate": "2023-01-03",
                    "text": "Introduced in House",
                    "type": "IntroReferral",
                },
                {
                    "actionDate": "2024-01-02",
                    "text": "Became Public Law No: 118-123.",
                    "type": "BecameLaw",
                },
            ]
        },
    }


def _fake_sources(monkeypatch, *, results=None, summary=None, actions=None):
    payloads = _source_payloads()
    if summary is not None:
        payloads["summary"] = summary
    if actions is not None:
        payloads["actions"] = actions
    calls = []

    def fake_post(
        url, params=None, json=None, headers=None, timeout=None, **kwargs
    ):
        calls.append(("POST", url, params, json, headers))
        return FakeResponse(
            {"results": results if results is not None else [{
                "packageId": "PLAW-118publ-123",
                "title": "Veterans Benefits Act",
                "dateIssued": "2024-01-02",
            }]}
        )

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        calls.append(("GET", url, params, None, headers))
        if url.endswith("/summary"):
            return FakeResponse(payloads["summary"])
        if url.endswith("/actions"):
            return FakeResponse(payloads["actions"])
        if "/bill/118/hr/1234" in url:
            return FakeResponse(payloads["bill"])
        if "/crsreport/" in url:
            number = url.rsplit("/", 1)[-1]
            return FakeResponse(
                {
                    "CRSReport": {
                        "title": f"Report {number}",
                        "url": f"www.congress.gov/crs-report/{number}",
                        "publishDate": "2024-02-03",
                    }
                }
            )
        return FakeResponse(status_code=404)

    monkeypatch.setattr(congress.requests, "post", fake_post)
    monkeypatch.setattr(congress.requests, "get", fake_get)
    return calls


def test_link_section_without_congress_key_is_not_assessed(tmp_path):
    client = _client(tmp_path, api_key="", govinfo_api_key="govinfo-key")

    link = client.link_section("1110")

    assert link.status == "not_assessed"
    assert "CONGRESS_API_KEY" in link.detail
    assert client.requests_made == 0


def test_link_section_without_govinfo_key_is_not_assessed(tmp_path):
    client = _client(tmp_path, api_key="congress-key", govinfo_api_key="")

    link = client.link_section("1110")

    assert link.status == "not_assessed"
    assert "GOVINFO_API_KEY" in link.detail
    assert client.requests_made == 0


def test_link_section_links_public_law_bill_and_actions(tmp_path, monkeypatch):
    calls = _fake_sources(monkeypatch)
    client = _client(tmp_path)

    link = client.link_section("1110(a)")

    assert link.status == "linked"
    assert link.public_laws[0].citation == "Public Law 118-123"
    assert link.public_laws[0].title == "Veterans Benefits Act"
    assert link.public_laws[0].bill_citation == "H.R. 1234"
    assert link.public_laws[0].bill_title == "Veterans Benefits Act"
    assert link.public_laws[0].bill_url.endswith("/house-bill/1234")
    assert link.public_laws[0].status == "linked"
    assert [action.type for action in link.public_laws[0].actions] == [
        "IntroReferral",
        "BecameLaw",
    ]
    assert link.public_laws[0].actions_truncated is False
    assert client.requests_made == 4
    assert calls[0][0] == "POST"
    assert calls[0][2] is None
    assert calls[0][4]["X-Api-Key"] == "govinfo-key"
    assert calls[1][2] is None
    assert calls[1][4]["X-Api-Key"] == "govinfo-key"
    assert calls[2][0] == "GET"
    assert calls[2][4]["X-Api-Key"] == "congress-key"
    assert "38 U.S.C. 1110" in calls[0][3]["query"]


def test_cache_reuses_public_law_bill_and_actions(tmp_path, monkeypatch):
    _fake_sources(monkeypatch)
    first = _client(tmp_path)
    assert first.link_section("1110").status == "linked"

    def fail(*args, **kwargs):
        raise AssertionError("cache miss made a live request")

    monkeypatch.setattr(congress.requests, "post", fail)
    monkeypatch.setattr(congress.requests, "get", fail)
    second = _client(tmp_path)

    link = second.link_section("1110")

    assert link.status == "linked"
    assert second.requests_made == 0
    cache_text = "\n".join(
        path.read_text() for path in (tmp_path / "cache").rglob("*.json")
    )
    assert "congress-key" not in cache_text
    assert "govinfo-key" not in cache_text


def test_link_section_without_public_law_is_not_reported_as_clean(tmp_path, monkeypatch):
    _fake_sources(monkeypatch, results=[])
    link = _client(tmp_path).link_section("1110")

    assert link.status == "no_public_law"
    assert "not proof" in link.detail
    assert link.public_laws == []


def test_link_section_source_failure_is_unavailable(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(congress.requests, "post", fail)
    link = _client(tmp_path).link_section("1110")

    assert link.status == "unavailable"
    assert "offline" in link.detail


def test_public_law_without_bill_citation_is_public_law_only(tmp_path, monkeypatch):
    _fake_sources(
        monkeypatch,
        summary={
            "title": "Veterans Benefits Act",
            "plawcitation": "Public Law 118-123",
            "congress": 118,
        },
    )

    law = _client(tmp_path).link_section("1110").public_laws[0]

    assert law.status == "public_law_only"
    assert "bill citation" in law.detail
    assert law.actions == []


def test_bill_actions_are_truncated_and_bad_bill_type_is_public_law_only(
    tmp_path, monkeypatch
):
    actions = [
        {
            "actionDate": "2024-01-01",
            "text": f"Action {index}",
            "type": "Floor",
        }
        for index in range(13)
    ]
    _fake_sources(monkeypatch, actions={"actions": actions})
    law = _client(tmp_path).link_section("1110").public_laws[0]

    assert len(law.actions) == 12
    assert law.actions_truncated is True


def test_bill_metadata_failure_keeps_public_law(tmp_path, monkeypatch):
    _fake_sources(monkeypatch)

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        if url.endswith("/summary"):
            return FakeResponse(_source_payloads()["summary"])
        return FakeResponse(status_code=503)

    monkeypatch.setattr(congress.requests, "get", fake_get)
    law = _client(tmp_path).link_section("1110").public_laws[0]

    assert law.status == "public_law_only"
    assert "bill metadata unavailable" in law.detail


def test_actions_failure_keeps_linked_bill(tmp_path, monkeypatch):
    _fake_sources(monkeypatch)

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        if url.endswith("/summary"):
            return FakeResponse(_source_payloads()["summary"])
        if url.endswith("/actions"):
            return FakeResponse(status_code=503)
        return FakeResponse(_source_payloads()["bill"])

    monkeypatch.setattr(congress.requests, "get", fake_get)
    law = _client(tmp_path).link_section("1110").public_laws[0]

    assert law.status == "linked"
    assert "actions were unavailable" in law.detail
    assert law.actions == []


def test_crs_report_is_secondary_and_url_is_normalized(tmp_path, monkeypatch):
    _fake_sources(monkeypatch)
    client = _client(tmp_path)

    report = client.crs_report("r12345")

    assert report.report_number == "R12345"
    assert report.title == "Report R12345"
    assert report.url == "https://www.congress.gov/crs-report/R12345"
    assert report.authority_class == "secondary"
    assert "not primary legal authority" in report.detail


def test_crs_reports_dedupe_cap_and_errors(tmp_path, monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        if url.endswith("R67890"):
            return FakeResponse(status_code=503)
        return FakeResponse({"CRSReport": {"title": "Found"}})

    monkeypatch.setattr(congress.requests, "get", fake_get)
    client = _client(tmp_path)

    reports = client.crs_reports_for_text(
        "See CRS R12345, R12345, R67890, and R99999."
    )

    assert [report.report_number for report in reports] == ["R12345", "R99999"]
    assert any("R67890" in error for error in client.errors)


def test_crs_reports_with_no_matches_is_empty(tmp_path):
    assert _client(tmp_path).crs_reports_for_text("no report here") == []


def test_note_error_ignores_empty_messages(tmp_path):
    client = _client(tmp_path)
    client._note_error("")
    assert client.errors == []


def test_crs_report_without_key_raises(tmp_path):
    client = _client(tmp_path, api_key="")

    with pytest.raises(congress.SearchError):
        client.crs_report("R12345")
    assert client.crs_reports_for_text("R12345") == []


def test_request_cap_returns_unavailable(tmp_path, monkeypatch):
    _fake_sources(monkeypatch)
    client = _client(tmp_path, max_requests=0)

    link = client.link_section("1110")

    assert link.status == "unavailable"
    assert "request cap" in link.detail


def test_congress_flags_only_surface_missing_coverage():
    links = [
        congress.CongressLink(
            usc_section="38 U.S.C. § 1110",
            status="unavailable",
            detail="offline",
        ),
        congress.CongressLink(
            usc_section="38 U.S.C. § 1111",
            status="no_public_law",
            detail="none",
        ),
    ]

    flags = congress.congress_flags(links, ["CRS down"])

    assert len(flags) == 2
    assert "unavailable" in flags[0]
    assert "CRS down" in flags[1]

    assert congress.congress_flags(links + links, ["CRS down", "CRS down"]) == flags


def test_verify_analysis_congress_extracts_sections_and_reports(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AUTHORITY_VERIFICATION", "1")
    monkeypatch.setenv("CONGRESS_API_KEY", "congress-key")
    monkeypatch.setenv("GOVINFO_API_KEY", "govinfo-key")
    monkeypatch.setenv("CONGRESS_CACHE_DIR", str(tmp_path / "cache"))

    class FakeClient:
        errors = ["CRS unavailable"]

        def link_section(self, section):
            return congress.CongressLink(
                usc_section=f"38 U.S.C. § {section}", status="linked"
            )

        def crs_reports_for_text(self, text):
            assert "R12345" in text
            return [
                CRSReport(
                    report_number="R12345",
                    title="Report",
                )
            ]

    monkeypatch.setattr(congress, "CongressClient", FakeClient)
    analysis = LegalAnalysis(
        issue="service connection",
        summary="Cite 38 U.S.C. § 1110 and CRS R12345.",
        how_it_affects_va_claims="",
    )

    bundle = congress.verify_analysis_congress(analysis, [])

    assert [link.usc_section for link in bundle.links] == ["38 U.S.C. § 1110"]
    assert bundle.crs_reports[0].authority_class == "secondary"
    assert bundle.flags == ["Congressional history source error: CRS unavailable"]


def test_verify_analysis_congress_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("AUTHORITY_VERIFICATION", "0")
    analysis = LegalAnalysis(
        issue="service connection",
        summary="38 U.S.C. § 1110",
        how_it_affects_va_claims="",
    )

    bundle = congress.verify_analysis_congress(analysis, [])

    assert bundle.links == []
    assert bundle.flags == []


def test_official_rate_limit_constant():
    assert congress.CONGRESS_RATE_LIMIT_PER_HOUR == 5_000
    assert "rate-limit" in congress.CONGRESS_RATE_LIMIT_URL
    assert Settings().congress_max_requests_per_run < congress.CONGRESS_RATE_LIMIT_PER_HOUR


def test_settings_read_congress_values(monkeypatch, tmp_path):
    monkeypatch.setenv("CONGRESS_API_KEY", "key")
    monkeypatch.setenv("CONGRESS_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CONGRESS_CACHE_TTL_SECONDS", "120")
    monkeypatch.setenv("CONGRESS_MAX_REQUESTS_PER_RUN", "12")

    settings = Settings.from_env()

    assert settings.congress_api_key == "key"
    assert settings.congress_cache_dir == str(tmp_path)
    assert settings.congress_cache_ttl_seconds == 120
    assert settings.congress_max_requests_per_run == 12


def test_congress_text_and_csv_rendering():
    analysis = LegalAnalysis(
        issue="service connection",
        summary="",
        how_it_affects_va_claims="",
        congress_links=[
            congress.CongressLink(
                usc_section="38 U.S.C. § 1110",
                status="linked",
                public_laws=[
                    congress.CongressPublicLaw(
                        citation="Pub. L. 118-123",
                        title="Veterans Benefits Act",
                        bill_citation="H.R. 1234",
                        bill_url="https://www.congress.gov/bill/118/hr/1234",
                        url="https://www.govinfo.gov/app/details/PLAW-118publ-123",
                        actions=[
                            congress.CongressAction(
                                date="2024-01-02",
                                text="Became Public Law No: 118-123.",
                            )
                        ],
                    )
                ],
            )
        ],
        congress_crs_reports=[
            CRSReport(report_number="R12345", title="Report")
        ],
        congress_flags=["Congressional history unavailable"],
    )

    text = render_analysis(analysis, "text")
    csv = render_analysis(analysis, "csv")

    assert "Congressional links:" in text
    assert "[linked] 38 U.S.C. § 1110" in text
    assert "Pub. L. 118-123" in text
    assert "[secondary] CRS R12345: Report" in text
    assert "Congressional flags:" in text
    assert "congress_links" in csv
    assert "Pub. L. 118-123" in csv


def test_congress_text_renders_empty_fields_and_truncation():
    analysis = LegalAnalysis(
        issue="service connection",
        summary="",
        how_it_affects_va_claims="",
        congress_links=[
            congress.CongressLink(
                usc_section="38 U.S.C. § 1111",
                status="linked",
                public_laws=[
                    congress.CongressPublicLaw(
                        citation="Pub. L. 118-124",
                        actions=[
                            congress.CongressAction(
                                date="", text="Action without a date"
                            )
                        ],
                        actions_truncated=True,
                    )
                ],
            )
        ],
        congress_crs_reports=[CRSReport(report_number="R54321")],
    )

    text = render_analysis(analysis, "text")

    assert "date unknown: Action without a date" in text
    assert "Actions truncated" in text
    assert "[secondary] CRS R54321: (untitled)" in text


def test_public_law_package_without_recognizable_citation_is_skipped(
    tmp_path, monkeypatch
):
    assert congress._public_law_citation("NOT-A-PLAW", {}) == ""
    assert congress._congress_number("NOT-A-PLAW", {}) is None
    assert congress._congress_number(
        "PLAW-118publ-123", {"congress": "not-an-integer"}
    ) == 118
    _fake_sources(monkeypatch, results=[{"packageId": "NOT-A-PLAW"}])

    link = _client(tmp_path).link_section("1110")

    assert link.status == "no_public_law"


def test_cache_init_and_read_failures_are_best_effort(tmp_path, monkeypatch):
    class BrokenCache:
        def __init__(self, *args, **kwargs):
            raise OSError("cache unavailable")

    working = _client(tmp_path)
    monkeypatch.setattr(congress, "CourtListenerCache", BrokenCache)
    client = _client(tmp_path)
    assert client._cache is None
    assert client._cache_get("key", {}) is None

    _fake_sources(monkeypatch)

    def broken_get(*args, **kwargs):
        raise OSError("read failed")

    working._cache.get = broken_get
    assert working.link_section("1110").status == "linked"


def test_no_cache_directory_is_supported(tmp_path, monkeypatch):
    _fake_sources(monkeypatch)
    client = _client(tmp_path, cache_dir="")

    client._cache_set("key", {}, {})

    assert client.link_section("1110").status == "linked"


def test_source_errors_redact_api_key_query_values(tmp_path, monkeypatch):
    class LeakyResponse:
        def raise_for_status(self):
            raise requests.HTTPError(
                "500 for url: https://api.govinfo.gov/search?api_key=super-secret"
            )

        def json(self):
            return {}

    monkeypatch.setattr(congress.requests, "post", lambda *a, **k: LeakyResponse())

    link = _client(tmp_path).link_section("1110")

    assert link.status == "unavailable"
    assert "super-secret" not in link.detail
    assert "api_key=[REDACTED]" in link.detail


def test_401_chained_exception_does_not_leak_govinfo_key(tmp_path, monkeypatch):
    secret = "SUPERSECRET123"

    class UnauthorizedResponse:
        def raise_for_status(self):
            raise requests.HTTPError(
                "401 Client Error: Unauthorized for url: "
                f"https://api.govinfo.gov/search?api_key={secret}",
                response=self,
            )

        def json(self):
            return {}

    monkeypatch.setattr(
        congress.requests, "post", lambda *args, **kwargs: UnauthorizedResponse()
    )

    with pytest.raises(congress.SearchError) as exc_info:
        _client(tmp_path)._request_json(
            "POST",
            congress.GOVINFO_SEARCH_URL,
            json_body={},
            cache_key="aud4-001-repro",
            source_name="GovInfo",
        )

    assert_exception_chain_redacted(exc_info.value, secret)


def test_non_object_source_response_is_unavailable(tmp_path, monkeypatch):
    def fake_post(*args, **kwargs):
        return FakeResponse(["not", "an", "object"])

    monkeypatch.setattr(congress.requests, "post", fake_post)

    link = _client(tmp_path).link_section("1110")

    assert link.status == "unavailable"
    assert "non-object" in link.detail


def test_non_dict_search_result_is_skipped(tmp_path, monkeypatch):
    _fake_sources(
        monkeypatch,
        results=[
            "not-a-row",
            {"packageId": "PLAW-118publ-123"},
        ],
    )

    assert _client(tmp_path).link_section("1110").status == "linked"


def test_public_law_limit_is_enforced(tmp_path, monkeypatch):
    def fake_post(*args, **kwargs):
        return FakeResponse(
            {
                "results": [
                    {"packageId": f"PLAW-118publ-{number}"}
                    for number in range(1, 5)
                ]
            }
        )

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        if url.endswith("/summary"):
            number = url.split("publ-", 1)[1].split("/", 1)[0]
            return FakeResponse(
                {
                    "plawcitation": f"Public Law 118-{number}",
                    "billscitation": "H.R. 1234",
                    "congress": 118,
                }
            )
        if url.endswith("/actions"):
            return FakeResponse({"actions": []})
        return FakeResponse(_source_payloads()["bill"])

    monkeypatch.setattr(congress.requests, "post", fake_post)
    monkeypatch.setattr(congress.requests, "get", fake_get)

    link = _client(tmp_path).link_section("1110")

    assert len(link.public_laws) == 3


def test_verify_analysis_congress_without_sections_is_empty(monkeypatch):
    monkeypatch.setenv("AUTHORITY_VERIFICATION", "1")
    monkeypatch.setenv("CONGRESS_API_KEY", "congress-key")
    analysis = LegalAnalysis(
        issue="service connection",
        summary="No statutory citation here.",
        how_it_affects_va_claims="",
    )

    assert congress.verify_analysis_congress(analysis, []).links == []


def test_bill_type_rejects_non_bill_resolution():
    assert congress._bill_type("H. Res. 5") is None
    assert congress._bill_type("S.J. Res. 5") == ("sjres", "5")


def test_cache_set_failure_is_best_effort(tmp_path, monkeypatch):
    _fake_sources(monkeypatch)
    client = _client(tmp_path)
    client._cache.set = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full"))

    link = client.link_section("1110")

    assert link.status == "linked"


def test_public_law_with_unsupported_bill_citation_is_not_enriched(
    tmp_path, monkeypatch
):
    _fake_sources(
        monkeypatch,
        summary={
            "plawcitation": "Public Law 118-123",
            "billscitation": "H. Res. 5",
            "congress": 118,
        },
    )

    law = _client(tmp_path).link_section("1110").public_laws[0]

    assert law.status == "public_law_only"
    assert "not a supported" in law.detail


def test_public_law_summary_failure_still_returns_law(tmp_path, monkeypatch):
    _fake_sources(monkeypatch, summary={"plawcitation": "Public Law 118-123"})
    original_get = congress.requests.get

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        if url.endswith("/summary"):
            return FakeResponse(status_code=503)
        return original_get(url, params=params, headers=headers, timeout=timeout, **kwargs)

    monkeypatch.setattr(congress.requests, "get", fake_get)

    link = _client(tmp_path).link_section("1110")

    assert link.status == "linked"
    assert link.public_laws[0].status == "public_law_only"
    assert "summary unavailable" in link.public_laws[0].detail
