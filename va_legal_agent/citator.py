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
API response. The pool is target-wide and is split across a cluster's
sub-opinions, so adding a concurrence or dissent does not multiply metadata
reads. Every unread candidate or page remains an explicit coverage gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
    SourceValiditySignal,
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
# candidates without turning a capped review into a full citation dump. The
# bound applies to the whole target, not each opinion in a cluster, and is also
# the upper bound on candidate metadata rows: edge fetches stop at their share,
# and both batch and fallback metadata lookup only see the resulting ids.
_CITING_PRIORITY_POOL_MIN = 20
_CITING_PRIORITY_POOL_FACTOR = 4
_CITING_PRIORITY_POOL_MAX = 100

# Source validation is advisory and deliberately bounded. A source is only a
# provenance check for one finding, so it gets a much smaller history budget
# than a target case. The cap is also the point at which the result becomes
# unverified rather than claiming the source is valid.
_SOURCE_HISTORY_MAX_CITING = 3
_SOURCE_HISTORY_MAX_PAGES = 1
_SOURCE_INVALIDATING_TREATMENTS = frozenset(
    {"overruled", "abrogated", "superseded", "vacated"}
)

# Deterministic confidence deductions. The score is a review-quality signal,
# not a statistical probability: it starts at 1.0 and subtracts known ways the
# review can be incomplete or misattributed. Status and good_law_as_of are
# computed separately and never consume this score.
_CONFIDENCE_GAP_PENALTIES = {
    "citing_opinion_cap": 0.15,
    "citing_date_missing": 0.15,
    "page_cap": 0.15,
    "count_mismatch": 0.20,
    "no_forward_citations": 0.20,
    "pagination_cursor_missing": 0.20,
    "pagination_cursor_repeated": 0.20,
    "cluster_unresolved": 0.25,
    "citing_metadata_unavailable": 0.25,
    "citing_text_empty": 0.25,
    "no_usable_citing_ids": 0.25,
    "target_not_located": 0.25,
    "citing_metadata_fetch_failed": 0.30,
    "citing_text_fetch_failed": 0.30,
    "forward_fetch_failed": 0.35,
}
_CONFIDENCE_DEFAULT_GAP_PENALTY = 0.10
_CONFIDENCE_MAX_GAP_PENALTY = 0.75
_CONFIDENCE_AMBIGUOUS_PENALTY = 0.10
_CONFIDENCE_MAX_AMBIGUOUS_PENALTY = 0.30
_METADATA_GAP_CODES = frozenset(
    {"citing_metadata_unavailable", "citing_metadata_fetch_failed"}
)

# Terms CourtListener does not label for us. Keep this list limited to
# explicit negative verbs; "distinguished" is intentionally absent because it
# is not necessarily negative treatment.
_NEGATIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\boverrul(?:e|ed|es|ing)\b", re.I), "overruled"),
    (re.compile(r"\babrogat(?:e|ed|es|ing)\b", re.I), "abrogated"),
    (re.compile(r"\bsupersed(?:e|ed|es|ing)\b", re.I), "superseded"),
    (re.compile(r"\bvacat(?:e|ed|es|ing)\b", re.I), "vacated"),
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
# A short-form case name is only a candidate for a party's first distinctive
# capitalized word. Skip ordinary legal and institutional words so a target
# such as "State v. Smith" does not make "State" a target alias.
_SHORT_NAME_STOPWORDS = frozenset(
    {
        "affairs",
        "animal",
        "american",
        "association",
        "board",
        "casualty",
        "center",
        "city",
        "coalition",
        "commission",
        "committee",
        "company",
        "corporation",
        "council",
        "county",
        "defense",
        "department",
        "district",
        "federal",
        "general",
        "government",
        "group",
        "health",
        "inc",
        "institute",
        "insurance",
        "legal",
        "life",
        "llc",
        "ltd",
        "national",
        "natural",
        "office",
        "planned",
        "public",
        "school",
        "secretary",
        "service",
        "services",
        "society",
        "state",
        "states",
        "the",
        "union",
        "united",
        "university",
        "veteran",
        "veterans",
        "welfare",
    }
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
    r"however|because|since|after|before|when|where|unless|until|which|so)\b"
    r"|,\s*and\b",
    re.I,
)
_INTERPOSED_ARGUMENT_RE = re.compile(_ATTRIBUTION_FRAME, re.I)
_PASSIVE_TARGET_RE = re.compile(
    r"\s*,?\s*(?:was|were|is|are|has\s+been|have\s+been)\s+"
    r"(?:\w+\s+){0,2}$",
    re.I,
)

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
    attribution_basis: str = "citation"
    attribution_ambiguous: bool = False
    attribution_reason: str = ""


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


@dataclass(frozen=True)
class _SourceAudit:
    """Raw, target-independent result of one source-history review."""

    source_cluster_id: str = ""
    metadata: dict[str, object] | None = None
    validity_status: str = "unverified"
    reason: str = ""
    history_complete: bool = False


@dataclass
class _SourceValidationCache:
    """Run-scoped cache for source audits, shared by citator checks."""

    audits: dict[tuple[int, str], _SourceAudit] = field(default_factory=dict)


def _checked_at() -> str:
    """Return a stable UTC timestamp for a citator check."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_as_of_date(value: str | None) -> str:
    """Validate and normalize an optional inclusive as-of cutoff."""
    if value is None:
        return ""
    normalized = str(value).strip()
    if not normalized:
        return ""
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError(
            "as_of_date must be an ISO date in YYYY-MM-DD form."
        ) from exc


def _candidate_date_relation(value: str, cutoff: date) -> str:
    """Classify a citing date relative to an inclusive as-of cutoff."""
    if not value:
        return "undated"
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return "undated"
    return "in_scope" if parsed <= cutoff else "post_as_of"


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
    """Return the bounded, target-wide metadata pool for the review cap."""
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
    excluded_undated: bool = False,
) -> tuple[str, str]:
    """Describe how strongly the reviewed citation dates bound the check.

    ``excluded_undated`` distinguishes an as-of check, where an opinion with
    no usable date is excluded rather than reviewed, from the ordinary path.
    """
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
        if excluded_undated:
            return (
                "low",
                f"Latest reviewed citing opinion is {latest}, but at least one "
                "citing opinion was excluded from the as-of result because it "
                "has no usable decision date.",
            )
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


def _confidence_assessment(
    *,
    reviewed: int,
    complete: bool,
    gaps: list[CitatorCoverageGap],
    ambiguous_findings: list[NegativeTreatment],
) -> tuple[float, str, str, bool, list[str]]:
    """Return score, level, reason, review flag, and explicit review reasons.

    The score is penalty-based rather than statistical: complete, explicit
    coverage starts at 1.0 and known coverage, metadata, or attribution
    problems reduce it. It is intentionally independent of the status rule.
    """
    review_reasons: list[str] = []
    gap_penalty = 0.0
    metadata_failures = 0
    for gap in gaps:
        if gap.code in _METADATA_GAP_CODES:
            metadata_failures += 1
            review_reasons.append(
                f"metadata failure [{gap.code}]: {gap.detail}"
            )
        else:
            review_reasons.append(f"coverage gap [{gap.code}]: {gap.detail}")
        gap_penalty += _CONFIDENCE_GAP_PENALTIES.get(
            gap.code, _CONFIDENCE_DEFAULT_GAP_PENALTY
        )
    gap_penalty = min(gap_penalty, _CONFIDENCE_MAX_GAP_PENALTY)

    ambiguous: list[NegativeTreatment] = []
    for finding in ambiguous_findings:
        if not finding.attribution_ambiguous:
            continue
        ambiguous.append(finding)
        review_reasons.append(
            f"ambiguous attribution [{finding.treatment} by "
            f"{finding.citing_case or 'unknown citing opinion'}]: "
            f"{finding.attribution_reason or 'attribution is not explicit'}: "
            f"\"{finding.excerpt}\""
        )

    ambiguous_penalty = min(
        len(ambiguous) * _CONFIDENCE_AMBIGUOUS_PENALTY,
        _CONFIDENCE_MAX_AMBIGUOUS_PENALTY,
    )
    if reviewed == 0:
        return (
            0.0,
            "none",
            "No citing opinion was reviewed, so the check cannot support a "
            "confidence score.",
            bool(review_reasons),
            review_reasons,
        )

    score = round(
        max(0.0, 1.0 - gap_penalty - ambiguous_penalty),
        2,
    )
    if complete and not review_reasons:
        level = "high"
    elif score >= 0.60:
        level = "medium"
    else:
        level = "low"

    coverage_count = len(gaps)
    if not review_reasons:
        reason = (
            "Complete coverage with explicit attribution and no metadata "
            "failures."
        )
    else:
        reason = (
            f"Penalty-based score {score:.2f}: {coverage_count} coverage "
            f"gap(s), including {metadata_failures} metadata failure(s), and "
            f"{len(ambiguous)} ambiguous attribution(s)."
        )
    return score, level, reason, bool(review_reasons), review_reasons


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
    review pool: the page request is clamped to the caller's share so it never
    fetches far beyond the candidates that can be ranked, and paging stops once
    that share is full. Rows beyond ``max_ids`` are ignored. Any remaining
    cursor is still reported as an explicit ``page_cap`` gap.
    """
    page_size = min(page_size, max_ids)
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
                if len(ids) >= max_ids:
                    break
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
            boundary = (
                f"the {max_ids}-candidate allocation from the priority pool"
            )
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
    or dissent. ``max_ids`` is one target-wide budget, split evenly across the
    cluster so every sub-opinion gets a chance without multiplying the pool.
    The returned ids are de-duplicated across the cluster. A one-opinion target
    keeps the API's reported count; a multi-opinion target reports the unique
    count, since the same citing opinion may be linked to more than one
    sub-opinion.
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
    # Split the target-wide pool evenly, giving the remainder to the earliest
    # sub-opinions so one lead opinion cannot consume every candidate.
    share, remainder = divmod(max_ids, len(opinion_ids))
    for index, opinion_id in enumerate(opinion_ids):
        allocation = share + (1 if index < remainder else 0)
        if allocation <= 0:
            detail = (
                f"Citator candidate pool for '{target.case_name}' had no "
                f"allocation left for opinion {opinion_id}; coverage is "
                "bounded by the target-wide pool."
            )
            flags.append(detail)
            gaps.append(_coverage_gap("page_cap", detail))
            complete = False
            truncated = True
            continue
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
            max_ids=allocation,
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


def _target_short_names(title: str) -> tuple[str, ...]:
    """Return conservative short-form aliases for a case title.

    Only the first distinctive word of each party is eligible. This captures
    the usual reporter style ("Roe," "Casey," "Chevron," "AMA") without
    treating a generic institutional word as a case-name alias.
    """
    cleaned = " ".join(title.replace("’", "'").split())
    parties = re.split(r"\s+v\.?\s+", cleaned, maxsplit=1, flags=re.I)
    names: list[str] = []
    for party in parties:
        party = re.sub(r"^(?:the)\s+", "", party, flags=re.I)
        words = re.findall(r"[A-Z][A-Za-z'.-]*", party)
        if not words:
            continue
        candidate = words[0].strip(".,")
        if len(candidate) < 3:
            continue
        if candidate.casefold() in _SHORT_NAME_STOPWORDS:
            continue
        names.append(candidate)
    return tuple(dict.fromkeys(names))


def _short_name_spans(
    text: str, short_names: tuple[str, ...]
) -> list[tuple[int, int]]:
    """Find case-sensitive whole-word spans for conservative short names."""
    spans: set[tuple[int, int]] = set()
    for name in short_names:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])"
        )
        spans.update((match.start(), match.end()) for match in pattern.finditer(text))
    return sorted(spans)


def _matching_citation_spans(
    text: str, target_citations: tuple[str, ...]
) -> list[tuple[int, int]]:
    """Find target citations, tolerating reporter spacing such as ``U. S.``."""
    target_keys = {
        _normalize_citation(citation)
        for citation in target_citations
        if citation
    }
    spans: set[tuple[int, int]] = set()
    for match in _CITATION_RE.finditer(text):
        if _normalize_citation(match.group(0)) in target_keys:
            spans.add((match.start(), match.end()))
    for citation in target_citations:
        spans.update(_phrase_spans(text, citation))
    return sorted(spans)


def _target_spans(
    text: str,
    target_title: str,
    target_citations: str | tuple[str, ...],
    target_short_names: tuple[str, ...] = (),
) -> list[tuple[int, int]]:
    """Find any parallel citation, case-name, or short-name target span."""
    citations = (target_citations,) if isinstance(target_citations, str) else target_citations
    spans = set(_matching_citation_spans(text, citations))
    spans.update(_phrase_spans(text, target_title))
    spans.update(_short_name_spans(text, target_short_names))
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


def _target_binding(
    sentence: str,
    verb: tuple[int, int],
    target_title: str,
    target_citations: tuple[str, ...],
    target_short_names: tuple[str, ...] = (),
) -> tuple[bool, str, tuple[int, int] | None]:
    """Whether *verb* binds to the target, and the basis for that match.

    Returns ``(bound, basis, target_span)``. ``basis`` is ``citation`` for an
    explicit reporter-citation match, ``case_name`` for a bare full-name match,
    ``short_name`` for a conservative short-form match, and an empty string
    when no binding was established.
    """
    citation_spans = [
        (match.start(), match.end()) for match in _CITATION_RE.finditer(sentence)
    ]
    target_citation_spans = _matching_citation_spans(
        sentence, tuple(target_citations)
    )
    target_title_spans = _phrase_spans(sentence, target_title)
    target_short_spans = _short_name_spans(sentence, target_short_names)
    target_references = [
        *target_citation_spans,
        *target_title_spans,
        *target_short_spans,
    ]
    if not target_references:
        return False, "", None

    clause_start, clause_end = _clause_bounds(sentence, verb[0])
    citations_in_clause = _spans_within(citation_spans, clause_start, clause_end)
    target_citations_in_clause = _spans_within(
        target_citation_spans, clause_start, clause_end
    )
    target_titles_in_clause = _spans_within(
        target_title_spans, clause_start, clause_end
    )
    target_short_in_clause = _spans_within(
        target_short_spans, clause_start, clause_end
    )
    if target_citations_in_clause:
        target_span = min(
            target_citations_in_clause,
            key=lambda span: _span_distance(verb, span),
        )
        basis = "citation"
    elif target_titles_in_clause:
        # A bare case name is usable only when no different reporter citation
        # appears in the same clause. This rejects same-name/different-citation
        # sentences before they can become treatment findings.
        if citations_in_clause:
            return False, "", None
        target_span = min(
            target_titles_in_clause,
            key=lambda span: _span_distance(verb, span),
        )
        basis = "case_name"
    elif target_short_in_clause:
        # A short name is usable only when the clause does not contain a
        # different reporter citation. A competing full case name is handled
        # by the same-side competitor check below.
        if citations_in_clause:
            return False, "", None
        target_span = min(
            target_short_in_clause,
            key=lambda span: _span_distance(verb, span),
        )
        basis = "short_name"
    else:
        return False, "", None

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
        return False, "", None

    between = (
        sentence[verb[1] : target_span[0]]
        if target_span[0] >= verb[1]
        else sentence[target_span[1] : verb[0]]
    )
    if _INTERPOSED_ARGUMENT_RE.search(between):
        return False, "", None
    return True, basis, target_span


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


def _is_non_verb_negative(
    treatment: str, phrase: str, sentence: str, verb_end: int
) -> bool:
    """Whether a matched negative term is a noun or adjective, not a verb."""
    lowered = phrase.casefold()
    if treatment == "questioned" and lowered in {"question", "questions"}:
        return True
    if treatment == "limited" and re.match(
        r"\s+to\s+(?:the\s+)?(?:question|issue|extent|scope|whether)\b",
        sentence[verb_end:],
        re.I,
    ):
        return True
    return False


def _scope_for(treatment: str, sentence: str) -> str:
    """Classify the scope stated for a negative treatment."""
    if re.search(r"\bin part\b", sentence, re.I):
        return "part"
    if treatment in {
        "overruled",
        "abrogated",
        "superseded",
        "vacated",
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
    target_short_names: tuple[str, ...] = (),
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
            if _is_non_verb_negative(
                treatment, match.group(0), sentence, verb_end
            ):
                continue
            bound, attribution_basis, target_span = _target_binding(
                sentence,
                (verb_start, verb_end),
                target_title,
                citations,
                target_short_names,
            )
            if not bound:
                continue
            key = (treatment, sentence)
            if key in seen:
                continue
            seen.add(key)
            attribution_reasons: list[str] = []
            if attribution_basis == "case_name":
                attribution_reasons.append(
                    "target matched by case name only, without a reporter "
                    "citation"
                )
            elif attribution_basis == "short_name":
                attribution_reasons.append(
                    "target matched by short name only, without a full case "
                    "name or reporter citation"
                )
            if (
                target_span is not None
                and target_span[1] <= verb_start
                and _PASSIVE_TARGET_RE.fullmatch(
                    sentence[target_span[1] : verb_start]
                )
            ):
                attribution_reasons.append(
                    "passive treatment verb has no explicit actor"
                )
            findings.append(
                TreatmentMatch(
                    treatment=treatment,
                    excerpt=sentence,
                    scope=_scope_for(treatment, sentence),
                    confidence=(
                        "medium"
                        if treatment == "limited"
                        or attribution_basis == "short_name"
                        else "high"
                    ),
                    attribution_basis=attribution_basis,
                    attribution_ambiguous=bool(attribution_reasons),
                    attribution_reason="; ".join(attribution_reasons),
                )
            )
    return findings


def extract_negative_treatments(
    text: str,
    target_title: str,
    target_citations: str | tuple[str, ...],
    *,
    opinion_role: str = "majority",
    target_short_names: tuple[str, ...] | None = None,
) -> list[TreatmentMatch]:
    """Return target-specific negative-treatment holdings from *text*.

    The verb must bind to the target citation, full case name, or conservative
    short-form name. A bare name is used only when no competing citation is
    present, and short-form attribution is flagged for review. For a majority
    opinion, treatment attributed to a dissent, party argument, or hypothetical
    is ignored.
    """
    short_names = (
        _target_short_names(target_title)
        if target_short_names is None
        else target_short_names
    )
    return _extract_treatment_matches(
        text,
        target_title,
        target_citations,
        _NEGATIVE_PATTERNS,
        opinion_role=opinion_role,
        target_short_names=short_names,
    )


def extract_reaffirmance_signals(
    text: str,
    target_title: str,
    target_citations: str | tuple[str, ...],
    *,
    opinion_role: str = "majority",
    target_short_names: tuple[str, ...] | None = None,
) -> list[TreatmentMatch]:
    """Return target-specific reaffirmance signals from *text*.

    These signals are advisory and never clear an authoritative negative
    finding. They use the same target-binding and attribution rules as
    negative-treatment extraction, including conservative short-name matches.
    """
    short_names = (
        _target_short_names(target_title)
        if target_short_names is None
        else target_short_names
    )
    return _extract_treatment_matches(
        text,
        target_title,
        target_citations,
        _REAFFIRMANCE_PATTERNS,
        opinion_role=opinion_role,
        target_short_names=short_names,
    )


def _binding_for_courts(target_court: str, citing_court: str) -> bool | None:
    """Whether a citing court outranks (or matches) the target court."""
    target_weight = AUTHORITY_WEIGHTS.get(target_court, 0)
    citing_weight = AUTHORITY_WEIGHTS.get(citing_court, 0)
    if not target_weight or not citing_weight:
        return None
    return citing_weight >= target_weight


def _binding_for(target: _Target, citing_court: str) -> bool | None:
    """Whether a citing court outranks (or matches) the target court."""
    return _binding_for_courts(target.case.court, citing_court)


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
        source_opinion_id=finding.source_opinion_id,
        source_cluster_id=finding.source_cluster_id,
        binding=finding.binding,
        binding_class=_binding_class(finding.binding),
        authoritative=finding.authoritative,
        opinion_type=finding.opinion_type,
        opinion_role=finding.opinion_role,
        precedential_status=finding.precedential_status,
        precedential_tier=finding.precedential_tier,
        gate_reason=finding.gate_reason,
    )


def _source_validity_signals(
    findings: list[NegativeTreatment],
) -> list[SourceValiditySignal]:
    """Extract stable source identifiers without assessing validity.

    This is deliberately schema-only: it performs no lookups and returns
    ``status="not_assessed"`` for every distinct source opinion. Production
    callers enable it through ``CITATOR_SOURCE_VALIDITY``; the result is never
    used in status, coverage, ranking, or queue calculations.
    """
    signals: list[SourceValiditySignal] = []
    seen: set[str] = set()
    for finding in findings:
        if not finding.source_opinion_id:
            continue
        if finding.source_opinion_id in seen:
            continue
        seen.add(finding.source_opinion_id)
        signals.append(
            SourceValiditySignal(
                source_opinion_id=finding.source_opinion_id,
                source_cluster_id=finding.source_cluster_id,
            )
        )
    return signals


def _batch_citing_metadata(
    provider: CourtListenerProvider, citing_ids: list[int]
) -> dict[int, dict[str, object]]:
    """Return batch metadata, treating any batch failure as unavailable.

    The caller retries every candidate missing from this mapping through the
    single-opinion path. That keeps the existing per-candidate metadata gaps
    when batch lookup is unsupported, rejected, or incomplete.
    """
    batch = getattr(provider, "opinion_metadata_batch", None)
    if not callable(batch):
        return {}
    try:
        response = batch(citing_ids)
    except Exception:  # noqa: BLE001 - batch is an optimization, never a blocker
        return {}
    if not isinstance(response, dict):
        return {}
    metadata: dict[int, dict[str, object]] = {}
    for key, value in response.items():
        parsed = _positive_id(key)
        if parsed is not None and isinstance(value, dict):
            metadata[parsed] = value
    return metadata


def _load_citing_candidates(
    provider: CourtListenerProvider, citing_ids: list[int]
) -> tuple[list[_CitingCandidate], list[str], list[CitatorCoverageGap]]:
    """Fetch ranking metadata once, then order candidates for a capped review.

    Later decisions come first because they are the most probative of current
    treatment. Court authority breaks date ties. API order is used only as the
    final stable tie-break. Metadata is requested in batch first; candidates
    missing from the batch fall back to the single-opinion chain. Failures in
    either path are retained as the same coverage gaps, since a failed
    candidate could have outranked the selected set.
    """
    candidates: list[_CitingCandidate] = []
    flags: list[str] = []
    gaps: list[CitatorCoverageGap] = []
    batch_metadata = _batch_citing_metadata(provider, citing_ids)
    for source_index, opinion_id in enumerate(citing_ids):
        metadata = batch_metadata.get(opinion_id)
        if not metadata:
            try:
                metadata = provider.opinion_metadata(opinion_id)
            except SearchError as exc:
                detail = (
                    f"Citator metadata fetch failed for citing opinion "
                    f"{opinion_id}: {exc}"
                )
                flags.append(detail)
                gaps.append(
                    _coverage_gap("citing_metadata_fetch_failed", detail)
                )
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


def _citing_summary(
    opinion_id: int, metadata: dict[str, object]
) -> LaterCitingOpinion:
    """Build the advisory summary and authority gate for one citing opinion."""
    opinion_type = str(metadata.get("opinion_type") or "")
    precedential_status = str(metadata.get("precedential_status") or "")
    opinion_role, precedential_tier, eligible, gate_reason = _authority_gate(
        opinion_type, precedential_status
    )
    return LaterCitingOpinion(
        case_name=str(metadata.get("title") or f"Opinion {opinion_id}"),
        citation=str(metadata.get("citation") or ""),
        court=str(metadata.get("court") or ""),
        decision_date=str(metadata.get("decision_date") or ""),
        url=str(metadata.get("url") or ""),
        opinion_id=str(opinion_id),
        cluster_id=str(metadata.get("cluster_id") or ""),
        opinion_type=opinion_type,
        opinion_role=opinion_role,
        precedential_status=precedential_status,
        precedential_tier=precedential_tier,
        treatment_eligible=eligible,
        gate_reason=gate_reason,
    )


def _review_candidates(
    provider: CourtListenerProvider,
    target: _Target,
    candidates: list[_CitingCandidate],
    max_citing: int,
    *,
    source_ids: list[int],
    total_citing: int,
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
    """Read one capped temporal bucket and collect its treatment signals."""
    treatments: list[NegativeTreatment] = []
    gated: list[NegativeTreatment] = []
    reviewed_citing: list[LaterCitingOpinion] = []
    timeline: list[TreatmentTimelineEntry] = []
    reviewed = 0
    latest = ""
    flags: list[str] = []
    gaps: list[CitatorCoverageGap] = []
    short_names = _target_short_names(target.case_name)
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
        if not _target_spans(
            text, target.case_name, target.citations, short_names
        ):
            detail = (
                f"Citator could not locate '{target.case_name}' in citing opinion "
                f"{opinion_id}; treatment was not verified."
            )
            flags.append(detail)
            gaps.append(_coverage_gap("target_not_located", detail))
            continue
        reviewed += 1
        summary = _citing_summary(opinion_id, metadata)
        reviewed_citing.append(summary)
        citing_date = summary.decision_date
        citing_court = summary.court
        opinion_type = summary.opinion_type
        opinion_role = summary.opinion_role
        precedential_status = summary.precedential_status
        precedential_tier = summary.precedential_tier
        eligible = summary.treatment_eligible
        gate_reason = summary.gate_reason
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
            target_short_names=short_names,
        ):
            item = NegativeTreatment(
                citing_case=summary.case_name,
                citing_citation=summary.citation,
                citing_court=citing_court,
                citing_date=citing_date,
                citing_url=summary.url,
                source_opinion_id=summary.opinion_id,
                source_cluster_id=summary.cluster_id,
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
                attribution_basis=finding.attribution_basis,
                attribution_ambiguous=finding.attribution_ambiguous,
                attribution_reason=finding.attribution_reason,
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
            target_short_names=short_names,
        ):
            # Reuse the treatment metadata shape for the timeline; this
            # positive entry never enters a negative-treatment list.
            reaffirmance = NegativeTreatment(
                citing_case=summary.case_name,
                citing_citation=summary.citation,
                citing_court=citing_court,
                citing_date=citing_date,
                citing_url=summary.url,
                source_opinion_id=summary.opinion_id,
                source_cluster_id=summary.cluster_id,
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
                attribution_basis=finding.attribution_basis,
                attribution_ambiguous=finding.attribution_ambiguous,
                attribution_reason=finding.attribution_reason,
                excerpt=finding.excerpt,
            )
            timeline.append(_timeline_entry("reaffirmed", reaffirmance))
    if total_citing > max_citing:
        detail = (
            f"Citator reviewed {reviewed} of {total_citing} citing "
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
        str(opinion_id): index for index, opinion_id in enumerate(source_ids)
    }
    reviewed_citing.sort(
        key=lambda cited: source_order.get(cited.opinion_id, len(source_order))
    )
    return (
        treatments,
        gated,
        reviewed_citing,
        reviewed,
        latest,
        flags,
        gaps,
        timeline,
        later_reaffirmance,
    )


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
    candidates, candidate_flags, candidate_gaps = _load_citing_candidates(
        provider, citing_ids
    )
    (
        treatments,
        gated,
        reviewed_citing,
        reviewed,
        latest,
        review_flags,
        review_gaps,
        timeline,
        later_reaffirmance,
    ) = _review_candidates(
        provider,
        target,
        candidates,
        max_citing,
        source_ids=citing_ids,
        total_citing=len(citing_ids),
    )
    return (
        treatments,
        gated,
        reviewed_citing,
        reviewed,
        latest,
        candidate_flags + review_flags,
        candidate_gaps + review_gaps,
        timeline,
        later_reaffirmance,
    )


def _source_metadata(
    provider: CourtListenerProvider, source_id: int
) -> dict[str, object] | None:
    """Fetch current metadata for one source."""
    try:
        return provider.opinion_metadata(source_id)
    except SearchError:
        return None


def _source_precedence(
    metadata: dict[str, object] | None, target_court: str
) -> tuple[str, str]:
    """Classify a source's current role, status, and court relationship."""
    if not metadata:
        return "unverified", "source metadata unavailable"
    role = _opinion_role(str(metadata.get("opinion_type") or ""))
    tier = _precedential_tier(str(metadata.get("precedential_status") or ""))
    source_court = str(metadata.get("court") or "")
    if role in {"separate", "plurality"}:
        return "separate", f"source opinion role is {role}"
    if role != "majority":
        return "unverified", f"source opinion role is {role}"
    if tier == "nonprecedential":
        return "nonprecedential", "source is currently nonprecedential"
    if tier != "precedential":
        return "unverified", f"source precedential status is {tier}"
    binding = _binding_for_courts(target_court, source_court)
    if binding is False:
        return (
            "persuasive",
            "source court is lower than the target court; identified lower-court "
            "sources are withheld from enforcement",
        )
    if binding is True:
        return "authoritative", ""
    return "unverified", "source court relationship is unknown"


def _source_history_audit(
    provider: CourtListenerProvider,
    source_target: _Target,
    metadata: dict[str, object],
    as_of_date: str,
) -> _SourceAudit:
    """Review a bounded one-hop history for one source opinion."""
    max_ids = max(1, _SOURCE_HISTORY_MAX_CITING * 4)
    (
        citing_ids,
        _found,
        _pages,
        edges_complete,
        _truncated,
        edge_flags,
        edge_gaps,
    ) = _fetch_cluster_forward_edges(
        provider,
        source_target,
        _SOURCE_HISTORY_MAX_PAGES,
        page_size=max_ids,
        max_ids=max_ids,
    )
    candidates, candidate_flags, candidate_gaps = _load_citing_candidates(
        provider, citing_ids
    )
    # Policy: as-of source validation stops at the cutoff. Only source-history
    # candidates dated on or before the cutoff can invalidate or qualify the
    # source. Later candidates are ignored; undated candidates make the as-of
    # review incomplete rather than being treated as in scope.
    if as_of_date:
        cutoff = date.fromisoformat(as_of_date)
        scoped: list[_CitingCandidate] = []
        undated = False
        for candidate in candidates:
            relation = _candidate_date_relation(candidate.decision_date, cutoff)
            if relation == "in_scope":
                scoped.append(candidate)
            elif relation == "undated":
                undated = True
        candidates = scoped
        if undated:
            candidate_flags.append(
                "Source-history candidate had no usable decision date; "
                "as-of source validation is incomplete."
            )
    scope_ids = [candidate.opinion_id for candidate in candidates]
    (
        treatments,
        gated,
        _later_citing,
        reviewed,
        _latest,
        review_flags,
        review_gaps,
        _timeline,
        _later_reaffirmance,
    ) = _review_candidates(
        provider,
        source_target,
        candidates,
        _SOURCE_HISTORY_MAX_CITING,
        source_ids=scope_ids,
        total_citing=len(scope_ids),
    )
    incomplete = (
        not edges_complete
        or bool(
            edge_flags
            or edge_gaps
            or candidate_flags
            or candidate_gaps
            or review_flags
            or review_gaps
        )
        or reviewed < len(scope_ids)
    )
    invalidating = [
        finding
        for finding in treatments
        if finding.treatment in _SOURCE_INVALIDATING_TREATMENTS
        and finding.scope == "all"
    ]
    partial = [
        finding
        for finding in treatments
        if finding.treatment in _SOURCE_INVALIDATING_TREATMENTS
        and finding.scope != "all"
    ]
    gated_invalidating = [
        finding
        for finding in gated
        if finding.treatment in _SOURCE_INVALIDATING_TREATMENTS
    ]
    if invalidating:
        first = invalidating[0]
        return _SourceAudit(
            source_cluster_id=source_target.cluster_id,
            metadata=metadata,
            validity_status="invalid",
            reason=(
                f"source was {first.treatment} by {first.citing_case} "
                f"({first.citing_date or 'date unknown'})"
            ),
            history_complete=not incomplete,
        )
    if partial:
        first = partial[0]
        return _SourceAudit(
            source_cluster_id=source_target.cluster_id,
            metadata=metadata,
            validity_status="qualified",
            reason=(
                f"source was {first.treatment} only in part by "
                f"{first.citing_case} ({first.citing_date or 'date unknown'})"
            ),
            history_complete=not incomplete,
        )
    if gated_invalidating:
        first = gated_invalidating[0]
        return _SourceAudit(
            source_cluster_id=source_target.cluster_id,
            metadata=metadata,
            validity_status="unverified",
            reason=(
                f"only a gated source-history finding was found: "
                f"{first.treatment} by {first.citing_case}"
            ),
            history_complete=not incomplete,
        )
    if incomplete:
        detail = (
            next(iter(edge_flags or candidate_flags or review_flags), "")
            or "bounded source-history review was incomplete"
        )
        return _SourceAudit(
            source_cluster_id=source_target.cluster_id,
            metadata=metadata,
            validity_status="unverified",
            reason=detail,
            history_complete=False,
        )
    return _SourceAudit(
        source_cluster_id=source_target.cluster_id,
        metadata=metadata,
        validity_status="valid",
        reason="no invalidating treatment found in the bounded source history",
        history_complete=True,
    )


def _source_audit(
    provider: CourtListenerProvider,
    source_id: int,
    as_of_date: str,
    cache: _SourceValidationCache,
) -> _SourceAudit:
    """Return a cached source audit, fetching current metadata and history once."""
    key = (source_id, as_of_date)
    if key in cache.audits:
        return cache.audits[key]
    metadata = _source_metadata(provider, source_id)
    if not metadata:
        audit = _SourceAudit(
            validity_status="unverified",
            reason="source metadata unavailable",
        )
        cache.audits[key] = audit
        return audit
    cluster_id = str(metadata.get("cluster_id") or "")
    role = _opinion_role(str(metadata.get("opinion_type") or ""))
    tier = _precedential_tier(str(metadata.get("precedential_status") or ""))
    if role != "majority" or tier != "precedential":
        audit = _SourceAudit(
            source_cluster_id=cluster_id,
            metadata=metadata,
            validity_status="unverified",
            reason="source history was not reviewed because its precedence gate failed",
        )
        cache.audits[key] = audit
        return audit
    source_case = CaseRecord(
        title=str(metadata.get("title") or f"Opinion {source_id}"),
        court=str(metadata.get("court") or ""),
        citation=str(metadata.get("citation") or ""),
        decision_date=str(metadata.get("decision_date") or ""),
        courtlistener_opinion_id=str(source_id),
        url=str(metadata.get("url") or ""),
    )
    source_target = _target_from_metadata(
        source_case, source_id, metadata, source_case.title
    )
    if not source_target.cluster_resolved:
        audit = _SourceAudit(
            source_cluster_id=cluster_id,
            metadata=metadata,
            validity_status="unverified",
            reason="source cluster could not be resolved for history review",
        )
        cache.audits[key] = audit
        return audit
    audit = _source_history_audit(provider, source_target, metadata, as_of_date)
    cache.audits[key] = audit
    return audit


def _apply_source_audit(
    signal: SourceValiditySignal,
    audit: _SourceAudit,
    target_court: str,
) -> None:
    """Merge one audit into a signal without touching citator or queue state.

    A ``precedence_defect`` signal remains visible in advisory output but is
    not enforceable. This is where a known lower-court source is withheld:
    enforcement must not let it set or downgrade citator status.
    """
    precedence, precedence_reason = _source_precedence(
        audit.metadata, target_court
    )
    if audit.source_cluster_id and not signal.source_cluster_id:
        signal.source_cluster_id = audit.source_cluster_id
    if (
        signal.source_cluster_id
        and audit.source_cluster_id
        and signal.source_cluster_id != audit.source_cluster_id
    ):
        status = "unverified"
        reason = "source cluster id did not match the fetched cluster"
    elif audit.validity_status == "invalid":
        status = "invalid"
        reason = audit.reason
    elif precedence in {"separate", "nonprecedential", "persuasive"}:
        status = "precedence_defect"
        reason = precedence_reason
    elif audit.validity_status == "qualified":
        status = "qualified"
        reason = audit.reason
    elif audit.validity_status == "unverified" or precedence == "unverified":
        status = "unverified"
        reason = audit.reason or precedence_reason
    else:
        status = "valid"
        reason = audit.reason
    signal.status = status
    signal.reason = reason
    signal.precedence = precedence
    signal.withheld = not (
        precedence == "authoritative" and status in {"valid", "invalid", "qualified"}
    )
    signal.history_complete = audit.history_complete
    signal.checked_at = _checked_at()


def _validate_source_validity(
    provider: CourtListenerProvider,
    check: CitatorCheck,
    cache: _SourceValidationCache,
) -> None:
    """Fill advisory source statuses; never change status or queue inputs."""
    for signal in check.source_validity:
        source_id = _positive_id(signal.source_opinion_id)
        if source_id is None:
            signal.status = "not_assessed"
            signal.reason = "source opinion id is not usable for validation"
            continue
        audit = _source_audit(provider, source_id, check.as_of_date, cache)
        _apply_source_audit(signal, audit, check.target_court)


def _source_enforcement_action(signal: SourceValiditySignal) -> str:
    """Describe one source's enforcement treatment for the audit trail."""
    source = signal.source_opinion_id or "unknown"
    if signal.withheld:
        detail = signal.status or "not assessed"
        if signal.reason:
            detail += f": {signal.reason}"
        return f"source {source} withheld from enforcement ({detail})"
    reason = signal.reason or "no reason recorded"
    return f"source {source} {signal.status}: {reason}"


def _apply_source_validity_enforcement(check: CitatorCheck) -> None:
    """Apply opt-in enforcement without deleting advisory findings.

    A source is blocked when it is withheld or authoritative-but-invalid. If
    every authoritative negative finding is blocked, status is downgraded to
    unknown rather than claiming good law. Withheld lower-court sources are
    kept in the audit trail and filtered from queue demotion separately.
    """
    check.source_validity_enforced = True
    blocked_ids = check.source_validity_blocked_source_ids()
    if not blocked_ids:
        return
    reasons: list[str] = []
    for signal in check.source_validity:
        if signal.source_opinion_id not in blocked_ids:
            continue
        action = _source_enforcement_action(signal)
        if action not in check.source_validity_actions:
            check.source_validity_actions.append(action)
        if signal.withheld or signal.status not in {"invalid", "qualified"}:
            continue
        detail = signal.reason or "no reason recorded"
        reasons.append(
            "source-validity enforcement blocked "
            f"source {signal.source_opinion_id or 'unknown'} "
            f"({signal.status}: {detail})"
        )
    remaining = [
        finding
        for finding in check.negative_treatments
        if not (
            finding.source_opinion_id
            and finding.source_opinion_id in blocked_ids
        )
    ]
    if check.status in {"negative", "good_law"} and not remaining:
        check.status = "unknown"
        check.good_law_as_of = ""
        reasons.insert(
            0,
            "source-validity enforcement removed all authoritative support; "
            "status was downgraded to unknown for manual review.",
        )
    for reason in reasons:
        if reason not in check.review_reasons:
            check.review_reasons.append(reason)
    if reasons:
        check.review_required = True


def _unknown_check(
    case: CaseRecord, flags: list[str], as_of_date: str = ""
) -> CitatorCheck:
    """Build an unknown check for a target that could not be resolved."""
    gaps = [
        _coverage_gap(
            "target_unresolved",
            "; ".join(flags) or "The target case could not be resolved.",
        )
    ]
    (
        confidence_score,
        confidence,
        confidence_reason,
        review_required,
        review_reasons,
    ) = _confidence_assessment(
        reviewed=0,
        complete=False,
        gaps=gaps,
        ambiguous_findings=[],
    )
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
        as_of_date=as_of_date,
        confidence=confidence,
        confidence_score=confidence_score,
        confidence_reason=confidence_reason,
        review_required=review_required,
        review_reasons=review_reasons,
        checked_at=_checked_at(),
        coverage=CitatorCoverage(
            gaps=gaps,
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
    as_of_date: str = "",
    history_records: list[AppellateHistoryRecord] | None = None,
    snapshot_store: SnapshotStore | None = None,
    authority_entry: AuthorityQueueEntry | None = None,
    source_validity: bool = False,
    source_validity_mode: str = "extract",
    source_validation_cache: _SourceValidationCache | None = None,
) -> CitatorCheck:
    """Resolve and check one queued authority, returning an advisory result.

    When *as_of_date* is set, only citing opinions dated on or before that
    inclusive cutoff can affect status, the good-law date, or as-of coverage.
    Later opinions are reviewed separately and their findings never change the
    as-of result. Citing opinions without a usable date are excluded and make
    as-of coverage incomplete, since they cannot be placed on either side of
    the cutoff. Findings always retain stable source opinion identifiers;
    when *source_validity* is true, the check also gets an advisory list of
    those sources. In ``advisory`` mode the source list is bounded and
    validated against current metadata and later history; that validation
    never changes status, coverage, ranking, or queue order. In ``enforce``
    mode the validated signals additionally withhold non-authoritative or
    invalid sources from status support and queue demotion. Enforcement is
    opt-in and never upgrades status.
    """
    as_of_date = _normalize_as_of_date(as_of_date)
    cutoff = date.fromisoformat(as_of_date) if as_of_date else None
    target, flags = _resolve_target(case, provider)
    if target is None:
        check = _unknown_check(case, flags, as_of_date)
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
    post_as_of_citing: list[LaterCitingOpinion] = []
    post_as_of_findings: list[NegativeTreatment] = []
    undated_citing: list[LaterCitingOpinion] = []
    post_as_of_incomplete = False
    post_as_of_flags: list[str] = []
    scope_citing_count = len(citing_ids)
    if cutoff is not None:
        candidates, candidate_flags, candidate_gaps = _load_citing_candidates(
            provider, citing_ids
        )
        flags.extend(candidate_flags)
        gaps.extend(candidate_gaps)
        in_scope: list[_CitingCandidate] = []
        post_as_of: list[_CitingCandidate] = []
        undated: list[_CitingCandidate] = []
        for candidate in candidates:
            relation = _candidate_date_relation(
                candidate.decision_date, cutoff
            )
            if relation == "in_scope":
                in_scope.append(candidate)
            elif relation == "post_as_of":
                post_as_of.append(candidate)
            else:
                undated.append(candidate)
        if undated:
            undated_ids = ", ".join(
                str(candidate.opinion_id) for candidate in undated
            )
            detail = (
                "Citator could not establish a usable decision date for "
                f"citing opinion(s) {undated_ids}; they could fall on either "
                f"side of the {as_of_date} as-of cutoff and were not used in "
                "the as-of result."
            )
            flags.append(detail)
            gaps.append(_coverage_gap("citing_date_missing", detail))
            undated_citing = [
                _citing_summary(candidate.opinion_id, candidate.metadata)
                for candidate in undated
            ]
        in_scope_ids = [candidate.opinion_id for candidate in in_scope]
        post_as_of_ids = [
            candidate.opinion_id for candidate in post_as_of
        ]
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
        ) = _review_candidates(
            provider,
            target,
            in_scope,
            max_citing_opinions,
            source_ids=in_scope_ids,
            total_citing=len(in_scope_ids),
        )
        flags.extend(review_flags)
        gaps.extend(review_gaps)
        scope_citing_count = len(in_scope_ids)
        if post_as_of_ids:
            (
                post_treatments,
                post_gated,
                post_as_of_citing,
                _post_reviewed,
                _post_latest,
                post_as_of_flags,
                post_gaps,
                _post_timeline,
                _post_reaffirmance,
            ) = _review_candidates(
                provider,
                target,
                post_as_of,
                max_citing_opinions,
                source_ids=post_as_of_ids,
                total_citing=len(post_as_of_ids),
            )
            post_as_of_findings = [*post_treatments, *post_gated]
            # Any post-cutoff review gap makes the separate later-treatment
            # report incomplete, not just the opinion cap.
            post_as_of_incomplete = bool(post_gaps)
        complete = (
            target.cluster_resolved
            and edges_complete
            and not review_flags
            and not candidate_gaps
            and not undated
            and reviewed == len(in_scope_ids)
        )
    else:
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
        ) = _review_citing_opinions(
            provider, target, citing_ids, max_citing_opinions
        )
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
    if scope_citing_count > max_citing_opinions:
        truncated = True
        complete = False
    if not found and edges_complete:
        detail = f"Citator found no forward citing opinions for '{target.case_name}'."
        flags.append(detail)
        gaps.append(_coverage_gap("no_forward_citations", detail))
    if not complete:
        reviewed_scope = (
            f"{reviewed}/{scope_citing_count} as-of citing opinions"
            if as_of_date
            else f"{reviewed}/{found} citing opinions"
        )
        flags.append(
            f"Citator coverage for '{target.case_name}' is incomplete "
            f"({reviewed_scope} reviewed)."
        )

    temporal_confidence, temporal_reason = _temporal_confidence(
        complete=complete,
        reviewed=reviewed,
        latest=latest,
        gaps=gaps,
        excluded_undated=bool(undated_citing),
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
    ambiguous_findings = [*treatments, *gated_treatments]
    (
        confidence_score,
        confidence,
        confidence_reason,
        review_required,
        review_reasons,
    ) = _confidence_assessment(
        reviewed=reviewed,
        complete=complete,
        gaps=gaps,
        ambiguous_findings=ambiguous_findings,
    )

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
        as_of_date=as_of_date,
        confidence=confidence,
        confidence_score=confidence_score,
        confidence_reason=confidence_reason,
        review_required=review_required,
        review_reasons=review_reasons,
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
        post_as_of_citing_opinions=post_as_of_citing,
        post_as_of_treatment_findings=post_as_of_findings,
        undated_citing_opinions=undated_citing,
        post_as_of_incomplete=post_as_of_incomplete,
        post_as_of_flags=post_as_of_flags,
        negative_treatments=treatments,
        gated_treatment_findings=gated_treatments,
        binding_negative_treatments=binding_treatments,
        persuasive_negative_treatments=persuasive_treatments,
        treatment_timeline=treatment_timeline,
        later_reaffirmance=later_reaffirmance,
        source_validity=(
            _source_validity_signals(
                [*treatments, *gated_treatments, *post_as_of_findings]
            )
            if source_validity
            else []
        ),
        flags=flags,
    )
    check.appellate_history = build_appellate_history(
        check, history_records
    )
    check.subsequent_history_assessed = (
        check.appellate_history.status == "verified"
    )
    if source_validity and source_validity_mode in {"advisory", "enforce"}:
        _validate_source_validity(
            provider,
            check,
            source_validation_cache or _SourceValidationCache(),
        )
    if source_validity and source_validity_mode == "enforce":
        _apply_source_validity_enforcement(check)
    _apply_authority_entry(check, authority_entry)
    return _persist_check(check, snapshot_store)


def run_citator(
    cases: list[CaseRecord],
    provider: CourtListenerProvider,
    *,
    max_cases: int = 10,
    max_citing_opinions: int = 5,
    max_pages: int = 1,
    as_of_date: str = "",
    history_records: list[AppellateHistoryRecord] | None = None,
    snapshot_store: SnapshotStore | None = None,
    authority_queue: list[QueuedAuthority] | None = None,
    source_validity: bool = False,
    source_validity_mode: str = "extract",
) -> list[CitatorCheck]:
    """Check the prioritized authority queue up to *max_cases*.

    Callers may pass a pre-built queue; otherwise the queue is built from
    *cases*. The queue changes review order only, never ranking or status.
    *source_validity* adds source identifiers to each check; in ``advisory``
    mode it also validates those sources without changing status or queue
    behavior. ``enforce`` mode additionally applies the withheld-source rules
    to status and queue demotion.
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
    validation_cache = _SourceValidationCache()
    return [
        check_case(
            item.case,
            active_provider,
            max_citing_opinions=max_citing_opinions,
            max_pages=max_pages,
            as_of_date=as_of_date,
            history_records=history_records,
            snapshot_store=snapshot_store,
            authority_entry=item.entry,
            source_validity=source_validity,
            source_validity_mode=source_validity_mode,
            source_validation_cache=validation_cache,
        )
        for item in queued
    ]
