"""Tests for optional Regulations.gov rulemaking context."""

from __future__ import annotations

import requests

import va_legal_agent.regulations as regulations
from va_legal_agent.__main__ import render_analysis
from va_legal_agent.config import Settings
from va_legal_agent.models import (
    CaseRecord,
    LegalAnalysis,
    RegulationsDocket,
    RegulationsDocument,
    RegulationsMapping,
    RegulationsPublicComment,
    RegulationsSupportingDocument,
)


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
        "api_key": "regulations-key",
        "cache_dir": str(tmp_path / "cache"),
        "cache_ttl_seconds": 3600,
    }
    data.update(overrides)
    return regulations.RegulationsClient(**data)


def _payloads():
    return {
        "fr": {
            "results": [
                {
                    "document_number": "2024-12345",
                    "title": "Veterans Rule",
                    "publication_date": "2024-06-01",
                    "type": "Rule",
                    "html_url": "https://www.federalregister.gov/d/2024-12345",
                    "abstract": "Rule abstract",
                    "action": "Final rule",
                    "docket_ids": ["VA-2024-VACO-0001"],
                    "regulations_dot_gov_info": {
                        "docket_id": "VA-2024-VACO-0001"
                    },
                }
            ]
        },
        "documents": {
            "data": [
                {
                    "id": "VA-2024-VACO-0001-0001",
                    "attributes": {
                        "documentId": "VA-2024-VACO-0001-0001",
                        "docketId": "VA-2024-VACO-0001",
                        "objectId": "obj-123",
                        "frDocNum": "2024-12345",
                        "title": "Veterans Rule",
                        "documentType": "Rule",
                        "postedDate": "2024-06-01T00:00:00Z",
                        "commentStartDate": "2024-05-01T00:00:00Z",
                        "commentEndDate": "2024-07-01T00:00:00Z",
                        "openForComment": False,
                    },
                }
            ]
        },
        "docket": {
            "data": {
                "id": "VA-2024-VACO-0001",
                "attributes": {
                    "title": "Veterans rulemaking",
                    "agencyId": "VA",
                    "docketType": "Rulemaking",
                    "rin": "2900-ABCD",
                },
            }
        },
        "supporting": {
            "data": [
                {
                    "id": "VA-2024-VACO-0001-0002",
                    "attributes": {
                        "documentId": "VA-2024-VACO-0001-0002",
                        "title": "Supporting analysis",
                        "documentType": "Supporting & Related Material",
                        "postedDate": "2024-06-02T00:00:00Z",
                    },
                }
            ]
        },
        "comments": {
            "meta": {"totalElements": 42},
            "data": [
                {
                    "id": "VA-2024-VACO-0001-0003",
                    "attributes": {
                        "trackingNbr": "track-1",
                        "title": "Comment one",
                        "postedDate": "2024-06-10T00:00:00Z",
                        "comment": "Please clarify the effective date.",
                    },
                },
                {
                    "id": "VA-2024-VACO-0001-0004",
                    "attributes": {
                        "trackingNbr": "track-2",
                        "title": "Comment two",
                        "postedDate": "2024-06-11T00:00:00Z",
                        "comment": "This rule should cover more veterans.",
                    },
                },
            ],
        },
    }


def _fake_sources(monkeypatch, payloads=None):
    data = _payloads()
    if payloads:
        data.update(payloads)
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        calls.append((url, params or {}, headers or {}))
        if "federalregister.gov" in url:
            return FakeResponse(data["fr"])
        if url.endswith("/comments"):
            return FakeResponse(data["comments"])
        if "/dockets/" in url:
            return FakeResponse(data["docket"])
        if params and "filter[docketId]" in params:
            return FakeResponse(data["supporting"])
        if params and "filter[frDocNum]" in params:
            return FakeResponse(data["documents"])
        return FakeResponse(status_code=404)

    monkeypatch.setattr(regulations.requests, "get", fake_get)
    return calls


def test_map_part_without_key_is_not_assessed(tmp_path):
    client = _client(tmp_path, api_key="")

    mapping = client.map_part("3")

    assert mapping.status == "not_assessed"
    assert "REGULATIONS_API_KEY" in mapping.detail
    assert client.requests_made == 0


def test_map_part_links_fr_document_to_docket_comments_and_supporting_docs(
    tmp_path, monkeypatch
):
    calls = _fake_sources(monkeypatch)
    client = _client(tmp_path, comment_sample_limit=2)

    mapping = client.map_part("3")

    assert mapping.status == "mapped"
    assert mapping.fr_documents[0].document_number == "2024-12345"
    assert mapping.dockets[0].docket_id == "VA-2024-VACO-0001"
    assert mapping.dockets[0].title == "Veterans rulemaking"
    assert mapping.dockets[0].agency_id == "VA"
    assert mapping.dockets[0].rin == "2900-ABCD"
    document = mapping.dockets[0].documents[0]
    assert document.document_id == "VA-2024-VACO-0001-0001"
    assert document.docket_id == "VA-2024-VACO-0001"
    assert document.comment_count == 42
    assert document.comments_sampled == 2
    assert [comment.authority_class for comment in document.comments] == [
        "public_input",
        "public_input",
    ]
    assert document.comments[0].excerpt == "Please clarify the effective date."
    assert mapping.dockets[0].supporting_documents[0].document_id == (
        "VA-2024-VACO-0001-0002"
    )
    assert client.requests_made == 5
    assert calls[0][1]["conditions[cfr][part]"] == "3"
    assert calls[0][1]["conditions[type][]"] == ["RULE", "PRORULE"]
    document_calls = [call for call in calls if "filter[frDocNum]" in call[1]]
    assert document_calls[0][2]["X-Api-Key"] == "regulations-key"
    comment_calls = [call for call in calls if call[0].endswith("/comments")]
    assert comment_calls[0][1]["page[size]"] == 5


def test_cache_reuses_all_regulations_responses(tmp_path, monkeypatch):
    _fake_sources(monkeypatch)
    assert _client(tmp_path).map_part("3").status == "mapped"

    def fail(*args, **kwargs):
        raise AssertionError("cache miss made a live request")

    monkeypatch.setattr(regulations.requests, "get", fail)
    second = _client(tmp_path)

    mapping = second.map_part("3")

    assert mapping.status == "mapped"
    assert second.requests_made == 0
    cache_text = "\n".join(
        path.read_text() for path in (tmp_path / "cache").rglob("*.json")
    )
    assert "regulations-key" not in cache_text


def test_no_federal_register_documents_is_no_docket(tmp_path, monkeypatch):
    _fake_sources(monkeypatch, {"fr": {"results": []}})

    mapping = _client(tmp_path).map_part("3")

    assert mapping.status == "no_docket"
    assert "No Federal Register" in mapping.detail
    assert mapping.fr_documents == []


def test_unmapped_fr_document_preserves_fr_context(tmp_path, monkeypatch):
    _fake_sources(monkeypatch, {"documents": {"data": []}})

    mapping = _client(tmp_path).map_part("3")

    assert mapping.status == "no_docket"
    assert mapping.fr_documents[0].document_number == "2024-12345"
    assert mapping.dockets == []


def test_comments_failure_keeps_docket_mapping(tmp_path, monkeypatch):
    calls = _fake_sources(monkeypatch)

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        calls.append((url, params or {}, headers or {}))
        if "federalregister.gov" in url:
            return FakeResponse(_payloads()["fr"])
        if url.endswith("/comments"):
            return FakeResponse(status_code=503)
        if "/dockets/" in url:
            return FakeResponse(_payloads()["docket"])
        if params and "filter[docketId]" in params:
            return FakeResponse(_payloads()["supporting"])
        return FakeResponse(_payloads()["documents"])

    monkeypatch.setattr(regulations.requests, "get", fake_get)
    client = _client(tmp_path)

    mapping = client.map_part("3")

    assert mapping.status == "mapped"
    document = mapping.dockets[0].documents[0]
    assert document.status == "comments_unavailable"
    assert document.comment_count == 0
    assert "comments unavailable" in document.detail
    assert client.errors


def test_supporting_document_failure_marks_docket_partial(tmp_path, monkeypatch):
    calls = _fake_sources(monkeypatch)

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        calls.append((url, params or {}, headers or {}))
        if "federalregister.gov" in url:
            return FakeResponse(_payloads()["fr"])
        if url.endswith("/comments"):
            return FakeResponse(_payloads()["comments"])
        if "/dockets/" in url:
            return FakeResponse(_payloads()["docket"])
        if params and "filter[docketId]" in params:
            return FakeResponse(status_code=503)
        return FakeResponse(_payloads()["documents"])

    monkeypatch.setattr(regulations.requests, "get", fake_get)
    client = _client(tmp_path)

    mapping = client.map_part("3")

    assert mapping.status == "mapped"
    assert mapping.dockets[0].status == "partial"
    assert "Supporting documents unavailable" in mapping.dockets[0].detail
    assert client.errors


def test_docket_detail_failure_still_maps_document(tmp_path, monkeypatch):
    calls = _fake_sources(monkeypatch)

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        calls.append((url, params or {}, headers or {}))
        if "federalregister.gov" in url:
            return FakeResponse(_payloads()["fr"])
        if url.endswith("/comments"):
            return FakeResponse(_payloads()["comments"])
        if "/dockets/" in url:
            return FakeResponse(status_code=503)
        return FakeResponse(_payloads()["documents"])

    monkeypatch.setattr(regulations.requests, "get", fake_get)
    client = _client(tmp_path)

    mapping = client.map_part("3")

    assert mapping.status == "mapped"
    assert mapping.dockets[0].status == "partial"
    assert mapping.dockets[0].documents[0].comment_count == 42
    assert "Docket metadata unavailable" in mapping.dockets[0].detail


def test_request_cap_returns_unavailable(tmp_path, monkeypatch):
    _fake_sources(monkeypatch)
    client = _client(tmp_path, max_requests=0)

    mapping = client.map_part("3")

    assert mapping.status == "unavailable"
    assert "request cap" in mapping.detail


def test_missing_object_id_marks_comments_unavailable(tmp_path, monkeypatch):
    documents = _payloads()["documents"]
    documents["data"][0]["attributes"].pop("objectId")
    _fake_sources(monkeypatch, {"documents": documents})

    mapping = _client(tmp_path).map_part("3")

    document = mapping.dockets[0].documents[0]
    assert document.status == "comments_unavailable"
    assert "objectId" in document.detail


def test_comment_count_missing_meta_is_explicit(tmp_path, monkeypatch):
    _fake_sources(
        monkeypatch,
        {"comments": {"data": _payloads()["comments"]["data"]}},
    )

    mapping = _client(tmp_path).map_part("3")

    document = mapping.dockets[0].documents[0]
    assert document.comment_count == 0
    assert "not a total" in document.detail


def test_public_comment_excerpt_is_truncated_and_date_fallback_is_rendered(
    tmp_path, monkeypatch
):
    comments = _payloads()["comments"]
    comments["data"][0]["attributes"]["comment"] = "x" * 600
    comments["data"][0]["attributes"]["postedDate"] = ""
    _fake_sources(monkeypatch, {"comments": comments})

    mapping = _client(tmp_path).map_part("3")
    comment = mapping.dockets[0].documents[0].comments[0]

    assert len(comment.excerpt) == 500
    assert comment.excerpt.endswith("...")

    text = render_analysis(
        LegalAnalysis(
            issue="service connection",
            summary="",
            how_it_affects_va_claims="",
            regulations_mappings=[mapping],
        ),
        "text",
    )
    assert "[public_input] date unknown:" in text


def test_verify_analysis_regulations_extracts_parts(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTHORITY_VERIFICATION", "1")
    monkeypatch.setenv("REGULATIONS_API_KEY", "regulations-key")
    monkeypatch.setenv("REGULATIONS_CACHE_DIR", str(tmp_path / "cache"))

    class FakeClient:
        errors = ["source warning"]

        def map_part(self, part):
            return RegulationsMapping(cfr_part=part, status="mapped")

    monkeypatch.setattr(regulations, "RegulationsClient", FakeClient)
    analysis = LegalAnalysis(
        issue="service connection",
        summary="See 38 C.F.R. § 3.303 and 38 C.F.R. § 4.1.",
        how_it_affects_va_claims="",
    )

    bundle = regulations.verify_analysis_regulations(analysis, [])

    assert [mapping.cfr_part for mapping in bundle.mappings] == ["3", "4"]
    assert bundle.flags == [
        "Regulations.gov source error: source warning"
    ]


def test_verify_analysis_regulations_disabled_is_empty(monkeypatch):
    monkeypatch.setenv("AUTHORITY_VERIFICATION", "0")
    analysis = LegalAnalysis(
        issue="service connection",
        summary="38 C.F.R. § 3.303",
        how_it_affects_va_claims="",
    )

    bundle = regulations.verify_analysis_regulations(analysis, [])

    assert bundle.mappings == []
    assert bundle.flags == []


def test_verify_analysis_regulations_without_parts_is_empty(monkeypatch):
    monkeypatch.setenv("AUTHORITY_VERIFICATION", "1")
    monkeypatch.setenv("REGULATIONS_API_KEY", "regulations-key")
    analysis = LegalAnalysis(
        issue="service connection",
        summary="No regulation citation.",
        how_it_affects_va_claims="",
    )

    assert regulations.verify_analysis_regulations(analysis, []).mappings == []


def test_settings_read_regulations_values(monkeypatch, tmp_path):
    monkeypatch.setenv("REGULATIONS_API_KEY", "key")
    monkeypatch.setenv("REGULATIONS_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("REGULATIONS_CACHE_TTL_SECONDS", "120")
    monkeypatch.setenv("REGULATIONS_MAX_REQUESTS_PER_RUN", "12")
    monkeypatch.setenv("REGULATIONS_COMMENT_SAMPLE_LIMIT", "4")

    settings = Settings.from_env()

    assert settings.regulations_api_key == "key"
    assert settings.regulations_cache_dir == str(tmp_path)
    assert settings.regulations_cache_ttl_seconds == 120
    assert settings.regulations_max_requests_per_run == 12
    assert settings.regulations_comment_sample_limit == 4


def test_official_regulations_rate_limit_constant():
    assert regulations.REGULATIONS_RATE_LIMIT_PER_HOUR == 1_000
    assert "faq" in regulations.REGULATIONS_RATE_LIMIT_URL
    assert (
        Settings().regulations_max_requests_per_run
        < regulations.REGULATIONS_RATE_LIMIT_PER_HOUR
    )


def test_regulations_text_and_csv_rendering():
    mapping = RegulationsMapping(
        cfr_part="3",
        status="mapped",
        fr_documents=[],
        dockets=[
            RegulationsDocket(
                docket_id="VA-2024-VACO-0001",
                title="",
                url="",
                documents=[
                    RegulationsDocument(
                        document_id="VA-2024-VACO-0001-0001",
                        title="",
                        comment_count=2,
                        comments_sampled=1,
                        comments=[
                            RegulationsPublicComment(
                                posted_date="",
                                excerpt="Public comment",
                            )
                        ],
                    )
                ],
                supporting_documents=[
                    RegulationsSupportingDocument(
                        document_id="VA-2024-VACO-0001-0002",
                        title="",
                    )
                ],
            )
        ],
    )
    analysis = LegalAnalysis(
        issue="service connection",
        summary="",
        how_it_affects_va_claims="",
        regulations_mappings=[mapping],
        regulations_flags=["Regulations.gov unavailable"],
    )

    text = render_analysis(analysis, "text")
    csv = render_analysis(analysis, "csv")

    assert "Regulations.gov rulemaking context:" in text
    assert "Public comments: 2 (sampled 1)" in text
    assert "[public_input] date unknown: Public comment" in text
    assert "Supporting documents:" in text
    assert "Regulations.gov flags:" in text
    assert "regulations_mappings" in csv
    assert "VA-2024-VACO-0001" in csv


def test_cache_init_failure_and_no_cache_are_supported(tmp_path, monkeypatch):
    class BrokenCache:
        def __init__(self, *args, **kwargs):
            raise OSError("cache unavailable")

    working = _client(tmp_path)
    monkeypatch.setattr(regulations, "CourtListenerCache", BrokenCache)
    broken = _client(tmp_path)
    assert broken._cache is None
    assert broken._cache_get("key", {}) is None

    _fake_sources(monkeypatch)
    no_cache = _client(tmp_path, cache_dir="")
    no_cache._cache_set("key", {}, {})
    assert no_cache.map_part("3").status == "mapped"
    assert working is not None


def test_docket_id_helpers_cover_fr_fallbacks():
    assert regulations._docket_ids(
        {
            "docket_ids": ["VA-1", "VA-1"],
            "docket_id": "VA-2",
            "regulations_dot_gov_info": {"docketId": "VA-3"},
        }
    ) == ["VA-1", "VA-3", "VA-2"]


def test_cache_read_oserror_is_a_miss(tmp_path, monkeypatch):
    client = _client(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("read failed")

    client._cache.get = fail
    assert client._cache_get("key", {}) is None


def test_source_error_is_unavailable(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(regulations.requests, "get", fail)

    mapping = _client(tmp_path).map_part("3")

    assert mapping.status == "unavailable"
    assert "offline" in mapping.detail


def test_non_object_json_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        regulations.requests, "get", lambda *args, **kwargs: FakeResponse([])
    )

    mapping = _client(tmp_path).map_part("3")

    assert mapping.status == "unavailable"
    assert "non-object" in mapping.detail


def test_regulations_document_lookup_failure_is_flagged(
    tmp_path, monkeypatch
):
    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        if "federalregister.gov" in url:
            return FakeResponse(_payloads()["fr"])
        return FakeResponse(status_code=503)

    monkeypatch.setattr(regulations.requests, "get", fake_get)
    client = _client(tmp_path)
    mapping = client.map_part("3")

    assert mapping.status == "no_docket"
    assert mapping.fr_documents
    assert client.errors


def test_document_without_docket_id_is_no_docket(tmp_path, monkeypatch):
    documents = _payloads()["documents"]
    documents["data"][0]["attributes"].pop("docketId")
    _fake_sources(monkeypatch, {"documents": documents})

    mapping = _client(tmp_path).map_part("3")

    assert mapping.status == "no_docket"
    assert mapping.dockets == []


def test_multiple_fr_documents_reuse_one_docket(tmp_path, monkeypatch):
    fr = _payloads()["fr"]
    fr["results"].append(
        {
            "document_number": "2024-12346",
            "title": "Second Veterans Rule",
            "publication_date": "2024-07-01",
            "docket_ids": [],
        }
    )
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        calls.append((url, params or {}))
        if "federalregister.gov" in url:
            return FakeResponse(fr)
        if url.endswith("/comments"):
            return FakeResponse(_payloads()["comments"])
        if "/dockets/" in url:
            return FakeResponse(_payloads()["docket"])
        if params and "filter[docketId]" in params:
            return FakeResponse(_payloads()["supporting"])
        number = params["filter[frDocNum]"]
        return FakeResponse(
            {
                "data": [
                    {
                        "id": f"doc-{number}",
                        "attributes": {
                            "docketId": "VA-2024-VACO-0001",
                            "objectId": f"obj-{number}",
                            "frDocNum": number,
                            "title": number,
                        },
                    }
                ]
            }
        )

    monkeypatch.setattr(regulations.requests, "get", fake_get)
    client = _client(tmp_path)

    mapping = client.map_part("3")

    assert mapping.status == "mapped"
    assert len(mapping.dockets) == 1
    assert len(mapping.dockets[0].documents) == 2
    assert sum(1 for call in calls if "/dockets/" in call[0]) == 1


def test_parsing_skips_invalid_rows_and_respects_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(regulations, "_MAX_FR_DOCUMENTS", 2)
    monkeypatch.setattr(regulations, "_MAX_REGULATIONS_DOCUMENTS", 2)
    monkeypatch.setattr(regulations, "_SUPPORTING_DOCUMENT_LIMIT", 2)
    monkeypatch.setattr(
        regulations.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {
                "results": [
                    "not-a-row",
                    {"title": "missing number"},
                    {"document_number": "A", "html_url": "//fr/a"},
                    {"document_number": "B"},
                    {"document_number": "C"},
                ],
                "data": [
                    "not-a-row",
                    {"id": ""},
                    {"id": "D1", "attributes": {"docketId": "D"}},
                    {"id": "D2", "attributes": {"docketId": "D"}},
                    {"id": "D3"},
                ],
            }
        ),
    )
    client = _client(tmp_path)

    fr_documents = client._federal_register_documents("3")
    assert [doc.document_number for doc in fr_documents] == ["A", "B"]
    assert fr_documents[0].url == "https://fr/a"

    documents = client._regulations_documents("A")
    assert [doc.document_id for doc in documents] == ["D1", "D2"]

    supporting = client._supporting_documents("D")
    assert [doc.document_id for doc in supporting] == ["D1", "D2"]


def test_comment_parsing_skips_invalid_rows_and_respects_sample_limit(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        regulations.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {
                "meta": {"totalElements": 9},
                "data": [
                    "not-a-row",
                    {
                        "id": "c1",
                        "attributes": {
                            "comment": "one",
                            "postedDate": "2024-01-01",
                        },
                    },
                    {
                        "id": "c2",
                        "attributes": {"comment": "two"},
                    },
                    {"id": "c3", "attributes": {"comment": "three"}},
                ],
            }
        ),
    )
    client = _client(tmp_path, comment_sample_limit=2)
    document = RegulationsDocument(object_id="obj")

    client._comments(document)

    assert document.comment_count == 9
    assert document.comments_sampled == 2
    assert [comment.comment_id for comment in document.comments] == ["c1", "c2"]


def test_regulations_flags_include_missing_coverage_and_deduplicate():
    missing = RegulationsMapping(
        cfr_part="3", status="unavailable", detail="offline"
    )
    flags = regulations.regulations_flags(
        [missing, missing, RegulationsMapping(cfr_part="4", status="mapped")],
        ["source warning", "source warning"],
    )

    assert flags == [
        "Regulations.gov context unavailable for 38 C.F.R. Part 3: offline",
        "Regulations.gov source error: source warning",
    ]
    assert regulations.regulations_flags([], None) == []


def test_small_helpers_and_cache_error_paths(tmp_path, monkeypatch):
    assert regulations._as_bool("YES") is True
    assert regulations._as_bool(None) is False
    assert regulations._url("") == ""
    assert regulations._url("//example.com/path") == "https://example.com/path"
    assert regulations._docket_ids({"docket_id": "VA-1"}) == ["VA-1"]

    client = _client(tmp_path)
    client._note_error("")
    client._note_error("duplicate")
    client._note_error("duplicate")
    assert client.errors == ["duplicate"]

    def fail(*args, **kwargs):
        raise OSError("cache write failed")

    client._cache.set = fail
    client._cache_set("key", {}, {})


def test_aud4_regulations_provision_wrapper_preserves_rulemaking_client():
    case = CaseRecord(
        title="Provision Case",
        court="Court of Appeals for Veterans Claims",
        holding=(
            "The Board must apply 38 C.F.R. § 3.303 and 38 C.F.R. § 4.14, and the "
            "examination must satisfy M21-1, Part IV, Subpart i, Chapter 3."
        ),
        url="https://uscourts.cavc.gov/opinions/provision-case",
        source_reliability="official",
    )

    impacts = regulations.provision_level_impacts([case])

    assert {impact.citation for impact in impacts} >= {
        "38 C.F.R. § 3.303",
        "38 C.F.R. § 4.14",
    }
    assert any(impact.m21_1_citations for impact in impacts)
    # The existing docket client remains available and unchanged.
    assert callable(regulations.RegulationsClient)
