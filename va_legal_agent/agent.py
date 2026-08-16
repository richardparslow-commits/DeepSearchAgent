from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from .config import get_settings
from .fetch import fetch_case_details
from .impact import analyze_case_impact
from .interpretation import build_interpretive_analysis
from .models import CaseRecord, LegalAnalysis
from .providers import recall_flags, rollup_search_telemetry, search_all
from .ranking import rank_cases
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


def build_case_queries(claim_issue: str, claim_type: str) -> list[str]:
    issue = claim_issue.strip()
    if not issue:
        issue = "VA compensation"
    normalized_issue = issue.replace('"', "")
    issue_query = f'"{normalized_issue}" "{claim_type}"'

    court_queries = [
        f"site:uscourts.cavc.gov {issue_query} veterans compensation",
        f"site:cafc.uscourts.gov {issue_query} veterans compensation",
        f"site:supremecourt.gov {issue_query} veterans compensation",
        f"site:bva.va.gov {issue_query} veterans compensation",
        f"site:uscourts.cavc.gov {normalized_issue} service connection veterans law",
        f"site:cafc.uscourts.gov {normalized_issue} veterans benefits court",
        f"site:supremecourt.gov {normalized_issue} veterans benefits law",
        f"site:bva.va.gov {normalized_issue} veterans compensation decision",
    ]
    return court_queries


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
    )


def fetch_cases_for_issue(
    claim_issue: str,
    claim_type: str = "Compensation",
    max_results: int = 10,
    enrich: bool = True,
    telemetry: list[dict[str, object]] | None = None,
    max_wall_seconds: float | None = None,
) -> list[CaseRecord]:
    """Search every court-site query and return the merged, enriched cases.

    *max_wall_seconds* caps the total wall time spent searching for this issue
    (``0`` or unset disables it; the default comes from
    ``SEARCH_MAX_WALL_SECONDS``). Once the budget is exhausted the remaining
    queries are abandoned: results already found are returned, and a query
    still in flight is stopped cooperatively via the deadline passed to
    :func:`search_all`. If the budget expires before anything was found, a
    ``SearchError`` is raised.
    """
    if max_wall_seconds is None:
        max_wall_seconds = get_settings().search_max_wall_seconds
    deadline: float | None = None
    if max_wall_seconds and max_wall_seconds > 0:
        deadline = time.monotonic() + max_wall_seconds

    cases: list[CaseRecord] = []
    errors: list[Exception] = []
    queries = build_case_queries(claim_issue, claim_type)
    settings = get_settings()
    max_workers = settings.search_max_workers
    stagger_seconds = settings.search_delay_seconds

    # Manual pool management (not a context manager) so an exhausted budget can
    # return without waiting for already-running queries: shutdown(wait=False)
    # cancels pending futures and lets in-flight ones finish in the background.
    pool = ThreadPoolExecutor(max_workers=max_workers)
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

    deduped: list[CaseRecord] = []
    seen: set[tuple[str, str]] = set()
    # Pre-rank by court authority then relevance so enrichment targets the strongest candidates.
    for case in sorted(cases, key=lambda c: (c.authority_weight, c.relevance_score), reverse=True):
        key = (case.title, case.url)
        if key not in seen:
            seen.add(key)
            deduped.append(case)

    if enrich:
        enrich_top_cases(deduped, limit=min(settings.enrich_case_limit, max(max_results, 1)))

    # Final ordering comes from the ranking layer (authority tiers strictly
    # dominant; within a tier: relevance, recency, and completeness).
    ranked = rank_cases(deduped)[:max_results]
    for case in ranked:
        case.impact = summarize_case_impact(case)
    return ranked


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


def analyze_cases_for_claim(
    claim_issue: str,
    claim_type: str = "Compensation",
    max_results: int = 10,
    enrich: bool = True,
    telemetry: list[dict[str, object]] | None = None,
    max_wall_seconds: float | None = None,
) -> LegalAnalysis:
    cases = fetch_cases_for_issue(
        claim_issue,
        claim_type,
        max_results=max_results,
        enrich=enrich,
        telemetry=telemetry,
        max_wall_seconds=max_wall_seconds,
    )
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
    rolled_telemetry = rollup_search_telemetry(telemetry or [])

    return LegalAnalysis(
        issue=claim_issue,
        summary=summary,
        likely_applicable_principles=interpretive.likely_applicable_principles,
        how_it_affects_va_claims=interpretive.how_it_affects_va_claims,
        next_steps=interpretive.next_steps,
        top_cases=top_cases,
        detected_elements=interpretive.detected_elements,
        principle_findings=interpretive.principle_findings,
        strengths=interpretive.strengths,
        gaps=interpretive.gaps,
        coverage_score=interpretive.coverage_score,
        interpretation_source=interpretive.interpretation_source,
        search_telemetry=rolled_telemetry,
        search_flags=recall_flags(rolled_telemetry),
    )


if __name__ == "__main__":
    result = analyze_cases_for_claim("service connection for tinnitus")
    print(result.model_dump_json(indent=2))
