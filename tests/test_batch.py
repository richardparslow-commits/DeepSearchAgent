"""Tests for per-run_id batch tracking (va_legal_agent.batch)."""

import threading

import pytest

from va_legal_agent.batch import BatchTracker, batch_state_dir


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_STATE_DIR", str(tmp_path))
    return tmp_path


def test_batch_state_dir_reads_env(state_dir, monkeypatch):
    assert batch_state_dir() == state_dir


def test_batch_state_dir_defaults_to_temp(monkeypatch):
    monkeypatch.delenv("BATCH_STATE_DIR", raising=False)
    assert str(batch_state_dir()).endswith("va_legal_agent_batches")


def test_record_and_outcomes(state_dir):
    tracker = BatchTracker("batch-1")
    tracker.record({"event": "analysis_complete", "issue": "tinnitus", "coverage_score": 0.8})
    tracker.record({"event": "analysis_complete", "issue": "hearing loss", "coverage_score": 0.9})

    outcomes = tracker.outcomes()
    assert len(outcomes) == 2
    assert outcomes[0]["coverage_score"] == 0.8


def test_summary_aggregates_completions(state_dir):
    tracker = BatchTracker("batch-1")
    tracker.record({"event": "analysis_complete", "issue": "a", "coverage_score": 0.6})
    tracker.record({"event": "analysis_complete", "issue": "b", "coverage_score": 1.0})
    tracker.record({"event": "analysis_failed", "issue": "c", "error": "rate limited"})

    summary = tracker.summary()
    assert summary["total"] == 3
    assert summary["completed"] == 2
    assert summary["failed"] == 1
    assert summary["coverage_mean"] == 0.8
    assert summary["coverage_min"] == 0.6
    assert summary["coverage_max"] == 1.0
    assert summary["failed_issues"] == ["c"]


def test_summary_empty(state_dir):
    summary = BatchTracker("fresh").summary()
    assert summary["total"] == 0
    assert summary["completed"] == 0
    assert summary["failed"] == 0
    assert summary["coverage_mean"] is None
    assert summary["failed_issues"] == []


def test_concurrent_writers_lose_no_outcomes(state_dir):
    """Concurrent trackers sharing a run_id must not lose or corrupt records."""
    tracker = BatchTracker("shared-run")

    def writer(tag: str) -> None:
        for i in range(30):
            BatchTracker("shared-run").record(
                {
                    "event": "analysis_complete",
                    "issue": f"{tag}-{i}",
                    "coverage_score": 0.5,
                }
            )

    threads = [threading.Thread(target=writer, args=(tag,)) for tag in ("a", "b", "c")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    outcomes = tracker.outcomes()
    issues = [str(o["issue"]) for o in outcomes]
    assert len(outcomes) == 90  # all three writers' records survived
    assert len(set(issues)) == 90  # none lost, none duplicated


def test_outcomes_skips_blank_lines(state_dir):
    tracker = BatchTracker("batch-1")
    tracker.record({"event": "analysis_complete", "issue": "a", "coverage_score": 0.5})
    with tracker.path.open("a") as handle:
        handle.write("\n\n")
    tracker.record({"event": "analysis_complete", "issue": "b", "coverage_score": 0.7})

    assert len(tracker.outcomes()) == 2


def test_outcomes_warns_when_state_unreadable(state_dir, caplog):
    tracker = BatchTracker("batch-1")
    tracker.path.mkdir()  # a directory can't be read as a state file

    assert tracker.outcomes() == []
    assert any("Could not read batch state" in r.message for r in caplog.records)


def test_finalize_warns_when_unlink_fails(state_dir, caplog):
    tracker = BatchTracker("batch-1")
    tracker.path.mkdir()

    summary = tracker.finalize()

    assert summary["total"] == 0
    assert any("Could not remove batch state" in r.message for r in caplog.records)


def test_summary_skips_malformed_lines(state_dir):
    tracker = BatchTracker("batch-1")
    tracker.record({"event": "analysis_complete", "issue": "a", "coverage_score": 0.5})
    with tracker.path.open("a") as handle:
        handle.write("not json\n")
    tracker.record({"event": "analysis_complete", "issue": "b", "coverage_score": 0.7})

    summary = tracker.summary()
    assert summary["total"] == 2
    assert summary["completed"] == 2
    assert summary["coverage_mean"] == 0.6


def test_summary_rolls_up_search_telemetry(state_dir):
    tracker = BatchTracker("batch-1")
    tracker.record(
        {
            "event": "analysis_complete",
            "issue": "a",
            "coverage_score": 0.8,
            "search_telemetry": [
                {
                    "provider": "duckduckgo", "queries_issued": 8, "results": 40, "deduped": 5, "failures": 0,
                    "variants": {
                        'tinnitus "5107"': {"results": 30, "failures": 0},
                        'tinnitus "Compensation"': {"results": 10, "failures": 0},
                    },
                },
                {"provider": "courtlistener", "queries_issued": 8, "results": 12, "deduped": 0, "failures": 1},
            ],
        }
    )
    tracker.record(
        {
            "event": "analysis_failed",
            "issue": "b",
            "error": "provider down",
            "search_telemetry": [
                {
                    "provider": "duckduckgo", "queries_issued": 8, "results": 20, "deduped": 2, "failures": 3,
                    "variants": {'tinnitus "5107"': {"results": 20, "failures": 3}},
                },
            ],
        }
    )

    summary = tracker.summary()
    ddg = summary["search_telemetry"]["duckduckgo"]
    assert ddg == {
        "queries_issued": 16, "results": 60, "deduped": 7, "failures": 3,
        "variants": {
            'tinnitus "5107"': {"results": 50, "failures": 3},
            'tinnitus "Compensation"': {"results": 10, "failures": 0},
        },
    }
    cl = summary["search_telemetry"]["courtlistener"]
    assert cl == {"queries_issued": 8, "results": 12, "deduped": 0, "failures": 1, "variants": {}}


def test_summary_search_telemetry_empty_when_none_recorded(state_dir):
    tracker = BatchTracker("batch-1")
    tracker.record({"event": "analysis_complete", "issue": "a", "coverage_score": 0.5})

    assert tracker.summary()["search_telemetry"] == {}


def test_finalize_removes_state_file(state_dir):
    tracker = BatchTracker("batch-1")
    tracker.record({"event": "analysis_complete", "issue": "a", "coverage_score": 0.5})
    assert tracker.path.exists()

    summary = tracker.finalize()
    assert summary["completed"] == 1
    assert not tracker.path.exists()
    assert not tracker.outcomes()  # re-created empty afterwards


def test_sanitizes_run_id_in_filename(state_dir):
    tracker = BatchTracker("batch/../evil")
    # Path separators are stripped, so the id can't escape the state dir.
    assert "/" not in tracker.path.name
    assert tracker.path.parent == state_dir
    assert tracker.path.name.endswith(".jsonl")


def test_sanitized_run_id_filename_is_exact(state_dir):
    tracker = BatchTracker("run-1")
    assert tracker.run_id == "run-1"
    assert tracker.path.name == "run-1.jsonl"
    # An id with nothing valid falls back to a safe literal.
    assert BatchTracker("!!!").path.name == "run.jsonl"


def test_record_creates_missing_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_STATE_DIR", str(tmp_path / "nested" / "state"))

    tracker = BatchTracker("batch-1")
    tracker.record({"event": "analysis_complete", "issue": "a", "coverage_score": 0.5})

    assert len(tracker.outcomes()) == 1


def test_summary_run_id_key_and_coverage_mean_rounding(state_dir):
    tracker = BatchTracker("batch-1")
    tracker.record({"event": "analysis_complete", "issue": "a", "coverage_score": 0.123456})

    summary = tracker.summary()

    assert summary["run_id"] == "batch-1"
    assert summary["coverage_mean"] == 0.1235  # rounded to 4 decimals


def test_summary_failed_issue_defaults_to_empty(state_dir):
    tracker = BatchTracker("batch-1")
    tracker.record({"event": "analysis_failed", "error": "boom"})

    assert tracker.summary()["failed_issues"] == [""]


def test_summary_telemetry_skips_non_list_entries(state_dir):
    tracker = BatchTracker("batch-1")
    tracker.record(
        {"event": "analysis_complete", "issue": "a", "coverage_score": 0.5, "search_telemetry": "oops"}
    )
    tracker.record(
        {
            "event": "analysis_complete",
            "issue": "b",
            "coverage_score": 0.5,
            "search_telemetry": [
                {"provider": "duckduckgo", "queries_issued": 2, "results": 4, "deduped": 0, "failures": 0}
            ],
        }
    )

    summary = tracker.summary()
    assert summary["search_telemetry"]["duckduckgo"]["queries_issued"] == 2


def test_outcomes_when_state_file_missing(state_dir):
    assert BatchTracker("nope").outcomes() == []
