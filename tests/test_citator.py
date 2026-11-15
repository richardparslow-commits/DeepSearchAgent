"""Tests for the conservative CourtListener citator."""

import json
from datetime import date

import pytest

from va_legal_agent.citator import (
    _authority_gate,
    _candidate_date_relation,
    _citing_priority_page_size,
    _citing_priority_pool_size,
    _is_abbreviation,
    _normalize_as_of_date,
    _opinion_role,
    _precedential_tier,
    _safe_int,
    _scope_for,
    _sentence_bounds,
    _source_validity_signals,
    _span_distance,
    _target_binding,
    _target_short_names,
    check_case,
    extract_negative_treatments,
    extract_reaffirmance_signals,
    run_citator,
)
from va_legal_agent.models import (
    AppellateHistoryRecord,
    CaseRecord,
    NegativeTreatment,
)
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
    cluster_id: str = "888",
) -> dict[str, str]:
    return {
        "title": case,
        "citation": "31 Vet.App. 456",
        "court": court,
        "decision_date": date,
        "url": "https://www.courtlistener.com/opinion/201/",
        "cluster_id": cluster_id,
        "opinion_type": opinion_type,
        "precedential_status": precedential_status,
    }


def _target_meta(opinion_id: int = 111) -> dict[str, object]:
    return {
        "title": "Smith v. Wilkie",
        "citation": "30 Vet.App. 123",
        "citations": ["30 Vet.App. 123", "2020 WL 123456"],
        "cluster_id": "777",
        "cluster_resolved": True,
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


class BatchFakeProvider(FakeProvider):
    """Fake provider that also exposes the batched metadata lookup."""

    def __init__(self):
        super().__init__()
        self.batch_calls: list[list[int]] = []
        self.batch_result: object = None
        self.batch_error: Exception | None = None

    def opinion_metadata_batch(self, opinion_ids: list[int]):
        self.batch_calls.append(list(opinion_ids))
        if self.batch_error is not None:
            raise self.batch_error
        if self.batch_result is not None:
            return self.batch_result
        return {
            opinion_id: self.metadata[opinion_id]
            for opinion_id in opinion_ids
            if self.metadata.get(opinion_id)
        }


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


def test_target_short_names_skip_generic_and_short_party_words():
    assert _target_short_names("Roe v. Wade") == ("Roe", "Wade")
    assert _target_short_names(
        "Chevron U. S. A. Inc. v. Natural Resources Defense Council, Inc."
    ) == ("Chevron",)
    assert _target_short_names(
        "Planned Parenthood of Southeastern Pennsylvania v. Casey"
    ) == ("Casey",)
    assert _target_short_names("United States v. Smith") == ("Smith",)
    assert _target_short_names(
        "Department of Veterans Affairs v. Smith"
    ) == ("Smith",)
    assert _target_short_names("In re X") == ()
    assert _target_short_names("smith v. Wilkie") == ("Wilkie",)


def test_extract_short_name_binds_holding_and_flags_review():
    text = "We hold that Roe and Casey must be overruled."

    findings = extract_negative_treatments(
        text, "Roe v. Wade", "410 U.S. 113"
    )

    assert [finding.treatment for finding in findings] == ["overruled"]
    assert findings[0].attribution_basis == "short_name"
    assert findings[0].attribution_ambiguous is True
    assert "short name" in findings[0].attribution_reason
    assert findings[0].confidence == "medium"


def test_extract_short_name_rejects_a_competing_citation():
    text = "We overruled Roe, 410 U.S. 999."

    assert extract_negative_treatments(
        text, "Roe v. Wade", "410 U.S. 113"
    ) == []


def test_extract_binds_a_spaced_reporter_citation():
    text = "The court overruled Austin, 494 U. S. 652."

    findings = extract_negative_treatments(
        text,
        "Austin v. Michigan State Chamber of Commerce",
        "494 U.S. 652",
    )

    assert [finding.treatment for finding in findings] == ["overruled"]
    assert findings[0].attribution_basis == "citation"


def test_extract_keeps_a_parenthetical_subsequent_history_finding():
    text = (
        "See Smith v. Wilkie, 30 Vet.App. 123 (1999) (en banc), overruled "
        "on other grounds by Jones v. McDonough, 31 Vet.App. 456 (2000) "
        "(en banc)."
    )

    findings = extract_negative_treatments(
        text, "Smith v. Wilkie", "30 Vet.App. 123"
    )

    assert [finding.treatment for finding in findings] == ["overruled"]


def test_extract_ignores_question_noun_and_adjectival_limited():
    text = (
        "The Court granted certiorari limited to the question whether "
        "Smith v. Wilkie applies."
    )

    assert extract_negative_treatments(text, "Smith v. Wilkie", "") == []


def test_extract_keeps_a_passive_overrule_by_an_explicit_actor():
    no_citation = "Smith v. Wilkie was overruled by the Supreme Court."
    with_citation = (
        "Smith v. Wilkie, 30 Vet.App. 123, was overruled by the Supreme Court."
    )

    assert [
        finding.treatment
        for finding in extract_negative_treatments(
            no_citation, "Smith v. Wilkie", "30 Vet.App. 123"
        )
    ] == ["overruled"]
    assert [
        finding.treatment
        for finding in extract_negative_treatments(
            with_citation, "Smith v. Wilkie", "30 Vet.App. 123"
        )
    ] == ["overruled"]


def test_extract_vacated_is_all_scope():
    text = "The panel opinion, reported at 30 Vet.App. 123, is vacated."

    findings = extract_negative_treatments(
        text, "Smith v. Wilkie", "30 Vet.App. 123"
    )

    assert [finding.treatment for finding in findings] == ["vacated"]
    assert findings[0].scope == "all"


def test_extract_criticized_uses_case_name_only():
    text = "The panel criticized Smith v. Wilkie for its reasoning."

    findings = extract_negative_treatments(text, "Smith v. Wilkie", "")

    assert [finding.treatment for finding in findings] == ["criticized"]


def test_extract_flags_case_name_only_attribution_as_ambiguous():
    text = "The court criticized Smith v. Wilkie for its reasoning."

    findings = extract_negative_treatments(
        text, "Smith v. Wilkie", "30 Vet.App. 123"
    )

    assert len(findings) == 1
    assert findings[0].attribution_basis == "case_name"
    assert findings[0].attribution_ambiguous is True
    assert "case name only" in findings[0].attribution_reason


def test_extract_flags_passive_treatment_as_ambiguous_but_retains_it():
    text = "Smith v. Wilkie, 30 Vet.App. 123, was overruled."

    findings = extract_negative_treatments(
        text, "Smith v. Wilkie", "30 Vet.App. 123"
    )

    assert [finding.treatment for finding in findings] == ["overruled"]
    assert findings[0].attribution_basis == "citation"
    assert findings[0].attribution_ambiguous is True
    assert "passive" in findings[0].attribution_reason


def test_extract_explicit_active_treatment_is_not_attribution_ambiguous():
    text = "The court overruled Smith v. Wilkie, 30 Vet.App. 123."

    findings = extract_negative_treatments(
        text, "Smith v. Wilkie", "30 Vet.App. 123"
    )

    assert findings[0].attribution_ambiguous is False
    assert findings[0].attribution_reason == ""


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


def test_extract_rejects_same_name_with_a_different_citation():
    text = (
        "We overruled Smith v. Wilkie, 30 Vet.App. 999, a different case that "
        "happens to share the name."
    )

    assert extract_negative_treatments(
        text, "Smith v. Wilkie", "30 Vet.App. 123"
    ) == []


def test_extract_rejects_dissent_attribution_in_a_majority_opinion():
    text = (
        "The majority affirmed, but the dissent criticized Smith v. Wilkie, "
        "30 Vet.App. 123."
    )

    assert extract_negative_treatments(
        text, "Smith v. Wilkie", "30 Vet.App. 123"
    ) == []

    separate = extract_negative_treatments(
        text,
        "Smith v. Wilkie",
        "30 Vet.App. 123",
        opinion_role="separate",
    )
    assert [finding.treatment for finding in separate] == ["criticized"]


def test_extract_reaffirmance_signals_use_explicit_target_bound_language():
    text = (
        "The court reaffirmed Smith v. Wilkie, 30 Vet.App. 123. "
        "It also declined to overrule Smith v. Wilkie, 30 Vet.App. 123."
    )

    findings = extract_reaffirmance_signals(
        text, "Smith v. Wilkie", "30 Vet.App. 123"
    )

    assert [finding.treatment for finding in findings] == [
        "reaffirmed",
        "declined to overrule",
    ]
    assert all(finding.scope == "all" for finding in findings)
    assert all(finding.confidence == "high" for finding in findings)
    assert findings[0].excerpt == "The court reaffirmed Smith v. Wilkie, 30 Vet.App. 123."


def test_extract_reaffirmance_accepts_noun_forms():
    text = (
        "The court's reaffirmance of Smith v. Wilkie, 30 Vet.App. 123 controls. "
        "Its adherence to Smith v. Wilkie, 30 Vet.App. 123 is clear."
    )

    findings = extract_reaffirmance_signals(
        text, "Smith v. Wilkie", "30 Vet.App. 123"
    )

    assert [finding.treatment for finding in findings] == [
        "reaffirmed",
        "adhered to",
    ]


def test_extract_reaffirmance_rejects_competing_citation_and_dissent_attribution():
    competing = "The court reaffirmed Smith v. Wilkie, 30 Vet.App. 999."
    dissent = (
        "The majority affirmed, but the dissent reaffirmed Smith v. Wilkie, "
        "30 Vet.App. 123."
    )

    assert extract_reaffirmance_signals(
        competing, "Smith v. Wilkie", "30 Vet.App. 123"
    ) == []
    assert extract_reaffirmance_signals(
        dissent, "Smith v. Wilkie", "30 Vet.App. 123"
    ) == []


def test_extract_rejects_party_argument_and_hypothetical_holdings():
    argument = "Appellant argues that the court overruled Smith v. Wilkie, 30 Vet.App. 123."
    hypothetical = "The court would overrule Smith v. Wilkie, 30 Vet.App. 123."

    assert extract_negative_treatments(
        argument, "Smith v. Wilkie", "30 Vet.App. 123"
    ) == []
    assert extract_negative_treatments(
        hypothetical, "Smith v. Wilkie", "30 Vet.App. 123"
    ) == []


def test_extract_rejects_reported_and_party_attribution():
    attributed = (
        "Smith v. Wilkie was overruled, according to the dissent.",
        "According to the dissent, Smith v. Wilkie was overruled.",
        "Smith v. Wilkie was overruled, according to the Secretary.",
        "Smith v. Wilkie was overruled, according to the amicus.",
        "Smith v. Wilkie was overruled by the Secretary.",
        "Smith v. Wilkie was overruled, the Secretary concluded.",
        "Smith v. Wilkie was overruled, the dissent concluded.",
        "Smith v. Wilkie was overruled, the dissent says.",
    )
    party_subjects = (
        "The Secretary's decision overruled Smith v. Wilkie.",
        "The government's decision limited Smith v. Wilkie.",
        "The Secretary was overruled by Smith v. Wilkie.",
    )

    for text in (*attributed, *party_subjects):
        assert extract_negative_treatments(text, "Smith v. Wilkie", "") == []

    assert extract_negative_treatments(
        attributed[0], "Smith v. Wilkie", "", opinion_role="separate"
    ) == []


def test_extract_keeps_the_courts_own_holding():
    holdings = (
        "Smith v. Wilkie was overruled, according to the court.",
        "The court overruled Smith v. Wilkie because the dissent argues that the "
        "rule is wrong.",
        "The court overruled Smith v. Wilkie, and the dissent argues that the "
        "rule is wrong.",
        "The court overruled Smith v. Wilkie, rejecting an argument by the "
        "Secretary.",
    )

    for text in holdings:
        assert [
            finding.treatment
            for finding in extract_negative_treatments(text, "Smith v. Wilkie", "")
        ] == ["overruled"]


def test_extract_rejects_nonholding_framing():
    texts = (
        "The court rejected the notion that it had overruled Smith v. Wilkie.",
        "The court overruled the notion that Smith v. Wilkie was controlling.",
    )

    for text in texts:
        assert extract_negative_treatments(text, "Smith v. Wilkie", "") == []


def test_extract_rejects_whether_and_if_hypotheticals():
    hypotheticals = (
        "The court considered whether Smith v. Wilkie was overruled.",
        "The court asked if Smith v. Wilkie was overruled.",
        "The court considered whether to overrule Smith v. Wilkie.",
    )

    for text in hypotheticals:
        assert extract_negative_treatments(text, "Smith v. Wilkie", "") == []


def test_extract_reaffirmance_rejects_party_action():
    texts = (
        "The Secretary reaffirmed Smith v. Wilkie.",
        "The Secretary's brief reaffirmed Smith v. Wilkie.",
    )

    for text in texts:
        assert extract_reaffirmance_signals(text, "Smith v. Wilkie", "") == []


def test_extract_allows_the_courts_own_holding_after_other_actors():
    text = (
        "The court affirmed the Board and overruled Smith v. Wilkie, "
        "30 Vet.App. 123."
    )

    findings = extract_negative_treatments(
        text, "Smith v. Wilkie", "30 Vet.App. 123"
    )
    assert [finding.treatment for finding in findings] == ["overruled"]


def test_extract_binds_the_verb_to_the_nearest_case_reference():
    different_case = (
        "We overruled Jones v. McDonough, 31 Vet.App. 456, and followed "
        "Smith v. Wilkie, 30 Vet.App. 123."
    )
    target_case = (
        "We followed Jones v. McDonough, 31 Vet.App. 456, and overruled "
        "Smith v. Wilkie, 30 Vet.App. 123."
    )

    assert extract_negative_treatments(
        different_case, "Smith v. Wilkie", "30 Vet.App. 123"
    ) == []
    findings = extract_negative_treatments(
        target_case, "Smith v. Wilkie", "30 Vet.App. 123"
    )
    assert [finding.treatment for finding in findings] == ["overruled"]


def test_target_binding_rejects_an_interposed_citation_argument():
    sentence = (
        "The court overruled the argument that 30 Vet.App. 123 was wrong."
    )
    start = sentence.index("overruled")

    assert _target_binding(
        sentence,
        (start, start + len("overruled")),
        "Smith v. Wilkie",
        ("30 Vet.App. 123",),
    ) == (False, "", None)


def test_extract_rejects_a_competing_case_name_and_interposed_argument():
    competing_name = (
        "The court overruled Jones v. McDonough and followed Smith v. Wilkie."
    )
    interposed = (
        "The court overruled the argument that Smith v. Wilkie, 30 Vet.App. 123, "
        "was wrong."
    )
    interposed_citation = (
        "The court overruled the argument that 30 Vet.App. 123 was wrong."
    )
    different_object = (
        "The Secretary's decision was overruled, and Smith v. Wilkie was followed."
    )

    assert extract_negative_treatments(
        competing_name, "Smith v. Wilkie", ""
    ) == []
    assert extract_negative_treatments(
        interposed, "Smith v. Wilkie", "30 Vet.App. 123"
    ) == []
    assert extract_negative_treatments(
        interposed_citation, "Smith v. Wilkie", "30 Vet.App. 123"
    ) == []
    assert extract_negative_treatments(
        different_object, "Smith v. Wilkie", ""
    ) == []


def test_extract_requires_the_target_in_the_negative_verb_clause():
    text = (
        "Smith v. Wilkie is discussed, but the court overruled Jones v. McDonough."
    )

    assert extract_negative_treatments(text, "Smith v. Wilkie", "") == []


def test_extract_handles_clause_boundaries_and_duplicate_verbs():
    before_boundary = (
        "The court overruled Smith v. Wilkie, while the dissent would have "
        "affirmed."
    )
    duplicate_verbs = (
        "The court overruled and then overruled Smith v. Wilkie, 30 Vet.App. 123."
    )

    assert [
        finding.treatment
        for finding in extract_negative_treatments(
            before_boundary, "Smith v. Wilkie", ""
        )
    ] == ["overruled"]
    assert [
        finding.treatment
        for finding in extract_negative_treatments(
            duplicate_verbs, "Smith v. Wilkie", "30 Vet.App. 123"
        )
    ] == ["overruled"]


def test_span_distance_handles_order_and_overlap():
    assert _span_distance((0, 5), (7, 9)) == 2
    assert _span_distance((7, 9), (0, 5)) == 2
    assert _span_distance((0, 5), (3, 8)) == 0


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
        "cluster_resolved": False,
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
    assert check.confidence == "none"
    assert check.confidence_score == 0.0
    assert check.review_required is True
    assert any("target_unresolved" in reason for reason in check.review_reasons)
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
    assert check.confidence == "high"
    assert check.confidence_score == 1.0
    assert check.review_required is False
    assert check.review_reasons == []
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
    assert check.binding_negative_treatments == [treatment]
    assert check.persuasive_negative_treatments == []
    assert check.later_reaffirmance is False


def test_findings_carry_source_opinion_identifiers():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    provider.metadata[201] = _meta(cluster_id="888")
    provider.texts[201] = (
        "The court overruled Smith v. Wilkie, 30 Vet.App. 123 in part."
    )

    check = check_case(_case(), provider)

    treatment = check.negative_treatments[0]
    assert treatment.source_opinion_id == "201"
    assert treatment.source_cluster_id == "888"
    assert check.treatment_timeline[0].source_opinion_id == "201"
    assert check.treatment_timeline[0].source_cluster_id == "888"
    assert check.later_citing_opinions[0].cluster_id == "888"
    assert check.source_validity == []
    assert check.status == "negative"


def test_source_validity_signals_deduplicate_and_skip_missing_ids():
    findings = [
        NegativeTreatment(
            citing_case="Jones v. McDonough",
            source_opinion_id="201",
            source_cluster_id="888",
            treatment="overruled",
            excerpt="The court overruled Smith v. Wilkie.",
        ),
        NegativeTreatment(
            citing_case="Jones v. McDonough",
            source_opinion_id="201",
            source_cluster_id="",
            treatment="abrogated",
            excerpt="The court abrogated Smith v. Wilkie.",
        ),
        NegativeTreatment(
            citing_case="No Identifier v. McDonough",
            treatment="criticized",
            excerpt="The court criticized Smith v. Wilkie.",
        ),
    ]

    signals = _source_validity_signals(findings)

    assert len(signals) == 1
    assert signals[0].source_opinion_id == "201"
    assert signals[0].source_cluster_id == "888"


def test_source_validity_extraction_is_opt_in_and_advisory():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202])
    provider.metadata[201] = _meta(cluster_id="888")
    provider.texts[201] = "The court overruled Smith v. Wilkie, 30 Vet.App. 123."
    provider.metadata[202] = _meta(
        case="Dissent v. McDonough", opinion_type="040dissent", cluster_id="889"
    )
    provider.texts[202] = (
        "The dissent would overrule Smith v. Wilkie, 30 Vet.App. 123."
    )

    check = check_case(_case(), provider, source_validity=True)

    assert check.status == "negative"
    assert [
        (signal.source_opinion_id, signal.source_cluster_id)
        for signal in check.source_validity
    ] == [("201", "888"), ("202", "889")]
    assert all(
        signal.status == "not_assessed" for signal in check.source_validity
    )

    without = check_case(_case(), provider)
    assert without.source_validity == []
    assert without.status == check.status


def test_source_validity_opt_in_reuses_existing_review_calls():
    def wired_provider():
        provider = FakeProvider()
        provider.pages[None] = _page([201, 202])
        provider.metadata[201] = _meta(cluster_id="888")
        provider.texts[201] = (
            "The court overruled Smith v. Wilkie, 30 Vet.App. 123."
        )
        provider.metadata[202] = _meta(
            case="Dissent v. McDonough",
            opinion_type="040dissent",
            cluster_id="889",
        )
        provider.texts[202] = (
            "The dissent would overrule Smith v. Wilkie, 30 Vet.App. 123."
        )
        return provider

    plain_provider = wired_provider()
    opt_in_provider = wired_provider()

    without = check_case(_case(), plain_provider)
    with_source_validity = check_case(
        _case(), opt_in_provider, source_validity=True
    )

    assert opt_in_provider.lookup_calls == plain_provider.lookup_calls
    assert opt_in_provider.edge_calls == plain_provider.edge_calls
    assert opt_in_provider.metadata_calls == plain_provider.metadata_calls
    assert opt_in_provider.text_calls == plain_provider.text_calls
    assert (
        opt_in_provider.same_docket_calls
        == plain_provider.same_docket_calls
    )
    assert with_source_validity.status == without.status
    assert with_source_validity.good_law_as_of == without.good_law_as_of
    assert with_source_validity.coverage == without.coverage
    assert with_source_validity.flags == without.flags
    assert with_source_validity.source_validity


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
    assert check.binding_negative_treatments == []
    assert [t.binding for t in check.persuasive_negative_treatments] == [False]
    assert [entry.binding_class for entry in check.treatment_timeline] == [
        "persuasive",
        "unknown",
    ]


def test_timeline_splits_binding_and_persuasive_and_flags_later_reaffirmance():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202, 203])
    provider.metadata[201] = _meta(
        case="Alpha v. McDonough", date="2020-01-01", court=COURT_CAVC
    )
    provider.texts[201] = "The court overruled Smith v. Wilkie, 30 Vet.App. 123."
    provider.metadata[202] = _meta(
        case="Beta v. McDonough", date="2021-01-01", court=COURT_CAVC
    )
    provider.texts[202] = "The court reaffirmed Smith v. Wilkie, 30 Vet.App. 123."
    provider.metadata[203] = _meta(
        case="Gamma v. McDonough", date="2019-01-01", court=COURT_BVA
    )
    provider.texts[203] = "The Board overruled Smith v. Wilkie, 30 Vet.App. 123."

    check = check_case(_case(), provider)

    assert check.status == "negative"
    assert check.coverage.complete is True
    assert len(check.negative_treatments) == 2
    assert [t.citing_case for t in check.binding_negative_treatments] == [
        "Alpha v. McDonough"
    ]
    assert [t.citing_case for t in check.persuasive_negative_treatments] == [
        "Gamma v. McDonough"
    ]
    assert [entry.signal for entry in check.treatment_timeline] == [
        "negative",
        "negative",
        "reaffirmed",
    ]
    assert [entry.citing_date for entry in check.treatment_timeline] == [
        "2019-01-01",
        "2020-01-01",
        "2021-01-01",
    ]
    assert [entry.binding_class for entry in check.treatment_timeline] == [
        "persuasive",
        "binding",
        "binding",
    ]
    assert check.treatment_timeline[-1].later_than_negative is True
    assert check.later_reaffirmance is True


def test_gated_reaffirmance_is_in_the_timeline_but_is_not_later_reaffirmance():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202])
    provider.metadata[201] = _meta(date="2020-01-01")
    provider.texts[201] = "The court overruled Smith v. Wilkie, 30 Vet.App. 123."
    provider.metadata[202] = _meta(
        case="Dissent v. McDonough",
        date="2021-01-01",
        opinion_type="040dissent",
    )
    provider.texts[202] = (
        "The dissent reaffirmed Smith v. Wilkie, 30 Vet.App. 123."
    )

    check = check_case(_case(), provider)

    assert check.status == "negative"
    assert len(check.treatment_timeline) == 2
    assert check.treatment_timeline[-1].signal == "reaffirmed"
    assert check.treatment_timeline[-1].authoritative is False
    assert check.treatment_timeline[-1].later_than_negative is True
    assert check.later_reaffirmance is False


def test_reaffirmance_before_the_latest_negative_is_not_later():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202])
    provider.metadata[201] = _meta(date="2020-01-01")
    provider.texts[201] = "The court overruled Smith v. Wilkie, 30 Vet.App. 123."
    provider.metadata[202] = _meta(
        case="Beta v. McDonough", date="2019-01-01"
    )
    provider.texts[202] = "The court reaffirmed Smith v. Wilkie, 30 Vet.App. 123."

    check = check_case(_case(), provider)

    assert check.status == "negative"
    assert [entry.signal for entry in check.treatment_timeline] == [
        "reaffirmed",
        "negative",
    ]
    assert check.treatment_timeline[0].later_than_negative is False
    assert check.later_reaffirmance is False


def test_reaffirmance_without_negative_preserves_good_law():
    provider = FakeProvider()
    provider.pages[None] = _page([201])
    provider.metadata[201] = _meta(date="2020-01-01")
    provider.texts[201] = "The court reaffirmed Smith v. Wilkie, 30 Vet.App. 123."

    check = check_case(_case(), provider)

    assert check.status == "good_law"
    assert check.good_law_as_of == "2020-01-01"
    assert check.negative_treatments == []
    assert [entry.signal for entry in check.treatment_timeline] == ["reaffirmed"]
    assert check.treatment_timeline[0].later_than_negative is False
    assert check.later_reaffirmance is False


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


def test_priority_pool_scales_with_the_citing_review_and_page_caps():
    assert _citing_priority_page_size(1) == 20
    assert _citing_priority_page_size(5) == 20
    assert _citing_priority_page_size(10) == 40
    assert _citing_priority_page_size(50) == 100
    assert _citing_priority_pool_size(1, 1) == 20
    assert _citing_priority_pool_size(5, 2) == 40
    assert _citing_priority_pool_size(50, 10) == 100


def test_multi_opinion_cluster_beyond_the_pool_reports_unattempted_opinion():
    provider = FakeProvider()
    opinion_ids = [111, *range(401, 421)]
    provider.metadata[111] = {
        **_target_meta(111),
        "opinion_ids": opinion_ids,
    }
    for opinion_id in opinion_ids:
        provider.opinion_pages[(opinion_id, None)] = _page([201])
    _wire_good_citing_opinions(provider, 201)

    check = check_case(_case(), provider, max_citing_opinions=1)

    assert [call[0] for call in provider.edge_calls] == opinion_ids[:20]
    assert check.coverage.forward_citations_found == 1
    assert check.coverage.truncated is True
    assert check.coverage.complete is False
    assert "page_cap" in _gap_codes(check)
    assert any(
        "no allocation" in gap.detail for gap in check.coverage.gaps
    )


def test_multi_opinion_cluster_shares_one_candidate_pool():
    provider = FakeProvider()
    provider.metadata[111] = {
        **_target_meta(111),
        "opinion_ids": [111, 112],
    }
    first_ids = list(range(201, 221))
    second_ids = list(range(301, 321))
    provider.opinion_pages[(111, None)] = _page(first_ids)
    provider.opinion_pages[(112, None)] = _page(second_ids)
    _wire_good_citing_opinions(provider, *first_ids, *second_ids)

    check = check_case(_case(), provider, max_citing_opinions=1)

    assert provider.edge_calls == [(111, 10, None), (112, 10, None)]
    assert [
        call for call in provider.metadata_calls if call not in (111, 112)
    ] == [*first_ids[:10], *second_ids[:10]]
    assert check.coverage.forward_citations_found == 20
    assert check.coverage.pages_fetched == 2
    assert check.coverage.citing_opinions_reviewed == 1
    assert check.coverage.truncated is True
    assert check.coverage.complete is False
    assert "count_mismatch" in _gap_codes(check)
    assert "citing_opinion_cap" in _gap_codes(check)
    assert check.status == "unknown"


def test_citing_cap_prioritizes_later_citing_opinions():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202, 203, 204], count=4)
    _wire_good_citing_opinions(provider, 201, 202, 203, 204)
    provider.metadata[201] = _meta(
        case="Older BVA", date="2022-01-01", court=COURT_BVA
    )
    provider.metadata[202] = _meta(
        case="Older CAVC", date="2020-01-01", court=COURT_CAVC
    )
    provider.metadata[203] = _meta(
        case="Newer CAVC", date="2023-01-01", court=COURT_CAVC
    )
    provider.metadata[204] = _meta(
        case="Older Supreme", date="2021-01-01", court=COURT_SUPREME
    )

    check = check_case(_case(), provider, max_citing_opinions=2)

    assert check.coverage.citing_opinions_reviewed == 2
    assert provider.text_calls == [203, 201]
    assert [cited.opinion_id for cited in check.later_citing_opinions] == [
        "201",
        "203",
    ]
    assert check.coverage.complete is False
    assert check.status == "unknown"
    assert _gap_codes(check) == ["citing_opinion_cap"]
    assert "prioritized by decision date" in check.coverage.gaps[0].detail
    assert check.confidence == "medium"
    assert check.confidence_score == 0.85
    assert check.review_required is True
    assert any("citing_opinion_cap" in reason for reason in check.review_reasons)


def test_citing_cap_uses_court_authority_to_break_date_ties():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202, 203], count=3)
    _wire_good_citing_opinions(provider, 201, 202, 203)
    provider.metadata[201] = _meta(date="2020-01-01", court=COURT_BVA)
    provider.metadata[202] = _meta(date="2020-01-01", court=COURT_CAVC)
    provider.metadata[203] = _meta(date="2020-01-01", court=COURT_SUPREME)

    check = check_case(_case(), provider, max_citing_opinions=2)

    assert provider.text_calls == [203, 202]
    assert [cited.opinion_id for cited in check.later_citing_opinions] == [
        "202",
        "203",
    ]
    assert check.status == "unknown"


def test_adaptive_pagination_reaches_a_later_opinion_on_the_next_page():
    provider = FakeProvider()
    provider.pages[None] = _page(
        [201, 202], count=3, next_url="?cursor=next-page"
    )
    provider.pages["next-page"] = _page([203], count=3)
    _wire_good_citing_opinions(provider, 201, 202, 203)
    provider.metadata[201] = _meta(date="2019-01-01")
    provider.metadata[202] = _meta(date="2020-01-01")
    provider.metadata[203] = _meta(date="2021-01-01")

    check = check_case(_case(), provider, max_citing_opinions=1, max_pages=2)

    assert check.coverage.pages_fetched == 2
    assert check.coverage.citing_opinions_reviewed == 1
    assert provider.text_calls == [203]
    assert [cited.opinion_id for cited in check.later_citing_opinions] == ["203"]
    assert _gap_codes(check) == ["citing_opinion_cap"]
    assert provider.edge_calls == [
        (111, 20, None),
        (111, 20, "next-page"),
    ]
    assert check.status == "unknown"


def test_adaptive_pagination_does_not_stop_after_a_full_first_page():
    provider = FakeProvider()
    older_ids = list(range(201, 221))
    provider.pages[None] = _page(
        older_ids, count=21, next_url="?cursor=next-page"
    )
    provider.pages["next-page"] = _page([221], count=21)
    _wire_good_citing_opinions(provider, *older_ids, 221)
    for opinion_id in older_ids:
        provider.metadata[opinion_id] = _meta(date="2020-01-01")
    provider.metadata[221] = _meta(date="2024-01-01")

    check = check_case(_case(), provider, max_citing_opinions=1, max_pages=2)

    assert check.coverage.pages_fetched == 2
    assert provider.text_calls == [221]
    assert [cited.opinion_id for cited in check.later_citing_opinions] == ["221"]
    assert check.status == "unknown"


def test_adaptive_pagination_stops_when_the_priority_pool_is_full():
    provider = FakeProvider()
    citing_ids = list(range(201, 241))
    provider.pages[None] = _page(
        citing_ids, count=41, next_url="?cursor=next-page"
    )
    _wire_good_citing_opinions(provider, *citing_ids)

    check = check_case(_case(), provider, max_citing_opinions=1, max_pages=2)

    assert provider.edge_calls == [(111, 20, None)]
    assert check.coverage.pages_fetched == 1
    assert check.coverage.forward_citations_found == 41
    assert check.coverage.citing_opinions_reviewed == 1
    assert check.coverage.truncated is True
    assert check.coverage.complete is False
    assert "page_cap" in _gap_codes(check)
    assert "count_mismatch" in _gap_codes(check)
    assert "priority pool" in check.coverage.gaps[0].detail
    assert check.status == "unknown"


def test_batch_metadata_avoids_per_candidate_lookups():
    provider = BatchFakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    _wire_good_citing_opinions(provider, 201, 202)
    provider.metadata[201] = _meta(date="2022-01-01")
    provider.metadata[202] = _meta(date="2021-01-01")

    check = check_case(_case(), provider, max_citing_opinions=1)

    assert provider.batch_calls == [[201, 202]]
    assert [call for call in provider.metadata_calls if call != 111] == []
    assert provider.text_calls == [201]
    assert check.coverage.citing_opinions_reviewed == 1
    assert not any(
        code.startswith("citing_metadata") for code in _gap_codes(check)
    )


def test_batch_metadata_failure_falls_back_and_preserves_metadata_gaps():
    provider = BatchFakeProvider()
    provider.pages[None] = _page([201, 202, 203], count=3)
    _wire_good_citing_opinions(provider, 201, 202, 203)
    provider.batch_error = SearchError("batch down")
    provider.metadata[201] = None
    provider.metadata_errors[202] = SearchError("metadata down")

    check = check_case(_case(), provider)

    assert provider.batch_calls == [[201, 202, 203]]
    assert [call for call in provider.metadata_calls if call != 111] == [
        201,
        202,
        203,
    ]
    assert _gap_codes(check) == [
        "citing_metadata_unavailable",
        "citing_metadata_fetch_failed",
    ]
    assert check.coverage.complete is False
    assert check.status == "unknown"


def test_batch_metadata_partial_result_falls_back_for_missing_candidates():
    provider = BatchFakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    _wire_good_citing_opinions(provider, 201, 202)
    provider.batch_result = {"201": {}, "bad": "ignored", 202: "also ignored"}

    check = check_case(_case(), provider)

    assert provider.batch_calls == [[201, 202]]
    assert [call for call in provider.metadata_calls if call != 111] == [
        201,
        202,
    ]
    assert not any(
        code.startswith("citing_metadata") for code in _gap_codes(check)
    )
    assert check.coverage.citing_opinions_reviewed == 2


def test_batch_metadata_non_mapping_result_falls_back():
    provider = BatchFakeProvider()
    provider.pages[None] = _page([201], count=1)
    _wire_good_citing_opinions(provider, 201)
    provider.batch_result = []

    check = check_case(_case(), provider)

    assert provider.batch_calls == [[201]]
    assert [call for call in provider.metadata_calls if call != 111] == [201]
    assert _gap_codes(check) == []
    assert check.coverage.complete is True
    assert check.status == "good_law"


def test_metadata_failure_does_not_consume_the_review_cap():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    _wire_good_citing_opinions(provider, 201, 202)
    provider.metadata[201] = _meta(date="2022-01-01")
    provider.metadata_errors[201] = SearchError("metadata down")
    provider.metadata[202] = _meta(date="2021-01-01")

    check = check_case(_case(), provider, max_citing_opinions=1)

    assert [call for call in provider.metadata_calls if call != 111] == [201, 202]
    assert provider.text_calls == [202]
    assert check.coverage.citing_opinions_reviewed == 1
    assert check.coverage.complete is False
    assert _gap_codes(check) == [
        "citing_metadata_fetch_failed",
        "citing_opinion_cap",
    ]
    assert [cited.opinion_id for cited in check.later_citing_opinions] == ["202"]
    assert check.status == "unknown"
    assert check.confidence == "low"
    assert check.confidence_score == 0.55
    assert check.review_required is True
    assert any(
        "metadata failure [citing_metadata_fetch_failed]" in reason
        for reason in check.review_reasons
    )


def test_metadata_failure_alone_lowers_confidence_and_requires_review():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    provider.metadata[201] = None
    _wire_good_citing_opinions(provider, 202)

    check = check_case(_case(), provider)

    assert check.coverage.complete is False
    assert check.confidence == "medium"
    assert check.confidence_score == 0.75
    assert check.review_required is True
    assert check.review_reasons == [
        "metadata failure [citing_metadata_unavailable]: Citator metadata "
        "was unavailable for citing opinion 201."
    ]
    assert "1 coverage gap(s), including 1 metadata failure(s)" in (
        check.confidence_reason
    )


def test_ambiguous_attribution_lowers_confidence_without_changing_status():
    provider = FakeProvider()
    provider.pages[None] = _page([201], count=1)
    provider.metadata[201] = _meta()
    provider.texts[201] = (
        "The court criticized Smith v. Wilkie for its reasoning."
    )

    check = check_case(_case(), provider)

    assert check.status == "negative"
    assert check.coverage.complete is True
    assert check.negative_treatments[0].attribution_ambiguous is True
    assert check.confidence == "medium"
    assert check.confidence_score == 0.9
    assert check.review_required is True
    assert any(
        "ambiguous attribution [criticized" in reason
        for reason in check.review_reasons
    )
    assert any(
        "target matched by case name only" in reason
        for reason in check.review_reasons
    )
    assert "0 coverage gap(s)" in check.confidence_reason
    assert "1 ambiguous attribution(s)" in check.confidence_reason


def test_text_failure_does_not_consume_the_review_cap():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    _wire_good_citing_opinions(provider, 201, 202)
    provider.metadata[201] = _meta(date="2022-01-01")
    provider.text_errors[201] = SearchError("text down")
    provider.metadata[202] = _meta(date="2021-01-01")

    check = check_case(_case(), provider, max_citing_opinions=1)

    assert provider.text_calls == [201, 202]
    assert check.coverage.citing_opinions_reviewed == 1
    assert check.coverage.complete is False
    assert _gap_codes(check) == [
        "citing_text_fetch_failed",
        "citing_opinion_cap",
    ]
    assert [cited.opinion_id for cited in check.later_citing_opinions] == ["202"]
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
    assert check.confidence == "none"
    assert check.confidence_score == 0.0
    assert check.review_required is True
    assert any("no_forward_citations" in reason for reason in check.review_reasons)
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
    assert "reviewed citing opinion has no decision date" in (
        check.coverage.temporal_confidence_reason
    )
    assert check.good_law_as_of == ""
    assert check.confidence == "medium"
    assert check.confidence_score == 0.85
    assert check.review_required is True
    assert any("citing_date_missing" in reason for reason in check.review_reasons)


def test_as_of_date_reviews_only_through_cutoff_and_reports_later_treatment():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    provider.metadata[201] = _meta(
        case="On Cutoff v. McDonough", date="2021-01-01"
    )
    provider.texts[201] = (
        "The court followed Smith v. Wilkie, 30 Vet.App. 123."
    )
    provider.metadata[202] = _meta(
        case="Later v. McDonough", date="2022-03-03"
    )
    provider.texts[202] = (
        "The court overruled Smith v. Wilkie, 30 Vet.App. 123."
    )

    check = check_case(_case(), provider, as_of_date="2021-01-01")

    assert check.as_of_date == "2021-01-01"
    assert check.status == "good_law"
    assert check.good_law_as_of == "2021-01-01"
    assert check.coverage.forward_citations_found == 2
    assert check.coverage.citing_opinions_reviewed == 1
    assert check.coverage.latest_citing_date == "2021-01-01"
    assert check.coverage.complete is True
    assert check.coverage.truncated is False
    assert [cited.opinion_id for cited in check.later_citing_opinions] == [
        "201"
    ]
    assert [cited.opinion_id for cited in check.post_as_of_citing_opinions] == [
        "202"
    ]
    assert check.negative_treatments == []
    assert check.gated_treatment_findings == []
    assert check.binding_negative_treatments == []
    assert check.persuasive_negative_treatments == []
    assert check.treatment_timeline == []
    assert check.later_reaffirmance is False
    assert [
        finding.treatment for finding in check.post_as_of_treatment_findings
    ] == ["overruled"]
    assert check.post_as_of_treatment_findings[0].authoritative is True
    assert check.post_as_of_incomplete is False
    assert check.confidence == "high"
    assert check.confidence_score == 1.0
    assert check.review_required is False
    assert provider.text_calls == [201, 202]


def test_as_of_date_missing_citing_date_is_excluded_and_incomplete():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    provider.metadata[201] = _meta(date="2020-02-02")
    provider.texts[201] = (
        "The court followed Smith v. Wilkie, 30 Vet.App. 123."
    )
    provider.metadata[202] = _meta(case="Undated v. McDonough", date="")
    provider.texts[202] = (
        "The court overruled Smith v. Wilkie, 30 Vet.App. 123."
    )

    check = check_case(_case(), provider, as_of_date="2021-01-01")

    assert check.status == "unknown"
    assert check.good_law_as_of == ""
    assert check.coverage.citing_opinions_reviewed == 1
    assert check.coverage.complete is False
    assert "citing_date_missing" in _gap_codes(check)
    assert check.coverage.temporal_confidence == "low"
    assert "excluded from the as-of result" in (
        check.coverage.temporal_confidence_reason
    )
    assert "no usable decision date" in (
        check.coverage.temporal_confidence_reason
    )
    assert [cited.opinion_id for cited in check.undated_citing_opinions] == [
        "202"
    ]
    assert check.post_as_of_citing_opinions == []
    assert check.post_as_of_treatment_findings == []
    assert check.confidence == "medium"
    assert check.confidence_score == 0.85
    assert check.review_required is True
    assert provider.text_calls == [201]


def test_as_of_date_in_scope_cap_remains_incomplete():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    _wire_good_citing_opinions(provider, 201, 202)
    provider.metadata[201] = _meta(date="2019-01-01")
    provider.metadata[202] = _meta(date="2020-01-01")

    check = check_case(
        _case(),
        provider,
        max_citing_opinions=1,
        as_of_date="2021-01-01",
    )

    assert check.status == "unknown"
    assert check.good_law_as_of == ""
    assert check.coverage.complete is False
    assert check.coverage.truncated is True
    assert check.coverage.citing_opinions_reviewed == 1
    assert "citing_opinion_cap" in _gap_codes(check)
    assert check.confidence == "medium"
    assert check.confidence_score == 0.85
    assert check.review_required is True
    assert provider.text_calls == [202]


def test_as_of_post_cutoff_review_failure_is_flagged_without_invalidating_as_of_result():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    provider.metadata[201] = _meta(date="2020-01-01")
    provider.texts[201] = (
        "The court followed Smith v. Wilkie, 30 Vet.App. 123."
    )
    provider.metadata[202] = _meta(
        case="Unread Later v. McDonough", date="2022-01-01"
    )

    check = check_case(_case(), provider, as_of_date="2021-01-01")

    assert check.status == "good_law"
    assert check.good_law_as_of == "2020-01-01"
    assert check.coverage.complete is True
    assert check.post_as_of_citing_opinions == []
    assert check.post_as_of_incomplete is True
    assert any("text was empty" in flag for flag in check.post_as_of_flags)
    assert provider.text_calls == [201, 202]


def test_as_of_post_cutoff_cap_is_reported_without_invalidating_as_of_result():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202, 203], count=3)
    provider.metadata[201] = _meta(date="2020-01-01")
    provider.texts[201] = (
        "The court followed Smith v. Wilkie, 30 Vet.App. 123."
    )
    provider.metadata[202] = _meta(
        case="Later Overruling v. McDonough", date="2022-01-01"
    )
    provider.texts[202] = (
        "The court overruled Smith v. Wilkie, 30 Vet.App. 123."
    )
    provider.metadata[203] = _meta(
        case="Latest v. McDonough", date="2023-01-01"
    )
    provider.texts[203] = (
        "The court followed Smith v. Wilkie, 30 Vet.App. 123."
    )

    check = check_case(
        _case(),
        provider,
        max_citing_opinions=1,
        as_of_date="2021-01-01",
    )

    assert check.status == "good_law"
    assert check.good_law_as_of == "2020-01-01"
    assert check.coverage.complete is True
    assert check.post_as_of_incomplete is True
    assert [
        cited.opinion_id for cited in check.post_as_of_citing_opinions
    ] == ["203"]
    assert check.post_as_of_treatment_findings == []
    assert check.confidence == "high"
    assert check.confidence_score == 1.0
    assert check.review_required is False
    assert any(
        "1 of 2 citing opinions" in flag
        for flag in check.post_as_of_flags
    )
    assert provider.text_calls == [201, 203]


def test_without_as_of_date_reviews_all_citing_opinions_unchanged():
    provider = FakeProvider()
    provider.pages[None] = _page([201, 202], count=2)
    _wire_good_citing_opinions(provider, 201, 202)
    provider.metadata[201] = _meta(date="2019-01-01")
    provider.metadata[202] = _meta(date="2020-01-01")

    check = check_case(_case(), provider)

    assert check.as_of_date == ""
    assert check.post_as_of_citing_opinions == []
    assert check.post_as_of_treatment_findings == []
    assert check.undated_citing_opinions == []
    assert check.coverage.complete is True
    assert check.coverage.citing_opinions_reviewed == 2
    assert [cited.opinion_id for cited in check.later_citing_opinions] == [
        "201",
        "202",
    ]


def test_as_of_date_helpers_handle_optional_and_malformed_dates():
    assert _normalize_as_of_date(None) == ""
    assert _normalize_as_of_date(" 2021-01-01 ") == "2021-01-01"
    cutoff = date(2021, 1, 1)
    assert _candidate_date_relation("", cutoff) == "undated"
    assert _candidate_date_relation("not-a-date", cutoff) == "undated"
    assert _candidate_date_relation("2021-01-01", cutoff) == "in_scope"
    assert _candidate_date_relation("2022-01-01", cutoff) == "post_as_of"


def test_as_of_date_rejects_a_non_iso_value_before_lookup():
    provider = FakeProvider()

    with pytest.raises(ValueError, match="ISO date"):
        check_case(_case(), provider, as_of_date="01/02/2021")

    assert provider.lookup_calls == []


def test_run_citator_passes_as_of_date_to_each_check():
    provider = FakeProvider()
    provider.pages[None] = _page([201], count=1)
    _wire_good_citing_opinions(provider, 201)

    checks = run_citator(
        [_case()], provider, max_cases=1, as_of_date="2021-01-01"
    )

    assert checks[0].as_of_date == "2021-01-01"
    assert checks[0].status == "good_law"


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
