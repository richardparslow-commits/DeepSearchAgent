from __future__ import annotations

import logging
import os
import time

from .models import CaseRecord, LegalAnalysis
from .search import SearchError, search_web

logger = logging.getLogger(__name__)


LEGAL_KEYWORDS = {
    "service connection": ["service connection", "compensation", "nexus", "diagnosis"],
    "benefit of the doubt": ["benefit of the doubt", "reasonable doubt", "doubtful"],
    "reasons and bases": ["reasons and bases", "reasoned decision", "adequate explanation"],
    "evidence": ["competent evidence", "medical evidence", "lay evidence", "evidence"],
    "presumption": ["presumption", "presumptive service connection"],
    "aggravation": ["aggravation", "aggravated"],
    "rating": ["rating", "evaluation", "schedular"],
}


def build_case_queries(claim_issue: str, claim_type: str) -> list[str]:
    issue = claim_issue.strip()
    if not issue:
        issue = "VA compensation"
    normalized_issue = issue.replace('"', "")
    issue_query = f'"{normalized_issue}" "{claim_type}"'

    court_queries = [
        f"site:uscourts.cavc.gov {issue_query} veterans compensation",
        f"site:caafc.uscourts.gov {issue_query} veterans compensation",
        f"site:supremecourt.gov {issue_query} veterans compensation",
        f"site:bva.va.gov {issue_query} veterans compensation",
        f"site:uscourts.cavc.gov {normalized_issue} service connection veterans law",
        f"site:caafc.uscourts.gov {normalized_issue} veterans benefits court",
        f"site:supremecourt.gov {normalized_issue} veterans benefits law",
        f"site:bva.va.gov {normalized_issue} veterans compensation decision",
    ]
    return court_queries


def detect_court_name(url: str) -> str:
    if "uscourts.cavc.gov" in url:
        return "Court of Appeals for Veterans Claims"
    if "caafc.uscourts.gov" in url:
        return "U.S. Court of Appeals for the Federal Circuit"
    if "supremecourt.gov" in url:
        return "U.S. Supreme Court"
    if "bva.va.gov" in url:
        return "Board of Veterans' Appeals"
    return "Veterans law research result"


def score_case_relevance(case: CaseRecord, issue: str) -> int:
    text = f"{case.title} {case.snippet} {case.holding} {case.impact} {case.issue}".lower()
    score = 0
    normalized_issue = issue.lower()
    if normalized_issue in text:
        score += 2
    for keyword, synonyms in LEGAL_KEYWORDS.items():
        if keyword in normalized_issue:
            if any(synonym in text for synonym in synonyms):
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
    return CaseRecord(
        title=title,
        court=court_name,
        citation="",
        url=url,
        snippet=snippet,
        issue=issue,
        holding="",
        impact="",
        authority_rank=0,
        relevance_score=0,
    )


def fetch_cases_for_issue(claim_issue: str, claim_type: str = "Compensation", max_results: int = 10) -> list[CaseRecord]:
    cases: list[CaseRecord] = []
    errors: list[Exception] = []
    delay_seconds = float(os.getenv("SEARCH_DELAY_SECONDS", "1.5"))
    for index, query in enumerate(build_case_queries(claim_issue, claim_type)):
        if index > 0 and delay_seconds > 0:
            time.sleep(delay_seconds)
        try:
            found = search_web(query, max_results=max_results)
        except Exception as exc:  # noqa: BLE001 - keep trying remaining queries; report below if all fail
            logger.warning("Search failed for query %r: %s", query, exc)
            errors.append(exc)
            continue
        for result in found:
            court_name = detect_court_name(result.get("url", ""))
            case = normalize_case(result, court_name, claim_issue)
            case.relevance_score = score_case_relevance(case, claim_issue)
            cases.append(case)

    if not cases and errors:
        raise SearchError(
            f"All {len(errors)} searches failed for issue {claim_issue!r}. "
            "The search provider may be blocking or rate-limiting automated requests. "
            f"Last error: {errors[-1]}"
        ) from errors[-1]

    deduped: list[CaseRecord] = []
    seen: set[tuple[str, str]] = set()
    for case in sorted(cases, key=lambda c: c.relevance_score, reverse=True):
        key = (case.title, case.url)
        if key not in seen:
            seen.add(key)
            deduped.append(case)

    ranked = []
    for index, case in enumerate(deduped, start=1):
        case.authority_rank = index
        ranked.append(case)
    return ranked[:max_results]


def summarize_case_impact(case: CaseRecord) -> str:
    text = f"{case.title} {case.snippet} {case.holding} {case.impact}".lower()
    issues: list[str] = []
    if "service connection" in text:
        issues.append("service connection")
    if "benefit of the doubt" in text:
        issues.append("benefit of the doubt")
    if "reasons and bases" in text:
        issues.append("reasons and bases")
    if "evidence" in text:
        issues.append("evidence evaluation")
    if "nexus" in text:
        issues.append("nexus")
    if "rating" in text:
        issues.append("rating")
    if not issues:
        issues.append(case.issue.lower() or "the factual and legal issue")

    return (
        f"This ruling is relevant to {', '.join(issues[:3])} in VA compensation claims. "
        f"It underscores that the agency must apply the governing standards carefully, explain the basis for the decision, "
        f"and assess the evidence in a way that is consistent with veterans-law principles and reviewable on appeal."
    )


def analyze_cases_for_claim(claim_issue: str, claim_type: str = "Compensation", max_results: int = 10) -> LegalAnalysis:
    cases = fetch_cases_for_issue(claim_issue, claim_type, max_results=max_results)
    if not cases:
        raise ValueError(f"No cases found for: {claim_issue}")

    summary = "\n".join(f"- {case.title} ({case.court}) [relevance: {case.relevance_score}]" for case in cases[:5])
    likely_principles = [
        "Evidence must be assessed under the applicable VA and veterans-law standards",
        "The agency must provide a reasoned decision supported by the record",
        "The claimant's entitlement depends on the quality, explanation, and evaluation of the evidence",
        "The court will review whether the Board followed the governing legal framework and credited the proper evidence",
    ]

    direct_relevancies = [summarize_case_impact(case) for case in cases[:3]]
    how_it_affects_va_claims = " ".join(direct_relevancies)
    top_cases = [f"{case.title} ({case.court})" for case in cases[:5]]

    return LegalAnalysis(
        issue=claim_issue,
        summary=summary,
        likely_applicable_principles=likely_principles,
        how_it_affects_va_claims=how_it_affects_va_claims,
        next_steps=[
            "Identify the precise legal issue and the evidence in the claim file",
            "Compare the facts of the current claim to the most analogous quoted authorities",
            "Prepare a concise argument tying the evidence to the governing veterans-law standard",
            "Check whether the Board or agency decision failed to explain a key fact, medical opinion, or benefit-of-the-doubt issue",
        ],
        top_cases=top_cases,
    )


if __name__ == "__main__":
    result = analyze_cases_for_claim("service connection for tinnitus")
    print(result.model_dump_json(indent=2))
