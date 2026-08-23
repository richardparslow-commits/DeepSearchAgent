"""Tests for the command-line entry point (va_legal_agent.__main__)."""

import csv
import io
import json
import logging

import pytest

from va_legal_agent.__main__ import (
    _configure_logging,
    _mask_proxy_url,
    _redacted_settings,
    _resolve_log_format,
    _run_id,
    main,
    render_analysis,
)
from va_legal_agent.models import ClaimElement, Contradiction, LegalAnalysis, PrincipleFinding


def _sample_analysis() -> LegalAnalysis:
    return LegalAnalysis(
        issue="service connection for tinnitus",
        summary="- Smith v. Wilkie (Court of Appeals for Veterans Claims) [score: 3.00]",
        likely_applicable_principles=[
            "Service connection requires a nexus (see: Smith v. Wilkie)"
        ],
        how_it_affects_va_claims=(
            "For the issue of service connection for tinnitus, the retrieved "
            "authorities indicate the following governing principles."
        ),
        next_steps=[
            "Map the record to the Caluza elements.",
            "Treat this output as research support.",
        ],
        top_cases=["Smith v. Wilkie (Court of Appeals for Veterans Claims)"],
        detected_elements=[
            ClaimElement(
                name="service connection",
                description="The Caluza elements.",
                guidance="Assemble a current diagnosis, in-service event, and nexus.",
                covered_by=["Smith v. Wilkie"],
            )
        ],
        principle_findings=[
            PrincipleFinding(
                principle="A nexus is required.", source_cases=["Smith v. Wilkie"]
            )
        ],
        contradictions=[
            Contradiction(
                statement="The decisions split on the nexus standard.",
                case_a="Smith v. Wilkie",
                case_b="Jones v. McDonough",
            )
        ],
        strengths=["Retrieved authority addresses 'service connection': Smith v. Wilkie."],
        gaps=[],
        coverage_score=1.0,
        interpretation_source="template",
    )


def _usage_payload(
    daily_used=0, daily_limit=125, daily_reset="2026-08-18T05:12:28+00:00"
):
    """A realistic api-usage payload (user scope, day/hour/minute rows)."""
    return {
        "current_usage": [
            {
                "scope": "user",
                "rate": "125/day",
                "used": daily_used,
                "limit": daily_limit,
                "remaining": max(daily_limit - daily_used, 0),
                "window_seconds": 86400,
                "reset_at": daily_reset,
                "blocked": False,
            },
            {
                "scope": "user",
                "rate": "50/hour",
                "used": 0,
                "limit": 50,
                "remaining": 50,
                "window_seconds": 3600,
                "reset_at": None,
                "blocked": False,
            },
            {
                "scope": "user",
                "rate": "5/min",
                "used": 0,
                "limit": 5,
                "remaining": 5,
                "window_seconds": 60,
                "reset_at": None,
                "blocked": False,
            },
        ],
        "historical_usage": {"2026-07-17": 75, "total": 123},
        "membership": None,
        "processing_delay": 0.4,
    }


def _expected_dry_run_requests(issue: str) -> int:
    """The worst-case query count a dry run should derive from the planner."""
    from va_legal_agent.config import get_settings
    from va_legal_agent.interpretation import detect_claim_elements
    from va_legal_agent.planning import decompose_issue, plan_queries

    settings = get_settings()
    queries = plan_queries(decompose_issue(issue, "Compensation"))
    elements = [spec.name for spec in detect_claim_elements(issue)]
    worst = len(queries) + settings.search_max_refinement_rounds * (len(elements) or 1)
    return worst * settings.search_query_variants


def test_dry_run_reports_plan_without_searching(capsys, monkeypatch):
    """--dry-run prints the cost plan and never executes a search."""
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--dry-run", "--output-format", "text", "tinnitus service connection"],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    main()

    out = capsys.readouterr().out
    assert "Dry run - no searches were executed" in out
    assert "Issue: tinnitus service connection" in out
    assert "Plan: " in out
    assert "duckduckgo" in out
    assert "Requests per provider" in out
    assert "Wall-time estimate" in out
    assert "Verdict: unchecked" in out
    assert "Top cases" not in out


def test_dry_run_json_shape_has_verdict_and_math(capsys, monkeypatch):
    """JSON dry-run exposes the estimate fields and consistent arithmetic."""
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--dry-run", "--output-format", "json", "tinnitus"],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    main()

    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "unchecked"
    assert report["courtlistener"] is None
    row = report["providers"][0]
    assert row["name"] == "duckduckgo"
    assert row["requests"] == _expected_dry_run_requests("tinnitus")
    assert (
        report["total_queries_worst_case"]
        == report["base_queries"] + report["refinement_rounds"] * report["gap_queries_per_round"]
    )
    assert report["deep_read"]["enabled"] is False
    assert report["total_requests_estimate"] == sum(
        r["requests"] for r in report["providers"]
    )
    assert report["wall_time_seconds"]["nominal"] >= 0
    assert report["wall_time_seconds"]["worst_case"] >= 0


def test_dry_run_deep_read_counts_opinion_fetches(capsys, monkeypatch):
    """Deep-read adds its opinion-detail fetches to the request totals."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--dry-run",
            "--output-format",
            "json",
            "--deep-read",
            "--deep-read-limit",
            "4",
            "--max-results",
            "6",
            "tinnitus",
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    main()

    report = json.loads(capsys.readouterr().out)
    assert report["deep_read"]["enabled"] is True
    assert report["deep_read"]["opinion_fetches"] == 4
    assert report["total_requests_estimate"] == _expected_dry_run_requests(
        "tinnitus"
    ) + 4


def test_dry_run_deep_read_fetches_capped_by_max_results(capsys, monkeypatch):
    """Opinion-detail fetches never exceed the number of cases kept."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--dry-run",
            "--output-format",
            "json",
            "--deep-read",
            "--deep-read-limit",
            "9",
            "--max-results",
            "3",
            "tinnitus",
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    main()

    report = json.loads(capsys.readouterr().out)
    assert report["deep_read"]["opinion_fetches"] == 3


def test_dry_run_courtlistener_quota_proceed(capsys, monkeypatch):
    """A healthy live usage payload yields a PROCEED verdict with numbers."""
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--dry-run", "--output-format", "json", "tinnitus service connection"],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage", lambda: _usage_payload()
    )

    main()

    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "proceed"
    cl = report["courtlistener"]
    assert cl["guard_enabled"] is True
    assert cl["daily"]["remaining"] == 125
    assert cl["daily"]["limit"] == 125
    assert cl["hourly"]["remaining"] == 50
    assert cl["minute"]["remaining"] == 5
    assert cl["abort_reasons"] == []
    assert int(cl["requests_estimated"]) <= 125


def test_dry_run_courtlistener_aborts_when_daily_budget_short(capsys, monkeypatch):
    """A nearly-exhausted daily window yields an ABORT verdict."""
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--dry-run", "--output-format", "json", "tinnitus service connection"],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=120),
    )

    main()

    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "abort"
    assert report["courtlistener"]["abort_reasons"]
    assert "daily window" in report["courtlistener"]["abort_reasons"][0]


def test_dry_run_guard_disabled_skips_live_usage_fetch(capsys, monkeypatch):
    """COURTLISTENER_USAGE_GUARD=0 never calls the api-usage endpoint."""
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--dry-run", "--output-format", "json", "tinnitus"],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setenv("COURTLISTENER_USAGE_GUARD", "0")

    def _should_not_be_called():
        raise AssertionError("usage endpoint must not be fetched with the guard off")

    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage", _should_not_be_called
    )

    main()

    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "unchecked"
    assert report["courtlistener"]["guard_enabled"] is False


def test_dry_run_reports_usage_fetch_failure(capsys, monkeypatch):
    """An unreadable usage endpoint degrades to unchecked with the reason."""
    from va_legal_agent.search import SearchError

    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--dry-run", "--output-format", "json", "tinnitus"],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: (_ for _ in ()).throw(SearchError("api-usage 401: no token")),
    )

    main()

    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "unchecked"
    assert "401" in report["courtlistener"]["error"]


def test_dry_run_text_renders_full_abort_report(capsys, monkeypatch):
    """Text dry-run surfaces deep-read, both quota windows, and the abort verdict."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--dry-run",
            "--output-format",
            "text",
            "--deep-read",
            "--deep-read-limit",
            "6",
            "--max-results",
            "10",
            "tinnitus service connection",
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=120),
    )

    main()

    out = capsys.readouterr().out
    assert "Deep-read: enabled - up to 6 opinion-detail fetches" in out
    assert "daily:  5 / 125 remaining (used 120)" in out
    assert "hourly: 50 / 50 remaining" in out
    assert "minute: 5 / 5 remaining" in out
    assert "would abort:" in out
    assert "daily window" in out
    assert "minute window" in out
    assert "Verdict: ABORT" in out


def test_dry_run_text_renders_proceed_verdict(capsys, monkeypatch):
    """Healthy windows render the PROCEED verdict with the fitted count."""
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--dry-run", "--output-format", "text", "tinnitus"],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage", lambda: _usage_payload()
    )

    main()

    out = capsys.readouterr().out
    assert "fits the planned" in out
    assert "Verdict: PROCEED" in out


def test_dry_run_text_guard_disabled_and_error_paths(capsys, monkeypatch):
    """Guard-off and unreadable-usage text paths render their notes."""
    from va_legal_agent.search import SearchError

    # Guard disabled: the note is printed and the endpoint is never hit.
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--dry-run", "--output-format", "text", "tinnitus"],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setenv("COURTLISTENER_USAGE_GUARD", "0")

    def _should_not_be_called():
        raise AssertionError("usage endpoint must not be fetched with the guard off")

    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage", _should_not_be_called
    )
    main()
    out = capsys.readouterr().out
    assert "COURTLISTENER_USAGE_GUARD=0 disables the pre-flight quota check" in out
    assert "Verdict: unchecked" in out

    # Guard on but the endpoint fails: the error is surfaced, verdict unchecked.
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--dry-run", "--output-format", "text", "tinnitus"],
    )
    monkeypatch.delenv("COURTLISTENER_USAGE_GUARD", raising=False)
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: (_ for _ in ()).throw(SearchError("api-usage 401: no token")),
    )
    main()
    out = capsys.readouterr().out
    assert "could not read usage: api-usage 401" in out
    assert "Verdict: unchecked" in out


def test_dry_run_text_sparse_report_branches():
    """Text renderer tolerates empty daily/hourly/minute blocks and no note."""
    from va_legal_agent.__main__ import _dry_run_to_text

    report = {
        "issue": "tinnitus",
        "claim_type": "Compensation",
        "base_queries": 8,
        "refinement_rounds": 1,
        "gap_queries_per_round": 1,
        "total_queries_worst_case": 9,
        "providers": [
            {
                "name": "courtlistener",
                "base_queries": 8,
                "gap_queries": 1,
                "variants": 3,
                "pages": 1,
                "requests": 27,
                "pacing_seconds": 12.0,
            }
        ],
        "deep_read": {"enabled": False, "limit": 0, "opinion_fetches": 0},
        "total_requests_estimate": 27,
        "wall_time_seconds": {"nominal": 324.0, "worst_case": 648.0},
        "courtlistener": {
            "configured": True,
            "guard_enabled": True,
            "requests_estimated": 27,
            "daily": {},
            "hourly": None,
            "minute": None,
            "abort_reasons": [],
        },
        "verdict": "proceed",
    }
    out = _dry_run_to_text(report)
    assert "fits the planned 27 request(s)." in out
    assert "Verdict: PROCEED" in out

    # Guard disabled without a note: no daily/minute lines are rendered.
    report["courtlistener"] = {"configured": True, "guard_enabled": False}
    report["verdict"] = "unchecked"
    out = _dry_run_to_text(report)
    assert "CourtListener quota (live api-usage endpoint)" in out
    assert "daily:" not in out
    assert "Verdict: unchecked" in out


def test_format_wall_seconds_unit(capsys):
    """Wall-time formatting covers seconds, minutes, and hours."""
    from va_legal_agent.__main__ import _format_wall_seconds

    assert _format_wall_seconds(5) == "5s"
    assert _format_wall_seconds(95) == "1.6 min"
    assert _format_wall_seconds(7200) == "2.00 h"


def test_estimate_missing_hourly_row_yields_none(capsys, monkeypatch):
    """A usage payload without an hourly row degrades to hourly=None."""
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--dry-run", "--output-format", "json", "tinnitus"],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    payload = _usage_payload()
    payload["current_usage"] = [
        row for row in payload["current_usage"] if "/hour" not in str(row["rate"])
    ]
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage", lambda: payload
    )

    main()

    report = json.loads(capsys.readouterr().out)
    assert report["courtlistener"]["hourly"] is None


def test_batch_dry_run_text_table_with_cumulative_quota(capsys, monkeypatch, tmp_path):
    """Batch table allocates the daily window cumulatively across issues."""
    issues = ["tinnitus service connection", "knee rating increase", "back pain nexus"]
    issues_file = tmp_path / "issues.txt"
    # Blank lines and a comment line must be skipped.
    issues_file.write_text(
        "# schedule for tonight\n\n" + "\n".join(issues) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--issues-file", str(issues_file)],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=0, daily_limit=125),
    )

    main()

    out = capsys.readouterr().out
    assert "Batch dry run - no searches were executed" in out
    assert "Issues: 3" in out
    expected = [_expected_dry_run_requests(issue) for issue in issues]
    assert all(f"{req:>6}" in out for req in expected)  # CL req column
    # Cumulative: 125 after issue 1, 125 - r1 after issue 2, ...
    running = 125
    for issue, req in zip(issues, expected):
        running -= req
        assert f"{running:>11}" in out
    assert "left after the batch" in out
    assert out.count("proceed") == 3
    assert "would ABORT" not in out


def test_batch_dry_run_abort_when_window_runs_dry(capsys, monkeypatch, tmp_path):
    """Issues beyond the daily window get an ABORT verdict."""
    issues = ["service connection for tinnitus", "knee rating increase"]
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("\n".join(issues) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--issues-file", str(issues_file)],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    first = _expected_dry_run_requests(issues[0])
    # Remaining window fits exactly issue 1 (plus 5 spare), so issue 2 aborts.
    remaining = first + 5
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=0, daily_limit=remaining),
    )

    main()

    out = capsys.readouterr().out
    assert "proceed" in out
    assert "ABORT" in out
    assert "1 of 2 issue(s) would ABORT" in out
    assert "left after the batch" in out


def test_batch_dry_run_csv_output(capsys, monkeypatch, tmp_path):
    """--output-format csv yields a machine-parseable table with the header."""
    issues = ["tinnitus"]
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("\n".join(issues) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--batch-dry-run",
            "--issues-file",
            str(issues_file),
            "--output-format",
            "csv",
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=0, daily_limit=125),
    )

    main()

    import csv as _csv
    import io as _io

    rows = list(_csv.DictReader(_io.StringIO(capsys.readouterr().out)))
    assert rows[0]["issue"] == "tinnitus"
    assert rows[0]["courtlistener_requests"] == str(
        _expected_dry_run_requests("tinnitus")
    )
    assert rows[0]["total_requests"] == rows[0]["courtlistener_requests"]
    assert rows[0]["verdict"] == "proceed"
    assert rows[0]["courtlistener_daily_after"] == str(
        125 - _expected_dry_run_requests("tinnitus")
    )
    assert rows[0]["worst_wall_seconds"]
    assert rows[0]["nominal_wall_seconds"]


def test_batch_dry_run_guard_off_skips_usage_fetch_and_uses_dash(
    capsys, monkeypatch, tmp_path
):
    """Guard off: the endpoint is never called and daily after shows '-.'"""
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("tinnitus\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--issues-file", str(issues_file)],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setenv("COURTLISTENER_USAGE_GUARD", "0")

    def _should_not_be_called():
        raise AssertionError("usage endpoint must not be fetched with the guard off")

    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage", _should_not_be_called
    )

    main()

    out = capsys.readouterr().out
    assert "unchecked" in out
    assert "daily window" not in out


def test_batch_dry_run_usage_error_surfaces(capsys, monkeypatch, tmp_path):
    """An unreadable usage endpoint degrades to unchecked with the reason."""
    from va_legal_agent.search import SearchError

    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("tinnitus\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--issues-file", str(issues_file)],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: (_ for _ in ()).throw(SearchError("api-usage 401: no token")),
    )

    main()

    out = capsys.readouterr().out
    assert "usage unavailable" in out
    assert "no token" in out
    assert "unchecked" in out


def test_batch_dry_run_reads_usage_once_for_all_issues(capsys, monkeypatch, tmp_path):
    """N issues trigger exactly one api-usage fetch."""
    calls = []

    def _counting_usage():
        calls.append(1)
        return _usage_payload()

    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("a\nb\nc\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--issues-file", str(issues_file)],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage", _counting_usage
    )

    main()

    assert len(calls) == 1


def test_batch_dry_run_requires_an_issue(capsys, monkeypatch, tmp_path):
    """An empty batch reports a usage error instead of running."""
    issues_file = tmp_path / "empty.txt"
    issues_file.write_text("\n# nothing here\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--issues-file", str(issues_file)],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code != 0


def test_batch_dry_run_missing_issues_file_errors(capsys, monkeypatch):
    """A missing issues file is a usage error, not a crash."""
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--issues-file", "/nope/nowhere.txt"],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code != 0


def test_batch_dry_run_positional_issue_without_file(capsys, monkeypatch):
    """A positional issue works without --issues-file (the file branch skipped)."""
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "tinnitus"],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    main()

    out = capsys.readouterr().out
    assert "Issues: 1" in out
    assert "tinnitus" in out


def test_batch_dry_run_positional_plus_file(capsys, monkeypatch, tmp_path):
    """A positional issue is appended after the file's issues."""
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("one\ntwo\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--batch-dry-run",
            "--issues-file",
            str(issues_file),
            "three",
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    main()

    out = capsys.readouterr().out
    assert "Issues: 3" in out
    assert "one" in out and "two" in out and "three" in out


def test_batch_dry_run_no_daily_row_degrades(capsys, monkeypatch, tmp_path):
    """A payload without a daily row still renders (daily after shows '-')."""
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("tinnitus\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--issues-file", str(issues_file)],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    from va_legal_agent.search import SearchError
    from va_legal_agent.providers import courtlistener_daily_budget

    real_budget = courtlistener_daily_budget
    calls = []

    def _flaky_budget(usage):
        calls.append(1)
        if len(calls) == 1:
            raise SearchError("no daily row")  # batch-level probe fails...
        return real_budget(usage)  # ...but per-issue estimates still resolve

    monkeypatch.setattr(
        "va_legal_agent.__main__.courtlistener_daily_budget", _flaky_budget
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage", lambda: _usage_payload()
    )

    main()

    out = capsys.readouterr().out
    assert "Issues: 1" in out  # no crash; the batch-level window probe degraded


def test_dry_run_output_file_write_failure(capsys, monkeypatch, tmp_path):
    """An unwritable --output-file is a usage error for --dry-run."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--dry-run",
            "--output-format",
            "json",
            "--output-file",
            str(tmp_path / "no" / "such" / "dir" / "x.json"),
            "tinnitus",
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code != 0


def test_batch_dry_run_output_file_write_failure(capsys, monkeypatch, tmp_path):
    """An unwritable --output-file is a usage error for --batch-dry-run too."""
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("tinnitus\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--batch-dry-run",
            "--issues-file",
            str(issues_file),
            "--output-format",
            "csv",
            "--output-file",
            str(tmp_path / "no" / "such" / "dir" / "x.csv"),
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code != 0


def test_batch_dry_run_start_at_skips_early_issues(capsys, monkeypatch, tmp_path):
    """--start-at N plans only from the Nth issue onward."""
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--start-at", "2", "--issues-file", str(issues_file)],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    main()

    out = capsys.readouterr().out
    assert "Issues: 2" in out
    assert "alpha" not in out
    assert "beta" in out
    assert "gamma" in out


def test_batch_dry_run_start_at_beyond_batch_errors(capsys, monkeypatch, tmp_path):
    """--start-at past the end of the batch is a usage error."""
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("alpha\nbeta\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--start-at", "9", "--issues-file", str(issues_file)],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code != 0


def test_batch_dry_run_start_at_zero_errors(capsys, monkeypatch, tmp_path):
    """--start-at 0 is rejected (indices are 1-based)."""
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("alpha\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--start-at", "0", "--issues-file", str(issues_file)],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code != 0


def test_batch_dry_run_only_blocked_shows_aborted_only(capsys, monkeypatch, tmp_path):
    """--only-blocked lists just the issues the window can't cover."""
    issues = ["alpha", "beta", "gamma"]
    reqs = [_expected_dry_run_requests(issue) for issue in issues]
    window = reqs[0] + reqs[1]  # exactly fits the first two
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("\n".join(issues) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--only-blocked", "--issues-file", str(issues_file)],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=0, daily_limit=window),
    )

    main()

    out = capsys.readouterr().out
    assert "Showing 1 of 3 issue(s) (--only-blocked)." in out
    assert "gamma" in out
    assert "1 of 3 issue(s) would ABORT" in out
    assert "beta" not in out  # the proceeding rows are hidden


def test_batch_dry_run_only_blocked_none(capsys, monkeypatch, tmp_path):
    """--only-blocked with nothing blocked says so instead of an empty table."""
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("alpha\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--batch-dry-run", "--only-blocked", "--issues-file", str(issues_file)],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage", lambda: _usage_payload()
    )

    main()

    out = capsys.readouterr().out
    assert "No blocked issues to show." in out
    assert "alpha" not in out


def test_batch_dry_run_only_blocked_csv(capsys, monkeypatch, tmp_path):
    """--only-blocked with --output-format csv emits just the aborted rows."""
    import csv as _csv
    import io as _io

    issues = ["alpha", "beta", "gamma"]
    reqs = [_expected_dry_run_requests(issue) for issue in issues]
    window = reqs[0] + reqs[1]
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("\n".join(issues) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--batch-dry-run",
            "--only-blocked",
            "--output-format",
            "csv",
            "--issues-file",
            str(issues_file),
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=0, daily_limit=window),
    )

    main()

    rows = list(_csv.DictReader(_io.StringIO(capsys.readouterr().out)))
    assert [r["issue"] for r in rows] == ["gamma"]
    assert rows[0]["verdict"] == "abort"


def test_batch_dry_run_retry_file_writes_blocked_issues(capsys, monkeypatch, tmp_path):
    """--retry-file writes exactly the aborted issues, one per line."""
    issues = ["alpha", "beta", "gamma"]
    reqs = [_expected_dry_run_requests(issue) for issue in issues]
    window = reqs[0] + reqs[1]  # gamma is blocked
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("\n".join(issues) + "\n", encoding="utf-8")
    retry_path = tmp_path / "retry.txt"
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--batch-dry-run",
            "--issues-file",
            str(issues_file),
            "--retry-file",
            str(retry_path),
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=0, daily_limit=window),
    )

    main()

    capsys.readouterr().out
    assert retry_path.read_text(encoding="utf-8") == "gamma\n"


def test_batch_dry_run_retry_file_empty_when_nothing_blocked(capsys, monkeypatch, tmp_path):
    """Explicit --retry-file still creates the file (empty) with nothing blocked."""
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("alpha\n", encoding="utf-8")
    retry_path = tmp_path / "retry.txt"
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--batch-dry-run",
            "--issues-file",
            str(issues_file),
            "--retry-file",
            str(retry_path),
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage", lambda: _usage_payload()
    )

    main()

    capsys.readouterr().out
    assert retry_path.exists()
    assert retry_path.read_text(encoding="utf-8") == ""


def test_batch_dry_run_retry_file_disabled_with_off(capsys, monkeypatch, tmp_path):
    """--retry-file off skips the write entirely."""
    issues = ["alpha", "beta"]
    reqs = [_expected_dry_run_requests(issue) for issue in issues]
    window = reqs[0] - 1  # even alpha is blocked
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("\n".join(issues) + "\n", encoding="utf-8")
    out_csv = tmp_path / "out.csv"
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--batch-dry-run",
            "--output-format",
            "csv",
            "--output-file",
            str(out_csv),
            "--issues-file",
            str(issues_file),
            "--retry-file",
            "off",
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=0, daily_limit=window),
    )

    main()

    capsys.readouterr().out
    # Explicitly disabled: neither an explicit nor the implicit-derived file.
    assert not out_csv.with_suffix(".retry.txt").exists()


def test_batch_dry_run_implicit_retry_file_next_to_csv(capsys, monkeypatch, tmp_path):
    """CSV output without a flag writes the default retry file next to it."""
    issues = ["alpha", "beta"]
    reqs = [_expected_dry_run_requests(issue) for issue in issues]
    window = reqs[0] - 1
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("\n".join(issues) + "\n", encoding="utf-8")
    out_csv = tmp_path / "plan.csv"
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--batch-dry-run",
            "--output-format",
            "csv",
            "--output-file",
            str(out_csv),
            "--issues-file",
            str(issues_file),
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=0, daily_limit=window),
    )

    main()

    capsys.readouterr().out
    retry_path = out_csv.with_suffix(".retry.txt")
    assert retry_path.exists()
    assert retry_path.read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_batch_dry_run_implicit_csv_skips_retry_when_none_blocked(capsys, monkeypatch, tmp_path):
    """CSV output with nothing blocked leaves no implicit retry file."""
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("alpha\n", encoding="utf-8")
    out_csv = tmp_path / "plan.csv"
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--batch-dry-run",
            "--output-format",
            "csv",
            "--output-file",
            str(out_csv),
            "--issues-file",
            str(issues_file),
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage", lambda: _usage_payload()
    )

    main()

    assert not out_csv.with_suffix(".retry.txt").exists()


def test_batch_dry_run_retry_file_write_failure(capsys, monkeypatch, tmp_path):
    """An unwritable --retry-file path is a usage error."""
    issues = ["alpha"]
    reqs = [_expected_dry_run_requests(issues[0])]
    window = reqs[0] - 1
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("\n".join(issues) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--batch-dry-run",
            "--issues-file",
            str(issues_file),
            "--retry-file",
            str(tmp_path / "no" / "such" / "dir" / "retry.txt"),
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "courtlistener")
    monkeypatch.setattr(
        "va_legal_agent.__main__.fetch_courtlistener_usage",
        lambda: _usage_payload(daily_used=0, daily_limit=window),
    )

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code != 0


def test_batch_dry_run_filters_require_batch_flag(capsys, monkeypatch):
    """--start-at / --only-blocked without --batch-dry-run is a usage error."""
    for extra in (["--only-blocked"], ["--start-at", "2"]):
        monkeypatch.setattr(
            "sys.argv", ["va-legal-agent"] + extra + ["tinnitus"]
        )
        monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code != 0


def test_dry_run_and_batch_dry_run_are_mutually_exclusive(capsys, monkeypatch):
    """--dry-run + --batch-dry-run is a usage error."""
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "--dry-run", "--batch-dry-run", "tinnitus"],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code != 0


def test_batch_dry_run_writes_csv_to_output_file(capsys, monkeypatch, tmp_path):
    """--output-file writes the batch CSV to disk."""
    issues_file = tmp_path / "issues.txt"
    issues_file.write_text("tinnitus\n", encoding="utf-8")
    out_path = tmp_path / "batch.csv"
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--batch-dry-run",
            "--issues-file",
            str(issues_file),
            "--output-format",
            "csv",
            "--output-file",
            str(out_path),
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    main()

    capsys.readouterr().out  # batch table goes to the file, not stdout
    written = out_path.read_text(encoding="utf-8")
    assert "issue" in written
    assert "tinnitus" in written
    assert "unchecked" in written  # no CourtListener configured -> no quota math


def test_dry_run_single_writes_output_file(capsys, monkeypatch, tmp_path):
    """The single --dry-run honors --output-file too."""
    out_path = tmp_path / "dry.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "va-legal-agent",
            "--dry-run",
            "--output-format",
            "json",
            "--output-file",
            str(out_path),
            "tinnitus",
        ],
    )
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckduckgo")

    main()

    capsys.readouterr().out
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["issue"] == "tinnitus"


def test_show_config_prints_settings_without_issue(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["va-legal-agent", "--show-config"])
    monkeypatch.setenv("SEARCH_MAX_WORKERS", "7")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Isolate from any SEARCH_PROVIDERS set in the local .env so the assertion
    # below exercises the true default.
    monkeypatch.delenv("SEARCH_PROVIDERS", raising=False)

    main()

    data = json.loads(capsys.readouterr().out)
    assert data["search_max_workers"] == 7
    assert data["openai_api_key"] is None
    assert data["enrich_case_limit"] == 5
    assert data["max_fetch_bytes"] == 20 * 1024 * 1024
    assert data["effective_search_providers"] == ["duckduckgo"]  # default


def test_show_config_warns_on_unknown_provider(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["va-legal-agent", "--show-config"])
    monkeypatch.setenv("SEARCH_PROVIDERS", "duckdduckgo")

    main()

    captured = capsys.readouterr()
    assert "Unknown search provider" in captured.err
    assert "duckdduckgo" in captured.err
    # stdout still carries the resolved config: the raw (typo'd) value plus the
    # post-validation list that will actually run.
    data = json.loads(captured.out)
    assert data["search_providers"] == "duckdduckgo"
    assert data["effective_search_providers"] == []


def test_show_config_redacts_openai_api_key(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["va-legal-agent", "--show-config"])
    monkeypatch.setenv("OPENAI_API_KEY", "sk-1234567890abcdef")

    main()

    output = capsys.readouterr().out
    assert "sk-1234567890abcdef" not in output
    assert json.loads(output)["openai_api_key"] == "sk-1...cdef"


def test_show_config_redacts_courtlistener_api_key(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["va-legal-agent", "--show-config"])
    monkeypatch.setenv("COURTLISTENER_API_KEY", "cl-secret-token-123456789")

    main()

    output = capsys.readouterr().out
    assert "cl-secret-token-123456789" not in output
    assert json.loads(output)["courtlistener_api_key"] == "cl-s...6789"


def test_redacted_settings_masks_short_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "short")

    assert _redacted_settings()["openai_api_key"] == "***"


def test_mask_proxy_url_masks_embedded_credentials():
    masked = _mask_proxy_url("http://user:pass@residential-proxy.example:8080")
    assert masked == "http://***@residential-proxy.example:8080"
    assert "pass" not in masked


def test_mask_proxy_url_keeps_credential_free_proxy_unchanged():
    value = "http://residential-proxy.example:8080"
    assert _mask_proxy_url(value) == value


def test_mask_proxy_url_masks_user_only_proxy():
    masked = _mask_proxy_url("socks5://alice@proxy.example:1080")
    assert masked == "socks5://***@proxy.example:1080"
    assert "alice" not in masked


def test_mask_proxy_url_masks_proxy_without_port():
    # A credential-bearing proxy with no port exercises the parts.port-is-None
    # branch, where the netloc is rebuilt without a trailing :port.
    masked = _mask_proxy_url("http://user:pass@proxy.example")
    assert masked == "http://***@proxy.example"
    assert "user" not in masked
    assert "pass" not in masked


def test_mask_proxy_url_redacts_unparseable_value():
    assert _mask_proxy_url("http://[invalid") == "***"


def test_mask_proxy_url_rebrackets_ipv6_host():
    masked = _mask_proxy_url("http://user:pass@[::1]:8080")
    assert masked == "http://***@[::1]:8080"


def test_show_config_redacts_proxy_credentials(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["va-legal-agent", "--show-config"])
    monkeypatch.setenv("SEARCH_HTTP_PROXY", "http://richardproxy:supersecret@proxy.example:8080")

    main()

    output = capsys.readouterr().out
    assert "supersecret" not in output
    assert "richardproxy" not in output
    assert json.loads(output)["search_http_proxy"] == "http://***@proxy.example:8080"


def test_missing_issue_is_reported(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["va-legal-agent"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    assert "issue" in capsys.readouterr().err


def test_cli_max_wall_time_flag_flows_to_analysis(capsys, monkeypatch):
    seen: dict[str, object] = {}

    def recording_analyze(issue, **kwargs):
        seen.update(kwargs)
        return _sample_analysis()

    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus", "--max-wall-time", "5"])
    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", recording_analyze)

    main()

    assert seen["max_wall_seconds"] == 5.0


def test_cli_max_wall_time_defaults_to_none(capsys, monkeypatch):
    seen: dict[str, object] = {}

    def recording_analyze(issue, **kwargs):
        seen.update(kwargs)
        return _sample_analysis()

    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus"])
    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", recording_analyze)

    main()

    assert seen["max_wall_seconds"] is None


def test_cli_max_results_defaults_to_env(capsys, monkeypatch):
    """Without --max-results, the SEARCH_MAX_RESULTS env var decides the cap."""
    seen: dict[str, object] = {}
    monkeypatch.setenv("SEARCH_MAX_RESULTS", "25")

    def recording_analyze(issue, **kwargs):
        seen.update(kwargs)
        return _sample_analysis()

    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus"])
    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", recording_analyze)

    main()

    assert seen["max_results"] == 25


def test_cli_max_results_defaults_to_ten_without_env(capsys, monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.delenv("SEARCH_MAX_RESULTS", raising=False)

    def recording_analyze(issue, **kwargs):
        seen.update(kwargs)
        return _sample_analysis()

    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus"])
    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", recording_analyze)

    main()

    assert seen["max_results"] == 10


def test_cli_max_results_flag_overrides_env(capsys, monkeypatch):
    """An explicit --max-results beats the SEARCH_MAX_RESULTS env var."""
    seen: dict[str, object] = {}
    monkeypatch.setenv("SEARCH_MAX_RESULTS", "25")

    def recording_analyze(issue, **kwargs):
        seen.update(kwargs)
        return _sample_analysis()

    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus", "--max-results", "5"])
    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", recording_analyze)

    main()

    assert seen["max_results"] == 5


def test_cli_deep_read_flag_flows_to_analysis(capsys, monkeypatch):
    seen: dict[str, object] = {}

    def recording_analyze(issue, **kwargs):
        seen.update(kwargs)
        return _sample_analysis()

    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus", "--deep-read"])
    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", recording_analyze)

    main()

    assert seen["deep_read"] is True


def test_cli_deep_read_flag_defaults_to_none(capsys, monkeypatch):
    """Without the flag, deep_read is None so the env/setting decides."""
    seen: dict[str, object] = {}

    def recording_analyze(issue, **kwargs):
        seen.update(kwargs)
        return _sample_analysis()

    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus"])
    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", recording_analyze)

    main()

    assert seen["deep_read"] is None


def test_cli_no_deep_read_flag_overrides_env(capsys, monkeypatch):
    """--no-deep-read beats a DEEP_READ=1 env var."""
    seen: dict[str, object] = {}
    monkeypatch.setenv("DEEP_READ", "1")

    def recording_analyze(issue, **kwargs):
        seen.update(kwargs)
        return _sample_analysis()

    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus", "--no-deep-read"])
    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", recording_analyze)

    main()

    assert seen["deep_read"] is False


def test_cli_deep_read_limit_flag_flows_to_analysis(capsys, monkeypatch):
    seen: dict[str, object] = {}

    def recording_analyze(issue, **kwargs):
        seen.update(kwargs)
        return _sample_analysis()

    monkeypatch.setattr(
        "sys.argv", ["va-legal-agent", "tinnitus", "--deep-read", "--deep-read-limit", "5"]
    )
    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", recording_analyze)

    main()

    assert seen["deep_read"] is True
    assert seen["deep_read_limit"] == 5


def test_cli_deep_read_limit_flag_defaults_to_none(capsys, monkeypatch):
    """Without the flag, deep_read_limit is None so the env/setting decides."""
    seen: dict[str, object] = {}

    def recording_analyze(issue, **kwargs):
        seen.update(kwargs)
        return _sample_analysis()

    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus", "--deep-read"])
    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", recording_analyze)

    main()

    assert seen["deep_read"] is True
    assert seen["deep_read_limit"] is None


def test_show_config_includes_deep_read_settings(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["va-legal-agent", "--show-config"])
    monkeypatch.setenv("DEEP_READ", "1")
    monkeypatch.setenv("DEEP_READ_LIMIT", "5")

    main()

    data = json.loads(capsys.readouterr().out)
    assert data["deep_read"] is True
    assert data["deep_read_limit"] == 5
    assert data["deep_read_pages"] == 0
    assert data["deep_chunk_chars"] == 6000


def test_render_analysis_text():
    text = render_analysis(_sample_analysis(), "text")

    assert "Issue: service connection for tinnitus" in text
    assert "- Smith v. Wilkie (Court of Appeals for Veterans Claims)" in text
    assert "How this affects VA claims:" in text
    assert "Coverage score: 1.00" in text
    assert "Interpretation source: template" in text
    # Contradictions surface with both sides named.
    assert (
        "- The decisions split on the nexus standard. "
        "(Smith v. Wilkie vs Jones v. McDonough)" in text
    )


def test_render_analysis_text_deep_summaries():
    analysis = _sample_analysis()
    analysis.deep_summaries = [
        {"case": "Smith v. Wilkie (Court of Appeals for Veterans Claims)", "summary": "The Court holds the Board erred."}
    ]

    text = render_analysis(analysis, "text")

    assert "Deep summaries:" in text
    assert (
        "- Smith v. Wilkie (Court of Appeals for Veterans Claims):\n"
        "The Court holds the Board erred." in text
    )


def test_render_analysis_text_deep_summaries_skips_empty():
    analysis = _sample_analysis()
    analysis.deep_summaries = [
        {"case": "Smith v. Wilkie (Court of Appeals for Veterans Claims)", "summary": "   "},
        {"case": "Jones v. VA (BVA)", "summary": "Board granted the claim."},
    ]

    text = render_analysis(analysis, "text")

    assert "Deep summaries:" in text
    # Only the case with content appears in the Deep summaries block; the
    # whitespace-only summary is omitted there (its title may still appear
    # elsewhere in the report, so scope the assertion to the block).
    deep_block = text.split("Deep summaries:")[1].split("How this affects VA claims:")[0]
    assert "Jones v. VA (BVA):\nBoard granted the claim." in deep_block
    assert "Smith v. Wilkie" not in deep_block


def test_render_analysis_text_no_deep_summaries_block_when_empty():
    text = render_analysis(_sample_analysis(), "text")  # deep_summaries defaults to []

    assert "Deep summaries:" not in text


def test_render_analysis_csv_deep_summaries_column():
    analysis = _sample_analysis()
    analysis.deep_summaries = [
        {"case": "Smith v. Wilkie (Court of Appeals for Veterans Claims)", "summary": "The Court holds the Board erred."}
    ]

    csv_text = render_analysis(analysis, "csv")
    rows = list(csv.reader(io.StringIO(csv_text)))
    header, row = rows[0], rows[1]
    data = dict(zip(header, row))

    assert "deep_summaries" in data
    assert json.loads(data["deep_summaries"]) == analysis.deep_summaries


def test_render_analysis_json_includes_deep_summaries():
    analysis = _sample_analysis()
    analysis.deep_summaries = [
        {"case": "Smith v. Wilkie (Court of Appeals for Veterans Claims)", "summary": "The Court holds the Board erred."}
    ]

    data = json.loads(render_analysis(analysis, "json"))

    assert data["deep_summaries"] == analysis.deep_summaries


def test_render_analysis_csv():
    csv_text = render_analysis(_sample_analysis(), "csv")
    rows = list(csv.reader(io.StringIO(csv_text)))
    header, row = rows[0], rows[1]
    data = dict(zip(header, row))

    assert data["issue"] == "service connection for tinnitus"
    assert data["coverage_score"] == "1.0"
    assert "Smith v. Wilkie" in data["top_cases"]
    assert "service connection [covered by: Smith v. Wilkie]" in data["detected_elements"]
    assert (
        data["contradictions"]
        == "The decisions split on the nexus standard. (Smith v. Wilkie vs Jones v. McDonough)"
    )


def test_render_analysis_json_default():
    analysis = _sample_analysis()

    assert render_analysis(analysis, "json") == analysis.model_dump_json(indent=2)


def test_cli_output_format_text(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus", "--output-format", "text"])
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    main()

    assert "Issue: service connection for tinnitus" in capsys.readouterr().out


def test_cli_output_format_csv(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus", "--output-format", "csv"])
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    main()

    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    assert rows[0][0] == "run_id"
    assert len(rows[1][0]) == 32  # auto-generated run id
    assert rows[1][1] == "service connection for tinnitus"


def test_cli_output_file_writes_analysis(capsys, tmp_path, monkeypatch):
    out_path = tmp_path / "analysis.json"
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--output-file", str(out_path)],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    main()

    assert capsys.readouterr().out == ""  # nothing printed to stdout
    data = json.loads(out_path.read_text())
    assert data["issue"] == "service connection for tinnitus"


def test_cli_output_file_error_is_reported(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--output-file", str(tmp_path / "missing" / "out.json")],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    assert "could not write output file" in capsys.readouterr().err


def _logged_analysis(logger: logging.Logger, level: int) -> None:
    """Mock analyze_cases_for_claim that logs at *level*, then returns a sample."""

    def _analyze(issue, **kwargs):
        logger.log(level, "analysis ran for %s", issue)
        return _sample_analysis()

    return _analyze


def test_cli_debug_log_level_emits_to_stderr_only(capsys, monkeypatch):
    logger = logging.getLogger("va_legal_agent.test_cli")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--log-level", "DEBUG"],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        _logged_analysis(logger, logging.INFO),
    )

    main()

    captured = capsys.readouterr()
    # stdout carries only the JSON analysis.
    json.loads(captured.out)
    assert "analysis ran for tinnitus" not in captured.out
    assert "analysis ran for tinnitus" in captured.err


def test_cli_default_log_level_suppresses_info(capsys, monkeypatch):
    logger = logging.getLogger("va_legal_agent.test_cli")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus"],  # default WARNING
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        _logged_analysis(logger, logging.INFO),
    )

    main()

    captured = capsys.readouterr()
    assert "analysis ran for tinnitus" not in captured.out
    assert "analysis ran for tinnitus" not in captured.err


def test_cli_warning_stays_off_stdout(capsys, monkeypatch):
    logger = logging.getLogger("va_legal_agent.test_cli")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--log-level", "WARNING"],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        _logged_analysis(logger, logging.WARNING),
    )

    main()

    captured = capsys.readouterr()
    json.loads(captured.out)  # stdout is clean JSON
    assert "analysis ran for tinnitus" in captured.err
    assert "analysis ran for tinnitus" not in captured.out


def test_configure_logging_sets_level_and_stderr(capsys):
    root = logging.getLogger()
    old_level, old_handlers = root.level, root.handlers[:]
    try:
        _configure_logging("ERROR")
        logger = logging.getLogger("va_legal_agent.test_configure")

        logger.info("info suppressed")
        logger.error("error shown")

        captured = capsys.readouterr()
        assert "info suppressed" not in captured.out
        assert "info suppressed" not in captured.err
        assert "error shown" in captured.err
        assert "error shown" not in captured.out
    finally:
        root.setLevel(old_level)
        root.handlers[:] = old_handlers


def test_cli_json_log_format_emits_parseable_lines(capsys, monkeypatch):
    logger = logging.getLogger("va_legal_agent.test_cli")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--log-level", "DEBUG", "--log-format", "json"],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        _logged_analysis(logger, logging.INFO),
    )

    main()

    captured = capsys.readouterr()
    json.loads(captured.out)  # stdout is clean JSON analysis
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert lines, "expected at least one log line on stderr"
    for line in lines:
        record = json.loads(line)  # each stderr line is standalone JSON
        assert set(record) >= {"timestamp", "level", "logger", "message"}
    assert any(
        record["message"] == "analysis ran for tinnitus" and record["level"] == "INFO"
        for record in (json.loads(line) for line in lines)
    )


def test_json_formatter_includes_exc_info(capsys):
    root = logging.getLogger()
    old_level, old_handlers = root.level, root.handlers[:]
    try:
        _configure_logging("ERROR", "json")
        logger = logging.getLogger("va_legal_agent.test_configure")

        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed to parse")

        captured = capsys.readouterr()
        record = json.loads(captured.err.strip().splitlines()[-1])
        assert record["message"] == "failed to parse"
        assert "ValueError" in record["exc_info"]
        assert "boom" in record["exc_info"]
    finally:
        root.setLevel(old_level)
        root.handlers[:] = old_handlers


def test_configure_logging_default_is_text(capsys):
    root = logging.getLogger()
    old_level, old_handlers = root.level, root.handlers[:]
    try:
        _configure_logging("WARNING")  # default log_format
        logger = logging.getLogger("va_legal_agent.test_configure")

        logger.warning("plain text warning")

        captured = capsys.readouterr()
        assert "WARNING va_legal_agent.test_configure: plain text warning" in captured.err
        assert not captured.err.startswith("{")
    finally:
        root.setLevel(old_level)
        root.handlers[:] = old_handlers


def test_resolve_log_format_cli_wins_over_env(monkeypatch):
    monkeypatch.setenv("LOG_JSON", "1")

    assert _resolve_log_format("text") == "text"
    assert _resolve_log_format("json") == "json"


def test_resolve_log_format_env_sets_json(monkeypatch):
    monkeypatch.setenv("LOG_JSON", "true")
    assert _resolve_log_format(None) == "json"
    monkeypatch.setenv("LOG_JSON", "1")
    assert _resolve_log_format(None) == "json"


def test_resolve_log_format_defaults_to_text(monkeypatch):
    monkeypatch.delenv("LOG_JSON", raising=False)
    assert _resolve_log_format(None) == "text"
    monkeypatch.setenv("LOG_JSON", "0")
    assert _resolve_log_format(None) == "text"
    monkeypatch.setenv("LOG_JSON", "nonsense")
    assert _resolve_log_format(None) == "text"


def test_cli_log_json_env_var_enables_json(capsys, monkeypatch):
    logger = logging.getLogger("va_legal_agent.test_cli")
    monkeypatch.setenv("LOG_JSON", "1")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--log-level", "DEBUG"],  # no --log-format
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        _logged_analysis(logger, logging.INFO),
    )

    main()

    captured = capsys.readouterr()
    json.loads(captured.out)
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert lines
    for line in lines:
        record = json.loads(line)
        assert set(record) >= {"timestamp", "level", "logger", "message"}
    assert any(
        record["message"] == "analysis ran for tinnitus"
        for record in (json.loads(line) for line in lines)
    )


def test_cli_log_format_flag_overrides_log_json_env(capsys, monkeypatch):
    logger = logging.getLogger("va_legal_agent.test_cli")
    monkeypatch.setenv("LOG_JSON", "1")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--log-level", "WARNING", "--log-format", "text"],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        _logged_analysis(logger, logging.WARNING),
    )

    main()

    captured = capsys.readouterr()
    json.loads(captured.out)
    assert "WARNING va_legal_agent.test_cli: analysis ran for tinnitus" in captured.err
    assert not captured.err.strip().startswith("{")


def test_cli_completion_event_in_json_log(capsys, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--log-format", "json"],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    main()

    captured = capsys.readouterr()
    json.loads(captured.out)  # stdout is clean JSON
    events = [
        json.loads(line)
        for line in captured.err.splitlines()
        if line.strip()
    ]
    completion = [e for e in events if e.get("event") == "analysis_complete"]
    assert len(completion) == 1
    assert completion[0]["coverage_score"] == 1.0
    assert completion[0]["interpretation_source"] == "template"
    assert completion[0]["logger"] == "va_legal_agent.events"
    assert completion[0]["run_id"]  # auto-generated random id
    assert len(completion[0]["run_id"]) == 32  # uuid4().hex


def test_cli_completion_event_emitted_even_at_error_level(capsys, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--log-level", "ERROR", "--log-format", "json"],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    main()

    captured = capsys.readouterr()
    json.loads(captured.out)
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert any(
        json.loads(line).get("event") == "analysis_complete" for line in lines
    )


def test_cli_completion_event_in_text_log(capsys, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus"],  # default text format
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    main()

    captured = capsys.readouterr()
    json.loads(captured.out)
    assert "analysis complete (run=" in captured.err
    assert "coverage=1.00, source=template" in captured.err
    assert "analysis complete" not in captured.out


def test_cli_completion_event_includes_courtlistener_quota(capsys, monkeypatch):
    quota = {
        "used": 8, "limit": 125, "remaining": 117, "reset_at": "2026-08-18T05:12:28+00:00"
    }

    def _analyze(issue, **kwargs):
        analysis = _sample_analysis()
        analysis.courtlistener_quota = quota
        return analysis

    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", _analyze)
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--log-format", "json"],
    )

    main()

    captured = capsys.readouterr()
    json.loads(captured.out)  # stdout stays clean JSON
    events = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    completion = [e for e in events if e.get("event") == "analysis_complete"]
    assert len(completion) == 1
    assert completion[0]["courtlistener_quota"] == quota
    assert (
        "courtlistener=117/125 remaining, resets 2026-08-18T05:12:28+00:00"
        in completion[0]["message"]
    )


def test_cli_completion_event_quota_without_reset_time(capsys, monkeypatch):
    def _analyze(issue, **kwargs):
        analysis = _sample_analysis()
        analysis.courtlistener_quota = {
            "used": 8, "limit": 125, "remaining": 117, "reset_at": None
        }
        return analysis

    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", _analyze)
    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus"])  # text log format

    main()

    captured = capsys.readouterr()
    assert "courtlistener=117/125 remaining, resets unknown" in captured.err


def test_cli_failure_event_in_json_log(capsys, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--log-format", "json"],
    )

    def _boom(issue, **kwargs):
        raise RuntimeError("provider blocked us")

    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        _boom,
    )

    with pytest.raises(RuntimeError, match="provider blocked us"):
        main()

    captured = capsys.readouterr()
    assert captured.out == ""  # nothing written to stdout on failure
    events = [
        json.loads(line)
        for line in captured.err.splitlines()
        if line.strip()
    ]
    failure = [e for e in events if e.get("event") == "analysis_failed"]
    assert len(failure) == 1
    assert failure[0]["issue"] == "tinnitus"
    assert failure[0]["error"] == "provider blocked us"
    assert failure[0]["level"] == "ERROR"
    assert failure[0]["logger"] == "va_legal_agent.events"
    assert len(failure[0]["run_id"]) == 32


def test_cli_failure_event_emitted_even_at_error_level(capsys, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--log-level", "ERROR", "--log-format", "json"],
    )

    def _boom(issue, **kwargs):
        raise ValueError("no cases found")

    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        _boom,
    )

    with pytest.raises(ValueError, match="no cases found"):
        main()

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert any(
        json.loads(line).get("event") == "analysis_failed" for line in lines
    )


def test_cli_failure_event_in_text_log(capsys, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus"],  # default text format
    )

    def _boom(issue, **kwargs):
        raise RuntimeError("search provider down")

    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        _boom,
    )

    with pytest.raises(RuntimeError, match="search provider down"):
        main()

    captured = capsys.readouterr()
    assert "analysis failed for tinnitus (run=" in captured.err
    assert "search provider down" in captured.err
    assert captured.out == ""


def test_run_id_cli_flag_beats_env(monkeypatch):
    monkeypatch.setenv("RUN_ID", "batch-2026-01")
    assert _run_id("--flag-id") == "--flag-id"


def test_run_id_reads_from_env(monkeypatch):
    monkeypatch.setenv("RUN_ID", "batch-2026-01")
    assert _run_id() == "batch-2026-01"


def test_run_id_generates_when_unset(monkeypatch):
    monkeypatch.delenv("RUN_ID", raising=False)
    first, second = _run_id(), _run_id()
    assert len(first) == 32
    assert first != second  # fresh id per call


def test_cli_events_carry_env_run_id(capsys, monkeypatch):
    monkeypatch.setenv("RUN_ID", "batch-42")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--log-format", "json"],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    main()

    captured = capsys.readouterr()
    events = [
        json.loads(line)
        for line in captured.err.splitlines()
        if line.strip()
    ]
    completion = next(e for e in events if e.get("event") == "analysis_complete")
    assert completion["run_id"] == "batch-42"


def test_cli_json_output_contains_run_id(capsys, monkeypatch):
    monkeypatch.setenv("RUN_ID", "batch-42")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus"],  # default json format
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    main()

    data = json.loads(capsys.readouterr().out)
    assert data["run_id"] == "batch-42"


def test_cli_text_output_contains_run_id(capsys, monkeypatch):
    monkeypatch.setenv("RUN_ID", "batch-42")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--output-format", "text"],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    main()

    assert "Run id: batch-42" in capsys.readouterr().out


def test_cli_run_id_flag_beats_env_in_output(capsys, monkeypatch):
    monkeypatch.setenv("RUN_ID", "env-id")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--run-id", "flag-id"],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    main()

    data = json.loads(capsys.readouterr().out)
    assert data["run_id"] == "flag-id"


def test_cli_run_id_flag_shows_in_completion_event(capsys, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--run-id", "cli-9", "--log-format", "json"],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    main()

    captured = capsys.readouterr()
    events = [
        json.loads(line)
        for line in captured.err.splitlines()
        if line.strip()
    ]
    completion = next(e for e in events if e.get("event") == "analysis_complete")
    assert completion["run_id"] == "cli-9"


def test_cli_batch_summary_includes_search_telemetry(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_STATE_DIR", str(tmp_path))

    def _analyze(issue, **kwargs):
        telemetry = kwargs.get("telemetry")
        assert telemetry is not None
        telemetry.append(
            {"provider": "duckduckgo", "queries_issued": 8, "results": 40, "deduped": 5, "failures": 1}
        )
        return _sample_analysis()

    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", _analyze)

    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--run-id", "batch-t", "--batch-size", "1", "--log-format", "json"],
    )
    main()

    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    summary = next(e for e in events if e.get("event") == "batch_summary")
    telemetry = summary["search_telemetry"]
    assert telemetry["duckduckgo"]["queries_issued"] == 8
    assert telemetry["duckduckgo"]["results"] == 40
    assert telemetry["duckduckgo"]["deduped"] == 5
    assert telemetry["duckduckgo"]["failures"] == 1


def test_cli_analysis_json_includes_search_telemetry(capsys, monkeypatch):
    from va_legal_agent.providers import recall_flags, rollup_search_telemetry

    def _analyze(issue, **kwargs):
        telemetry = kwargs.get("telemetry")
        telemetry.append(
            {
                "provider": "duckduckgo", "queries_issued": 8, "results": 40, "deduped": 5, "failures": 1,
                "variants": {'tinnitus "5107"': {"results": 40, "failures": 1}},
            }
        )
        rolled = rollup_search_telemetry(telemetry)
        analysis = _sample_analysis()
        analysis.search_telemetry = rolled
        analysis.search_flags = recall_flags(rolled)
        return analysis

    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", _analyze)
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--run-id", "r1", "--batch-size", "1"],
    )

    main()

    data = json.loads(capsys.readouterr().out)
    assert data["search_telemetry"]["duckduckgo"] == {
        "queries_issued": 8, "results": 40, "deduped": 5, "failures": 1,
        "variants": {'tinnitus "5107"': {"results": 40, "failures": 1}},
    }
    # Low-recall provider flagged in JSON, without needing the text renderer.
    assert data["search_flags"] == [
        "Search provider duckduckgo had 1 failed query attempt(s); results may be incomplete."
    ]


def test_cli_analysis_collects_telemetry_without_batch(capsys, monkeypatch):
    seen: dict[str, object] = {}

    def _analyze(issue, **kwargs):
        seen.update(kwargs)
        return _sample_analysis()

    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", _analyze)
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus"],  # no --batch-size
    )

    main()

    # A single-issue run must still receive a telemetry sink so the output's
    # search_telemetry / search_flags carry the recall picture (not just batch runs).
    assert seen["telemetry"] == []


def test_recall_flags_shared_between_renderer_and_model():
    from va_legal_agent.__main__ import _search_gaps
    from va_legal_agent.providers import recall_flags

    telemetry = {
        "duckduckgo": {"queries_issued": 8, "results": 0, "deduped": 0, "failures": 0},
        "courtlistener": {"queries_issued": 8, "results": 3, "deduped": 1, "failures": 2},
    }

    assert _search_gaps(telemetry) == recall_flags(telemetry)
    assert len(recall_flags(telemetry)) == 2


def test_render_analysis_text_telemetry():
    analysis = _sample_analysis()
    analysis.search_telemetry = {
        "duckduckgo": {"queries_issued": 8, "results": 40, "deduped": 5, "failures": 1}
    }

    text = render_analysis(analysis, "text")

    assert "Search telemetry:" in text
    assert "duckduckgo: 8 queries, 40 results, 5 deduped, 1 failed" in text


def test_render_analysis_text_telemetry_lists_variant_hits():
    analysis = _sample_analysis()
    analysis.search_telemetry = {
        "duckduckgo": {
            "queries_issued": 8, "results": 33, "deduped": 7, "failures": 1,
            "variants": {
                'tinnitus "5107"': {"results": 30, "failures": 1},
                'tinnitus "Compensation"': {"results": 3, "failures": 0},
                "tinnitus": {"results": 0, "failures": 0},
            },
        }
    }

    text = render_analysis(analysis, "text")

    assert 'tinnitus "5107" (30)' in text
    assert 'tinnitus "Compensation" (3)' in text
    assert "tinnitus (0)" not in text  # zero-result variants are not hits


def test_render_analysis_text_flags_low_recall_provider():
    analysis = _sample_analysis()
    analysis.search_telemetry = {
        "duckduckgo": {"queries_issued": 8, "results": 40, "deduped": 5, "failures": 0},
        "courtlistener": {"queries_issued": 8, "results": 0, "deduped": 0, "failures": 0},
    }

    text = render_analysis(analysis, "text")

    # Strength for the productive provider, gap flag for the empty one.
    assert "surfaced 40 results across 8 queries" in text
    assert "returned no results across 8 queries" in text


def test_render_analysis_text_flags_failing_provider():
    analysis = _sample_analysis()
    analysis.search_telemetry = {
        "duckduckgo": {"queries_issued": 8, "results": 10, "deduped": 2, "failures": 6}
    }

    text = render_analysis(analysis, "text")

    assert "had 6 failed query attempt(s)" in text
    assert "surfaced 10 results" in text  # still credited as a strength


def test_render_analysis_text_no_flags_when_telemetry_empty():
    text = render_analysis(_sample_analysis(), "text")

    # No telemetry-derived notes when no search stats were collected.
    assert "surfaced" not in text
    assert "failed query attempt" not in text


def test_render_analysis_csv_telemetry_column():
    analysis = _sample_analysis()
    analysis.search_telemetry = {
        "duckduckgo": {"queries_issued": 8, "results": 40, "deduped": 5, "failures": 1}
    }

    csv_text = render_analysis(analysis, "csv")
    rows = list(csv.reader(io.StringIO(csv_text)))
    header, row = rows[0], rows[1]
    data = dict(zip(header, row))

    assert json.loads(data["search_telemetry"])["duckduckgo"]["queries_issued"] == 8


def test_render_analysis_json_courtlistener_quota():
    analysis = _sample_analysis()
    assert analysis.courtlistener_quota is None  # absent unless the guard recorded one

    analysis.courtlistener_quota = {
        "used": 8, "limit": 125, "remaining": 117, "reset_at": "2026-08-18T05:12:28+00:00"
    }

    data = json.loads(render_analysis(analysis, "json"))

    assert data["courtlistener_quota"] == analysis.courtlistener_quota


def test_render_analysis_text_courtlistener_quota():
    analysis = _sample_analysis()
    assert "CourtListener daily quota" not in render_analysis(analysis, "text")

    analysis.courtlistener_quota = {
        "used": 8, "limit": 125, "remaining": 117, "reset_at": "2026-08-18T05:12:28+00:00"
    }
    text = render_analysis(analysis, "text")
    assert (
        "CourtListener daily quota: 117/125 remaining (used 8; resets 2026-08-18T05:12:28+00:00)."
        in text
    )

    # A missing reset time renders as "unknown" instead of crashing the report.
    analysis.courtlistener_quota = {"used": 8, "limit": 125, "remaining": 117, "reset_at": None}
    assert "resets unknown" in render_analysis(analysis, "text")


def test_render_analysis_csv_courtlistener_quota_column():
    analysis = _sample_analysis()
    analysis.courtlistener_quota = {"used": 8, "limit": 125, "remaining": 117, "reset_at": None}

    csv_text = render_analysis(analysis, "csv")
    rows = list(csv.reader(io.StringIO(csv_text)))
    data = dict(zip(rows[0], rows[1]))

    assert json.loads(data["courtlistener_quota"]) == analysis.courtlistener_quota
    # An absent quota renders as JSON null in the column.
    rows = list(csv.reader(io.StringIO(render_analysis(_sample_analysis(), "csv"))))
    assert dict(zip(rows[0], rows[1]))["courtlistener_quota"] == "null"


def test_cli_without_batch_size_does_not_emit_batch_summary(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_STATE_DIR", str(tmp_path))

    def _analyze(issue, **kwargs):
        assert kwargs.get("telemetry") == []  # telemetry is collected even without batch
        return _sample_analysis()

    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", _analyze)
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--run-id", "solo", "--log-format", "json"],
    )

    main()

    captured = capsys.readouterr()
    assert "batch_summary" not in captured.err


def test_cli_batch_summary_emitted_when_batch_completes(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    # First run: records an outcome, but batch of 2 not reached yet.
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--run-id", "batch-1", "--batch-size", "2", "--log-format", "json"],
    )
    main()
    first = capsys.readouterr()
    assert "batch_summary" not in first.err

    # Second run: batch of 2 reached, summary emitted and state cleaned up.
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "hearing loss", "--run-id", "batch-1", "--batch-size", "2", "--log-format", "json"],
    )
    main()
    second = capsys.readouterr()
    events = [json.loads(line) for line in second.err.splitlines() if line.strip()]
    summary = next(e for e in events if e.get("event") == "batch_summary")
    assert summary["run_id"] == "batch-1"
    assert summary["total"] == 2
    assert summary["completed"] == 2
    assert summary["failed"] == 0
    assert summary["coverage_mean"] == 1.0
    assert summary["coverage_min"] == 1.0
    assert summary["coverage_max"] == 1.0
    assert not list(tmp_path.glob("batch-1.jsonl"))  # state file removed


def test_cli_batch_summary_counts_failures(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--run-id", "batch-2", "--batch-size", "2", "--log-format", "json"],
    )
    main()
    capsys.readouterr()  # swallow first run

    def _boom(issue, **kwargs):
        raise RuntimeError("rate limited")

    monkeypatch.setattr("va_legal_agent.__main__.analyze_cases_for_claim", _boom)
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "hearing loss", "--run-id", "batch-2", "--batch-size", "2", "--log-format", "json"],
    )
    with pytest.raises(RuntimeError, match="rate limited"):
        main()

    second = capsys.readouterr()
    events = [json.loads(line) for line in second.err.splitlines() if line.strip()]
    summary = next(e for e in events if e.get("event") == "batch_summary")
    assert summary["total"] == 2
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["failed_issues"] == ["hearing loss"]
    assert not list(tmp_path.glob("batch-2.jsonl"))


def test_cli_without_batch_size_does_not_track(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--run-id", "solo", "--log-format", "json"],
    )

    main()

    captured = capsys.readouterr()
    assert "batch_summary" not in captured.err
    assert not list(tmp_path.iterdir())  # no state files created


def test_cli_csv_output_contains_run_id(capsys, monkeypatch):
    monkeypatch.setenv("RUN_ID", "batch-42")
    monkeypatch.setattr(
        "sys.argv",
        ["va-legal-agent", "tinnitus", "--output-format", "csv"],
    )
    monkeypatch.setattr(
        "va_legal_agent.__main__.analyze_cases_for_claim",
        lambda issue, **kwargs: _sample_analysis(),
    )

    main()

    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    data = dict(zip(rows[0], rows[1]))
    assert data["run_id"] == "batch-42"


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
def test_cli_main_guard_runs_end_to_end(monkeypatch, capsys):
    """Re-execute __main__ as a script, exercising the CLI entry guard."""
    import runpy

    analysis = _sample_analysis()
    monkeypatch.setattr(
        "va_legal_agent.agent.analyze_cases_for_claim",
        lambda issue, **kwargs: analysis,
    )
    monkeypatch.setattr("sys.argv", ["va-legal-agent", "tinnitus"])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    runpy.run_module("va_legal_agent.__main__", run_name="__main__")

    out = capsys.readouterr().out
    assert "service connection for tinnitus" in out


def test_matrix_rendering_in_text_output():
    """The matrix header and rows render as aligned columns in text output."""
    from va_legal_agent.__main__ import _analysis_to_text, _matrix_header
    from va_legal_agent.models import StatuteOutcomeRow

    analysis = _sample_analysis()
    analysis.statute_outcome_matrix = [
        StatuteOutcomeRow(
            statute="38 U.S.C. § 5107(b)",
            court="Court of Appeals for Veterans Claims",
            favorable=3,
            unfavorable=1,
        ),
    ]

    text = _analysis_to_text(analysis)

    assert "Statute-outcome matrix:" in text
    assert "38 U.S.C. § 5107(b)" in text
    assert "Court of Appeals for Veterans Claims" in text
    assert "3" in text
    assert "1" in text
    # The header line is present.
    assert _matrix_header() in text
