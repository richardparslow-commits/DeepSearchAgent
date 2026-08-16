"""Tests for cognitive decomposition (va_legal_agent.planning)."""

from va_legal_agent.planning import (
    SEARCH,
    SYNTHESIZE,
    ResearchPlan,
    SubTask,
    decompose_issue,
    detect_issue_topics,
    plan_queries,
    refine_plan,
    relevant_statutes,
)


def test_detect_issue_topics_maps_intent_to_elements():
    assert detect_issue_topics("service connection for tinnitus") == ("service connection",)
    assert detect_issue_topics("rating increase for my back") == ("rating",)
    # A keyword implicates its topic even when the topic name is not verbatim.
    assert detect_issue_topics("the Board ignored my evidence") == ("evidence evaluation",)
    # A bare condition raises no claim element.
    assert detect_issue_topics("tinnitus") == ()


def test_relevant_statutes_follow_detected_topics():
    assert relevant_statutes(("service connection",)) == ("1110", "1131", "3.303")
    assert relevant_statutes(("rating",)) == ("4.1",)
    assert relevant_statutes(("duty to assist",)) == ("5103", "3.159")
    assert relevant_statutes(()) == ()


def test_decompose_issue_starts_with_broad_court_recalls():
    plan = decompose_issue("service connection for tinnitus", "Compensation")

    queries = plan_queries(plan)
    assert queries[:8] == [
        'site:uscourts.cavc.gov "service connection for tinnitus" "Compensation" veterans compensation',
        'site:cafc.uscourts.gov "service connection for tinnitus" "Compensation" veterans compensation',
        'site:supremecourt.gov "service connection for tinnitus" "Compensation" veterans compensation',
        'site:bva.va.gov "service connection for tinnitus" "Compensation" veterans compensation',
        "site:uscourts.cavc.gov service connection for tinnitus service connection veterans law",
        "site:cafc.uscourts.gov service connection for tinnitus veterans benefits court",
        "site:supremecourt.gov service connection for tinnitus veterans benefits law",
        "site:bva.va.gov service connection for tinnitus veterans compensation decision",
    ]


def test_decompose_issue_appends_statute_queries_for_detected_elements():
    plan = decompose_issue("service connection for tinnitus", "Compensation")
    queries = plan_queries(plan)

    assert len(queries) == 11  # 8 broad + 3 statute-anchored (1110, 1131, 3.303)
    assert '"1110" "service connection for tinnitus" veterans compensation' in queries
    assert '"3.303" "service connection for tinnitus" veterans compensation' in queries


def test_decompose_issue_bare_condition_has_only_broad_queries():
    plan = decompose_issue("tinnitus", "Compensation")
    assert len(plan_queries(plan)) == 8


def test_decompose_issue_empty_issue_falls_back_to_generic():
    plan = decompose_issue("   ", "Compensation")
    assert plan.issue == "VA compensation"
    assert '"VA compensation" "Compensation"' in plan_queries(plan)[0]


def test_decompose_issue_empty_string_and_none_fall_back():
    for empty in ("", None):
        plan = decompose_issue(empty, "Compensation")
        assert plan.issue == "VA compensation"
        assert plan.claim_type == "Compensation"
        assert '"VA compensation" "Compensation"' in plan_queries(plan)[0]


def test_decompose_issue_strips_embedded_quotes():
    plan = decompose_issue('service connection for tin"nitus', "Compensation")
    queries = plan_queries(plan)

    assert '"service connection for tinnitus" "Compensation"' in queries[0]
    assert '"1110" "service connection for tinnitus" veterans compensation' in queries


def test_decompose_issue_subtask_ids_and_goals():
    plan = decompose_issue("service connection", "Compensation")

    assert [t.id for t in plan.subtasks[:8]] == [f"broad-{i}" for i in range(1, 9)]
    assert plan.subtasks[0].goal == "Find court precedent on the issue"
    assert any(t.id == "statute-3-303" for t in plan.subtasks)
    statute = next(t for t in plan.subtasks if t.id == "statute-1110")
    assert statute.goal == "Find cases citing statute/regulation 1110"
    assert plan.subtasks[-1].goal == "Synthesize the retrieved authority into guidance"


def test_decompose_issue_ends_with_synthesis_depending_on_all_searches():
    plan = decompose_issue("service connection", "Compensation")

    synthesize = plan.subtasks[-1]
    assert synthesize.kind == SYNTHESIZE
    search_ids = [task.id for task in plan.search_subtasks()]
    assert synthesize.depends_on == tuple(search_ids)


def test_ready_subtasks_respects_dependencies():
    plan = decompose_issue("service connection", "Compensation")
    search_ids = {task.id for task in plan.search_subtasks()}

    # Nothing done yet: only the search tasks (no dependencies) are ready.
    ready = plan.ready_subtasks(set())
    assert {task.id for task in ready} == search_ids

    # All searches done: synthesis is now ready.
    ready = plan.ready_subtasks(search_ids)
    assert [task.id for task in ready] == ["synthesize"]


def test_refine_plan_adds_gap_searches_for_uncovered_elements():
    plan = decompose_issue("service connection", "Compensation")
    refined = refine_plan(plan, uncovered_elements=("service connection", "rating"))

    gap_ids = [task.id for task in refined.subtasks if task.id.startswith("gap-")]
    assert gap_ids == ["gap-service-connection", "gap-rating"]
    rating = next(t for t in refined.subtasks if t.id == "gap-rating")
    assert rating.kind == SEARCH
    assert rating.goal == "Find precedent covering rating"
    assert rating.query == '"rating" "service connection" veterans law'
    assert refined.issue == plan.issue
    assert refined.claim_type == plan.claim_type


def test_refine_plan_preserves_original_and_skips_duplicates():
    plan = decompose_issue("service connection", "Compensation")
    refined = refine_plan(plan, uncovered_elements=("nexus", "nexus", "rating"))

    assert plan_queries(refined)[: len(plan_queries(plan))] == plan_queries(plan)
    assert [t.id for t in refined.subtasks if t.id.startswith("gap-")] == ["gap-nexus", "gap-rating"]


def test_refine_plan_noop_without_gaps():
    plan = decompose_issue("service connection", "Compensation")
    assert refine_plan(plan) == plan
    assert refine_plan(plan, uncovered_elements=()) == plan


def test_every_statute_hint_has_a_topic_anchor():
    from va_legal_agent.planning import _TOPIC_STATUTES
    from va_legal_agent.topics import STATUTE_HINTS

    # The anchor map must cover every fragment so a missing entry degrades to
    # "no statutes" (via the .get default) rather than raising at runtime.
    assert all(fragment in _TOPIC_STATUTES for fragment, _hint in STATUTE_HINTS)


def test_plan_types_are_frozen_and_queryable():
    plan = ResearchPlan(
        issue="x",
        claim_type="Compensation",
        subtasks=(
            SubTask(id="a", kind=SEARCH, goal="search", query="q"),
            SubTask(id="b", kind=SYNTHESIZE, goal="synthesize", depends_on=("a",)),
        ),
    )
    assert plan.search_subtasks() == (plan.subtasks[0],)
    assert plan.ready_subtasks({"a"}) == (plan.subtasks[1],)
