"""Query expansion and provider-specific query adaptation.

The research pipeline searches with `site:`-prefixed court queries (useful for
DuckDuckGo) but CourtListener has its own court filter and its full-text
search would treat `site:` tokens as noise. This module derives variant
queries from the shared topic vocabulary (TOPICS synonyms) and the
statute/doctrine table (STATUTE_HINTS), and adapts queries per provider.

Expansion is issue-aware: only topics whose keyword appears in the issue
contribute synonyms, so unrelated topics don't pollute the search. The issue
is read from the first quoted phrase of the query (the format produced by
``build_case_queries``).
"""

from __future__ import annotations

import re

from .topics import STATUTE_HINTS, TOPICS

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
    """Topic synonyms and statute fragments, deduped, for building variant queries.

    Only topics whose ``keyword`` appears in *issue* contribute synonyms, so
    an issue about rating doesn't pull in service-connection phrasing. Topics
    without a keyword never match and are skipped. Statute/doctrine fragments
    follow the synonyms (only the fragment is used, not the prose hint).
    """
    issue_lower = (issue or "").lower()
    phrases: list[str] = []
    seen: set[str] = set()
    for topic in TOPICS:
        if not topic.keyword or topic.keyword not in issue_lower:
            continue
        for synonym in topic.synonyms:
            # Skip phrases already present in the issue (e.g. keyword == synonym)
            # so we only add genuinely new terms.
            if not synonym or synonym in issue_lower or synonym in seen:
                continue
            seen.add(synonym)
            phrases.append(synonym)
    for fragment, _ in STATUTE_HINTS:
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
    if provider_name in ("courtlistener", "bva"):
        return strip_site_prefixes(query)
    return query
