"""Rendering tests for the citator report fields."""

import csv
import io
import json

from va_legal_agent.__main__ import render_analysis
from va_legal_agent.models import (
    AppellateHistoryCandidate,
    AppellateHistoryCheck,
    AppellateHistoryRecord,
    CitatorCheck,
    CitatorCoverage,
    CitatorCoverageGap,
    LaterCitingOpinion,
    LegalAnalysis,
    NegativeTreatment,
    SameDocketDecision,
)


def _analysis(check: CitatorCheck) -> LegalAnalysis:
    return LegalAnalysis(
        issue="service connection",
        summary="summary",
        how_it_affects_va_claims="effect",
        citator_checks=[check],
        citator_flags=["Citator coverage is incomplete."],
    )


def _check() -> CitatorCheck:
    return CitatorCheck(
        target_case="Smith v. Wilkie",
        target_citation="30 Vet.App. 123",
        target_court="Court of Appeals for Veterans Claims",
        target_decision_date="2018-01-01",
        target_opinion_id="111",
        target_url="https://www.courtlistener.com/opinion/111/",
        target_cluster_id="777",
        target_citations=["30 Vet.App. 123", "2020 WL 123456"],
        target_opinion_ids=["111", "112"],
        status="negative",
        checked_at="2026-01-01T00:00:00+00:00",
        coverage=CitatorCoverage(
            forward_citations_found=3,
            citing_opinions_reviewed=2,
            pages_fetched=1,
            complete=False,
            truncated=True,
            latest_citing_date="2021-03-03",
        ),
        same_docket_decisions=[
            SameDocketDecision(
                case_name="Smith v. McDonough",
                citation="32 Vet.App. 9",
                court="Court of Appeals for Veterans Claims",
                decision_date="2019-01-01",
                url="https://www.courtlistener.com/opinion/999/",
                cluster_id="999",
            )
        ],
        later_citing_opinions=[
            LaterCitingOpinion(
                case_name="Alpha v. McDonough",
                citation="33 Vet.App. 10",
                court="Court of Appeals for Veterans Claims",
                decision_date="2019-01-01",
                url="https://www.courtlistener.com/opinion/201/",
                opinion_id="201",
                opinion_type="020lead",
                opinion_role="majority",
                precedential_status="Published",
                precedential_tier="precedential",
                treatment_eligible=True,
            )
        ],
        negative_treatments=[
            NegativeTreatment(
                citing_case="Alpha v. McDonough",
                citing_url="https://www.courtlistener.com/opinion/201/",
                citing_date="2019-01-01",
                treatment="overruled",
                scope="all",
                binding=True,
                confidence="high",
                opinion_type="020lead",
                opinion_role="majority",
                precedential_status="Published",
                precedential_tier="precedential",
                excerpt="The court overruled Smith v. Wilkie.",
            ),
            NegativeTreatment(
                citing_case="Beta v. McDonough",
                citing_date="",
                treatment="limited",
                scope="unclear",
                binding=False,
                confidence="medium",
                excerpt="The court limited Smith v. Wilkie to its facts.",
            ),
            NegativeTreatment(
                citing_case="Gamma v. McDonough",
                treatment="questioned",
                binding=None,
                excerpt="The court questioned Smith v. Wilkie.",
            ),
        ],
        gated_treatment_findings=[
            NegativeTreatment(
                citing_case="Delta v. McDonough",
                citing_url="https://www.courtlistener.com/opinion/203/",
                citing_date="2020-05-05",
                treatment="overruled",
                scope="all",
                binding=False,
                confidence="medium",
                opinion_type="040dissent",
                opinion_role="separate",
                precedential_status="Unpublished",
                precedential_tier="nonprecedential",
                authoritative=False,
                gate_reason=(
                    "opinion role is separate; "
                    "precedential status is nonprecedential"
                ),
                excerpt="The dissent would overrule Smith v. Wilkie.",
            )
        ],
        flags=["Citator coverage for 'Smith v. Wilkie' is incomplete."],
    )


def test_citator_text_includes_every_exact_excerpt_and_coverage():
    text = render_analysis(_analysis(_check()), "text")

    assert "Citator checks:" in text
    assert (
        "good_law means only no authoritative negative treatment in reviewed "
        "citing opinions" in text
    )
    assert (
        "Subsequent history is not inferred from citation signals: it is "
        "reported only as candidates or as a human-verified record." in text
    )
    assert "Smith v. Wilkie (30 Vet.App. 123): negative" in text
    assert "Cluster: 777; parallel citations: 30 Vet.App. 123 | 2020 WL 123456" in text
    assert "opinions: 111, 112" in text
    assert "authoritative negative treatment in reviewed citing opinions" in text
    assert "good-law cutoff: not established" in text
    assert "reviewed 2/3 citing opinions" in text
    assert "coverage complete=no" in text
    assert "subsequent history: not assessed" in text
    assert "Same-docket decisions (advisory context only; not appellate history):" in text
    assert "Later citing opinions reviewed (advisory only; not appellate history or treatment):" in text
    assert "Smith v. McDonough" in text
    assert "Alpha v. McDonough" in text
    assert "Authoritative negative treatment findings:" in text
    assert "Gated treatment findings (not counted toward status):" in text
    assert "The dissent would overrule Smith v. Wilkie." in text
    assert "opinion role is separate" in text
    assert 'The court overruled Smith v. Wilkie.' in text
    assert "binding" in text
    assert "not binding" in text
    assert "binding status unknown" in text
    assert "Citator flags:" in text
    assert "Appellate history: not assessed (history layer did not run)" in text


def test_citator_text_renders_history_candidates_and_verified_records():
    record = AppellateHistoryRecord(
        target_cluster_id="777",
        candidate_case="Smith v. McDonough",
        candidate_cluster_id="999",
        relationship="direct_appeal",
        disposition="affirmed",
        scope="all",
        source_quality="official",
        source_url="https://www.cafc.uscourts.gov/opinions/1.pdf",
        reviewer="Reviewer",
        reviewed_at="2026-01-02T03:04:05+00:00",
        rationale="Checked the docket and disposition.",
    )
    history = AppellateHistoryCheck(
        target_case="Smith v. Wilkie",
        target_cluster_id="777",
        target_citations=["30 Vet.App. 123"],
        status="verified",
        candidates=[
            AppellateHistoryCandidate(
                candidate_case="Smith v. McDonough",
                citation="32 Vet.App. 9",
                decision_date="2019-01-01",
                signals=["same_docket"],
                match_reasons=["appears on the target's docket"],
                verification_status="verified",
                relationship="direct_appeal",
                disposition="affirmed",
                scope="all",
                source_quality="official",
                source_url="https://www.cafc.uscourts.gov/opinions/1.pdf",
                reviewer="Reviewer",
                reviewed_at="2026-01-02T03:04:05+00:00",
            ),
            AppellateHistoryCandidate(
                candidate_case="Other v. McDonough",
                signal_type="later_citing_opinion",
                verification_status="candidate",
            ),
        ],
        verified_history=[record],
        flags=["Appellate history has one candidate pending review."],
    )
    check = _check().model_copy(
        update={
            "appellate_history": history,
            "subsequent_history_assessed": True,
        }
    )

    text = render_analysis(_analysis(check), "text")

    assert "Appellate history: verified (human-verified record available)" in text
    assert "subsequent history: human-verified" in text
    assert "History candidates requiring verification" in text
    assert "[verified; same_docket] Smith v. McDonough" in text
    assert "[candidate; later_citing_opinion] Other v. McDonough" in text
    assert "human review: relationship=direct_appeal" in text
    assert "Verified appellate history:" in text
    assert "rationale: Checked the docket and disposition." in text
    assert "History note: Appellate history has one candidate pending review." in text


def test_citator_text_renders_not_assessed_history_without_candidates():
    history = AppellateHistoryCheck(
        target_case="Smith v. Wilkie",
        status="not_assessed",
        flags=["No candidate signals were available."],
    )
    check = _check().model_copy(update={"appellate_history": history})

    text = render_analysis(_analysis(check), "text")

    assert "Appellate history: not_assessed" in text
    assert "History candidates requiring verification" not in text
    assert "Verified appellate history:" not in text
    assert "History note: No candidate signals were available." in text


def test_citator_text_renders_structured_coverage_gaps_and_temporal_confidence():
    check = _check().model_copy(
        update={
            "coverage": CitatorCoverage(
                forward_citations_found=3,
                citing_opinions_reviewed=2,
                pages_fetched=1,
                complete=False,
                truncated=True,
                latest_citing_date="2021-03-03",
                gaps=[
                    CitatorCoverageGap(
                        code="page_cap",
                        detail="Forward-citation fetch hit the 1-page cap.",
                    ),
                    CitatorCoverageGap(
                        code="citing_opinion_cap",
                        detail="Reviewed 2 of 3 citing opinions.",
                    ),
                ],
                temporal_confidence="medium",
                temporal_confidence_reason=(
                    "Latest reviewed citing opinion is 2021-03-03; coverage is "
                    "incomplete, so later citing opinions may exist."
                ),
            )
        }
    )

    text = render_analysis(_analysis(check), "text")

    assert "Coverage gaps:" in text
    assert "- page_cap: Forward-citation fetch hit the 1-page cap." in text
    assert "- citing_opinion_cap: Reviewed 2 of 3 citing opinions." in text
    assert "Temporal confidence: medium" in text
    assert "later citing opinions may exist" in text


def test_citator_text_omits_cluster_block_without_resolution_metadata():
    check = _check().model_copy(
        update={
            "target_cluster_id": "",
            "target_citations": [],
            "target_opinion_ids": [],
        }
    )

    text = render_analysis(_analysis(check), "text")

    assert "Cluster:" not in text


def test_good_law_text_states_the_reviewed_citation_scope():
    check = _check().model_copy(
        update={
            "status": "good_law",
            "good_law_as_of": "2021-03-03",
            "negative_treatments": [],
            "gated_treatment_findings": [],
            "same_docket_decisions": [],
            "later_citing_opinions": [],
        }
    )

    text = render_analysis(_analysis(check), "text")

    assert (
        "good_law; no authoritative negative treatment in reviewed citing "
        "opinions" in text
    )
    assert "good-law cutoff: 2021-03-03" in text
    assert "subsequent history: not assessed" in text


def test_citator_json_and_csv_carry_structured_checks_and_excerpts():
    data = json.loads(render_analysis(_analysis(_check()), "json"))
    assert data["citator_checks"][0]["negative_treatments"][0]["excerpt"] == (
        "The court overruled Smith v. Wilkie."
    )
    assert data["citator_checks"][0]["subsequent_history_assessed"] is False
    assert "reviewed citing opinions" in data["citator_checks"][0]["good_law_meaning"]
    assert data["citator_checks"][0]["target_cluster_id"] == "777"
    assert data["citator_checks"][0]["target_citations"] == [
        "30 Vet.App. 123",
        "2020 WL 123456",
    ]
    assert data["citator_checks"][0]["target_opinion_ids"] == ["111", "112"]
    assert data["citator_checks"][0]["coverage"]["gaps"] == []
    assert data["citator_checks"][0]["coverage"]["temporal_confidence"] == "none"
    assert data["citator_checks"][0]["appellate_history"] is None
    assert data["citator_checks"][0]["same_docket_decisions"][0]["cluster_id"] == "999"
    assert data["citator_checks"][0]["later_citing_opinions"][0]["opinion_id"] == "201"
    assert data["citator_checks"][0]["later_citing_opinions"][0][
        "treatment_eligible"
    ] is True
    assert data["citator_checks"][0]["gated_treatment_findings"][0][
        "authoritative"
    ] is False
    assert "opinion role is separate" in data["citator_checks"][0][
        "gated_treatment_findings"
    ][0]["gate_reason"]
    assert data["citator_flags"] == ["Citator coverage is incomplete."]

    rows = list(csv.reader(io.StringIO(render_analysis(_analysis(_check()), "csv"))))
    row = dict(zip(rows[0], rows[1]))
    checks = json.loads(row["citator_checks"])
    assert checks[0]["negative_treatments"][1]["treatment"] == "limited"
    assert checks[0]["gated_treatment_findings"][0]["opinion_role"] == "separate"
    assert row["citator_flags"] == "Citator coverage is incomplete."


def test_gated_only_text_makes_clear_the_finding_is_not_counted():
    check = _check().model_copy(
        update={
            "status": "unknown",
            "negative_treatments": [],
            "gated_treatment_findings": [
                NegativeTreatment(
                    citing_case="Delta v. McDonough",
                    treatment="overruled",
                    scope="all",
                    opinion_role="separate",
                    precedential_status="Unpublished",
                    authoritative=False,
                    gate_reason="opinion role is separate",
                    excerpt="The dissent would overrule Smith v. Wilkie.",
                )
            ],
        }
    )

    text = render_analysis(_analysis(check), "text")

    assert "only gated negative treatment found; not counted" in text
    assert "Gated treatment findings (not counted toward status):" in text


def test_citator_text_renders_prioritized_authority_queue():
    from va_legal_agent.models import AuthorityQueueEntry

    check = _check()
    analysis = _analysis(check)
    analysis.authority_queue = [
        AuthorityQueueEntry(
            priority=1,
            case_title="Smith v. Wilkie",
            citation="30 Vet.App. 123",
            court="Court of Appeals for Veterans Claims",
            authority_weight=2,
            eligible=True,
            reasons=["authority tier 2", "precedential"],
        )
    ]

    text = render_analysis(analysis, "text")

    assert "Authority queue:" in text
    assert "Review order only; this queue does not change case ranking" in text
    assert "1. Smith v. Wilkie (30 Vet.App. 123;" in text
    assert "authority tier 2; precedential" in text


def test_citator_csv_carries_authority_queue():
    from va_legal_agent.models import AuthorityQueueEntry

    analysis = _analysis(_check())
    analysis.authority_queue = [
        AuthorityQueueEntry(priority=1, case_title="Smith v. Wilkie", eligible=True)
    ]

    rows = list(csv.DictReader(io.StringIO(render_analysis(analysis, "csv"))))
    queue = json.loads(rows[0]["authority_queue"])

    assert queue[0]["priority"] == 1
    assert queue[0]["case_title"] == "Smith v. Wilkie"
