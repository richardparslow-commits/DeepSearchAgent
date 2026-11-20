from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError

import re
from collections import Counter
from urllib.parse import urlsplit

from .appellate_history import load_appellate_history_records
from .authority_queue import apply_citator_findings
from .authorities import (
    authority_flags,
    statutory_supersession_flags,
    validate_cavc_panel_cases,
    verify_analysis_authorities,
    verify_analysis_supersession,
)
from .citator import run_citator
from .congress import verify_analysis_congress
from .config import get_settings
from .deep_read import deep_read_cases
from .extraction import extract_quote_candidates
from .fetch import extract_case_details, fetch_case_details, fetch_case_details_and_text
from .impact import analyze_case_impact
from .interpretation import build_interpretive_analysis, uncovered_element_names
from .llm import llm_enabled
from .models import (
    CaseExtractionCandidates,
    CaseRecord,
    CitationTreatment,
    LegalAnalysis,
    TopCaseRecord,
)
from .planning import decompose_issue, plan_queries, refine_plan
from .providers import (
    CourtListenerProvider,
    check_courtlistener_daily_budget,
    check_courtlistener_minute_budget,
    recall_flags,
    resolve_search_providers,
    rollup_search_telemetry,
    search_all,
    traverse_citations,
)
from .ranking import _parse_year, rank_cases, select_with_court_floor
from .redaction import redact_secrets
from .regulations import verify_analysis_regulations
from .reliability import (
    classify_authority_tier,
    classify_source,
    summarize_authority_reliability,
    RELIABILITY_OFFICIAL,
)
from .search import SearchError
from .snapshots import SnapshotStore
from .topics import (
    COURT_BVA,
    COURT_CAVC,
    COURT_FEDERAL_CIRCUIT,
    COURT_SUPREME,
    COURT_UNKNOWN,
    TOPICS,
    authority_weight_for,
    phrase_matches,
    topic_matches_issue,
)
from .va_domain import (
    apply_citator_checks,
    build_case_abstract,
    build_rvsr_guidance,
    build_senior_rater_guidance,
    build_surfaced_case,
    impacts_from_cases,
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


_COURT_HOSTS: tuple[tuple[str, str], ...] = (
    ("uscourts.cavc.gov", COURT_CAVC),
    ("cafc.uscourts.gov", COURT_FEDERAL_CIRCUIT),
    ("caafc.uscourts.gov", COURT_FEDERAL_CIRCUIT),
    ("supremecourt.gov", COURT_SUPREME),
    ("bva.va.gov", COURT_BVA),
)


def detect_court_name(url: str) -> str:
    """Return the court for *url*, matching only its hostname.

    Search results are third-party content, so a URL that merely contains an
    official domain in a path, query or userinfo -- or in a lookalike host
    such as ``uscourts.cavc.gov.evil.example`` -- must not inherit that
    court's authority weight.
    """
    candidate = (url or "").strip()
    if not candidate:
        return COURT_UNKNOWN
    parsed = urlsplit(candidate)
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
        return COURT_UNKNOWN
    host = (parsed.hostname or "").rstrip(".")
    if not host:
        # A scheme-less URL ("uscourts.cavc.gov/opinion/1") parses as a path.
        host = (urlsplit("//" + candidate).hostname or "").rstrip(".")
    for domain, court in _COURT_HOSTS:
        if host == domain or host.endswith("." + domain):
            return court
    return COURT_UNKNOWN


# ---------------------------------------------------------------------------
# Semantic similarity helpers
# ---------------------------------------------------------------------------

# Stopwords filtered from token streams so only content-bearing terms
# contribute to the overlap score.
_SEMANTIC_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "shall", "it", "its", "that",
        "this", "these", "those", "not", "no", "nor", "as", "if", "than",
        "about", "into", "through", "during", "before", "after", "above",
        "below", "between", "under", "again", "further", "then", "once",
        "here", "there", "when", "where", "why", "how", "all", "both",
        "each", "every", "any", "such", "only", "other", "some", "more",
        "most", "also", "very", "just", "now", "up", "out", "so", "which",
    }
)


def _tokenize(text: str) -> list[str]:
    """Lowercase *text*, extract alphabetic tokens >= 3 chars, drop stopwords."""
    tokens = re.findall(r"[a-z]{3,}", text.lower())
    return [t for t in tokens if t not in _SEMANTIC_STOPWORDS]


def _issue_term_weights(issue: str) -> dict[str, float]:
    """Build weighted term vector from *issue* and matching ``TOPICS`` synonyms.

    Issue tokens get weight 1.0. Synonyms for topic entries whose name, keyword,
    or any detection phrase matches the issue get weight 0.5 (they are
    semantically related but not the exact issue phrase).
    """
    weights: dict[str, float] = {}
    normalized_issue = issue.lower()

    for term in _tokenize(issue):
        weights[term] = max(weights.get(term, 0.0), 1.0)

    for topic in TOPICS:
        if topic_matches_issue(topic, normalized_issue):
            for synonym in topic.synonyms:
                for term in _tokenize(synonym):
                    weights[term] = max(weights.get(term, 0.0), 0.5)

    return weights


def _cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Cosine similarity between two sparse vectors (``{term: weight}`` dicts).

    Returns 0.0 when either vector is empty.
    """
    if not vec_a or not vec_b:
        return 0.0

    dot = sum(vec_a.get(k, 0.0) * vec_b.get(k, 0.0) for k in set(vec_a) | set(vec_b))
    mag_a = (sum(v ** 2 for v in vec_a.values())) ** 0.5
    mag_b = (sum(v ** 2 for v in vec_b.values())) ** 0.5

    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0

    return dot / (mag_a * mag_b)


def _semantic_similarity(case_text: str, issue: str) -> float:
    """Weighted-term-overlap score in [0.0, 1.0] between *case_text* and *issue*.

    Builds a weighted issue vector via :func:`_issue_term_weights`, converts
    *case_text* into a relative-frequency vector (each term's count divided by
    the most-frequent term's count in the text), and returns the cosine
    similarity. Returns 0.0 when the issue yields no content terms.
    """
    issue_weights = _issue_term_weights(issue)
    if not issue_weights:
        return 0.0

    case_terms = _tokenize(case_text)
    if not case_terms:
        return 0.0

    case_tf = Counter(case_terms)
    max_tf = max(case_tf.values())
    case_vector = {term: count / max_tf for term, count in case_tf.items()}

    return _cosine_similarity(issue_weights, case_vector)


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------


def score_case_relevance(case: CaseRecord, issue: str) -> int:
    text = f"{case.title} {case.snippet} {case.holding} {case.impact} {case.issue}".lower()
    score = 0
    normalized_issue = issue.lower()
    if phrase_matches(text, normalized_issue):
        score += 2
    for topic in TOPICS:
        if topic_matches_issue(topic, normalized_issue):
            if any(
                phrase_matches(text, phrase)
                for phrase in (*topic.phrases, *topic.synonyms)
            ):
                score += 2
    if phrase_matches(text, "veterans"):
        score += 1
    if (
        phrase_matches(text, "service connection")
        or phrase_matches(text, "benefit of the doubt")
        or phrase_matches(text, "reasons and bases")
    ):
        score += 2
    # COURT_UNKNOWN is the literal "Veterans law research result"; it must not
    # inherit the Veterans-court bonus merely because its label contains the
    # word "Veterans".
    if case.court and case.court != COURT_UNKNOWN and "Veterans" in case.court:
        score += 1
    # Semantic similarity bonus: 0-4 points for paraphrased concept overlap.
    # The bonus rewards cases whose vocabulary overlaps with the issue's
    # weighted term set even when the exact issue phrase never appears.
    similarity = _semantic_similarity(text, issue)
    if similarity >= 0.3:
        score += 2
    if similarity >= 0.5:
        score += 2
    return score


def normalize_case(raw: dict[str, str], court_name: str, issue: str) -> CaseRecord:
    title = raw.get("title", "Unknown case")
    url = raw.get("url", "")
    snippet = raw.get("snippet", "")
    # Providers (e.g. CourtListener) may return structured fields directly;
    # keep them so enrichment only fills in what's still missing.
    source_reliability = classify_source(url)
    authority_weight = authority_weight_for(court_name)
    # Cap authority_weight <= 1 for non-official sources with a URL
    # (secondary sources like blog mirrors should not outrank official sources)
    if url and source_reliability != RELIABILITY_OFFICIAL:
        authority_weight = min(authority_weight, 1)
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
        panel=raw.get("panel", ""),
        recap_docket_url=raw.get("recap_docket_url", ""),
        recap_docket_id=raw.get("recap_docket_id", ""),
        oral_argument_url=raw.get("oral_argument_url", ""),
        oral_argument_date=raw.get("oral_argument_date", ""),
        courtlistener_opinion_id=raw.get("courtlistener_opinion_id", ""),
        holding="",
        impact="",
        authority_rank=0,
        authority_weight=authority_weight,
        relevance_score=0,
        source_reliability=source_reliability,
    )


def _stamp_authority_tier(case: CaseRecord) -> None:
    """Recompute a case's authority tier after enrichment changes its status."""
    tier, official = classify_authority_tier(
        case.url, case.court, case.precedential
    )
    case.authority_tier = tier
    case.is_official_authority = official


def _case_from_raw(raw: dict[str, str], issue: str) -> CaseRecord:
    """Normalize one provider row and score it against the issue."""
    court_name = raw.get("court") or detect_court_name(raw.get("url", ""))
    case = normalize_case(raw, court_name, issue)
    case.relevance_score = score_case_relevance(case, issue)
    _stamp_authority_tier(case)
    return case


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
                cases.append(_case_from_raw(result, claim_issue))
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
    """Dedupe *cases* by (title, url), URL, and (citation, decision_date).

    The same decision often appears at multiple URLs (CourtListener, CAVC,
    law-blog mirrors) with slightly different titles. The citation + date
    pair is the stable identity of an opinion; the URL key catches the same
    page when parsed titles differ or one copy lacks citation metadata.
    """
    deduped: list[CaseRecord] = []
    seen: set[tuple[str, str]] = set()
    seen_urls: set[str] = set()
    for case in sorted(cases, key=lambda c: (c.authority_weight, c.relevance_score), reverse=True):
        key = (case.title, case.url)
        if key in seen:
            continue
        # The URL is always a same-page identity, regardless of whether the
        # later copy happens to have citation metadata. Applying this only to
        # citation-less copies lets an enriched later copy bypass the URL
        # check and duplicate an earlier, unenriched record.
        if case.url and case.url in seen_urls:
            continue
        # Citation + date is the canonical identity of an opinion. When both
        # fields are populated, also reject any later case that shares the
        # same citation and date (same opinion at a different mirror URL).
        if case.citation and case.decision_date:
            citation_key = (case.citation, case.decision_date)
            if citation_key in seen:
                continue
            seen.add(citation_key)
        if case.url:
            seen_urls.add(case.url)
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
        # Re-dedupe after enrichment: the first pass runs on search-time
        # fields, where citation/decision_date are often empty (unreported
        # decisions) and the same case returned as separate opinion records
        # (same cluster, different opinion IDs/URLs) survives. Enrichment
        # extracts the citation + date from the full text, so the second
        # pass collapses those mirror duplicates. ``_dedupe`` sorts by
        # (authority_weight, relevance_score), so the highest-scoring copy
        # survives.
        deduped = _dedupe(deduped)

    # Publication status and URL can both change during enrichment, so stamp
    # the authority tier again before ranking. Ranking uses the official flag
    # only as a same-score tie-breaker, never to override court authority.
    for case in deduped:
        _stamp_authority_tier(case)

    # Final ordering comes from the ranking layer (authority tiers strictly
    # dominant; within a tier: relevance, recency, and completeness), then a
    # per-court representation floor so lower-authority tiers (BVA) are not
    # truncated out of the cap by a full slate of higher-tier cases.
    ranked = rank_cases(deduped)
    ranked = select_with_court_floor(ranked, max_results)

    # Deep-read mode fetches the full body of the top cases and summarizes it
    # so the reasoning pass cross-references holdings across the whole corpus.
    # The selection mirrors the ranking floor so every represented authority
    # tier (including BVA) gets full-text ingestion instead of only the
    # authority-sorted top-N. ``deep_read_limit`` still caps the total number
    # of ingests for cost control; the floor guarantees each tier a slot when
    # the limit is at least ``floor x #tiers``. ``renumber=False`` keeps the
    # returned ordering's authority_rank from being clobbered.
    if deep_read:
        deep_selection = select_with_court_floor(
            ranked, min(deep_read_limit, len(ranked)), renumber=False
        )
        deep_read_cases(deep_selection, claim_issue, limit=len(deep_selection))

    for case in ranked:
        # Deep-read sets a full-text abstract; when deep-read is off (or the
        # body could not be fetched) retain an explicitly incomplete
        # metadata-only abstract rather than dropping the structured contract.
        if case.case_abstract is None:
            case.case_abstract = build_case_abstract("", case)
        _stamp_authority_tier(case)
        profile = analyze_case_impact(case)
        case.impact = profile.nuance
        case.provision_impacts = profile.provision_impacts
        # AUD4-007: retain the full profile, not only its narrative.
        case.impact_profile = profile
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


def _semantic_gap_enabled() -> bool:
    """True when one CourtListener semantic search per gap round is allowed."""
    settings = get_settings()
    return bool(
        settings.courtlistener_semantic_gap_search
        and settings.courtlistener_api_key
        and "courtlistener"
        in resolve_search_providers(settings.search_providers)
    )


def _semantic_gap_query(issue: str, uncovered: tuple[str, ...]) -> str:
    """Build a plain-language query for the current uncovered elements."""
    elements = "; ".join(uncovered)
    return (
        f"Veterans benefits case law addressing {elements} in the context of "
        f"{issue}."
    )


def _semantic_gap_cases(
    issue: str,
    uncovered: tuple[str, ...],
    max_results: int,
    deadline: float | None,
) -> list[CaseRecord]:
    """Run one selective CourtListener semantic query for a gap round.

    Semantic search is deliberately limited to the refinement loop: it is
    more expensive than keyword search and should not replace broad recall.
    Failures are logged and ignored so a semantic outage cannot abort an
    otherwise productive research round.
    """
    if not _semantic_gap_enabled():
        return []
    if deadline is not None and time.monotonic() >= deadline:
        logger.warning("Semantic gap search skipped: wall-time budget exhausted.")
        return []
    query = _semantic_gap_query(issue, uncovered)
    try:
        found = CourtListenerProvider().semantic_search(query, max_results=max_results)
    except SearchError as exc:
        logger.warning("Semantic gap search failed for %r: %s", issue, exc)
        return []
    cases = [_case_from_raw(raw, issue) for raw in found]
    if cases:
        logger.info(
            "Semantic gap search added %d case(s) for uncovered=%s.",
            len(cases),
            list(uncovered),
        )
    return cases


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
    # Pre-flight the CourtListener daily budget before the first search round
    # (this is the entry point the CLI and batch runner use, so the guard must
    # live here, not only in the single-pass fetch_cases_for_issue primitive).
    _settings = get_settings()
    _check_courtlistener_budget_before_run(
        [task.query for task in initial],
        telemetry,
        deep_read=bool(deep_read if deep_read is not None else _settings.deep_read),
        deep_read_limit=(
            deep_read_limit if deep_read_limit is not None else _settings.deep_read_limit
        ),
    )
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
    # Deterministic cap on refinement rounds: even with the wall-time budget
    # disabled, a broken ready-filter (or a refine_plan that mints a fresh
    # task every round) must not spin the re-search loop forever.
    max_refinement_rounds = get_settings().search_max_refinement_rounds
    rounds = 0
    while (
        uncovered
        and (deadline is None or time.monotonic() < deadline)
        and rounds < max_refinement_rounds
    ):
        plan = refine_plan(plan, uncovered)
        ready = [task for task in plan.search_subtasks() if task.id not in done and task.query]
        if not ready:
            break
        rounds += 1
        # Re-check per round: an earlier round may have drained the daily
        # window, and a gap round would otherwise grind through 429s.
        _check_courtlistener_budget_before_run(
            [task.query for task in ready],
            telemetry,
            deep_read=bool(deep_read if deep_read is not None else _settings.deep_read),
            deep_read_limit=(
                deep_read_limit if deep_read_limit is not None else _settings.deep_read_limit
            ),
            semantic_queries=1 if _semantic_gap_enabled() else 0,
        )
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
        cases.extend(
            _semantic_gap_cases(claim_issue, uncovered, max_results, deadline)
        )
        uncovered, flags = _observe(claim_issue, cases, telemetry)
        logger.info(
            "Research refinement complete: uncovered=%s search_flags=%s.",
            list(uncovered),
            flags,
        )
    if uncovered and rounds >= max_refinement_rounds:
        logger.info(
            "Refinement round limit of %s reached for issue %r; stopping with uncovered=%s.",
            max_refinement_rounds,
            claim_issue,
            list(uncovered),
        )

    # Multi-hop recall (opt-in): follow CourtListener citation trails from the
    # strongest cases found so far, and merge the newly discovered opinions
    # before the final ranking.
    if get_settings().citation_traversal:
        seeds = _dedupe(cases)[: get_settings().citation_traverse_limit]
        if deadline and time.monotonic() >= deadline:
            logger.warning(
                "Citation traversal skipped: wall-time budget exhausted before "
                "traversal started (deadline %.1f, now %.1f).",
                deadline,
                time.monotonic(),
            )
        else:
            traversed = 0
            for raw in traverse_citations(
                [case.url for case in seeds], max_results=max_results, deadline=deadline
            ):
                if deadline and time.monotonic() >= deadline:
                    logger.warning(
                        "Citation traversal aborted mid-loop: wall-time budget "
                        "exhausted (deadline %.1f, now %.1f). %d new opinions found.",
                        deadline,
                        time.monotonic(),
                        traversed,
                    )
                    break
                cases.append(_case_from_raw(raw, claim_issue))
                traversed += 1

    return _dedupe_rank_and_enrich(cases, max_results, enrich, claim_issue, deep_read, deep_read_limit)


def _check_courtlistener_budget_before_run(
    queries: list[str],
    telemetry: list[dict[str, object]] | None = None,
    deep_read: bool = False,
    deep_read_limit: int = 0,
    semantic_queries: int = 0,
) -> None:
    """Abort early when CourtListener's budget can't cover the planned run.

    The free tier applies three rolling windows concurrently (5/min, 50/hour,
    125/day). This guard estimates the total CourtListener request cost
    — search queries × variants × pages **plus** deep-read opinion-detail
    fetches — and pre-flights both the daily window and the per-minute
    window.  Deep-read adds one opinion-detail REST call per ingested case
    that carries a ``courtlistener_opinion_id``, so a deep-read of 6 cases
    burns up to 6 extra requests from both windows.

    When *telemetry* is given, a passing check appends a
    ``{"courtlistener_quota": {...}}`` snapshot so the final output can
    surface the remaining daily window.  Skips when CourtListener is not a
    configured provider or the guard is disabled.

    Called from both :func:`research_issue` (per round, the CLI/batch path)
    and :func:`fetch_cases_for_issue` (the single-pass primitive).
    """
    settings = get_settings()
    if not settings.courtlistener_usage_guard:
        return
    if "courtlistener" not in resolve_search_providers(settings.search_providers):
        return
    variants = max(
        settings.search_query_variants_by_provider.get(
            "courtlistener", settings.search_query_variants
        ),
        1,
    )
    pages = max(
        settings.search_pages_per_query_by_provider.get(
            "courtlistener", settings.search_pages_per_query
        ),
        1,
    )
    search_cost = len(queries) * variants * pages
    # Deep-read opinion-detail fetches each burn one CourtListener REST
    # request (the opinion detail endpoint).  The actual count depends on
    # how many cases carry a courtlistener_opinion_id, but the worst case
    # is every case; use the limit (capped to the search results) as the
    # upper bound so the guard never under-estimates.
    dr_cost = 0
    if deep_read:
        dr_cost = max(deep_read_limit, 0)
    estimated = search_cost + dr_cost + max(semantic_queries, 0)

    # --- Daily window ---
    try:
        budget = check_courtlistener_daily_budget(estimated)
    except SearchError as exc:
        logger.warning("CourtListener usage guard aborted run: %s", exc)
        raise
    logger.info(
        "CourtListener daily budget OK for run (need %d, %d remaining).",
        estimated,
        int(budget["remaining"]),
    )
    if telemetry is not None:
        telemetry.append({"courtlistener_quota": dict(budget)})

    # --- Per-minute window ---
    # Deep-read fetches hit the opinion detail endpoint in rapid succession;
    # without this check the run would grind through 429s with 60-second
    # backoffs.  Search queries are already spaced by _throttle, so the
    # minute window only bites when deep-read adds extra calls.
    if dr_cost:
        try:
            minute_budget = check_courtlistener_minute_budget(dr_cost)
        except SearchError as exc:
            logger.warning("CourtListener usage guard aborted run: %s", exc)
            raise
        if minute_budget:
            logger.info(
                "CourtListener minute budget OK for deep-read (need %d, %d remaining).",
                dr_cost,
                int(minute_budget.get("remaining", 0)),
            )


def _last_courtlistener_quota(
    telemetry: list[dict[str, object]] | None,
) -> dict[str, object] | None:
    """Return the most recent CourtListener quota snapshot, if one was recorded.

    The usage guard appends one ``{"courtlistener_quota": {...}}`` record per
    pre-flight check, so a multi-round run carries several; the last one is
    the freshest picture of the daily window. Returns ``None`` when
    CourtListener was never pre-flighted (guard disabled or not a configured
    provider).
    """
    quota: dict[str, object] | None = None
    for record in telemetry or []:
        snapshot = record.get("courtlistener_quota")
        if isinstance(snapshot, dict):
            quota = snapshot
    return quota


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
    _check_courtlistener_budget_before_run(
        queries,
        telemetry,
        deep_read=bool(deep_read if deep_read is not None else get_settings().deep_read),
        deep_read_limit=(
            deep_read_limit if deep_read_limit is not None else get_settings().deep_read_limit
        ),
    )
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
    settings = get_settings()
    if limit is None:
        limit = settings.enrich_case_limit
    # Quote-first extraction is candidate-only and opt-in. It needs the
    # readable source text, which is already in hand for CourtListener and is
    # returned alongside details for the generic fetch path.
    quote_extraction = bool(getattr(settings, "llm_extraction", False)) and llm_enabled()
    courtlistener = CourtListenerProvider()
    for case in cases[:limit]:
        if not case.url:
            continue
        source_text = ""
        try:
            if case.courtlistener_opinion_id:
                # CourtListener's frontend page is AWS-WAF JS-challenged for
                # every non-browser client (curl_cffi included), so extract
                # the same structured details from the REST opinion detail
                # endpoint's full text instead of scraping the URL.
                text = courtlistener.fetch_opinion_text(int(case.courtlistener_opinion_id))
                details = extract_case_details(text) if text else {}
                source_text = text if text else ""
            elif quote_extraction:
                details, source_text = fetch_case_details_and_text(case.url)
            else:
                details = fetch_case_details(case.url)
        except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
            logger.warning("Could not fetch details for %s: %s", case.url, exc)
            continue
        for key in ("citation", "decision_date", "holding", "docket", "judge", "outcome", "legal_standard"):
            value = details.get(key) or getattr(case, key)
            if value:
                setattr(case, key, value)
        # Oral-argument metadata is advisory and optional. The search result
        # supplies the date and docket id; the recording's public page needs a
        # separate audio lookup, which is best-effort and never blocks the
        # case's text enrichment. Older/fake providers without the method are
        # simply skipped.
        fetch_oral_argument = getattr(courtlistener, "fetch_oral_argument", None)
        oral_lookup = bool(
            getattr(settings, "courtlistener_oral_argument_lookup", False)
        )
        if oral_lookup and callable(fetch_oral_argument) and case.recap_docket_id:
            try:
                oral = fetch_oral_argument(case.recap_docket_id)
            except Exception as exc:  # noqa: BLE001 - advisory metadata only
                logger.info(
                    "Could not fetch oral-argument metadata for %s: %s",
                    case.url,
                    exc,
                )
            else:
                for key in ("oral_argument_url", "oral_argument_date"):
                    value = oral.get(key) if isinstance(oral, dict) else ""
                    if value and not getattr(case, key):
                        setattr(case, key, value)
        # Precedential is tri-state: False means a non-precedential marker was
        # found and True means a readable body carried none. Keep None as
        # unknown instead of coercing it to either certainty.
        status = details.get("precedential")
        if status is not None:
            case.precedential = bool(status)
        if details.get("rule_30a"):
            case.rule_30a = True
            case.precedential = False
        # Populate appellant_role from enrichment so the contradiction detector
        # and impact notes are party-aware (e.g. "affirmed" when the Secretary
        # cross-appealed is favorable to the veteran, not unfavorable).
        role = details.get("appellant_role") or ""
        if role and role != "unknown":
            case.appellant_role = role
        statutes = details.get("statutes") or []
        if statutes:
            case.statutes = list(statutes)
        treatments = details.get("citation_treatments") or []
        if treatments:
            # ``extract_citation_treatments`` returns plain dicts; coerce
            # them into ``CitationTreatment`` models so downstream consumers
            # (``_resolve_superseded_cases``, the LLM prompt) can use
            # attribute access.  Plain assignment would bypass pydantic
            # coercion and leave raw dicts in the field.
            case.citation_treatments = [
                t if isinstance(t, CitationTreatment) else CitationTreatment(**t)
                for t in treatments
            ]
        if quote_extraction and source_text.strip():
            try:
                extraction = extract_quote_candidates(source_text)
            except Exception as exc:  # noqa: BLE001 - candidates are best-effort
                logger.warning(
                    "Quote-first extraction failed for %s: %s",
                    case.url,
                    redact_secrets(str(exc)),
                )
                case.extraction_flags = [
                    f"LLM extraction failed: {redact_secrets(str(exc))}"
                ]
                continue
            for candidate in extraction.candidates:
                deterministic = str(getattr(case, candidate.field, "") or "")
                if deterministic and candidate.value and deterministic != candidate.value:
                    candidate.detail += (
                        f" Deterministic extraction differs ({deterministic!r}); "
                        "candidate only."
                    )
                elif not deterministic:
                    candidate.detail += (
                        " Deterministic extraction was empty; candidate only."
                    )
            case.extraction_candidates = extraction.candidates
            case.extraction_flags = extraction.flags
        elif quote_extraction:
            case.extraction_flags = [
                "LLM extraction not assessed: no readable source text was available."
            ]
    return cases


def summarize_case_impact(case: CaseRecord) -> str:
    """Build a nuanced impact summary for one case.

    Delegates to the impact-analysis layer, which layers procedural-posture,
    statutory-anchor, and authority-weight notes onto the relevance sentence.
    Kept as the public entry point used by the research pipeline.
    """
    return analyze_case_impact(case).nuance


# Treatments that extinguish the prior authority — these make the cited
# case "superseded" and exclude it from the always-on contradiction detector
# (a contradiction between a live case and a superseded one is expected, not
# a conflict). "questioned" and "criticized" are NOT here: they weaken but
# do not overrule the prior authority.
_SUPERSEDING_TREATMENTS = frozenset({"overruled", "abrogated"})


def _resolve_superseded_cases(cases: list[CaseRecord]) -> list[CaseRecord]:
    """Cross-reference citation treatments with dates to mark superseded cases.

    If case B carries ``citation_treatments`` that overrule or abrogate case
    A, and B's decision date is later than A's (or the same year: within
    publication order), A's ``superseded_by`` is set to B's title.

    Called after enrichment (so both ``citation_treatments`` and
    ``decision_date`` are populated) but before the interpretation pass (so
    the contradiction detector can skip superseded pairs). Returns the same
    list of cases, mutated in place.
    """
    if len(cases) < 2:
        return cases
    # Build a title→case index for quick lookup.
    by_title: dict[str, CaseRecord] = {case.title: case for case in cases}
    year_cache: dict[str, int | None] = {}

    for citing in cases:
        for treatment in citing.citation_treatments:
            if treatment.treatment not in _SUPERSEDING_TREATMENTS:
                continue
            cited_name = treatment.cited_case
            cited = by_title.get(cited_name)
            if cited is None:
                continue
            if cited is citing:
                continue
            # Compare years; unknown dates skip the comparison (assume
            # the overruling/abrogating case is later by nature of the
            # treatment signal alone).
            citing_year = year_cache.setdefault(citing.title, _parse_year(citing.decision_date))
            cited_year = year_cache.setdefault(cited.title, _parse_year(cited.decision_date))
            if citing_year is not None and cited_year is not None and citing_year < cited_year:
                continue  # earlier case cannot overrule a later one
            # Only set superseded_by when either:
            #   - both dates known and citing >= cited (same year is OK —
            #     later within the publication cycle), or
            #   - either date unknown (treatment signal alone is enough).
            if not cited.superseded_by:
                cited.superseded_by = citing.title
    return cases


def _citation_lookup_text(analysis: LegalAnalysis, cases: list[CaseRecord]) -> str:
    """Collect the citations and analysis prose sent to one lookup request.

    Case citation fields are the primary inputs; the narrative fields are
    included so a citation invented in the analysis is checked too. Duplicate
    fragments are dropped to keep the single request comfortably below the
    endpoint's 250-citation cap.
    """
    fragments: list[str] = [case.citation for case in cases]
    fragments.extend(
        [
            analysis.summary,
            analysis.how_it_affects_va_claims,
        ]
    )
    fragments.extend(analysis.likely_applicable_principles)
    fragments.extend(analysis.next_steps)
    fragments.extend(analysis.strengths)
    fragments.extend(analysis.gaps)
    fragments.extend(finding.principle for finding in analysis.principle_findings)
    fragments.extend(
        contradiction.statement for contradiction in analysis.contradictions
    )
    fragments.extend(
        str(entry.get("summary") or "") for entry in analysis.deep_summaries
    )
    unique = dict.fromkeys(fragment.strip() for fragment in fragments if fragment and fragment.strip())
    return "\n".join(unique)


def _citation_flags_from_lookup(results: list[dict[str, object]]) -> list[str]:
    """Turn per-citation lookup statuses into actionable output flags."""
    flags: list[str] = []
    seen: set[str] = set()
    for item in results:
        citation = str(item.get("citation") or "").strip()
        if not citation:
            normalized = item.get("normalized_citations")
            if isinstance(normalized, list) and normalized:
                citation = str(normalized[0]).strip()
        citation = citation or "(unknown citation)"
        try:
            status = int(item.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        if status == 200:
            continue
        detail = str(item.get("error_message") or "").strip()
        if status == 404:
            flag = (
                f"Unresolved citation '{citation}': CourtListener found no matching case; "
                "verify it before relying on it."
            )
        elif status == 300:
            flag = (
                f"Ambiguous citation '{citation}': multiple CourtListener cases match; "
                "disambiguate it before relying on it."
            )
        elif status == 400:
            flag = (
                f"Invalid citation '{citation}': CourtListener does not recognize the reporter; "
                "verify the reporter and citation."
            )
        elif status == 429:
            flag = (
                f"Citation '{citation}' was not checked: CourtListener's 250-citation "
                "request limit was exceeded."
            )
        elif status:
            flag = f"Citation '{citation}' returned CourtListener lookup status {status}."
        else:
            flag = f"Citation '{citation}' returned no usable CourtListener lookup status."
        if detail and status in (0, 400):
            flag = f"{flag} ({detail})"
        if flag not in seen:
            flags.append(flag)
            seen.add(flag)
    return flags


def check_analysis_citations(
    analysis: LegalAnalysis, cases: list[CaseRecord]
) -> list[str]:
    """Flag unresolved or ambiguous citations with one batched lookup.

    The check is best-effort: it runs only when ``COURTLISTENER_API_KEY`` is
    configured, pre-flights the live daily window so it never spends the last
    request blindly, and turns lookup/quota failures into explanatory flags
    instead of failing the analysis. A single POST covers every citation in
    the analysis, so the guardrail costs at most one request per analysis.
    """
    if not get_settings().courtlistener_api_key:
        return []
    text = _citation_lookup_text(analysis, cases)
    if not text:
        return []
    try:
        # The lookup itself consumes one main API request, so require at least
        # that much headroom in the live daily window before sending it.
        check_courtlistener_daily_budget(1)
    except SearchError as exc:
        return [f"CourtListener citation check skipped: {exc}"]
    try:
        results = CourtListenerProvider().lookup_citations(text)
    except SearchError as exc:
        return [f"CourtListener citation check failed: {exc}"]
    return _citation_flags_from_lookup(results)


def _top_case_status_and_note(case: CaseRecord) -> tuple[str, str]:
    """Return the precedential status and exact summary note for one case.

    AUD4-012: the human-readable summary and structured top cases must not
    drift. This is the single source of truth for both. A case can carry both
    a non-precedential note and a supersession note, so the note is the
    ordered concatenation used by the summary.
    """
    notes: list[str] = []
    if case.precedential is False:
        notes.append("[non-precedential]")
    elif case.precedential is None:
        notes.append("[precedential status unknown]")
    if case.superseded_by:
        notes.append(f"[superseded by {case.superseded_by}]")

    if case.superseded_by:
        status = "superseded"
    elif case.precedential is False:
        status = "non-precedential"
    elif case.precedential is None:
        status = "unknown"
    else:
        status = "precedential"
    return status, " ".join(notes)


def _top_case_record(case: CaseRecord) -> TopCaseRecord:
    """Build the structured record from the same note used by the summary."""
    status, note = _top_case_status_and_note(case)
    return TopCaseRecord(
        case_title=case.title,
        court=case.court,
        precedential_status=status,
        precedential_note=note,
        display_label=f"{case.title} ({case.court})" + (f" {note}" if note else ""),
    )


def _build_analysis(
    claim_issue: str,
    claim_type: str,
    cases: list[CaseRecord],
    telemetry: list[dict[str, object]] | None,
) -> LegalAnalysis:
    """Build the structured ``LegalAnalysis`` from retrieved, ranked cases."""
    if not cases:
        raise ValueError(f"No cases found for: {claim_issue}")

    # Cross-reference citation treatments with dates: if case B overrules case
    # A, mark A as superseded so the contradiction detector doesn't flag the
    # expected outcome difference as a live conflict.
    _resolve_superseded_cases(cases)
    for case in cases:
        _stamp_authority_tier(case)
        if case.impact_profile is None:
            profile = analyze_case_impact(case)
            case.impact = profile.nuance
            # Preserve already-enriched impacts (for example from a fixture
            # or an earlier extraction pass); the profile retains its own
            # complete impact list regardless.
            if not case.provision_impacts:
                case.provision_impacts = profile.provision_impacts
            case.impact_profile = profile

    top_case_records = [_top_case_record(case) for case in cases[:5]]
    top_cases = [record.display_label for record in top_case_records]
    summary = "\n".join(
        f"- {case.title} ({case.court}) "
        f"[score: {case.composite_score:.2f}, authority: {case.authority_weight}, relevance: {case.relevance_score}]"
        + (f" {record.precedential_note}" if record.precedential_note else "")
        for case, record in zip(cases[:5], top_case_records)
    )

    # The interpretive analysis layer derives principles, strengths, gaps, and
    # next steps from the cases (optionally enhanced by the LLM).
    interpretive = build_interpretive_analysis(claim_issue, claim_type, cases)
    # D1 metadata is preserved in parallel to the legacy display strings.  The
    # legacy list remains for callers that expect strings; every renderer that
    # can carry structure consumes ``surfaced_cases`` instead.
    surfaced_cases = [build_surfaced_case(case) for case in cases[:5]]
    case_abstracts = [
        record.case_abstract
        for record in surfaced_cases
        if record.case_abstract is not None
    ]
    provision_impacts = impacts_from_cases(cases[:5])
    impact_profiles = [
        case.impact_profile
        for case in cases[:5]
        if case.impact_profile is not None
    ]
    impact_profile = impact_profiles[0] if impact_profiles else None
    rvsr_guidance = build_rvsr_guidance(claim_issue, cases[:5])
    senior_rater_guidance = build_senior_rater_guidance(claim_issue, cases[:5])
    source_reliability_summary = summarize_authority_reliability(
        [record.authority_tier for record in surfaced_cases]
    )
    # Deep-read summaries ride along with the top cases so the output shows what
    # full-text ingestion produced per case (empty when deep-read was off or a
    # body could not be fetched).
    deep_summaries: list[dict[str, object]] = []
    for label, case in zip(top_cases, cases[:5]):
        entry: dict[str, object] = {"case": label, "summary": case.deep_summary}
        if case.deep_summary_source:
            entry.update(
                {
                    "summary_source": case.deep_summary_source,
                    "provenance_verified": case.deep_summary_provenance_verified,
                    "verification_notes": list(
                        case.deep_summary_verification_notes
                    ),
                }
            )
            # A provenance-verified summary exports its holding (or snippet)
            # as verified. An unverified summary never promotes its own text,
            # but a real source holding is still surfaced as verified so the
            # reader can compare it against the unverified model text.
            if case.deep_summary_provenance_verified:
                entry["verified_holding"] = case.holding or case.snippet
            elif case.holding:
                entry["verified_holding"] = case.holding
            else:
                entry["unverified_snippet"] = case.snippet
        deep_summaries.append(entry)
    # Quote-backed extraction proposals ride along with the surfaced cases so
    # reviewers can inspect them. They are deliberately not merged into any
    # authoritative field or interpretive input.
    extraction_candidates = [
        CaseExtractionCandidates(
            case=label,
            candidates=case.extraction_candidates,
            flags=case.extraction_flags,
        )
        for label, case in zip(top_cases, cases[:5])
        if case.extraction_candidates or case.extraction_flags
    ]
    rolled_telemetry = rollup_search_telemetry(telemetry or [])

    return LegalAnalysis(
        issue=claim_issue,
        summary=summary,
        likely_applicable_principles=interpretive.likely_applicable_principles,
        how_it_affects_va_claims=interpretive.how_it_affects_va_claims,
        next_steps=interpretive.next_steps,
        top_cases=top_cases,
        top_case_records=top_case_records,
        surfaced_cases=surfaced_cases,
        case_abstracts=case_abstracts,
        provision_impacts=provision_impacts,
        impact_profile=impact_profile,
        impact_profiles=impact_profiles,
        rvsr_guidance=rvsr_guidance,
        senior_rater_guidance=senior_rater_guidance,
        adjudication_guidance=interpretive.adjudication_guidance,
        source_reliability_summary=source_reliability_summary,
        llm_provenance_warnings=interpretive.llm_provenance_warnings,
        llm_provenance_validated=interpretive.llm_provenance_validated,
        deep_summaries=deep_summaries,
        extraction_candidates=extraction_candidates,
        detected_elements=interpretive.detected_elements,
        principle_findings=interpretive.principle_findings,
        contradictions=interpretive.contradictions,
        strengths=interpretive.strengths,
        gaps=interpretive.gaps,
        coverage_score=interpretive.coverage_score,
        coverage_confidence=interpretive.coverage_confidence,
        interpretation_source=interpretive.interpretation_source,
        search_telemetry=rolled_telemetry,
        search_flags=recall_flags(rolled_telemetry),
        courtlistener_quota=_last_courtlistener_quota(telemetry),
        statute_outcome_matrix=interpretive.statute_outcome_matrix,
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
    snapshot_dir: str | None = None,
) -> LegalAnalysis:
    """Run the adaptive research loop and build structured VA-claims guidance."""
    if telemetry is None:
        # Always collect into a sink so the output carries the recall picture
        # and the usage-guard quota snapshot even when the caller doesn't care.
        telemetry = []
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
    settings = get_settings()
    cavc_checks = (
        validate_cavc_panel_cases(cases) if settings.cavc_panel_validation else []
    )
    analysis = _build_analysis(claim_issue, claim_type, cases, telemetry)
    analysis.citation_flags = check_analysis_citations(analysis, cases)
    authority_checks = list(cavc_checks)
    if settings.authority_verification:
        authority_checks.extend(verify_analysis_authorities(analysis, cases))
    analysis.authority_checks = authority_checks
    analysis.authority_flags = authority_flags(authority_checks)
    if settings.authority_verification:
        analysis.statutory_supersession_signals = verify_analysis_supersession(
            analysis, cases, authority_checks
        )
        analysis.statutory_supersession_flags = statutory_supersession_flags(
            analysis.statutory_supersession_signals
        )
        congress = verify_analysis_congress(analysis, cases)
        analysis.congress_links = congress.links
        analysis.congress_crs_reports = congress.crs_reports
        analysis.congress_flags = congress.flags
        regulations = verify_analysis_regulations(analysis, cases)
        analysis.regulations_mappings = regulations.mappings
        analysis.regulations_flags = regulations.flags
    if settings.citator:
        history_records = []
        history_flags: list[str] = []
        snapshot_flags: list[str] = []
        snapshot_store = None
        effective_snapshot_dir = (
            settings.snapshot_dir if snapshot_dir is None else snapshot_dir
        )
        if settings.appellate_history_file:
            try:
                history_records = load_appellate_history_records(
                    settings.appellate_history_file
                )
            except (OSError, ValueError) as exc:
                history_flags.append(
                    "Appellate history verification records unavailable: "
                    f"{exc}"
                )
        if not settings.courtlistener_api_key:
            analysis.citator_flags = [
                "CourtListener citator skipped: COURTLISTENER_API_KEY is not set.",
                *history_flags,
                *snapshot_flags,
            ]
        else:
            if effective_snapshot_dir:
                try:
                    snapshot_store = SnapshotStore(effective_snapshot_dir)
                except OSError as exc:
                    snapshot_flags.append(
                        f"Evidence snapshots unavailable: {exc}"
                    )
            citator_kwargs: dict[str, object] = {
                "max_cases": settings.citator_max_cases,
                "max_citing_opinions": settings.citator_max_citing_opinions,
                "max_pages": settings.citator_max_pages,
            }
            if settings.citator_as_of_date:
                citator_kwargs["as_of_date"] = settings.citator_as_of_date
            if settings.citator_source_validity:
                citator_kwargs["source_validity"] = True
                citator_kwargs["source_validity_mode"] = (
                    settings.citator_source_validity_mode
                )
            if settings.citator_target_cluster_audit:
                citator_kwargs["target_cluster_audit"] = True
            if settings.citator_source_adaptive_allocation:
                citator_kwargs["source_adaptive_allocation"] = True
            if history_records:
                citator_kwargs["history_records"] = history_records
            if snapshot_store is not None:
                citator_kwargs["snapshot_store"] = snapshot_store
            analysis.citator_checks = run_citator(
                cases,
                CourtListenerProvider(),
                **citator_kwargs,
            )
            analysis.authority_queue = [
                item.entry
                for item in apply_citator_findings(
                    cases,
                    analysis.citator_checks,
                    settings.citator_max_cases,
                )
            ]
            if settings.appellate_history_file:
                for check in analysis.citator_checks:
                    if check.appellate_history is not None:
                        check.appellate_history.source = settings.appellate_history_file
            analysis.citator_flags = [
                flag for check in analysis.citator_checks for flag in check.flags
            ]
            analysis.citator_flags.extend(history_flags)
            analysis.citator_flags.extend(snapshot_flags)
    # The citator runs after the initial analysis assembly; fold its optional
    # appellate-history/status evidence back into the D1 records before return.
    apply_citator_checks(analysis, analysis.citator_checks)

    # Post-citator reconciliation (Item 7(a)):
    # Remove citator-negative and source-validity-withheld cases from the live set,
    # then recompute contradictions and statute_outcome_matrix over the remaining cases.
    if settings.citator and analysis.citator_checks:
        live_cases = []
        for record in analysis.surfaced_cases:
            # Check if this record corresponds to a citator-negative or withheld check
            is_negative_or_withheld = False
            for check in analysis.citator_checks:
                if check.target_case.strip().lower() == record.case_title.strip().lower():
                    if check.status == "negative":
                        is_negative_or_withheld = True
                        break
                    # Check for source-validity withheld
                    for signal in check.source_validity:
                        if signal.withheld:
                            is_negative_or_withheld = True
                            break
                    if is_negative_or_withheld:
                        break
            if not is_negative_or_withheld:
                live_cases.append(record)

        # Only recompute if we actually filtered cases and have live cases
        if len(live_cases) < len(analysis.surfaced_cases) and live_cases:
            # Recompute contradictions and statute_outcome_matrix over live cases
            from va_legal_agent.interpretation import (
                detect_contradictions,
                build_statute_outcome_matrix,
            )
            # We need CaseRecord objects for the interpretation functions
            # Create minimal CaseRecord objects from the live SurfacedCaseRecord objects
            from va_legal_agent.models import CaseRecord
            live_case_records = [
                CaseRecord(
                    title=r.case_title,
                    court=r.court,
                    citation=r.reporter_citation or "",
                    decision_date=r.decision_date or "",
                    url=r.retrieved_url or r.official_transcript_url or "",
                )
                for r in live_cases
            ]
            analysis.contradictions = detect_contradictions(live_case_records)
            analysis.statute_outcome_matrix = build_statute_outcome_matrix(live_case_records)

    return analysis


if __name__ == "__main__":
    result = analyze_cases_for_claim("service connection for tinnitus")
    print(result.model_dump_json(indent=2))
