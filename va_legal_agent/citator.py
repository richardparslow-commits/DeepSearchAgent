"""Conservative, advisory citator for a prioritized authority queue.

The citator deliberately answers a narrow question: *within the forward
citations actually reviewed, does an opinion explicitly say that this target
case was negatively treated?* It does not infer treatment from citation depth,
does not search by case name for later appeals, and never changes ranking or
``superseded_by``.

A ``good_law`` result means only that no authoritative negative treatment
was found in the reviewed citing opinions. Only a lead/majority/combined
opinion with CourtListener's ``Published`` status can supply an authoritative
finding; separate, plurality, nonprecedential, and unclassified opinions are
kept as gated signals and force ``unknown`` rather than ``good_law``. It is
not a finding about subsequent history, finality, or whether the case remains
good law. Same-docket decisions and reviewed later citing opinions are
reported as separate advisory signals.

Coverage is part of the result rather than an implementation detail. A clean
result is called ``good_law`` only when the API-reported forward-citation set
was fully fetched, every citing opinion was read, the target reference was
found in each body, and at least one citing date is available. Otherwise the
result is ``unknown`` (or ``negative`` when explicit negative language was
actually found).

When the citing-opinion cap binds, a bounded candidate pool is ranked by
decision date and court authority before review; API edge order is only a
stable tie-break. Pagination is sized around that pool so later, higher-
authority candidates are not skipped merely because they appeared later in the
API response. Every unread candidate or page remains an explicit coverage gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from .appellate_history import build_appellate_history
from .authority_queue import QueuedAuthority, prioritize_authorities
from .snapshots import SnapshotStore, SnapshottingCourtListenerProvider
from .models import (
    AppellateHistoryRecord,
    AuthorityQueueEntry,
    CaseRecord,
    CitatorCheck,
    CitatorCoverage,
    CitatorCoverageGap,
    LaterCitingOpinion,
    NegativeTreatment,
    SameDocketDecision,
    TreatmentTimelineEntry,
)
from .providers import (
    CourtListenerProvider,
    _cluster_citation_values,
    _cluster_identifier,
    _cluster_opinion_ids,
    _positive_id,
)
from .search import SearchError
from .topics import AUTHORITY_WEIGHTS

# Ranking requires one metadata read per candidate, so the candidate pool is
# deliberately bounded. Four times the review cap (at least 20, at most one
# API page) leaves room for metadata/text failures and for lower-ranked
# candidates without turning a capped review into a full citation dump.
_CITING_PRIORITY_POOL_MIN = 20
_CITING_PRIORITY_POOL_FACTOR = 4
_CITING_PRIORITY_POOL_MAX = 100

# Terms CourtListener does not label for us. Keep this list limited to
# explicit negative verbs; "distinguished" is intentionally absent because it
# is not necessarily negative treatment.
_NEGATIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\boverrul(?:e|ed|es|ing)\b", re.I), "overruled"),
    (re.compile(r"\babrogat(?:e|ed|es|ing)\b", re.I), "abrogated"),
    (re.compile(r"\bsupersed(?:e|ed|es|ing)\b", re.I), "superseded"),
    (re.compile(r"\bquestion(?:s|ed|ing)?\b", re.I), "questioned"),
    (re.compile(r"\bcriticiz(?:e|ed|es|ing)\b", re.I), "criticized"),
    (re.compile(r"\blimit(?:ed|s|ing)?\b", re.I), "limited"),
    (re.compile(r"\bdeclin(?:e|ed|es|ing)\s+to\s+follow\b", re.I), "declined to follow"),
)

# Explicit reaffirmance language. "Affirmed" is deliberately absent because
# it usually describes an appellate disposition, not treatment of the target.
_REAFFIRMANCE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\breaffirm(?:ed|s|ing|ance|ation)\b", re.I), "reaffirmed"),
    (re.compile(r"\badher(?:ed|es|ing|ence)\s+to\b", re.I), "adhered to"),
    (
        re.compile(r"\bdeclin(?:ed|es|ing)\s+to\s+overrule\b", re.I),
        "declined to overrule",
    ),
)

# A reporter citation is a competing case reference. Keep this deliberately
# narrow to avoid treating docket numbers or regulations as case citations.
_CITATION_RE = re.compile(
    r"\b\d{4}\s+WL\s+\d+\b"
    r"|\b\d{1,3}\s+(?:Vet\.?\s*App\.?|F\.?\s*(?:2d|3d|4th|App'x|Supp\.?\s*2d)?"
    r"|S\.?\s*Ct\.?|U\.?\s*S\.?|L\.?\s*Ed\.?(?:\s*2d)?|Ct\.?\s*Cl\.?"
    r"|Fed\.?\s*Cir\.?|B\.?\s*R\.?)\s+\d+\b",
    re.I,
)
_CASE_NAME_RE = re.compile(
    r"\b[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*){0,4}\s+v\.\s+"
    r"[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*){0,4}"
)
_ATTRIBUTION_NOUNS = (
    r"(?:argument|assertion|contention|position|views?|claim|notion|theory|"
    r"suggestion|assumption|premise)"
)
_ATTRIBUTION_VERBS = (
    r"(?:argues?|argued|contends?|contended|asserts?|asserted|"
    r"maintains?|maintained|submits?|submitted|urges?|urged|"
    r"requests?|requested|asks?|asked|concedes?|conceded|"
    r"acknowledges?|acknowledged|says?|said|notes?|noted|"
    r"states?|stated|explains?|explained|observes?|observed|"
    r"conclud(?:e|ed|es|ing)|hold(?:s|ing)?|held|find(?:s|ing)?|found|"
    r"determin(?:e|ed|es|ing)|criticiz(?:e|ed|es|ing))"
)
_PARTY_SUBJECT = (
    r"(?:appellant|appellee|claimant|veteran|petitioner|respondent|"
    r"plaintiff|defendant|secretary|government|counsel|amic(?:us|i)|"
    r"briefs?)"
)
_TRIBUNAL_SPEAKER = r"(?:agency|board|department)"
_ADVOCATE_SPEAKER = rf"(?:{_PARTY_SUBJECT}|{_TRIBUNAL_SPEAKER})"
_SEPARATE_SPEAKER = (
    r"(?:dissent(?:ed|ing|er|ers)?|concurrence|concurring|plurality)"
)
_ATTRIBUTION_SPEAKER = rf"(?:{_ADVOCATE_SPEAKER}|{_SEPARATE_SPEAKER})"
_SUBJECT_TAIL = (
    r"(?:'s|’s)?"
    r"(?:\s+(?:in|of|to)\s+[A-Z][A-Za-z'’.-]*)?"
    r"(?:\s+(?:judge|justice|opinion|opinions?|decision|regulation|"
    r"argument|position|view|brief|determination))?"
    r"(?:\s+(?:was|were|is|are|has\s+been|have\s+been))?"
    r"(?:\s*,\s*(?:however|though|also))?\s*$"
)
_PARTY_SUBJECT_RE = re.compile(rf"\b{_PARTY_SUBJECT}\b{_SUBJECT_TAIL}", re.I)
_ATTRIBUTION_FRAME = rf"\b{_ATTRIBUTION_NOUNS}\b(?:\s+(?:is|was))?\s+that\b"
_ADVOCACY_ATTRIBUTION_RE = re.compile(
    rf"{_ATTRIBUTION_FRAME}"
    rf"|\b{_ADVOCATE_SPEAKER}\b(?:\W+\w+){{0,5}}\W+"
    rf"(?:{_ATTRIBUTION_VERBS}|{_ATTRIBUTION_NOUNS})\b",
    re.I,
)
_PASSIVE_PARTY_RE = re.compile(
    rf"^\s*by\s+(?:the\s+)?{_PARTY_SUBJECT}\b", re.I
)
_MODAL = r"(?:would|could|should|might|may)"
_SEPARATE_VOICE_RE = re.compile(
    rf"\b{_SEPARATE_SPEAKER}\b(?:\W+\w+){{0,5}}\W+"
    rf"(?:{_ATTRIBUTION_VERBS}|{_ATTRIBUTION_NOUNS}|{_MODAL})\b",
    re.I,
)
_SEPARATE_SUBJECT_RE = re.compile(
    rf"\b{_SEPARATE_SPEAKER}\b{_SUBJECT_TAIL}", re.I
)
_HYPOTHETICAL_PREFIX_RE = re.compile(
    rf"\b{_MODAL}\b(?:\W+\w+){{0,3}}\W*$"
    r"|\b(?:whether|if)\b(?:\W+\w+){0,12}\W*$",
    re.I,
)
_REPORTED_ATTRIBUTION_RE = re.compile(
    rf"\b(?:according\s+to|per)\s+(?:the\s+)?{_ATTRIBUTION_SPEAKER}\b",
    re.I,
)
_CLAUSE_BOUNDARY_RE = re.compile(
    r";|:|\s+[—–-]\s+|(?:,\s*|\s+)(?:but|yet|while|whereas|although|though|"
    r"however|because|since|after|before|when|where|unless|until|which)\b"
    r"|,\s*and\b",
    re.I,
)
_INTERPOSED_ARGUMENT_RE = re.compile(_ATTRIBUTION_FRAME, re.I)

# Periods after these tokens are not sentence boundaries. This matters for
# reporter citations ("30 Vet.App. 123"), party names ("U.S."), and the
# abbreviation "v." that appears in nearly every case name.
_SENTENCE_BOUNDARY = re.compile(r"[.!?](?=\s|$)")
_ABBREVIATIONS = {
    "app",
    "cir",
    "co",
    "corp",
    "ct",
    "dep't",
    "dr",
    "f.2d",
    "f.3d",
    "f.supp",
    "inc",
    "jr",
    "l.ed",
    "ltd",
    "mr",
    "mrs",
    "ms",
    "mt",
    "no",
    "nos",
    "s.ct",
    "sec'y",
    "sr",
    "st",
    "u.s",
    "vet",
    "vet.app",
    "vs",
}


@dataclass(frozen=True)
class TreatmentMatch:
    """A verbatim negative-treatment sentence found for a target."""

    treatment: str
    excerpt: str
    scope: str
    confidence: str


@dataclass(frozen=True)
class _Target:
    """A top case resolved to a CourtListener cluster and all its opinions."""

    case: CaseRecord
    # Primary opinion/citation retained for display and the same-docket lookup.
    opinion_id: int
    citation: str
    case_name: str
    cluster_id: str = ""
    opinion_ids: tuple[int, ...] = ()
    citations: tuple[str, ...] = ()
    # False only when a direct opinion id could not be expanded to its cluster;
    # that path must not be reported as complete coverage.
    cluster_resolved: bool = True


@dataclass(frozen=True)
class _CitingCandidate:
    """A citing opinion with the metadata needed to prioritize its review."""

    opinion_id: int
    metadata: dict[str, object]
    decision_date: str
    authority: int
    source_index: int


def _checked_at() -> str:
    """Return a stable UTC timestamp for a citator check."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_int(value: object) -> int:
    """Coerce an API counter/status, treating malformed values as zero."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _coverage_gap(code: str, detail: str) -> CitatorCoverageGap:
    """Build one structured coverage gap."""
    return CitatorCoverageGap(code=code, detail=detail)


def _citing_priority_page_size(max_citing: int) -> int:
    """Return the adaptive forward-citation page size for a review cap."""
    return min(
        _CITING_PRIORITY_POOL_MAX,
        max(
            _CITING_PRIORITY_POOL_MIN,
            max(max_citing, 1) * _CITING_PRIORITY_POOL_FACTOR,
        ),
    )


def _citing_priority_pool_size(max_citing: int, max_pages: int) -> int:
    """Return the bounded metadata pool available to the review cap."""
    return min(
        _CITING_PRIORITY_POOL_MAX,
        _citing_priority_page_size(max_citing) * max(max_pages, 1),
    )


def _temporal_confidence(
    *,
    complete: bool,
    reviewed: int,
    latest: str,
    gaps: list[CitatorCoverageGap],
) -> tuple[str, str]:
    """Describe how strongly the reviewed citation dates bound the check."""
    if reviewed == 0:
        return (
            "none",
            "No citing opinion was reviewed, so no later date can bound the check.",
        )
    if not latest:
        return (
            "none",
            "No reviewed citing opinion had a usable decision date.",
        )
    if any(gap.code == "citing_date_missing" for gap in gaps):
        return (
            "low",
            f"Latest reviewed citing opinion is {latest}, but at least one "
            "reviewed citing opinion has no decision date.",
        )
    if complete:
        return (
            "high",
            f"Latest reviewed citing opinion is {latest}; coverage is complete "
            "and every reviewed citing opinion has a date.",
        )
    return (
        "medium",
        f"Latest reviewed citing opinion is {latest}; coverage is incomplete, "
        "so later citing opinions may exist.",
    )


def _normalize_citation(value: object) -> str:
    """Normalize a citation for comparison across lookup response variants."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _result_citations(row: dict[str, object]) -> list[str]:
    """Return the citation strings a citation-lookup result describes."""
    values: list[object] = [row.get("citation")]
    normalized = row.get("normalized_citations")
    if isinstance(normalized, list):
        values.extend(normalized)
    return [str(value) for value in values if value]


def _select_lookup_row(
    results: list[dict[str, object]], citation: str
) -> dict[str, object] | None:
    """Pick the one lookup row matching *citation*, or None when ambiguous."""
    if not results:
        return None
    if len(results) == 1:
        return results[0]
    wanted = _normalize_citation(citation)
    matches = [
        row
        for row in results
        if wanted and wanted in {_normalize_citation(value) for value in _result_citations(row)}
    ]
    return matches[0] if len(matches) == 1 else None


def _merged_citations(primary: str, values: object) -> tuple[str, ...]:
    """Put the case's known citation first, then every cluster citation."""
    citations: list[str] = []
    for value in (primary, *_cluster_citation_values(values)):
        text = str(value or "").strip()
        if text and text not in citations:
            citations.append(text)
    return tuple(citations)


def _target_from_metadata(
    case: CaseRecord, opinion_id: int, metadata: dict[str, object], title: str
) -> _Target:
    """Expand a direct opinion id with its cluster's citations and sub-opinions."""
    opinion_ids = [opinion_id]
    for value in metadata.get("opinion_ids") or []:
        parsed = _positive_id(value)
        if parsed is not None and parsed not in opinion_ids:
            opinion_ids.append(parsed)
    citations = _merged_citations(case.citation, metadata.get("citations"))
    cluster_id = str(metadata.get("cluster_id") or "")
    case_name = case.title or str(metadata.get("title") or "") or title
    # Only the provider's explicit flag proves the cluster fetch succeeded;
    # sparse metadata always carries the lone requested opinion id.
    resolved = metadata.get("cluster_resolved") is True
    return _Target(
        case,
        opinion_id,
        citations[0] if citations else "",
        case_name,
        cluster_id,
        tuple(opinion_ids),
        citations,
        resolved,
    )


def _resolve_target(
    case: CaseRecord, provider: CourtListenerProvider
) -> tuple[_Target | None, list[str]]:
    """Resolve one top case to a CourtListener cluster.

    An existing opinion id is expanded with a metadata fetch so the whole
    cluster's sub-opinions and parallel citations are known. Otherwise the
    case citation is resolved with one POST. Ambiguous, missing, and malformed
    lookup results are left as unknown rather than guessed.
    """
    title = case.title or "Untitled case"
    direct_id = _positive_id(case.courtlistener_opinion_id)
    if direct_id is not None:
        flags: list[str] = []
        try:
            metadata = provider.opinion_metadata(direct_id)
        except SearchError as exc:
            metadata = None
            flags.append(
                f"Citator could not resolve the full cluster for '{title}' "
                f"(opinion {direct_id}): {exc}"
            )
        if metadata:
            target = _target_from_metadata(case, direct_id, metadata, title)
            if not target.cluster_resolved:
                flags.append(
                    f"Citator could not establish the full CourtListener "
                    f"cluster for '{title}'; only opinion {direct_id} was checked."
                )
            return target, flags
        flags.append(
            f"Citator could not resolve the full CourtListener cluster for "
            f"'{title}'; only opinion {direct_id} was checked."
        )
        return (
            _Target(
                case,
                direct_id,
                case.citation,
                title,
                opinion_ids=(direct_id,),
                citations=(case.citation,) if case.citation else (),
                cluster_resolved=False,
            ),
            flags,
        )

    citation = case.citation.strip()
    if not citation:
        return None, [
            f"Citator skipped '{title}': no citation or CourtListener opinion id."
        ]

    try:
        results = provider.lookup_citations(citation)
    except SearchError as exc:
        return None, [f"Citator resolution failed for '{title}': {exc}"]

    row = _select_lookup_row(results, citation)
    if row is None:
        if not results:
            detail = "lookup returned no results"
        else:
            detail = "lookup returned multiple ambiguous results"
        return None, [
            f"Citator skipped '{title}': citation '{citation}' was unresolved "
            f"({detail})."
        ]

    status = _safe_int(row.get("status"))
    if status != 200:
        return None, [
            f"Citator skipped '{title}': citation '{citation}' returned lookup "
            f"status {status or 'unknown'}."
        ]

    clusters = row.get("clusters")
    if not isinstance(clusters, list) or len(clusters) != 1:
        return None, [
            f"Citator skipped '{title}': citation '{citation}' did not resolve "
            "to exactly one CourtListener cluster."
        ]
    cluster = clusters[0]
    if not isinstance(cluster, dict):
        return None, [
            f"Citator skipped '{title}': citation '{citation}' had a malformed "
            "CourtListener cluster."
        ]
    opinion_ids = _cluster_opinion_ids(cluster)
    if not opinion_ids:
        return None, [
            f"Citator skipped '{title}': citation '{citation}' resolved to a "
            "cluster without an opinion id."
        ]
    case_name = case.title or str(cluster.get("case_name") or "") or title
    citations = _merged_citations(
        case.citation, _cluster_citation_values(cluster.get("citations"))
    )
    return (
        _Target(
            case,
            opinion_ids[0],
            citations[0],
            case_name,
            _cluster_identifier(cluster),
            tuple(opinion_ids),
            citations,
            True,
        ),
        [],
    )


def _cursor_from_next(value: object) -> str:
    """Return the cursor parameter from a CourtListener ``next`` URL."""
    if not value:
        return ""
    query = parse_qs(urlsplit(str(value)).query)
    return (query.get("cursor") or [""])[0]


def _fetch_forward_edges(
    provider: CourtListenerProvider,
    opinion_id: int,
    max_pages: int,
    page_size: int = 100,
    max_ids: int = _CITING_PRIORITY_POOL_MAX,
) -> tuple[
    list[int], int, int, bool, bool, list[str], list[CitatorCoverageGap]
]:
    """Fetch forward-citation edges and report what was actually covered.

    Returns ``(citing_ids, count, pages_fetched, complete, truncated, flags,
    gaps)``. The ids are de-duplicated in API order. ``count`` is the API's
    full forward-citation count when available; ``complete`` is true only when
    no page cap, count mismatch, cursor problem, or request error intervened.

    ``page_size`` and ``max_ids`` adapt pagination to the citator's bounded
    review pool: the page request never fetches far beyond the candidates the
    caller can rank, and paging stops once that pool is full. Any remaining
    cursor is still reported as an explicit ``page_cap`` gap.
    """
    ids: list[int] = []
    seen: set[int] = set()
    seen_cursors: set[str] = set()
    pages = 0
    count = 0
    cursor: str | None = None
    complete = True
    truncated = False
    flags: list[str] = []
    gaps: list[CitatorCoverageGap] = []

    while pages < max_pages and len(ids) < max_ids:
        try:
            data = provider.citing_edges(
                opinion_id, page_size=page_size, cursor=cursor
            )
        except SearchError as exc:
            detail = (
                f"Citator forward-citation fetch failed for opinion "
                f"{opinion_id}: {exc}"
            )
            flags.append(detail)
            gaps.append(_coverage_gap("forward_fetch_failed", detail))
            complete = False
            break
        pages += 1
        page_count = _safe_int(data.get("count"))
        if page_count > count:
            count = page_count
        rows = data.get("results")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                citing_id = _positive_id(row.get("citing_opinion"))
                if citing_id is not None and citing_id not in seen:
                    seen.add(citing_id)
                    ids.append(citing_id)
        next_url = data.get("next")
        cursor = _cursor_from_next(next_url)
        if not cursor:
            if next_url:
                detail = (
                    f"Citator forward-citation pagination failed for opinion "
                    f"{opinion_id}: next link had no cursor."
                )
                flags.append(detail)
                gaps.append(_coverage_gap("pagination_cursor_missing", detail))
                complete = False
            break
        if cursor in seen_cursors:
            detail = (
                f"Citator forward-citation pagination repeated a cursor for "
                f"opinion {opinion_id}."
            )
            flags.append(detail)
            gaps.append(_coverage_gap("pagination_cursor_repeated", detail))
            complete = False
            break
        seen_cursors.add(cursor)

    if count < len(ids):
        count = len(ids)
    if cursor and (pages >= max_pages or len(ids) >= max_ids):
        truncated = True
        complete = False
        if pages >= max_pages:
            boundary = f"the {max_pages}-page cap"
        else:
            boundary = f"the {max_ids}-candidate priority pool"
        detail = (
            f"Citator forward-citation coverage for opinion {opinion_id} "
            f"stopped at {boundary}; more pages remain."
        )
        flags.append(detail)
        gaps.append(_coverage_gap("page_cap", detail))
    if count > len(ids):
        truncated = True
        complete = False
        detail = (
            f"Citator forward-citation coverage for opinion {opinion_id} is "
            f"incomplete: {count} reported, {len(ids)} collected."
        )
        flags.append(detail)
        gaps.append(_coverage_gap("count_mismatch", detail))
    return ids, count, pages, complete, truncated, flags, gaps


def _fetch_cluster_forward_edges(
    provider: CourtListenerProvider,
    target: _Target,
    max_pages: int,
    page_size: int = 100,
    max_ids: int = _CITING_PRIORITY_POOL_MAX,
) -> tuple[
    list[int], int, int, bool, bool, list[str], list[CitatorCoverageGap]
]:
    """Fetch forward citations for every opinion in the target's cluster.

    A citing opinion is linked to one specific target opinion, so checking only
    the first sub-opinion misses citations attached to the lead, concurrence,
    or dissent. The returned ids are de-duplicated across the cluster. A
    one-opinion target keeps the API's reported count; a multi-opinion target
    reports the unique count, since the same citing opinion may be linked to
    more than one sub-opinion.
    """
    ids: list[int] = []
    seen: set[int] = set()
    pages = 0
    count = 0
    complete = True
    truncated = False
    flags: list[str] = []
    gaps: list[CitatorCoverageGap] = []
    opinion_ids = target.opinion_ids
    for opinion_id in opinion_ids:
        (
            found_ids,
            found_count,
            found_pages,
            found_complete,
            found_truncated,
            edge_flags,
            edge_gaps,
        ) = _fetch_forward_edges(
            provider,
            opinion_id,
            max_pages,
            page_size=page_size,
            max_ids=max_ids,
        )
        pages += found_pages
        count += found_count
        complete = complete and found_complete
        truncated = truncated or found_truncated
        flags.extend(edge_flags)
        gaps.extend(edge_gaps)
        for citing_id in found_ids:
            if citing_id not in seen:
                seen.add(citing_id)
                ids.append(citing_id)
    count = len(ids) if len(opinion_ids) > 1 else count
    return ids, count, pages, complete, truncated, flags, gaps


def _phrase_spans(text: str, phrase: str) -> list[tuple[int, int]]:
    """Return non-overlapping exact-phrase spans, ignoring whitespace runs."""
    cleaned = " ".join(phrase.split())
    if not cleaned:
        return []
    body = r"\s+".join(re.escape(part) for part in cleaned.split())
    pattern = re.compile(rf"(?<!\w){body}(?!\w)", re.I)
    return [(match.start(), match.end()) for match in pattern.finditer(text)]


def _target_spans(
    text: str, target_title: str, target_citations: str | tuple[str, ...]
) -> list[tuple[int, int]]:
    """Find any parallel citation or case-name span for the target in *text*."""
    citations = (target_citations,) if isinstance(target_citations, str) else target_citations
    spans: set[tuple[int, int]] = set()
    for phrase in (*citations, target_title):
        spans.update(_phrase_spans(text, phrase))
    return sorted(spans)


def _span_distance(left: tuple[int, int], right: tuple[int, int]) -> int:
    """Return the distance between two spans, or zero when they overlap."""
    if left[1] <= right[0]:
        return right[0] - left[1]
    if right[1] <= left[0]:
        return left[0] - right[1]
    return 0


def _overlaps(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _spans_within(
    spans: list[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    return [span for span in spans if span[0] >= start and span[1] <= end]


def _clause_bounds(text: str, position: int) -> tuple[int, int]:
    """Return the clause containing *position* within a sentence."""
    start = 0
    end = len(text)
    for match in _CLAUSE_BOUNDARY_RE.finditer(text):
        if match.end() <= position:
            start = match.end()
            continue
        end = match.start()
        break
    return start, end


def _nearest_distance(
    span: tuple[int, int], candidates: list[tuple[int, int]]
) -> int | None:
    """Return the distance to the nearest candidate span, if any."""
    return min((_span_distance(span, candidate) for candidate in candidates), default=None)


def _attribution_rejects(
    prefix: str, opinion_role: str, suffix: str = ""
) -> bool:
    """Whether a treatment belongs to an advocate, another voice, or a hypothetical."""
    if (
        _PARTY_SUBJECT_RE.search(prefix)
        or _PASSIVE_PARTY_RE.search(prefix)
        or _PASSIVE_PARTY_RE.search(suffix)
        or _ADVOCACY_ATTRIBUTION_RE.search(prefix)
        or _ADVOCACY_ATTRIBUTION_RE.search(suffix)
        or _REPORTED_ATTRIBUTION_RE.search(prefix)
        or _REPORTED_ATTRIBUTION_RE.search(suffix)
    ):
        return True
    if opinion_role != "majority":
        return False
    return bool(
        _SEPARATE_VOICE_RE.search(prefix)
        or _SEPARATE_VOICE_RE.search(suffix)
        or _SEPARATE_SUBJECT_RE.search(prefix)
        or _HYPOTHETICAL_PREFIX_RE.search(prefix)
    )


def _binds_to_target(
    sentence: str,
    verb: tuple[int, int],
    target_title: str,
    target_citations: tuple[str, ...],
) -> bool:
    """Whether *verb* is bound to the target rather than a competing case."""
    citation_spans = [
        (match.start(), match.end()) for match in _CITATION_RE.finditer(sentence)
    ]
    target_keys = {
        _normalize_citation(citation)
        for citation in target_citations
        if citation
    }
    target_citation_spans = [
        span
        for span in citation_spans
        if _normalize_citation(sentence[span[0] : span[1]]) in target_keys
    ]
    for citation in target_citations:
        target_citation_spans.extend(_phrase_spans(sentence, citation))
    target_citation_spans = sorted(set(target_citation_spans))
    target_title_spans = _phrase_spans(sentence, target_title)
    target_references = [*target_citation_spans, *target_title_spans]
    if not target_references:
        return False

    clause_start, clause_end = _clause_bounds(sentence, verb[0])
    citations_in_clause = _spans_within(citation_spans, clause_start, clause_end)
    target_citations_in_clause = _spans_within(
        target_citation_spans, clause_start, clause_end
    )
    target_titles_in_clause = _spans_within(
        target_title_spans, clause_start, clause_end
    )
    if target_citations_in_clause:
        target_span = min(
            target_citations_in_clause,
            key=lambda span: _span_distance(verb, span),
        )
    elif target_titles_in_clause:
        # A bare case name is usable only when no different reporter citation
        # appears in the same clause. This rejects same-name/different-citation
        # sentences before they can become treatment findings.
        if citations_in_clause:
            return False
        target_span = min(
            target_titles_in_clause,
            key=lambda span: _span_distance(verb, span),
        )
    else:
        return False

    case_name_spans = [
        (match.start(), match.end()) for match in _CASE_NAME_RE.finditer(sentence)
    ]
    competitors = [
        span
        for span in (*citation_spans, *case_name_spans)
        if not any(_overlaps(span, target) for target in target_references)
    ]
    competitors_in_clause = _spans_within(
        competitors, clause_start, clause_end
    )
    # Only compare references on the same side of the verb. A preceding
    # subject is not competing to be the verb's object.
    if target_span[0] >= verb[1]:
        competitors_in_clause = [
            span for span in competitors_in_clause if span[0] >= verb[1]
        ]
    else:
        competitors_in_clause = [
            span for span in competitors_in_clause if span[1] <= verb[0]
        ]
    competitor_distance = _nearest_distance(verb, competitors_in_clause)
    target_distance = _span_distance(verb, target_span)
    if competitor_distance is not None and competitor_distance < target_distance:
        return False

    between = (
        sentence[verb[1] : target_span[0]]
        if target_span[0] >= verb[1]
        else sentence[target_span[1] : verb[0]]
    )
    return not _INTERPOSED_ARGUMENT_RE.search(between)


def _is_abbreviation(text: str, position: int) -> bool:
    """Whether a period at *position* ends an abbreviation, not a sentence."""
    prefix = text[max(0, position - 30) : position]
    match = re.search(r"([A-Za-z][A-Za-z.'’]*)$", prefix)
    if not match:
        return False
    token = match.group(1).rstrip(".").lower()
    return token in _ABBREVIATIONS or (len(token) == 1 and token.isalpha())


def _sentence_bounds(text: str, position: int) -> tuple[int, int]:
    """Return the sentence containing *position*, with abbreviation handling."""
    start = 0
    end = len(text)
    for match in _SENTENCE_BOUNDARY.finditer(text):
        if _is_abbreviation(text, match.start()):
            continue
        boundary = match.end()
        if boundary <= position:
            start = boundary
        else:
            end = boundary
            break
    return start, end


def _is_negated(sentence: str, term_start: int) -> bool:
    """Whether a negative term is itself negated (e.g. 'not overruled')."""
    prefix = sentence[max(0, term_start - 40) : term_start].lower()
    return bool(
        re.search(
            r"\b(?:not|never|without|no\s+longer|declined\s+to|refused\s+to|"
            r"failed\s+to)\b(?:\W+\w+){0,3}\W*$",
            prefix,
        )
    )


def _scope_for(treatment: str, sentence: str) -> str:
    """Classify the scope stated for a negative treatment."""
    if re.search(r"\bin part\b", sentence, re.I):
        return "part"
    if treatment in {
        "overruled",
        "abrogated",
        "superseded",
        "reaffirmed",
        "adhered to",
        "declined to overrule",
    }:
        return "all"
    return "unclear"


_MAJORITY_OPINION_TYPES = frozenset(
    {
        "010combined",
        "015unamimous",
        "015unanimous",
        "020lead",
        "combined",
        "combined-opinion",
        "lead",
        "lead-opinion",
        "majority",
        "majority-opinion",
        "unanimous",
        "unanimous-opinion",
    }
)
_PLURALITY_OPINION_TYPES = frozenset(
    {"025plurality", "plurality", "plurality-opinion"}
)
_SEPARATE_OPINION_TYPES = frozenset(
    {
        "030concurrence",
        "035concurrenceinpart",
        "040dissent",
        "concurrence",
        "concurrence-in-part",
        "concurrence-in-part-opinion",
        "concurrence-opinion",
        "dissent",
        "dissent-opinion",
    }
)


def _opinion_role(opinion_type: str) -> str:
    """Normalize CourtListener's opinion-type code into a review role."""
    normalized = opinion_type.strip().lower().replace("_", "-").replace(" ", "-")
    if normalized in _MAJORITY_OPINION_TYPES:
        return "majority"
    if normalized in _PLURALITY_OPINION_TYPES:
        return "plurality"
    if normalized in _SEPARATE_OPINION_TYPES:
        return "separate"
    if not normalized:
        return "unknown"
    return "other"


def _precedential_tier(status: str) -> str:
    """Normalize a cluster's precedential status for the treatment gate."""
    normalized = status.strip().lower()
    if normalized == "published":
        return "precedential"
    if normalized == "unpublished":
        return "nonprecedential"
    if normalized in {"", "unknown"}:
        return "unknown"
    return "other"


def _authority_gate(
    opinion_type: str, status: str
) -> tuple[str, str, bool, str]:
    """Return role, tier, eligibility, and a human-readable gate reason.

    Only a lead/majority/combined opinion with CourtListener's ``Published``
    status can support an authoritative negative-treatment finding. Separate
    opinions, plurality opinions, nonprecedential opinions, and opinions with
    unknown or nonstandard status remain visible but are gated.
    """
    role = _opinion_role(opinion_type)
    tier = _precedential_tier(status)
    reasons: list[str] = []
    if role != "majority":
        reasons.append(f"opinion role is {role}")
    if tier != "precedential":
        reasons.append(f"precedential status is {tier}")
    return role, tier, not reasons, "; ".join(reasons)


def _extract_treatment_matches(
    text: str,
    target_title: str,
    target_citations: str | tuple[str, ...],
    patterns: tuple[tuple[re.Pattern[str], str], ...],
    *,
    opinion_role: str,
) -> list[TreatmentMatch]:
    """Return target-bound treatment sentences for the supplied patterns."""
    citations = (
        (target_citations,)
        if isinstance(target_citations, str)
        else target_citations
    )
    findings: list[TreatmentMatch] = []
    seen: set[tuple[str, str]] = set()
    for pattern, treatment in patterns:
        for match in pattern.finditer(text):
            start, end = _sentence_bounds(text, match.start())
            raw = text[start:end]
            lead = len(raw) - len(raw.lstrip())
            sentence = raw.strip()
            verb_start = match.start() - start - lead
            verb_end = match.end() - start - lead
            clause_start, clause_end = _clause_bounds(sentence, verb_start)
            if _attribution_rejects(
                sentence[clause_start:verb_start],
                opinion_role,
                sentence[verb_end:clause_end],
            ):
                continue
            if _is_negated(sentence, verb_start):
                continue
            if not _binds_to_target(
                sentence, (verb_start, verb_end), target_title, citations
            ):
                continue
            key = (treatment, sentence)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                TreatmentMatch(
                    treatment=treatment,
                    excerpt=sentence,
                    scope=_scope_for(treatment, sentence),
                    confidence="medium" if treatment == "limited" else "high",
                )
            )
    return findings


def extract_negative_treatments(
    text: str,
    target_title: str,
    target_citations: str | tuple[str, ...],
    *,
    opinion_role: str = "majority",
) -> list[TreatmentMatch]:
    """Return target-specific negative-treatment holdings from *text*.

    The verb must bind to the target citation, or to a bare target name only
    when no competing citation is present. For a majority opinion, treatment
    attributed to a dissent, party argument, or hypothetical is ignored.
    """
    return _extract_treatment_matches(
        text,
        target_title,
        target_citations,
        _NEGATIVE_PATTERNS,
        opinion_role=opinion_role,
    )


def extract_reaffirmance_signals(
    text: str,
    target_title: str,
    target_citations: str | tuple[str, ...],
    *,
    opinion_role: str = "majority",
) -> list[TreatmentMatch]:
    """Return target-specific reaffirmance signals from *text*.

    These signals are advisory and never clear an authoritative negative
    finding. They use the same target-binding and attribution rules as
    negative-treatment extraction.
    """
    return _extract_treatment_matches(
        text,
        target_title,
        target_citations,
        _REAFFIRMANCE_PATTERNS,
        opinion_role=opinion_role,
    )


def _binding_for(target: _Target, citing_court: str) -> bool | None:
    """Whether a citing court outranks (or matches) the target court."""
    target_weight = AUTHORITY_WEIGHTS.get(target.case.court, 0)
    citing_weight = AUTHORITY_WEIGHTS.get(citing_court, 0)
    if not target_weight or not citing_weight:
        return None
    return citing_weight >= target_weight


def _binding_class(value: bool | None) -> str:
    """Return the report label for a treatment's binding relationship."""
    if value is True:
        return "binding"
    if value is False:
        return "persuasive"
    return "unknown"


def _timeline_entry(
    signal: str, finding: NegativeTreatment
) -> TreatmentTimelineEntry:
    """Build one chronological timeline view from a treatment finding."""
    return TreatmentTimelineEntry(
        signal=signal,
        treatment=finding.treatment,
        citing_case=finding.citing_case,
        excerpt=finding.excerpt,
        scope=finding.scope,
        citing_citation=finding.citing_citation,
        citing_court=finding.citing_court,
        citing_date=finding.citing_date,
        citing_url=finding.citing_url,
        binding=finding.binding,
        binding_class=_binding_class(finding.binding),
        authoritative=finding.authoritative,
        opinion_type=finding.opinion_type,
        opinion_role=finding.opinion_role,
        precedential_status=finding.precedential_status,
        precedential_tier=finding.precedential_tier,
        gate_reason=finding.gate_reason,
    )


def _load_citing_candidates(
    provider: CourtListenerProvider, citing_ids: list[int]
) -> tuple[list[_CitingCandidate], list[str], list[CitatorCoverageGap]]:
    """Fetch ranking metadata once, then order candidates for a capped review.

    Later decisions come first because they are the most probative of current
    treatment. Court authority breaks date ties. API order is used only as the
    final stable tie-break. Metadata failures are retained as coverage gaps,
    since a failed candidate could have outranked the selected set.
    """
    candidates: list[_CitingCandidate] = []
    flags: list[str] = []
    gaps: list[CitatorCoverageGap] = []
    for source_index, opinion_id in enumerate(citing_ids):
        try:
            metadata = provider.opinion_metadata(opinion_id)
        except SearchError as exc:
            detail = (
                f"Citator metadata fetch failed for citing opinion "
                f"{opinion_id}: {exc}"
            )
            flags.append(detail)
            gaps.append(_coverage_gap("citing_metadata_fetch_failed", detail))
            continue
        if not metadata:
            detail = (
                f"Citator metadata was unavailable for citing opinion "
                f"{opinion_id}."
            )
            flags.append(detail)
            gaps.append(_coverage_gap("citing_metadata_unavailable", detail))
            continue
        citing_court = str(metadata.get("court") or "")
        candidates.append(
            _CitingCandidate(
                opinion_id=opinion_id,
                metadata=metadata,
                decision_date=str(metadata.get("decision_date") or ""),
                authority=AUTHORITY_WEIGHTS.get(citing_court, 0),
                source_index=source_index,
            )
        )
    candidates.sort(
        key=lambda candidate: (
            bool(candidate.decision_date),
            candidate.decision_date,
            candidate.authority,
            -candidate.source_index,
        ),
        reverse=True,
    )
    return candidates, flags, gaps


def _review_citing_opinions(
    provider: CourtListenerProvider,
    target: _Target,
    citing_ids: list[int],
    max_citing: int,
) -> tuple[
    list[NegativeTreatment],
    list[NegativeTreatment],
    list[LaterCitingOpinion],
    int,
    str,
    list[str],
    list[CitatorCoverageGap],
    list[TreatmentTimelineEntry],
    bool,
]:
    """Read capped citing opinions and collect gated and authoritative signals."""
    treatments: list[NegativeTreatment] = []
    gated: list[NegativeTreatment] = []
    later_citing: list[LaterCitingOpinion] = []
    timeline: list[TreatmentTimelineEntry] = []
    reviewed = 0
    latest = ""
    flags: list[str] = []
    gaps: list[CitatorCoverageGap] = []
    candidates, candidate_flags, candidate_gaps = _load_citing_candidates(
        provider, citing_ids
    )
    flags.extend(candidate_flags)
    gaps.extend(candidate_gaps)
    for candidate in candidates:
        if reviewed >= max_citing:
            break
        opinion_id = candidate.opinion_id
        metadata = candidate.metadata
        try:
            text = provider.fetch_opinion_text(opinion_id)
        except SearchError as exc:
            detail = (
                f"Citator text fetch failed for citing opinion {opinion_id}: {exc}"
            )
            flags.append(detail)
            gaps.append(_coverage_gap("citing_text_fetch_failed", detail))
            continue
        if not text.strip():
            detail = f"Citator text was empty for citing opinion {opinion_id}."
            flags.append(detail)
            gaps.append(_coverage_gap("citing_text_empty", detail))
            continue
        if not _target_spans(text, target.case_name, target.citations):
            detail = (
                f"Citator could not locate '{target.case_name}' in citing opinion "
                f"{opinion_id}; treatment was not verified."
            )
            flags.append(detail)
            gaps.append(_coverage_gap("target_not_located", detail))
            continue
        reviewed += 1
        citing_date = str(metadata.get("decision_date") or "")
        citing_court = str(metadata.get("court") or "")
        opinion_type = str(metadata.get("opinion_type") or "")
        precedential_status = str(metadata.get("precedential_status") or "")
        opinion_role, precedential_tier, eligible, gate_reason = _authority_gate(
            opinion_type, precedential_status
        )
        later_citing.append(
            LaterCitingOpinion(
                case_name=str(metadata.get("title") or f"Opinion {opinion_id}"),
                citation=str(metadata.get("citation") or ""),
                court=citing_court,
                decision_date=citing_date,
                url=str(metadata.get("url") or ""),
                opinion_id=str(opinion_id),
                opinion_type=opinion_type,
                opinion_role=opinion_role,
                precedential_status=precedential_status,
                precedential_tier=precedential_tier,
                treatment_eligible=eligible,
                gate_reason=gate_reason,
            )
        )
        if not citing_date:
            detail = (
                f"Citator could not establish a decision date for citing opinion "
                f"{opinion_id}; a good-law date cannot be bounded."
            )
            flags.append(detail)
            gaps.append(_coverage_gap("citing_date_missing", detail))
        elif not latest or citing_date > latest:
            latest = citing_date
        for finding in extract_negative_treatments(
            text,
            target.case_name,
            target.citations,
            opinion_role=opinion_role,
        ):
            item = NegativeTreatment(
                citing_case=str(metadata.get("title") or f"Opinion {opinion_id}"),
                citing_citation=str(metadata.get("citation") or ""),
                citing_court=citing_court,
                citing_date=citing_date,
                citing_url=str(metadata.get("url") or ""),
                treatment=finding.treatment,
                scope=finding.scope,
                binding=_binding_for(target, citing_court),
                confidence=finding.confidence,
                opinion_type=opinion_type,
                opinion_role=opinion_role,
                precedential_status=precedential_status,
                precedential_tier=precedential_tier,
                authoritative=eligible,
                gate_reason=gate_reason,
                excerpt=finding.excerpt,
            )
            if eligible:
                treatments.append(item)
            else:
                gated.append(item)
            timeline.append(_timeline_entry("negative", item))
        for finding in extract_reaffirmance_signals(
            text,
            target.case_name,
            target.citations,
            opinion_role=opinion_role,
        ):
            # Reuse the treatment metadata shape for the timeline; this
            # positive entry never enters a negative-treatment list.
            reaffirmance = NegativeTreatment(
                citing_case=str(metadata.get("title") or f"Opinion {opinion_id}"),
                citing_citation=str(metadata.get("citation") or ""),
                citing_court=citing_court,
                citing_date=citing_date,
                citing_url=str(metadata.get("url") or ""),
                treatment=finding.treatment,
                scope=finding.scope,
                binding=_binding_for(target, citing_court),
                confidence=finding.confidence,
                opinion_type=opinion_type,
                opinion_role=opinion_role,
                precedential_status=precedential_status,
                precedential_tier=precedential_tier,
                authoritative=eligible,
                gate_reason=gate_reason,
                excerpt=finding.excerpt,
            )
            timeline.append(_timeline_entry("reaffirmed", reaffirmance))
    if len(citing_ids) > max_citing:
        detail = (
            f"Citator reviewed {reviewed} of {len(citing_ids)} citing "
            f"opinions because of the {max_citing}-opinion cap; candidates "
            "were prioritized by decision date and court authority."
        )
        flags.append(detail)
        gaps.append(_coverage_gap("citing_opinion_cap", detail))
    negative_dates = sorted(
        entry.citing_date
        for entry in timeline
        if entry.signal == "negative"
        and entry.authoritative
        and entry.citing_date
    )
    latest_negative = negative_dates[-1] if negative_dates else ""
    for entry in timeline:
        entry.later_than_negative = bool(
            entry.signal == "reaffirmed"
            and latest_negative
            and entry.citing_date
            and entry.citing_date > latest_negative
        )
    timeline.sort(
        key=lambda entry: (
            not bool(entry.citing_date),
            entry.citing_date,
            entry.signal != "negative",
            entry.citing_case.casefold(),
        )
    )
    later_reaffirmance = any(
        entry.signal == "reaffirmed"
        and entry.authoritative
        and entry.later_than_negative
        for entry in timeline
    )
    # Review order is priority order, but the report keeps the API's citation
    # order so a capped run remains comparable with an uncapped one.
    source_order = {
        str(opinion_id): index for index, opinion_id in enumerate(citing_ids)
    }
    later_citing.sort(
        key=lambda cited: source_order.get(cited.opinion_id, len(source_order))
    )
    return (
        treatments,
        gated,
        later_citing,
        reviewed,
        latest,
        flags,
        gaps,
        timeline,
        later_reaffirmance,
    )


def _unknown_check(case: CaseRecord, flags: list[str]) -> CitatorCheck:
    """Build an unknown check for a target that could not be resolved."""
    return CitatorCheck(
        target_case=case.title or "Untitled case",
        target_citation=case.citation,
        target_court=case.court,
        target_decision_date=case.decision_date,
        target_opinion_id=str(case.courtlistener_opinion_id or ""),
        target_url=case.url,
        target_citations=[case.citation] if case.citation else [],
        target_opinion_ids=(
            [str(case.courtlistener_opinion_id)]
            if case.courtlistener_opinion_id
            else []
        ),
        status="unknown",
        checked_at=_checked_at(),
        coverage=CitatorCoverage(
            gaps=[
                _coverage_gap(
                    "target_unresolved",
                    "; ".join(flags) or "The target case could not be resolved.",
                )
            ],
            temporal_confidence="none",
            temporal_confidence_reason=(
                "No target cluster was resolved, so no citing-opinion date could "
                "be reviewed."
            ),
        ),
        flags=flags,
    )


def _apply_authority_entry(
    check: CitatorCheck, entry: AuthorityQueueEntry | None
) -> CitatorCheck:
    """Attach review-order metadata without changing citator status."""
    if entry is not None:
        check.authority_priority = entry.priority
        check.authority_reasons = list(entry.reasons)
    return check


def _persist_check(
    check: CitatorCheck, snapshot_store: SnapshotStore | None
) -> CitatorCheck:
    """Persist normalized evidence without letting a disk failure abort a check."""
    if snapshot_store is None:
        return check
    try:
        snapshot_store.record_evidence(check, source="citator")
    except OSError as exc:
        check.flags.append(f"Could not persist evidence snapshot: {exc}")
    return check


def check_case(
    case: CaseRecord,
    provider: CourtListenerProvider,
    *,
    max_citing_opinions: int = 5,
    max_pages: int = 1,
    max_same_docket: int = 5,
    history_records: list[AppellateHistoryRecord] | None = None,
    snapshot_store: SnapshotStore | None = None,
    authority_entry: AuthorityQueueEntry | None = None,
) -> CitatorCheck:
    """Resolve and check one queued authority, returning an advisory result."""
    target, flags = _resolve_target(case, provider)
    if target is None:
        check = _unknown_check(case, flags)
        return _persist_check(
            _apply_authority_entry(check, authority_entry), snapshot_store
        )
    gaps: list[CitatorCoverageGap] = []
    if not target.cluster_resolved:
        gaps.append(
            _coverage_gap(
                "cluster_unresolved",
                f"Citator could not expand '{target.case_name}' beyond opinion "
                f"{target.opinion_id}.",
            )
        )

    same_docket: list[SameDocketDecision] = []
    try:
        same_docket_rows = provider.same_docket_decisions(
            target.opinion_id, max_results=max_same_docket
        )
    except SearchError as exc:
        flags.append(
            f"Citator same-docket advisory unavailable for "
            f"'{target.case_name}': {exc}"
        )
    else:
        same_docket = [
            SameDocketDecision(
                case_name=str(row.get("case_name") or "Untitled case"),
                citation=str(row.get("citation") or ""),
                court=str(row.get("court") or ""),
                decision_date=str(row.get("decision_date") or ""),
                url=str(row.get("url") or ""),
                cluster_id=str(row.get("cluster_id") or ""),
            )
            for row in same_docket_rows
        ]

    priority_page_size = _citing_priority_page_size(max_citing_opinions)
    priority_pool = _citing_priority_pool_size(
        max_citing_opinions, max_pages
    )
    (
        citing_ids,
        found,
        pages,
        edges_complete,
        truncated,
        edge_flags,
        edge_gaps,
    ) = _fetch_cluster_forward_edges(
        provider,
        target,
        max_pages,
        page_size=priority_page_size,
        max_ids=priority_pool,
    )
    flags.extend(edge_flags)
    gaps.extend(edge_gaps)
    (
        treatments,
        gated_treatments,
        later_citing,
        reviewed,
        latest,
        review_flags,
        review_gaps,
        treatment_timeline,
        later_reaffirmance,
    ) = _review_citing_opinions(provider, target, citing_ids, max_citing_opinions)
    flags.extend(review_flags)
    gaps.extend(review_gaps)

    complete = (
        target.cluster_resolved
        and edges_complete
        and not review_flags
        and reviewed == len(citing_ids)
    )
    if found and not citing_ids:
        detail = (
            f"Citator forward-citation response for '{target.case_name}' reported "
            "citations but returned no usable opinion ids."
        )
        flags.append(detail)
        gaps.append(_coverage_gap("no_usable_citing_ids", detail))
        complete = False
    if len(citing_ids) > max_citing_opinions:
        truncated = True
        complete = False
    if not found and edges_complete:
        detail = f"Citator found no forward citing opinions for '{target.case_name}'."
        flags.append(detail)
        gaps.append(_coverage_gap("no_forward_citations", detail))
    if not complete:
        flags.append(
            f"Citator coverage for '{target.case_name}' is incomplete "
            f"({reviewed}/{found} citing opinions reviewed)."
        )

    temporal_confidence, temporal_reason = _temporal_confidence(
        complete=complete,
        reviewed=reviewed,
        latest=latest,
        gaps=gaps,
    )

    status = "unknown"
    good_law_as_of = ""
    if treatments:
        status = "negative"
    elif gated_treatments:
        flags.append(
            f"Citator found negative treatment only in "
            f"{len(gated_treatments)} non-authoritative opinion(s) for "
            f"'{target.case_name}'; it was not counted toward status."
        )
    elif complete and reviewed and latest:
        status = "good_law"
        good_law_as_of = latest

    binding_treatments = [
        treatment for treatment in treatments if treatment.binding is True
    ]
    persuasive_treatments = [
        treatment for treatment in treatments if treatment.binding is False
    ]

    check = CitatorCheck(
        target_case=target.case_name,
        target_citation=target.citation,
        target_court=case.court,
        target_decision_date=case.decision_date,
        target_opinion_id=str(target.opinion_id),
        target_url=case.url,
        target_cluster_id=target.cluster_id,
        target_citations=list(target.citations),
        target_opinion_ids=[str(value) for value in target.opinion_ids],
        status=status,
        good_law_as_of=good_law_as_of,
        checked_at=_checked_at(),
        coverage=CitatorCoverage(
            forward_citations_found=found,
            citing_opinions_reviewed=reviewed,
            pages_fetched=pages,
            complete=complete,
            truncated=truncated,
            latest_citing_date=latest,
            gaps=gaps,
            temporal_confidence=temporal_confidence,
            temporal_confidence_reason=temporal_reason,
        ),
        same_docket_decisions=same_docket,
        later_citing_opinions=later_citing,
        negative_treatments=treatments,
        gated_treatment_findings=gated_treatments,
        binding_negative_treatments=binding_treatments,
        persuasive_negative_treatments=persuasive_treatments,
        treatment_timeline=treatment_timeline,
        later_reaffirmance=later_reaffirmance,
        flags=flags,
    )
    check.appellate_history = build_appellate_history(
        check, history_records
    )
    check.subsequent_history_assessed = (
        check.appellate_history.status == "verified"
    )
    _apply_authority_entry(check, authority_entry)
    return _persist_check(check, snapshot_store)


def run_citator(
    cases: list[CaseRecord],
    provider: CourtListenerProvider,
    *,
    max_cases: int = 10,
    max_citing_opinions: int = 5,
    max_pages: int = 1,
    history_records: list[AppellateHistoryRecord] | None = None,
    snapshot_store: SnapshotStore | None = None,
    authority_queue: list[QueuedAuthority] | None = None,
) -> list[CitatorCheck]:
    """Check the prioritized authority queue up to *max_cases*.

    Callers may pass a pre-built queue; otherwise the queue is built from
    *cases*. The queue changes review order only, never ranking or status.
    """
    active_provider = (
        SnapshottingCourtListenerProvider(provider, snapshot_store)
        if snapshot_store is not None
        else provider
    )
    queued = (
        prioritize_authorities(cases, max_cases)
        if authority_queue is None
        else authority_queue[: max(0, max_cases)]
    )
    return [
        check_case(
            item.case,
            active_provider,
            max_citing_opinions=max_citing_opinions,
            max_pages=max_pages,
            history_records=history_records,
            snapshot_store=snapshot_store,
            authority_entry=item.entry,
        )
        for item in queued
    ]
