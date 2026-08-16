"""Cognitive decomposition (Phase 1): intent mapping and sub-task generation.

Turns a claim issue into a dependency-ordered :class:`ResearchPlan`. The plan
is deterministic -- no network call and no LLM -- so the app keeps its offline
path; in hybrid mode the executor may use an LLM to enrich phrasing without
changing the plan's structure.

The plan is the single source of truth for search queries: the broad
court-site recalls plus statute-anchored queries for the claim elements
detected in the issue, followed by a terminal synthesis step that depends on
every search sub-task.
"""

from __future__ import annotations

from dataclasses import dataclass

from .topics import STATUTE_HINTS, TOPICS

SEARCH = "search"
SYNTHESIZE = "synthesize"

# Statute fragment -> topic names it primarily anchors. Planning selects the
# statutes relevant to the elements detected in the issue via this map.
# Invariant: every fragment in topics.STATUTE_HINTS must appear here (a test
# enforces this), so the `.get(fragment, ())` default below is never reached
# and a missing anchor degrades to "no statutes" rather than a KeyError.
_TOPIC_STATUTES: dict[str, tuple[str, ...]] = {
    "5107": ("benefit of the doubt",),
    "7104": ("reasons and bases",),
    "1110": ("service connection",),
    "1131": ("service connection",),
    "5103": ("duty to assist",),
    "3.303": ("service connection",),
    "3.159": ("duty to assist",),
    "4.1": ("rating",),
}


@dataclass(frozen=True)
class SubTask:
    """One step of a research plan.

    ``kind`` is ``SEARCH`` or ``SYNTHESIZE``. ``query`` is set for search
    steps. ``depends_on`` holds the ids of the steps that must complete first
    (used by the executor to schedule and by synthesis to know its inputs).
    """

    id: str
    kind: str
    goal: str
    query: str | None = None
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchPlan:
    """A dependency-ordered decomposition of a claim issue into sub-tasks."""

    issue: str
    claim_type: str
    subtasks: tuple[SubTask, ...]

    def search_subtasks(self) -> tuple[SubTask, ...]:
        """The sub-tasks that issue a search query, in plan order."""
        return tuple(task for task in self.subtasks if task.kind == SEARCH and task.query)

    def ready_subtasks(self, done: set[str]) -> tuple[SubTask, ...]:
        """Sub-tasks not in *done* whose dependencies are all satisfied."""
        return tuple(
            task
            for task in self.subtasks
            if task.id not in done and set(task.depends_on) <= done
        )


def detect_issue_topics(issue: str) -> tuple[str, ...]:
    """Return the topic names implicated by *issue* (keyword or name match).

    Topics are returned in ``TOPICS`` order. This is the intent-mapping step:
    it decides which claim elements the issue actually raises rather than
    searching every element's vocabulary.
    """
    text = (issue or "").lower()
    return tuple(
        topic.name
        for topic in TOPICS
        if topic.name in text or (topic.keyword and topic.keyword in text)
    )


def relevant_statutes(topics: tuple[str, ...]) -> tuple[str, ...]:
    """Return the statute fragments anchored to any of *topics*, in table order."""
    wanted = set(topics)
    return tuple(
        fragment
        for fragment, _hint in STATUTE_HINTS
        if any(topic in wanted for topic in _TOPIC_STATUTES.get(fragment, ()))
    )


def _broad_queries(issue: str, claim_type: str) -> list[str]:
    """The court-site recalls that anchor any research run.

    Mirrors the original fixed query set so broad recall is unchanged; the
    planner appends statute-anchored queries for the detected elements.
    """
    normalized = (issue or "").strip() or "VA compensation"
    normalized = normalized.replace('"', "")
    issue_query = f'"{normalized}" "{claim_type}"'
    return [
        f"site:uscourts.cavc.gov {issue_query} veterans compensation",
        f"site:cafc.uscourts.gov {issue_query} veterans compensation",
        f"site:supremecourt.gov {issue_query} veterans compensation",
        f"site:bva.va.gov {issue_query} veterans compensation",
        f"site:uscourts.cavc.gov {normalized} service connection veterans law",
        f"site:cafc.uscourts.gov {normalized} veterans benefits court",
        f"site:supremecourt.gov {normalized} veterans benefits law",
        f"site:bva.va.gov {normalized} veterans compensation decision",
    ]


def decompose_issue(issue: str, claim_type: str) -> ResearchPlan:
    """Map a claim issue to a dependency-ordered research plan.

    Emits the broad court-site searches, one statute-anchored search per
    statute relevant to the detected elements, and a terminal synthesis step
    that depends on all of them. A bare condition with no detected elements
    (e.g. ``"tinnitus"``) still yields the broad searches plus synthesis.
    """
    normalized = (issue or "").strip() or "VA compensation"
    clean = normalized.replace('"', "")
    topics = detect_issue_topics(normalized)
    statutes = relevant_statutes(topics)

    subtasks: list[SubTask] = []
    search_ids: list[str] = []

    for index, query in enumerate(_broad_queries(issue, claim_type), start=1):
        task_id = f"broad-{index}"
        subtasks.append(
            SubTask(id=task_id, kind=SEARCH, goal="Find court precedent on the issue", query=query)
        )
        search_ids.append(task_id)

    for fragment in statutes:
        task_id = f"statute-{fragment.replace('.', '-')}"
        subtasks.append(
            SubTask(
                id=task_id,
                kind=SEARCH,
                goal=f"Find cases citing statute/regulation {fragment}",
                query=f'"{fragment}" "{clean}" veterans compensation',
            )
        )
        search_ids.append(task_id)

    subtasks.append(
        SubTask(
            id="synthesize",
            kind=SYNTHESIZE,
            goal="Synthesize the retrieved authority into guidance",
            depends_on=tuple(search_ids),
        )
    )
    return ResearchPlan(issue=normalized, claim_type=claim_type, subtasks=tuple(subtasks))


def plan_queries(plan: ResearchPlan) -> list[str]:
    """Materialize the plan's search sub-tasks into an ordered query list."""
    return [task.query for task in plan.search_subtasks() if task.query]


def refine_plan(plan: ResearchPlan, uncovered_elements: tuple[str, ...] = ()) -> ResearchPlan:
    """Return a new plan with targeted searches for uncovered elements.

    Adaptive iteration (Phase 4): elements that no retrieved authority covered
    get a dedicated search sub-task. Existing sub-tasks are preserved; the new
    gap tasks are appended before synthesis runs again.
    """
    if not uncovered_elements:
        return plan
    existing_ids = {task.id for task in plan.subtasks}
    additions: list[SubTask] = []
    for element in uncovered_elements:
        task_id = f"gap-{element.replace(' ', '-')}"
        if task_id in existing_ids:
            continue
        existing_ids.add(task_id)
        additions.append(
            SubTask(
                id=task_id,
                kind=SEARCH,
                goal=f"Find precedent covering {element}",
                query=f'"{element}" "{plan.issue}" veterans law',
            )
        )
    return ResearchPlan(
        issue=plan.issue,
        claim_type=plan.claim_type,
        subtasks=plan.subtasks + tuple(additions),
    )
