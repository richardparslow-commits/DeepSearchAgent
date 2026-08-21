"""Query expansion and provider-specific query adaptation.

The research pipeline searches with `site:`-prefixed court queries (useful for
DuckDuckGo) but CourtListener has its own court filter and its full-text
search would treat `site:` tokens as noise. This module derives variant
queries from the shared topic vocabulary (TOPICS synonyms) and the
statute/doctrine table (STATUTE_HINTS), and adapts queries per provider.

Expansion is issue-aware: only topics implicated by the issue contribute
synonyms, and only the statute fragments anchored to those topics are
appended, so unrelated topics and statutes don't pollute the search. The
issue is read from the first quoted phrase of the query (the format produced
by ``build_case_queries``).
"""

from __future__ import annotations

import re

from .planning import detect_issue_topics, relevant_statutes
from .topics import TOPICS_BY_NAME

# A `site:` token (e.g. `site:uscourts.cavc.gov`). CourtListener can't use it.
_SITE_PATTERN = re.compile(r"\bsite:[^\s]+")
# The first quoted phrase in a query (build_case_queries puts the issue first).
_QUOTED_PHRASE = re.compile(r'"([^"]+)"')


def strip_site_prefixes(query: str) -> str:
    """Remove ``site:`` tokens from *query* (CourtListener has its own filter)."""
    return _SITE_PATTERN.sub("", query).strip()


def _issue_from_query(query: str) -> str:
    """Extract the issue from a search query.

    ``build_case_queries`` embeds the issue as the first quoted phrase
    (``site:... \"tinnitus\" \"Compensation\" ...``). Falls back to the whole
    query with ``site:`` tokens removed when nothing is quoted.
    """
    match = _QUOTED_PHRASE.search(query or "")
    if match:
        return match.group(1)
    return strip_site_prefixes(query)


def _expansion_phrases(issue: str) -> list[str]:
    """Topic synonyms and relevant statute fragments, deduped, for variant queries.

    Only topics implicated by *issue* contribute synonyms (via
    :func:`planning.detect_issue_topics`), so an issue about rating doesn't
    pull in service-connection phrasing; topics without a keyword never match
    and are skipped. Statute/doctrine fragments are likewise limited to those
    anchored to the detected topics (via :func:`planning.relevant_statutes`),
    so an unrelated issue like ``tinnitus`` no longer tacks on ``5107``/
    ``7104``. Only the fragment is used, not the prose hint.
    """
    issue_lower = (issue or "").lower()
    topics = detect_issue_topics(issue)
    phrases: list[str] = []
    seen: set[str] = set()
    for name in topics:
        for synonym in TOPICS_BY_NAME[name].synonyms:
            # Skip phrases already present in the issue (e.g. keyword ==
            # synonym) so we only add genuinely new terms.
            if not synonym or synonym in issue_lower or synonym in seen:
                continue
            seen.add(synonym)
            phrases.append(synonym)
    for fragment in relevant_statutes(topics):
        if fragment not in seen:
            seen.add(fragment)
            phrases.append(fragment)
    return phrases


def derive_variants(query: str, limit: int = 3, issue: str | None = None) -> list[str]:
    """Return *query* plus up to *limit* variant queries broadening it.

    Each variant appends one expansion phrase (topic synonym relevant to the
    issue, or a statute fragment) to the original query. The original query is
    always first and is not counted against *limit*. ``limit <= 0`` returns
    just the original. Pass *issue* explicitly to skip the query parse.
    """
    if issue is None:
        issue = _issue_from_query(query)
    variants = [query]
    if limit <= 0:
        return variants
    for phrase in _expansion_phrases(issue):
        variants.append(f"{query} \"{phrase}\"")
        if len(variants) - 1 >= limit:
            break
    return variants


def adapt_query_for_provider(query: str, provider_name: str) -> str:
    """Adapt *query* for the named provider (e.g. strip ``site:`` tokens).

    CourtListener and BVA have their own court/decision filters, so ``site:``
    tokens are noise there; DuckDuckGo keeps them.
    """
    if provider_name in ("courtlistener", "bva", "bvasitemap"):
        return strip_site_prefixes(query)
    return query
