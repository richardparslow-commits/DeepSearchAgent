"""Tests for the command-line entry point (va_legal_agent.__main__)."""

import csv
import io
import json
import logging

import pytest

from va_legal_agent.__main__ import (
    _configure_logging,
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
