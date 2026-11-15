"""Tests for the conservative CourtListener citator."""

import json

import pytest

from va_legal_agent.citator import (
    _as_opinion_id,
    _authority_gate,
    _cluster_citation,
    _cluster_citations,
    _cluster_identifier,
    _cluster_opinion_id,
    _cluster_opinion_ids,
    _is_abbreviation,
    _opinion_role,
    _precedential_tier,
    _safe_int,
    _scope_for,
    _sentence_bounds,
    check_case,
    extract_negative_treatments,
    run_citator,
)
from va_legal_agent.models import AppellateHistoryRecord, CaseRecord
from va_legal_agent.search import SearchError
from va_legal_agent.topics import COURT_BVA, COURT_CAVC, COURT_SUPREME, COURT_UNKNOWN


def _case(**overrides) -> CaseRecord:
    values = {
        "title": "Smith v. Wilkie",
        "court": COURT_CAVC,
        "citation": "30 Vet.App. 123",
        "decision_date": "2018-01-01",
        "courtlistener_opinion_id": "111",
        "url": "https://www.courtlistener.com/opinion/111/",
    }
    values.update(overrides)
    return CaseRecord(**values)


def _meta(
    case: str = "Jones v. McDonough",
    date: str = "2020-02-02",
    court: str = COURT_CAVC,
    opinion_type: str = "020lead",
    precedential_status: str = "Published",
) -> dict[str, str]:
    return {
        "title": case,
        "citation": "31 Vet.App. 456",
        "court": court,
        "decision_date": date,
        "url": "https://www.courtlistener.com/opinion/201/",
        "opinion_type": opinion_type,
        "precedential_status": precedential_status,
    }


def _target_meta(opinion_id: int = 111) -> dict[str, object]:
    return {
        "title": "Smith v. Wilkie",
        "citation": "30 Vet.App. 123",
        "citations": ["30 Vet.App. 123", "2020 WL 123456"],
        "cluster_id": "777",
        "opinion_ids": [opinion_id],
        "court": COURT_CAVC,
        "decision_date": "2018-01-01",
    }


def _page(
    ids: list[int],
    count: int | None = None,
    next_url: str = "",
) -> dict[str, object]:
    return {
        "count": len(ids) if count is None else count,
        "results": [
            {"citing_opinion": f"https://www.courtlistener.com/api/rest/v4/opinions/{oid}/"}
            for oid in ids
        ],
        "next": next_url,
    }


class FakeProvider:
    """Small CourtListener surface used by the citator."""

    def __init__(self):
        self.lookup_results: list[dict[str, object]] = []
        self.lookup_error: SearchError | None = None
        self.pages: dict[str | None, dict[str, object]] = {}
        self.opinion_pages: dict[tuple[int, str | None], dict[str, object]] = {}
        self.page_errors: dict[str | None, SearchError] = {}
        self.opinion_page_errors: dict[int, SearchError] = {}
        self.metadata: dict[int, dict[str, object] | None] = {
            111: _target_meta(111),
            112: _target_meta(112),
        }
        self.metadata_errors: dict[int, SearchError] = {}
        self.texts: dict[int, str] = {}
        self.text_errors: dict[int, SearchError] = {}
        self.same_docket: dict[int, list[dict[str, str]]] = {}
        self.same_docket_errors: dict[int, SearchError] = {}
        self.lookup_calls: list[str] = []
        self.edge_calls: list[tuple[int, int, str | None]] = []
        self.metadata_calls: list[int] = []
        self.text_calls: list[int] = []
        self.same_docket_calls: list[tuple[int, int]] = []

    def lookup_citations(self, text: str):
        self.lookup_calls.append(text)
        if self.lookup_error:
            raise self.lookup_error
        return self.lookup_results

    def citing_edges(self, opinion_id: int, page_size: int = 100, cursor=None):
        self.edge_calls.append((opinion_id, page_size, cursor))
        if opinion_id in self.opinion_page_errors:
            raise self.opinion_page_errors[opinion_id]
        if cursor in self.page_errors:
            raise self.page_errors[cursor]
        key = (opinion_id, cursor)
        if key in self.opinion_pages:
            return self.opinion_pages[key]
        return self.pages.get(cursor, {"count": 0, "results": [], "next": ""})

    def same_docket_decisions(self, opinion_id: int, max_results: int = 5):
        self.same_docket_calls.append((opinion_id, max_results))
        if opinion_id in self.same_docket_errors:
            raise self.same_docket_errors[opinion_id]
        return self.same_docket.get(opinion_id, [])[:max_results]

    def opinion_metadata(self, opinion_id: int):
        self.metadata_calls.append(opinion_id)
        if opinion_id in self.metadata_errors:
            raise self.metadata_errors[opinion_id]
        return self.metadata.get(opinion_id)

    def fetch_opinion_text(self, opinion_id: int):
        self.text_calls.append(opinion_id)
        if opinion_id in self.text_errors:
            raise self.text_errors[opinion_id]
        return self.texts.get(opinion_id, "")


def _wire_good_citing_opinions(provider: FakeProvider, *ids: int) -> None:
    for opinion_id in ids:
        provider.metadata[opinion_id] = _meta()
        provider.texts[opinion_id] = (
            "The court followed Smith v. Wilkie, 30 Vet.App. 123."
        )


def _gap_codes(check) -> list[str]:
    return [gap.code for gap in check.coverage.gaps]


# ── target extraction ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("opinion_type", "expected"),
    [
        ("010combined", "majority"),
        ("015unamimous", "majority"),
        ("020lead", "majority"),
        ("combined-opinion", "majority"),
        ("lead opinion", "majority"),
        ("025plurality", "plurality"),
        ("plurality-opinion", "plurality"),
        ("030concurrence", "separate"),
        ("035concurrenceinpart", "separate"),
        ("040dissent", "separate"),
        ("concurrence in part", "separate"),
        ("dissent", "separate"),
        ("", "unknown"),
        ("070rehearing", "other"),
    ],
)
def test_opinion_role_classifies_courtlistener_codes(opinion_type, expected):
    assert _opinion_role(opinion_type) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Published", "precedential"),
        ("unpublished", "nonprecedential"),
        ("", "unknown"),
        ("Unknown", "unknown"),
        ("Errata", "other"),
    ],
)
def test_precedential_tier_classifies_courtlistener_status(status, expected):
    assert _precedential_tier(status) == expected


def test_authority_gate_requires_majority_and_published_status():
    assert _authority_gate("020lead", "Published") == (
        "majority",
        "precedential",
        True,
        "",
    )
    assert _authority_gate("040dissent", "Published")[2:] == (
        False,
        "opinion role is separate",
    )
    assert _authority_gate("020lead", "Unpublished")[2:] == (
        False,
        "precedential status is nonprecedential",
    )
    assert _authority_gate("040dissent", "Unpublished")[2:] == (
        False,
        "opinion role is separate; precedential status is nonprecedential",
    )


def test_extract_negative_treatment_uses_exact_target_sentence():
    text = (
        "Intro. The court overruled Smith v. Wilkie, 30 Vet.App. 123 in part. "
        "Other discussion."
    )

    findings = extract_negative_treatments(text, "Smith v. Wilkie", "30 Vet.App. 123")

    assert len(findings) == 1
    assert findings[0].treatment == "overruled"
    assert findings[0].scope == "part"
    assert findings[0].confidence == "high"
    assert findings[0].excerpt == (
        "The court overruled Smith v. Wilkie, 30 Vet.App. 123 in part."
    )
    assert findings[0].excerpt in text


def test_extract_negative_treatment_requires_target_in_same_sentence():
    text = "The court overruled another line of cases. Smith v. Wilkie is discussed."

    assert extract_negative_treatments(text, "Smith v. Wilkie", "30 Vet.App. 123") == []


def test_extract_negative_treatment_ignores_negated_verb():
    text = "The court did not overrule Smith v. Wilkie, 30 Vet.App. 123."

    assert extract_negative_treatments(text, "Smith v. Wilkie", "30 Vet.App. 123") == []


def test_extract_limited_is_medium_confidence_and_unclear_scope():
    text = "The court limited Smith v. Wilkie to its facts."

    findings = extract_negative_treatments(text, "Smith v. Wilkie", "")

    assert len(findings) == 1
    assert findings[0].treatment == "limited"
    assert findings[0].scope == "unclear"
    assert findings[0].confidence == "medium"


def test_extract_criticized_uses_case_name_only():
    text = "The panel criticized Smith v. Wilkie for its reasoning."

    findings = extract_negative_treatments(text, "Smith v. Wilkie", "")

    assert [finding.treatment for finding in findings] == ["criticized"]


def test_extract_does_not_split_reporter_abbreviation():
    text = (
        "In Smith v. Wilkie, 30 Vet.App. 123, the court criticized the rule. "
        "The next sentence follows."
    )

    findings = extract_negative_treatments(text, "Smith v. Wilkie", "30 Vet.App. 123")

    assert len(findings) == 1
    assert findings[0].excerpt.startswith("In Smith v. Wilkie")


def test_extract_skips_target_span_crossing_a_sentence_boundary():
    # This artificial citation contains a non-abbreviation period followed by
    # whitespace, so the target span crosses the sentence boundary and is not a
    # usable same-sentence reference.
    text = "Foo. 123 was overruled."

    assert extract_negative_treatments(text, "Smith v. Wilkie", "Foo. 123") == []


def test_scope_for_overruled_without_in_part_is_all():
    assert _scope_for("overruled", "The case was overruled.") == "all"
    assert _scope_for("questioned", "The case was questioned.") == "unclear"


def test_sentence_bounds_handles_boundary_after_target_and_abbreviation():
    text = "Smith v. Wilkie was discussed. The court overruled it."
    start, end = _sentence_bounds(text, text.index("Smith"))
    assert text[start:end].strip() == "Smith v. Wilkie was discussed."

    assert _is_abbreviation(". Foo", 0) is False
    assert _is_abbreviation("Smith v. Wilkie", 7) is True


# ── target resolution ───────────────────────────────────────────────────────


def test_safe_int_handles_malformed_values():
    assert _safe_int("12") == 12
    assert _safe_int("not-a-number") == 0
    assert _safe_int([]) == 0


def test_sentence_bounds_returns_whole_text_when_no_sentence_end_exists():
    text = "Smith v. Wilkie"
    assert _sentence_bounds(text, 0) == (0, len(text))


def test_as_opinion_id_accepts_digits_urls_and_rejects_invalid_values():
    assert _as_opinion_id("123") == 123
    assert _as_opinion_id("https://www.courtlistener.com/opinion/456/") == 456
    assert _as_opinion_id("0") is None
    assert _as_opinion_id("") is None
    assert _as_opinion_id(None) is None
    assert _as_opinion_id("not-an-id") is None


def test_cluster_helpers_handle_dicts_strings_and_empty_values():
    cluster = {
        "citations": [{"cite": "30 Vet.App. 123"}, {"citation": "31 Vet.App. 456"}],
        "sub_opinions": [{"resource_uri": "https://www.courtlistener.com/opinions/789/"}],
    }
    assert _cluster_citation(cluster) == "30 Vet.App. 123"
    assert _cluster_citations(cluster) == ("30 Vet.App. 123", "31 Vet.App. 456")
    assert _cluster_opinion_id(cluster) == 789
    assert _cluster_opinion_ids(cluster) == (789,)
    assert _cluster_identifier(cluster) == ""
    assert _cluster_identifier({"id": 98765}) == "98765"
    assert _cluster_identifier(
        {"resource_uri": "https://www.courtlistener.com/api/rest/v4/clusters/42/"}
    ) == "42"
    assert _cluster_citation({"citations": ["32 Vet.App. 1"]}) == "32 Vet.App. 1"
    assert _cluster_citation({"citations": [{}, "33 Vet.App. 2"]}) == "33 Vet.App. 2"
    assert _cluster_citation({"citations": []}) == ""
    assert _cluster_citation({}) == ""
    assert _cluster_opinion_id({"sub_opinions": ["bad", "https://x/opinions/9/"]}) == 9
    assert _cluster_opinion_id({}) is None


def test_direct_opinion_id_skips_citation_lookup():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    _wire_good_citing_opinions(provider, 201)
    case = _case()

    check = check_case(case, provider)

    assert provider.lookup_calls == []
    assert check.target_opinion_id == "111"
    assert check.status == "good_law"
    assert check.good_law_as_of == "2020-02-02"
    assert check.coverage.complete is True
    assert check.coverage.citing_opinions_reviewed == 1


def test_lookup_resolves_single_cluster_and_uses_cluster_opinion_id():
    provider = FakeProvider()
    provider.lookup_results = [
        {
            "citation": "30 Vet.App. 123",
            "normalized_citations": ["30 Vet.App. 123"],
            "status": 200,
            "clusters": [
                {
                    "case_name": "Smith v. Wilkie",
                    "citations": [{"cite": "30 Vet.App. 123"}],
                    "sub_opinions": ["https://www.courtlistener.com/api/rest/v4/opinions/222/"],
                }
            ],
        }
    ]
    provider.pages[None] = _page([201])
    _wire_good_citing_opinions(provider, 201)

    check = check_case(_case(courtlistener_opinion_id=""), provider)

    assert provider.lookup_calls == ["30 Vet.App. 123"]
    assert check.target_opinion_id == "222"
    assert check.target_citation == "30 Vet.App. 123"
    assert check.status == "good_law"


def test_lookup_resolves_all_parallel_citations_and_cluster_opinions():
    provider = FakeProvider()
    provider.lookup_results = [
        {
            "citation": "30 Vet.App. 123",
            "normalized_citations": ["30 Vet.App. 123"],
            "status": 200,
            "clusters": [
                {
                    "id": 777,
                    "case_name": "Smith v. Wilkie",
                    "citations": [
                        {"cite": "30 Vet.App. 123"},
                        {"cite": "2020 WL 123456"},
                    ],
                    "sub_opinions": [
                        "https://www.courtlistener.com/api/rest/v4/opinions/222/",
                        "https://www.courtlistener.com/api/rest/v4/opinions/223/",
                    ],
                }
            ],
        }
    ]
    provider.opinion_pages[(222, None)] = _page([201])
    provider.opinion_pages[(223, None)] = _page([202])
    _wire_good_citing_opinions(provider, 201, 202)
    provider.texts[202] = (
        "The court followed Smith v. Wilkie, 2020 WL 123456."
    )

    check = check_case(_case(courtlistener_opinion_id=""), provider)

    assert check.target_opinion_id == "222"
    assert check.target_opinion_ids == ["222", "223"]
    assert check.target_cluster_id == "777"
    assert check.target_citations == ["30 Vet.App. 123", "2020 WL 123456"]
    assert [call[0] for call in provider.edge_calls] == [222, 223]
    assert check.coverage.citing_opinions_reviewed == 2
    assert check.status == "good_law"


def test_direct_opinion_expands_to_its_whole_cluster_and_parallel_citations():
    provider = FakeProvider()
    provider.metadata[111] = {
        **_target_meta(111),
        "cluster_id": "888",
        "citations": ["30 Vet.App. 123", "2020 WL 123456"],
        "opinion_ids": [111, 112],
    }
    provider.opinion_pages[(111, None)] = _page([201])
    provider.opinion_pages[(112, None)] = _page([202])
    _wire_good_citing_opinions(provider, 201, 202)
    provider.texts[201] = "The court followed Smith v. Wilkie, 2020 WL 123456."

    check = check_case(_case(), provider)

    assert check.target_cluster_id == "888"
    assert check.target_citations == ["30 Vet.App. 123", "2020 WL 123456"]
    assert check.target_opinion_ids == ["111", "112"]
    assert [call[0] for call in provider.edge_calls] == [111, 112]
    assert check.coverage.citing_opinions_reviewed == 2
    assert check.status == "good_law"


def test_cluster_forward_edges_deduplicate_a_shared_citing_opinion():
    provider = FakeProvider()
    provider.metadata[111] = {
        **_target_meta(111),
        "opinion_ids": [111, 112],
    }
    provider.opinion_pages[(111, None)] = _page([201])
    provider.opinion_pages[(112, None)] = _page([201])
    _wire_good_citing_opinions(provider, 201)

    check = check_case(_case(), provider)

    assert [call[0] for call in provider.edge_calls] == [111, 112]
    assert check.coverage.citing_opinions_reviewed == 1
    assert check.coverage.forward_citations_found == 1
    assert check.later_citing_opinions[0].opinion_id == "201"
    assert check.status == "good_law"


def test_cluster_forward_edges_partial_failure_keeps_coverage_incomplete():
    provider = FakeProvider()
    provider.metadata[111] = {
        **_target_meta(111),
        "opinion_ids": [111, 112],
    }
    provider.opinion_pages[(111, None)] = _page([201])
    provider.opinion_page_errors[112] = SearchError("edge unavailable")
    _wire_good_citing_opinions(provider, 201)

    check = check_case(_case(), provider)

    assert check.coverage.forward_citations_found == 1
    assert check.coverage.citing_opinions_reviewed == 1
    assert check.coverage.complete is False
    assert "forward_fetch_failed" in _gap_codes(check)
    assert check.status == "unknown"


def test_direct_opinion_without_cluster_metadata_is_incomplete():
    provider = FakeProvider()
    provider.metadata[111] = None
    provider.pages[None] = _page([201])
    _wire_good_citing_opinions(provider, 201)

    check = check_case(_case(), provider)

    assert check.target_opinion_ids == ["111"]
    assert check.target_citations == ["30 Vet.App. 123"]
    assert check.coverage.complete is False
    assert _gap_codes(check) == ["cluster_unresolved"]
    assert check.coverage.temporal_confidence == "medium"
    assert check.status == "unknown"
    assert any("full CourtListener cluster" in flag for flag in check.flags)


def test_direct_opinion_cluster_lookup_error_is_incomplete():
    provider = FakeProvider()
    provider.metadata_errors[111] = SearchError("cluster unavailable")
    provider.pages[None] = _page([201])
    _wire_good_citing_opinions(provider, 201)

    check = check_case(_case(), provider)

    assert check.coverage.complete is False
    assert check.status == "unknown"
    assert any("cluster unavailable" in flag for flag in check.flags)


def test_direct_opinion_metadata_without_cluster_fields_is_incomplete():
    provider = FakeProvider()
    # The provider always carries the requested opinion id, even when the
    # cluster link is missing; that fallback alone is not cluster evidence.
    provider.metadata[111] = {
        "title": "Smith v. Wilkie",
        "citation": "30 Vet.App. 123",
        "citations": [],
        "cluster_id": "",
        "opinion_ids": [111],
    }
    provider.pages[None] = _page([201])
    _wire_good_citing_opinions(provider, 201)

    check = check_case(_case(), provider)

    assert check.coverage.complete is False
    assert _gap_codes(check) == ["cluster_unresolved"]
    assert check.status == "unknown"
    assert any("only opinion 111" in flag for flag in check.flags)


def test_lookup_selects_the_matching_row_when_multiple_are_returned():
    provider = FakeProvider()
    provider.lookup_results = [
        {
            "citation": "29 Vet.App. 1",
            "normalized_citations": ["29 Vet.App. 1"],
            "status": 404,
        },
        {
            "citation": "30 Vet.App. 123",
            "normalized_citations": ["30 Vet.App. 123"],
            "status": 200,
            "clusters": [{"sub_opinions": ["/api/rest/v4/opinions/333/"]}],
        },
    ]
    provider.pages[None] = _page([201])
    _wire_good_citing_opinions(provider, 201)

    check = check_case(_case(courtlistener_opinion_id=""), provider)

    assert check.target_opinion_id == "333"
    assert check.status == "good_law"


def test_missing_citation_without_opinion_id_is_unknown():
    provider = FakeProvider()

    check = check_case(_case(courtlistener_opinion_id="", citation=""), provider)

    assert check.status == "unknown"
    assert "no citation" in check.flags[0]
    assert _gap_codes(check) == ["target_unresolved"]
    assert check.coverage.temporal_confidence == "none"
    assert "No target cluster" in check.coverage.temporal_confidence_reason
    assert provider.lookup_calls == []


def test_lookup_error_is_reported_as_unknown():
    provider = FakeProvider()
    provider.lookup_error = SearchError("quota exhausted")

    check = check_case(_case(courtlistener_opinion_id=""), provider)

    assert check.status == "unknown"
    assert "quota exhausted" in check.flags[0]


def test_unresolved_and_ambiguous_lookups_are_unknown():
    provider = FakeProvider()
    provider.lookup_results = []
    check = check_case(_case(courtlistener_opinion_id=""), provider)
    assert check.status == "unknown"
    assert "no results" in check.flags[0]

    provider.lookup_results = [
        {"citation": "29 Vet.App. 1", "status": 404},
        {"citation": "31 Vet.App. 2", "status": 404},
    ]
    check = check_case(_case(courtlistener_opinion_id=""), provider)
    assert check.status == "unknown"
    assert "ambiguous" in check.flags[0]


def test_non_200_lookup_status_is_unknown():
    provider = FakeProvider()
    provider.lookup_results = [
        {
            "citation": "30 Vet.App. 123",
            "status": 404,
            "clusters": [{"sub_opinions": ["/opinions/222/"]}],
        }
    ]

    check = check_case(_case(courtlistener_opinion_id=""), provider)

    assert check.status == "unknown"
    assert "status 404" in check.flags[0]


def test_missing_status_is_reported_as_unknown():
    provider = FakeProvider()
    provider.lookup_results = [{"citation": "30 Vet.App. 123"}]

    check = check_case(_case(courtlistener_opinion_id=""), provider)

    assert check.status == "unknown"
    assert "status unknown" in check.flags[0]


def test_cluster_count_and_shape_are_validated():
    provider = FakeProvider()
    provider.lookup_results = [
        {"citation": "30 Vet.App. 123", "status": 200, "clusters": []}
    ]
    assert "exactly one" in check_case(_case(courtlistener_opinion_id=""), provider).flags[0]

    provider.lookup_results = [
        {"citation": "30 Vet.App. 123", "status": 200, "clusters": [[], []]}
    ]
    assert "exactly one" in check_case(_case(courtlistener_opinion_id=""), provider).flags[0]

    provider.lookup_results = [
        {"citation": "30 Vet.App. 123", "status": 200, "clusters": ["not-a-dict"]}
    ]
    assert "malformed" in check_case(_case(courtlistener_opinion_id=""), provider).flags[0]

    provider.lookup_results = [
        {"citation": "30 Vet.App. 123", "status": 200, "clusters": [{"sub_opinions": []}]}
    ]
    assert "without an opinion id" in check_case(
        _case(courtlistener_opinion_id=""), provider
    ).flags[0]


# ── coverage and status ─────────────────────────────────────────────────────


def test_clean_complete_forward_citations_produce_good_law_date():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    _wire_good_citing_opinions(provider, 201, 202)
    provider.metadata[202] = _meta(date="2021-03-03")

    check = check_case(_case(), provider)

    assert check.status == "good_law"
    assert check.good_law_as_of == "2021-03-03"
    assert check.coverage.forward_citations_found == 2
    assert check.coverage.citing_opinions_reviewed == 2
    assert check.coverage.complete is True
    assert check.coverage.gaps == []
    assert check.coverage.temporal_confidence == "high"
    assert "coverage is complete" in check.coverage.temporal_confidence_reason
    assert "no authoritative negative treatment" in check.good_law_meaning
    assert "subsequent history" in check.good_law_meaning
    assert check.subsequent_history_assessed is False
    assert [citation.opinion_id for citation in check.later_citing_opinions] == [
        "201",
        "202",
    ]


def test_citator_attaches_history_candidates_and_only_verifies_with_a_record():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    _wire_good_citing_opinions(provider, 201)

    candidate_only = check_case(_case(), provider)

    assert candidate_only.appellate_history is not None
    assert candidate_only.appellate_history.status == "insufficient_source"
    assert candidate_only.subsequent_history_assessed is False
    assert candidate_only.appellate_history.candidates[0].verification_status == (
        "candidate"
    )

    record = AppellateHistoryRecord(
        target_cluster_id="777",
        candidate_case="Jones v. McDonough",
        candidate_citation="31 Vet.App. 456",
        relationship="direct_appeal",
        disposition="affirmed",
        scope="all",
        source_quality="official",
        source_url="https://www.cafc.uscourts.gov/opinions/1.pdf",
        reviewer="Reviewer",
        reviewed_at="2026-01-02T03:04:05+00:00",
        rationale="Checked the docket and disposition.",
    )
    verified = check_case(_case(), provider, history_records=[record])

    assert verified.appellate_history is not None
    assert verified.appellate_history.status == "verified"
    assert verified.appellate_history.verified_history == [record]
    assert verified.subsequent_history_assessed is True


def test_same_docket_decisions_are_a_separate_advisory_signal():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    _wire_good_citing_opinions(provider, 201)
    provider.same_docket[111] = [
        {
            "case_name": "Smith v. McDonough",
            "citation": "32 Vet.App. 9",
            "court": COURT_CAVC,
            "decision_date": "2019-01-01",
            "url": "https://www.courtlistener.com/opinion/999/",
            "cluster_id": "999",
        }
    ]

    check = check_case(_case(), provider)

    assert check.status == "good_law"
    assert check.coverage.complete is True
    assert provider.same_docket_calls == [(111, 5)]
    assert [decision.cluster_id for decision in check.same_docket_decisions] == ["999"]
    assert check.same_docket_decisions[0].case_name == "Smith v. McDonough"


def test_same_docket_failure_is_advisory_only():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    _wire_good_citing_opinions(provider, 201)
    provider.same_docket_errors[111] = SearchError("docket down")

    check = check_case(_case(), provider)

    assert check.status == "good_law"
    assert check.coverage.complete is True
    assert check.same_docket_decisions == []
    assert any("same-docket advisory unavailable" in flag for flag in check.flags)


def test_explicit_negative_treatment_sets_negative_and_clears_good_law_date():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    provider.metadata[201] = _meta()
    provider.texts[201] = (
        "The court overruled Smith v. Wilkie, 30 Vet.App. 123 in part."
    )

    check = check_case(_case(), provider)

    assert check.status == "negative"
    assert check.good_law_as_of == ""
    assert len(check.negative_treatments) == 1
    treatment = check.negative_treatments[0]
    assert treatment.treatment == "overruled"
    assert treatment.scope == "part"
    assert treatment.binding is True
    assert treatment.opinion_role == "majority"
    assert treatment.precedential_status == "Published"
    assert treatment.authoritative is True
    assert treatment.gate_reason == ""
    assert check.gated_treatment_findings == []
    assert check.later_citing_opinions[0].treatment_eligible is True
    assert treatment.excerpt in provider.texts[201]
    assert treatment.citing_case == "Jones v. McDonough"


def test_dissent_negative_is_gated_and_does_not_set_negative():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    provider.metadata[201] = _meta(opinion_type="040dissent")
    provider.texts[201] = (
        "The dissent would overrule Smith v. Wilkie, 30 Vet.App. 123."
    )

    check = check_case(_case(), provider)

    assert check.status == "unknown"
    assert check.negative_treatments == []
    assert len(check.gated_treatment_findings) == 1
    gated = check.gated_treatment_findings[0]
    assert gated.opinion_role == "separate"
    assert gated.authoritative is False
    assert "opinion role is separate" in gated.gate_reason
    assert check.later_citing_opinions[0].treatment_eligible is False
    assert check.later_citing_opinions[0].gate_reason == "opinion role is separate"
    assert any("non-authoritative" in flag for flag in check.flags)


def test_concurrence_negative_is_gated_and_does_not_set_negative():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    provider.metadata[201] = _meta(opinion_type="030concurrence")
    provider.texts[201] = (
        "The concurrence would overrule Smith v. Wilkie, 30 Vet.App. 123."
    )

    check = check_case(_case(), provider)

    assert check.status == "unknown"
    assert check.negative_treatments == []
    assert len(check.gated_treatment_findings) == 1
    assert check.gated_treatment_findings[0].opinion_role == "separate"
    assert check.gated_treatment_findings[0].authoritative is False
    assert check.later_citing_opinions[0].treatment_eligible is False


def test_nonprecedential_negative_is_gated_and_does_not_set_negative():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    provider.metadata[201] = _meta(precedential_status="Unpublished")
    provider.texts[201] = (
        "The court overruled Smith v. Wilkie, 30 Vet.App. 123."
    )

    check = check_case(_case(), provider)

    assert check.status == "unknown"
    assert check.negative_treatments == []
    assert check.gated_treatment_findings[0].precedential_tier == "nonprecedential"
    assert "precedential status is nonprecedential" in (
        check.gated_treatment_findings[0].gate_reason
    )


def test_plurality_and_unknown_status_negative_findings_are_gated():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    provider.metadata[201] = _meta(opinion_type="025plurality", precedential_status="")
    provider.texts[201] = (
        "The plurality would overrule Smith v. Wilkie, 30 Vet.App. 123."
    )

    check = check_case(_case(), provider)

    assert check.status == "unknown"
    assert check.negative_treatments == []
    gated = check.gated_treatment_findings[0]
    assert gated.opinion_role == "plurality"
    assert gated.precedential_tier == "unknown"
    assert gated.gate_reason == (
        "opinion role is plurality; precedential status is unknown"
    )


def test_authoritative_and_gated_findings_are_kept_separate():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202])
    provider.metadata[201] = _meta(opinion_type="020lead")
    provider.texts[201] = "The court overruled Smith v. Wilkie, 30 Vet.App. 123."
    provider.metadata[202] = _meta(opinion_type="040dissent")
    provider.texts[202] = "The dissent would overrule Smith v. Wilkie, 30 Vet.App. 123."

    check = check_case(_case(), provider)

    assert check.status == "negative"
    assert len(check.negative_treatments) == 1
    assert len(check.gated_treatment_findings) == 1
    assert check.negative_treatments[0].authoritative is True
    assert check.gated_treatment_findings[0].authoritative is False


def test_separate_opinion_without_negative_treatment_stays_good_law():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    provider.metadata[201] = _meta(opinion_type="030concurrence")
    provider.texts[201] = "The concurrence follows Smith v. Wilkie, 30 Vet.App. 123."

    check = check_case(_case(), provider)

    assert check.status == "good_law"
    assert check.gated_treatment_findings == []
    assert check.later_citing_opinions[0].treatment_eligible is False


def test_binding_comparison_can_be_false_or_unknown():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202])
    provider.metadata[201] = _meta(court=COURT_CAVC)
    provider.texts[201] = "The court questioned Smith v. Wilkie, 30 Vet.App. 123."
    provider.metadata[202] = _meta(court=COURT_UNKNOWN)
    provider.texts[202] = "The court criticized Smith v. Wilkie, 30 Vet.App. 123."
    case = _case(court=COURT_SUPREME)

    check = check_case(case, provider)

    assert [t.binding for t in check.negative_treatments] == [False, None]


def test_cursor_pagination_collects_all_pages():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=3, next_url="?cursor=next-page")
    provider.pages["next-page"] = _page([203], count=3)
    _wire_good_citing_opinions(provider, 201, 202, 203)

    check = check_case(_case(), provider, max_pages=2)

    assert check.coverage.pages_fetched == 2
    assert check.coverage.citing_opinions_reviewed == 3
    assert check.coverage.complete is True
    assert check.status == "good_law"


def test_page_cap_truncates_and_does_not_report_good_law():
    provider = FakeProvider()
    provider.pages[None] = _page([201], count=2, next_url="?cursor=next-page")
    _wire_good_citing_opinions(provider, 201)

    check = check_case(_case(), provider, max_pages=1)

    assert check.coverage.truncated is True
    assert check.coverage.complete is False
    assert "page_cap" in _gap_codes(check)
    assert check.coverage.temporal_confidence == "medium"
    assert "coverage is incomplete" in check.coverage.temporal_confidence_reason
    assert check.status == "unknown"
    assert any("page cap" in flag for flag in check.flags)


def test_reported_count_larger_than_collected_ids_truncates():
    provider = FakeProvider()
    provider.pages[None] = _page([201], count=5)
    _wire_good_citing_opinions(provider, 201)

    check = check_case(_case(), provider)

    assert check.coverage.forward_citations_found == 5
    assert check.coverage.truncated is True
    assert _gap_codes(check) == ["count_mismatch"]
    assert check.coverage.temporal_confidence == "medium"
    assert check.status == "unknown"


def test_citing_opinion_cap_truncates_after_reviewed_subset():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    _wire_good_citing_opinions(provider, 201, 202)

    check = check_case(_case(), provider, max_citing_opinions=1)

    assert check.coverage.citing_opinions_reviewed == 1
    assert check.coverage.forward_citations_found == 2
    assert check.coverage.truncated is True
    assert check.coverage.complete is False
    assert _gap_codes(check) == ["citing_opinion_cap"]
    assert check.coverage.temporal_confidence == "medium"
    assert check.status == "unknown"


def test_forward_fetch_error_is_unknown():
    provider = FakeProvider()
    provider.page_errors[None] = SearchError("network down")

    check = check_case(_case(), provider)

    assert check.status == "unknown"
    assert _gap_codes(check) == ["forward_fetch_failed"]
    assert check.coverage.temporal_confidence == "none"
    assert any("network down" in flag for flag in check.flags)


def test_malformed_forward_rows_make_coverage_incomplete():
    provider = FakeProvider()
    provider.pages[None] = {
        "count": 2,
        "results": [None, {"citing_opinion": "not-a-url"}],
        "next": "",
    }

    check = check_case(_case(), provider)

    assert check.status == "unknown"
    assert check.coverage.complete is False
    assert "no_usable_citing_ids" in _gap_codes(check)
    assert check.coverage.temporal_confidence == "none"
    assert any("no usable opinion ids" in flag for flag in check.flags)

    provider.pages[None] = {"count": 1, "results": "not-a-list", "next": ""}
    check = check_case(_case(), provider)
    assert check.status == "unknown"
    assert check.coverage.complete is False
    assert "no_usable_citing_ids" in _gap_codes(check)


def test_count_lower_than_collected_ids_is_raised_to_the_id_count():
    provider = FakeProvider()
    provider.pages[None] = _page([201], count=0)
    _wire_good_citing_opinions(provider, 201)

    check = check_case(_case(), provider)

    assert check.coverage.forward_citations_found == 1
    assert check.coverage.complete is True


def test_zero_forward_citations_is_unknown_even_though_coverage_is_complete():
    check = check_case(_case(), FakeProvider())

    assert check.status == "unknown"
    assert check.coverage.complete is True
    assert check.coverage.citing_opinions_reviewed == 0
    assert _gap_codes(check) == ["no_forward_citations"]
    assert check.coverage.temporal_confidence == "none"
    assert "No citing opinion was reviewed" in check.coverage.temporal_confidence_reason
    assert any("no forward citing opinions" in flag for flag in check.flags)


def test_repeated_cursor_stops_with_incomplete_coverage():
    provider = FakeProvider()
    provider.pages[None] = _page([201], count=2, next_url="?cursor=same")
    provider.pages["same"] = _page([202], count=2, next_url="?cursor=same")
    _wire_good_citing_opinions(provider, 201, 202)

    check = check_case(_case(), provider, max_pages=3)

    assert check.status == "unknown"
    assert _gap_codes(check) == ["pagination_cursor_repeated"]
    assert any("repeated a cursor" in flag for flag in check.flags)


def test_next_link_without_cursor_is_incomplete():
    provider = FakeProvider()
    provider.pages[None] = _page([201], count=2, next_url="?page=2")
    _wire_good_citing_opinions(provider, 201)

    check = check_case(_case(), provider)

    assert check.status == "unknown"
    assert "pagination_cursor_missing" in _gap_codes(check)
    assert any("no cursor" in flag for flag in check.flags)


def test_metadata_missing_error_and_empty_text_are_flagged():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202, 203], count=3)
    provider.metadata[201] = None
    provider.metadata_errors[202] = SearchError("metadata down")
    provider.metadata[203] = _meta()
    provider.texts[203] = ""

    check = check_case(_case(), provider)

    assert check.status == "unknown"
    assert check.coverage.citing_opinions_reviewed == 0
    assert _gap_codes(check) == [
        "citing_metadata_unavailable",
        "citing_metadata_fetch_failed",
        "citing_text_empty",
    ]
    assert check.coverage.temporal_confidence == "none"
    assert any("metadata was unavailable" in flag for flag in check.flags)
    assert any("metadata down" in flag for flag in check.flags)
    assert any("text was empty" in flag for flag in check.flags)


def test_text_fetch_error_and_missing_target_are_flagged():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    provider.metadata[201] = _meta()
    provider.text_errors[201] = SearchError("text down")
    provider.metadata[202] = _meta()
    provider.texts[202] = "The court discussed an unrelated decision."

    check = check_case(_case(), provider)

    assert check.status == "unknown"
    assert check.later_citing_opinions == []
    assert _gap_codes(check) == ["citing_text_fetch_failed", "target_not_located"]
    assert any("text down" in flag for flag in check.flags)
    assert any("could not locate" in flag for flag in check.flags)


def test_missing_citing_date_prevents_a_good_law_date():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    provider.metadata[201] = _meta(date="")
    provider.texts[201] = "The court followed Smith v. Wilkie, 30 Vet.App. 123."

    check = check_case(_case(), provider)

    assert check.coverage.citing_opinions_reviewed == 1
    assert check.coverage.complete is False
    assert _gap_codes(check) == ["citing_date_missing"]
    assert check.coverage.temporal_confidence == "none"
    assert check.status == "unknown"
    assert any("decision date" in flag for flag in check.flags)


def test_missing_date_alongside_a_dated_opinion_lowers_temporal_confidence():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    _wire_good_citing_opinions(provider, 201, 202)
    provider.metadata[201] = _meta(date="2020-02-02")
    provider.metadata[202] = _meta(date="")

    check = check_case(_case(), provider)

    assert check.coverage.citing_opinions_reviewed == 2
    assert check.coverage.latest_citing_date == "2020-02-02"
    assert check.coverage.complete is False
    assert "citing_date_missing" in _gap_codes(check)
    assert check.coverage.temporal_confidence == "low"
    assert "has no decision date" in check.coverage.temporal_confidence_reason
    assert check.good_law_as_of == ""


def test_direct_id_without_citation_uses_title_for_target_matching():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    provider.metadata[201] = _meta()
    provider.texts[201] = "The court followed Smith v. Wilkie."

    check = check_case(_case(citation=""), provider)

    assert check.status == "good_law"
    assert check.target_citation == "30 Vet.App. 123"
    assert check.target_citations == ["30 Vet.App. 123", "2020 WL 123456"]


def test_run_citator_honors_max_cases_and_can_select_none():
    provider = FakeProvider()
    provider.pages[None] = _page([])

    assert run_citator([_case()], provider, max_cases=0) == []

    checks = run_citator(
        [_case(), _case(title="Other v. McDonough", courtlistener_opinion_id="112")],
        provider,
        max_cases=1,
    )
    assert len(checks) == 1
    assert checks[0].target_case == "Smith v. Wilkie"


def test_run_citator_persists_evidence_and_provider_response_snapshots(tmp_path):
    from va_legal_agent.snapshots import SnapshotStore

    store = SnapshotStore(tmp_path / "snapshots")

    checks = run_citator(
        [_case()], FakeProvider(), max_cases=1, snapshot_store=store
    )

    assert len(checks) == 1
    rows = [
        json.loads(line)
        for line in store.manifest.read_text(encoding="utf-8").splitlines()
    ]
    kinds = [row["kind"] for row in rows]
    assert "evidence.citator" in kinds
    assert "courtlistener.opinion_metadata" in kinds
    assert "courtlistener.same_docket" in kinds
    assert "courtlistener.citing_edges" in kinds


def test_check_case_persists_evidence_for_an_unresolved_target(tmp_path):
    from va_legal_agent.snapshots import SnapshotStore

    provider = FakeProvider()
    provider.metadata[111] = None
    store = SnapshotStore(tmp_path / "snapshots")

    check = check_case(_case(), provider, snapshot_store=store)

    assert check.status == "unknown"
    rows = [
        json.loads(line)
        for line in store.manifest.read_text(encoding="utf-8").splitlines()
    ]
    assert any(row["kind"] == "evidence.citator" for row in rows)


def test_check_case_reports_evidence_snapshot_failure_without_aborting():
    class FailingStore:
        def record_evidence(self, *args, **kwargs):
            raise OSError("disk full")

    check = check_case(_case(), FakeProvider(), snapshot_store=FailingStore())

    assert any(
        "Could not persist evidence snapshot" in flag for flag in check.flags
    )


def test_run_citator_prioritizes_binding_authority_over_input_order():
    from va_legal_agent.authority_queue import prioritize_authorities
    provider = FakeProvider()
    provider.pages[None] = _page([])
    lower = _case(
        title="Board Decision",
        court=COURT_BVA,
        courtlistener_opinion_id="112",
    )
    higher = _case(
        title="Smith v. Wilkie",
        court=COURT_CAVC,
        courtlistener_opinion_id="111",
    )

    checks = run_citator([lower, higher], provider, max_cases=1)

    assert [check.target_case for check in checks] == ["Smith v. Wilkie"]
    assert checks[0].authority_priority == 1
    assert any("authority tier 2" in reason for reason in checks[0].authority_reasons)

    prebuilt = prioritize_authorities([lower, higher], 1)
    prebuilt_checks = run_citator(
        [lower, higher], provider, max_cases=1, authority_queue=prebuilt
    )
    assert [check.target_case for check in prebuilt_checks] == ["Smith v. Wilkie"]
