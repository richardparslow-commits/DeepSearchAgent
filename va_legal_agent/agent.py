from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError

from .config import get_settings
from .deep_read import deep_read_cases
from .fetch import fetch_case_details
from .impact import analyze_case_impact
from .interpretation import build_interpretive_analysis, uncovered_element_names
from .models import CaseRecord, LegalAnalysis
from .planning import decompose_issue, plan_queries, refine_plan
from .providers import recall_flags, rollup_search_telemetry, search_all, traverse_citations
from .ranking import rank_cases
from .reliability import classify_source
from .search import SearchError
from .topics import (
    COURT_BVA,
    COURT_CAVC,
    COURT_FEDERAL_CIRCUIT,
    COURT_SUPREME,
    COURT_UNKNOWN,
    TOPICS,
    authority_weight_for,
)

logger = logging.getLogger(__name__)


class _DaemonThreadPoolExecutor:
    """Bounded pool of *daemon* worker threads for the search fan-out.

    A minimal stand-in for ``ThreadPoolExecutor`` covering just the
    ``submit`` / ``shutdown`` surface :func:`fetch_cases_for_issue` uses. The
    one deliberate difference from the stdlib executor: workers are daemon
    threads. Stdlib executor workers are non-daemon, so when a wall-time-budget
    abort returns while an in-flight HTTP request is still running, the
    interpreter joins that thread at exit and the CLI hangs for up to
    ``REQUEST_TIMEOUT_SECONDS`` after it has already printed its result.
    Daemon workers are abandoned at exit instead of blocking it.
    """

    def __init__(self, max_workers: int):
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._futures: set[Future] = set()
        self._lock = threading.Lock()
        self._workers = [
            threading.Thread(target=self._run, daemon=True)
            for _ in range(max_workers)
        ]
        for worker in self._workers:
            worker.start()

    def submit(self, fn, *args, **kwargs) -> Future:
        """Queue ``fn(*args, **kwargs)`` and return a future for its result."""
        future: Future = Future()
        with self._lock:
            self._futures.add(future)
        self._queue.put((future, fn, args, kwargs))
        return future

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, fn, args, kwargs = item
            # Returns False when the future was cancelled while still queued,
            # in which case the work is skipped (mirroring executor semantics).
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn(*args, **kwargs))
                except BaseException as exc:  # noqa: BLE001 - executor semantics
                    future.set_exception(exc)
            with self._lock:
                self._futures.discard(future)

    def shutdown(self, wait: bool = False, cancel_futures: bool = False) -> None:
        """Stop accepting work and cancel queued futures on request.

        ``wait`` is intentionally ignored: these workers are daemon and must
        never block the caller (or interpreter exit) by being joined.
        """
        with self._lock:
            if cancel_futures:
                for future in list(self._futures):
                    future.cancel()
        for _ in self._workers:
            self._queue.put(None)


def build_case_queries(claim_issue: str, claim_type: str) -> list[str]:
    """Return the plan-derived search queries for the issue.

    The first eight queries are the broad court-site recalls (CAVC, Federal
    Circuit, Supreme Court, and BVA); the planner appends statute-anchored
    queries for the claim elements detected in the issue (see
    :func:`va_legal_agent.planning.decompose_issue`).
    """
    return plan_queries(decompose_issue(claim_issue, claim_type))


def detect_court_name(url: str) -> str:
    normalized = (url or "").lower()
    if "uscourts.cavc.gov" in normalized:
        return COURT_CAVC
    if "cafc.uscourts.gov" in normalized or "caafc.uscourts.gov" in normalized:
        return COURT_FEDERAL_CIRCUIT
    if "supremecourt.gov" in normalized:
        return COURT_SUPREME
    if "bva.va.gov" in normalized:
        return COURT_BVA
    return COURT_UNKNOWN


def score_case_relevance(case: CaseRecord, issue: str) -> int:
    text = f"{case.title} {case.snippet} {case.holding} {case.impact} {case.issue}".lower()
    score = 0
    normalized_issue = issue.lower()
    if normalized_issue in text:
        score += 2
    for topic in TOPICS:
        if topic.keyword and topic.keyword in normalized_issue:
            if any(synonym in text for synonym in topic.synonyms):
                score += 2
    if "veterans" in text:
        score += 1
    if "service connection" in text or "benefit of the doubt" in text or "reasons and bases" in text:
        score += 2
    if case.court and "Veterans" in case.court:
        score += 1
    return score


def normalize_case(raw: dict[str, str], court_name: str, issue: str) -> CaseRecord:
    title = raw.get("title", "Unknown case")
    url = raw.get("url", "")
    snippet = raw.get("snippet", "")
    # Providers (e.g. CourtListener) may return structured fields directly;
    # keep them so enrichment only fills in what's still missing.
    return CaseRecord(
        title=title,
        court=court_name,
        citation=raw.get("citation", ""),
        url=url,
        snippet=snippet,
        issue=issue,
        decision_date=raw.get("decision_date", ""),
        docket=raw.get("docket", ""),
        judge=raw.get("judge", ""),
        holding="",
        impact="",
        authority_rank=0,
        authority_weight=authority_weight_for(court_name),
        relevance_score=0,
        source_reliability=classify_source(url),
    )


def _deadline_for(max_wall_seconds: float) -> float | None:
    """Return an absolute monotonic deadline for the budget, or None if disabled.

    ``0`` (or a negative value) disables the budget; a positive value sets the
    deadline that far in the future. Shared by the single-pass primitive and
    the adaptive loop so both rounds bound against one clock.
    """
    if max_wall_seconds and max_wall_seconds > 0:
        return time.monotonic() + max_wall_seconds
    return None


def _fanout_search(
    queries: list[str],
    claim_issue: str,
    max_results: int,
    telemetry: list[dict[str, object]] | None,
    deadline: float | None,
    max_wall_seconds: float,
) -> tuple[list[CaseRecord], list[Exception], bool]:
    """Fan *queries* out across the worker pool and return normalized cases.

    Submits each query to :func:`search_all` with inter-query staggering and
    waits up to *deadline* (an absolute ``time.monotonic`` timestamp, or
    ``None`` for no cap). Returns ``(cases, errors, budget_exhausted)``:
    *cases* are normalized and relevance-scored but not yet deduplicated,
    enriched, or ranked; a failing query is recorded in *errors* without
    aborting the others; and once *deadline* passes the remaining work is
    abandoned. Never raises -- callers decide how an empty/error/budget
    outcome should surface.
    """
    settings = get_settings()
    max_workers = settings.search_max_workers
    stagger_seconds = settings.search_delay_seconds

    cases: list[CaseRecord] = []
    errors: list[Exception] = []

    # Manual pool management (not a context manager) so an exhausted budget can
    # return without waiting for already-running queries: shutdown(wait=False)
    # cancels queued futures and lets in-flight ones finish in the background.
    # Workers are daemon threads, so an abandoned in-flight request cannot
    # block the process at interpreter exit.
    pool = _DaemonThreadPoolExecutor(max_workers=max_workers)
    budget_exhausted = False
    try:
        futures: list[object] = []
        for index, query in enumerate(queries):
            if deadline is not None and time.monotonic() >= deadline:
                budget_exhausted = True
                break  # budget spent before this query could start
            if index > 0 and stagger_seconds > 0:
                sleep_for = stagger_seconds
                if deadline is not None:
                    sleep_for = min(sleep_for, max(deadline - time.monotonic(), 0.0))
                if sleep_for > 0:
                    time.sleep(sleep_for)
                if deadline is not None and time.monotonic() >= deadline:
                    budget_exhausted = True
                    break  # budget spent during the stagger; don't start this query
            futures.append(pool.submit(search_all, query, max_results, telemetry, deadline))
        # Any ``budget_exhausted = True`` set above (pre-submission or during
        # the stagger) is redundant, not authoritative: every break there still
        # leaves at least one future in flight, and once the deadline has
        # passed the loop below deterministically computes ``remaining <= 0``,
        # raises ``FutureTimeoutError``, and re-derives ``budget_exhausted = True``
        # before anything else can read the flag. The flag is therefore always
        # re-derived here, never trusted from the submission loop -- keep it
        # that way, or the post-stagger flag becomes observable.
        for query, future in zip(queries, futures):
            try:
                if deadline is None:
                    found = future.result()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise FutureTimeoutError()
                    found = future.result(timeout=remaining)
            except FutureTimeoutError:
                budget_exhausted = True
                break  # stop waiting; return whatever completed so far
            except Exception as exc:  # noqa: BLE001 - keep results from other queries; report below if all fail
                logger.warning("Search failed for query %r: %s", query, exc)
                errors.append(exc)
                continue
            for result in found:
                # Providers may label the court directly (e.g. CourtListener);
                # otherwise fall back to URL-based detection.
                court_name = result.get("court") or detect_court_name(result.get("url", ""))
                case = normalize_case(result, court_name, claim_issue)
                case.relevance_score = score_case_relevance(case, claim_issue)
                cases.append(case)
        if budget_exhausted:
            logger.warning(
                "Search wall-time budget of %ss exhausted for issue %r; "
                "returning partial results.",
                max_wall_seconds,
                claim_issue,
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return cases, errors, budget_exhausted


def _raise_if_no_results(
    cases: list[CaseRecord],
    errors: list[Exception],
    budget_exhausted: bool,
    claim_issue: str,
    max_wall_seconds: float,
) -> None:
    """Raise the canonical ``SearchError`` when a search round found nothing."""
    if not cases and errors:
        raise SearchError(
            f"All {len(errors)} searches failed for issue {claim_issue!r}. "
            "The search provider may be blocking or rate-limiting automated requests. "
            f"Last error: {errors[-1]}"
        ) from errors[-1]
    if not cases and budget_exhausted:
        raise SearchError(
            f"Search wall-time budget of {max_wall_seconds}s exhausted for issue "
            f"{claim_issue!r} before any results were returned."
        )


def _dedupe(cases: list[CaseRecord]) -> list[CaseRecord]:
    """Dedupe *cases* by (title, url), pre-ranked by authority then relevance."""
    deduped: list[CaseRecord] = []
    seen: set[tuple[str, str]] = set()
    for case in sorted(cases, key=lambda c: (c.authority_weight, c.relevance_score), reverse=True):
        key = (case.title, case.url)
        if key not in seen:
            seen.add(key)
            deduped.append(case)
    return deduped


def _dedupe_rank_and_enrich(
    cases: list[CaseRecord],
    max_results: int,
    enrich: bool,
    claim_issue: str,
    deep_read: bool | None = None,
    deep_read_limit: int | None = None,
) -> list[CaseRecord]:
    """Dedupe, enrich, deep-read (opt-in), rank, and attach impact summaries.

    ``deep_read`` / ``deep_read_limit`` of ``None`` fall back to the
    ``DEEP_READ`` / ``DEEP_READ_LIMIT`` settings, so callers can force them
    per run (e.g. from CLI flags) without editing the environment.
    """
    settings = get_settings()
    if deep_read is None:
        deep_read = settings.deep_read
    if deep_read_limit is None:
        deep_read_limit = settings.deep_read_limit
    deduped = _dedupe(cases)

    if enrich:
        enrich_top_cases(deduped, limit=min(settings.enrich_case_limit, max(max_results, 1)))
    # Deep-read mode fetches the full body of the top cases and summarizes it
    # so the reasoning pass cross-references holdings across the whole corpus.
    if deep_read:
        deep_read_cases(deduped, claim_issue, limit=min(deep_read_limit, len(deduped)))

    # Final ordering comes from the ranking layer (authority tiers strictly
    # dominant; within a tier: relevance, recency, and completeness).
    ranked = rank_cases(deduped)[:max_results]
    for case in ranked:
        case.impact = summarize_case_impact(case)
    return ranked


def _observe(
    claim_issue: str, cases: list[CaseRecord], telemetry: list[dict[str, object]] | None
) -> tuple[tuple[str, ...], list[str]]:
    """Observe a research round: coverage gaps plus low-recall flags.

    Returns ``(uncovered, flags)`` where *uncovered* is the claim elements no
    retrieved case covers (the refinement trigger) and *flags* is the
    per-provider low-recall picture for the round, which the loop logs and
    which the final report surfaces as ``search_flags``.
    """
    uncovered = uncovered_element_names(claim_issue, cases)
    flags = recall_flags(rollup_search_telemetry(telemetry or []))
    return uncovered, flags


def research_issue(
    claim_issue: str,
    claim_type: str = "Compensation",
    max_results: int = 10,
    enrich: bool = True,
    telemetry: list[dict[str, object]] | None = None,
    max_wall_seconds: float | None = None,
    deep_read: bool | None = None,
    deep_read_limit: int | None = None,
) -> list[CaseRecord]:
    """Run the adaptive research loop and return the merged, ranked cases.

    Executes the plan's search sub-tasks (round 1), observes which detected
    claim elements remain uncovered, and -- while coverage is incomplete and
    the wall-time budget lasts -- refines the plan with targeted gap searches
    (:func:`va_legal_agent.planning.refine_plan`) and re-searches. Every round
    shares one absolute *deadline* derived from *max_wall_seconds*, so the
    nested retry/backoff loops stay bounded without resetting per round. The
    terminal synthesis step runs in :func:`analyze_cases_for_claim` once all
    search sub-tasks complete.
    """
    if max_wall_seconds is None:
        max_wall_seconds = get_settings().search_max_wall_seconds
    deadline = _deadline_for(max_wall_seconds)

    plan = decompose_issue(claim_issue, claim_type)
    done: set[str] = set()
    cases: list[CaseRecord] = []

    initial = [task for task in plan.search_subtasks() if task.query]
    done.update(task.id for task in initial)
    round_cases, errors, budget_exhausted = _fanout_search(
        [task.query for task in initial if task.query],
        claim_issue,
        max_results,
        telemetry,
        deadline,
        max_wall_seconds,
    )
    cases.extend(round_cases)
    _raise_if_no_results(cases, errors, budget_exhausted, claim_issue, max_wall_seconds)

    uncovered, flags = _observe(claim_issue, cases, telemetry)
    while uncovered and (deadline is None or time.monotonic() < deadline):
        plan = refine_plan(plan, uncovered)
        ready = [task for task in plan.search_subtasks() if task.id not in done and task.query]
        if not ready:
            break
        logger.info(
            "Research gap detected: uncovered=%s search_flags=%s; refining queries.",
            list(uncovered),
            flags,
        )
        done.update(task.id for task in ready)
        gap_cases, _errors, _budget = _fanout_search(
            [task.query for task in ready if task.query],
            claim_issue,
            max_results,
            telemetry,
            deadline,
            max_wall_seconds,
        )
        cases.extend(gap_cases)
        uncovered, flags = _observe(claim_issue, cases, telemetry)
        logger.info(
            "Research refinement complete: uncovered=%s search_flags=%s.",
            list(uncovered),
            flags,
        )

    # Multi-hop recall (opt-in): follow CourtListener citation trails from the
    # strongest cases found so far, and merge the newly discovered opinions
    # before the final ranking.
    if get_settings().citation_traversal:
        seeds = _dedupe(cases)[: get_settings().citation_traverse_limit]
        for raw in traverse_citations([case.url for case in seeds], max_results=max_results):
            court_name = raw.get("court") or detect_court_name(raw.get("url", ""))
            found = normalize_case(raw, court_name, claim_issue)
            found.relevance_score = score_case_relevance(found, claim_issue)
            cases.append(found)

    return _dedupe_rank_and_enrich(cases, max_results, enrich, claim_issue, deep_read, deep_read_limit)


def fetch_cases_for_issue(
    claim_issue: str,
    claim_type: str = "Compensation",
    max_results: int = 10,
    enrich: bool = True,
    telemetry: list[dict[str, object]] | None = None,
    max_wall_seconds: float | None = None,
    deep_read: bool | None = None,
    deep_read_limit: int | None = None,
) -> list[CaseRecord]:
    """Search every planned query once and return the merged, enriched cases.

    Single-pass primitive under :func:`research_issue`: it fans out the
    plan-derived queries with the same deadline semantics, raises on an
    unrecoverable empty round, and post-processes the result. Use
    :func:`research_issue` for the adaptive multi-round loop.
    """
    if max_wall_seconds is None:
        max_wall_seconds = get_settings().search_max_wall_seconds
    deadline = _deadline_for(max_wall_seconds)

    queries = build_case_queries(claim_issue, claim_type)
    cases, errors, budget_exhausted = _fanout_search(
        queries, claim_issue, max_results, telemetry, deadline, max_wall_seconds
    )
    _raise_if_no_results(cases, errors, budget_exhausted, claim_issue, max_wall_seconds)
    return _dedupe_rank_and_enrich(cases, max_results, enrich, claim_issue, deep_read, deep_read_limit)


def enrich_top_cases(cases: list[CaseRecord], limit: int | None = None) -> list[CaseRecord]:
    """Fetch source pages for the top cases and fill in structured details.

    Fills citation, decision date, holding, docket, judge, statutes, and
    outcome where the source page provides them. ``limit`` defaults to
    ENRICH_CASE_LIMIT.
    """
    if limit is None:
        limit = get_settings().enrich_case_limit
    for case in cases[:limit]:
        if not case.url:
            continue
        try:
            details = fetch_case_details(case.url)
        except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
            logger.warning("Could not fetch details for %s: %s", case.url, exc)
            continue
        for key in ("citation", "decision_date", "holding", "docket", "judge", "outcome"):
            value = details.get(key) or getattr(case, key)
            if value:
                setattr(case, key, value)
        statutes = details.get("statutes") or []
        if statutes:
            case.statutes = list(statutes)
    return cases


def summarize_case_impact(case: CaseRecord) -> str:
    """Build a nuanced impact summary for one case.

    Delegates to the impact-analysis layer, which layers procedural-posture,
    statutory-anchor, and authority-weight notes onto the relevance sentence.
    Kept as the public entry point used by the research pipeline.
    """
    return analyze_case_impact(case).nuance


def _build_analysis(
    claim_issue: str,
    claim_type: str,
    cases: list[CaseRecord],
    telemetry: list[dict[str, object]] | None,
) -> LegalAnalysis:
    """Build the structured ``LegalAnalysis`` from retrieved, ranked cases."""
    if not cases:
        raise ValueError(f"No cases found for: {claim_issue}")

    summary = "\n".join(
        f"- {case.title} ({case.court}) "
        f"[score: {case.composite_score:.2f}, authority: {case.authority_weight}, relevance: {case.relevance_score}]"
        for case in cases[:5]
    )

    # The interpretive analysis layer derives principles, strengths, gaps, and
    # next steps from the cases (optionally enhanced by the LLM).
    interpretive = build_interpretive_analysis(claim_issue, claim_type, cases)
    top_cases = [f"{case.title} ({case.court})" for case in cases[:5]]
    # Deep-read summaries ride along with the top cases so the output shows what
    # full-text ingestion produced per case (empty when deep-read was off or a
    # body could not be fetched).
    deep_summaries = [
        {"case": label, "summary": case.deep_summary}
        for label, case in zip(top_cases, cases[:5])
    ]
    rolled_telemetry = rollup_search_telemetry(telemetry or [])

    return LegalAnalysis(
        issue=claim_issue,
        summary=summary,
        likely_applicable_principles=interpretive.likely_applicable_principles,
        how_it_affects_va_claims=interpretive.how_it_affects_va_claims,
        next_steps=interpretive.next_steps,
        top_cases=top_cases,
        deep_summaries=deep_summaries,
        detected_elements=interpretive.detected_elements,
        principle_findings=interpretive.principle_findings,
        contradictions=interpretive.contradictions,
        strengths=interpretive.strengths,
        gaps=interpretive.gaps,
        coverage_score=interpretive.coverage_score,
        interpretation_source=interpretive.interpretation_source,
        search_telemetry=rolled_telemetry,
        search_flags=recall_flags(rolled_telemetry),
    )


def analyze_cases_for_claim(
    claim_issue: str,
    claim_type: str = "Compensation",
    max_results: int = 10,
    enrich: bool = True,
    telemetry: list[dict[str, object]] | None = None,
    max_wall_seconds: float | None = None,
    deep_read: bool | None = None,
    deep_read_limit: int | None = None,
) -> LegalAnalysis:
    """Run the adaptive research loop and build structured VA-claims guidance."""
    cases = research_issue(
        claim_issue,
        claim_type,
        max_results=max_results,
        enrich=enrich,
        telemetry=telemetry,
        max_wall_seconds=max_wall_seconds,
        deep_read=deep_read,
        deep_read_limit=deep_read_limit,
    )
    return _build_analysis(claim_issue, claim_type, cases, telemetry)


if __name__ == "__main__":
    result = analyze_cases_for_claim("service connection for tinnitus")
    print(result.model_dump_json(indent=2))
