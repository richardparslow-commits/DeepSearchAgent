"""Tests for deep-read mode (va_legal_agent.deep_read)."""

from va_legal_agent.deep_read import (
    _EMPTY_DIGEST,
    _build_reduce_prompt,
    _digest_chunk,
    _synthesize_case,
    chunk_text,
    deep_read_case,
    deep_read_cases,
)
from va_legal_agent.fetch import FetchError
from va_legal_agent.models import CaseRecord


def _case(**overrides) -> CaseRecord:
    defaults = dict(
        title="Fountain v. McDonald",
        court="CAVC",
        url="https://uscourts.cavc.gov/fountain",
    )
    defaults.update(overrides)
    return CaseRecord(**defaults)


def test_chunk_text_empty_returns_no_chunks():
    assert chunk_text("", 100) == []
    assert chunk_text("   \n\n  ", 100) == []


def test_chunk_text_packs_paragraphs_greedily():
    text = "AAA\n\nBBB\n\nCCC"
    chunks = chunk_text(text, 100)

    # All three short paragraphs fit in one chunk.
    assert chunks == ["AAA\n\nBBB\n\nCCC"]


def test_chunk_text_splits_on_paragraph_boundary():
    text = "A" * 40 + "\n\n" + "B" * 40
    chunks = chunk_text(text, 50)

    assert chunks == ["A" * 40, "B" * 40]


def test_chunk_text_hard_splits_single_long_paragraph():
    text = "X" * 120
    chunks = chunk_text(text, 50)

    assert chunks == ["X" * 50, "X" * 50, "X" * 20]


def test_chunk_text_long_paragraph_starts_fresh_chunk():
    # A long paragraph empties the current chunk before being hard-split.
    text = "AAA\n\n" + "X" * 120
    chunks = chunk_text(text, 50)

    assert chunks[0] == "AAA"
    assert chunks[1:] == ["X" * 50, "X" * 50, "X" * 20]


def test_chunk_text_paragraph_exactly_at_cap_does_not_emit_blank_chunk():
    # A paragraph exactly max_chars long must not produce an empty chunk that
    # would later digest to "(nothing relevant)".
    assert chunk_text("X" * 50, 50) == ["X" * 50]


def test_chunk_text_pack_boundary_plus_two():
    # Packing uses len(current) + len(para) + 2 <= max: a pair that overflows
    # by exactly 1 with +2 must not pack (the +2 mutant would pack it).
    assert chunk_text("AAAA\n\nBBBB", 9) == ["AAAA", "BBBB"]


def test_chunk_text_pack_boundary_equality():
    # Exactly len(current) + len(para) + 2 == max packs (the < mutant would not).
    assert chunk_text("AAA\n\nBBB", 8) == ["AAA\n\nBBB"]


def test_chunk_text_long_paragraph_then_short_paragraph():
    # After a hard-split the current chunk is reset; a following short
    # paragraph must start a fresh chunk (the None-reset mutant would crash).
    text = "X" * 120 + "\n\n" + "BBB"
    chunks = chunk_text(text, 50)

    assert chunks == ["X" * 50, "X" * 50, "X" * 20, "BBB"]


def test_chunk_text_reset_after_current_then_long_then_short():
    # A non-empty current chunk, then a long paragraph that hard-splits AND
    # resets current, then another paragraph: the reset must yield a real
    # string so the next pack works (the None-reset mutant would crash).
    text = "AAA\n\n" + "X" * 120 + "\n\n" + "BBB"
    chunks = chunk_text(text, 50)

    assert chunks == ["AAA", "X" * 50, "X" * 50, "X" * 20, "BBB"]


def test_digest_chunk_extracts_holding_outcome_statutes():
    chunk = (
        "The Court holds that the Board erred in weighing the evidence. "
        "The decision is vacated and remanded. See 38 U.S.C. § 5107 and "
        "38 C.F.R. § 3.303."
    )

    digest = _digest_chunk(chunk)

    # Pinned exactly: the join separator and field order are the contract.
    assert digest == (
        "Holdings: The Court holds that the Board erred in weighing the evidence.; "
        "Outcome: vacated and remanded; "
        "Statutes: 38 U.S.C. § 5107, 38 C.F.R. § 3.303"
    )


def test_digest_chunk_returns_empty_sentinel_on_no_salient_content():
    assert _digest_chunk("Procedural history only, no holdings or outcomes here.") == _EMPTY_DIGEST


def test_build_reduce_prompt_system_is_exact():
    messages = _build_reduce_prompt("Fountain v. McDonald", "tinnitus", ["digest one"])

    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == (
        "You are summarizing a U.S. veterans-law decision for legal research. "
        "The section digests below were extracted from one decision. Synthesize "
        "them into a single coherent summary (under 200 words) of what this "
        "decision holds for the issue, preserving the holding, outcome, and "
        "cited statutes. Cite the decision by name. Do not invent facts."
    )


def test_build_reduce_prompt_user_is_exact():
    messages = _build_reduce_prompt("Fountain v. McDonald", "tinnitus", ["digest one", "digest two"])

    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == (
        "Decision: Fountain v. McDonald\nIssue: tinnitus\n\nSection digests:\n"
        "- digest one\n- digest two\n\nReturn only the summary."
    )


def test_synthesize_case_empty_digests_returns_empty(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _synthesize_case("Fountain v. McDonald", "tinnitus", []) == ""


def test_synthesize_case_joins_digests_without_llm(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = _synthesize_case("Fountain v. McDonald", "tinnitus", ["digest one", "digest two"])

    assert result == "- digest one\n- digest two"


def test_synthesize_case_uses_llm_summary_when_enabled(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: dict[str, object] = {}

    def recording_call(messages):
        captured["messages"] = messages
        return "  The deep summary.  "

    monkeypatch.setattr("va_legal_agent.deep_read.call_openai", recording_call)

    result = _synthesize_case("Fountain v. McDonald", "tinnitus", ["digest one"])

    assert result == "The deep summary."
    # The reduce prompt (not a placeholder) reaches the client, carrying the
    # case title and issue (the None-arg mutants would drop them).
    assert captured["messages"] == _build_reduce_prompt(
        "Fountain v. McDonald", "tinnitus", ["digest one"]
    )
    assert "Fountain v. McDonald" in captured["messages"][1]["content"]
    assert "tinnitus" in captured["messages"][1]["content"]


def test_synthesize_case_falls_back_on_llm_failure(monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def failing_call(messages):
        raise RuntimeError("API timeout")

    monkeypatch.setattr("va_legal_agent.deep_read.call_openai", failing_call)

    result = _synthesize_case("Fountain v. McDonald", "tinnitus", ["digest one"])

    assert result == "- digest one"
    # The warning names the failure verbatim (message prefix, exception text,
    # and no stray XX decorations) so the fallback is traceable.
    assert any(
        r.getMessage().startswith("Deep-read synthesis failed")
        and "API timeout" in r.getMessage()
        for r in caplog.records
    )


def test_synthesize_case_falls_back_on_empty_llm_summary(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("va_legal_agent.deep_read.call_openai", lambda messages: "   ")

    result = _synthesize_case("Fountain v. McDonald", "tinnitus", ["digest one"])

    assert result == "- digest one"


def test_deep_read_case_uses_courtlistener_api_text(monkeypatch):
    """A CourtListener case fetches its body from the detail endpoint, not the WAF-blocked URL."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fetched_urls: list[str] = []
    seen_ids: list[int] = []

    class _FakeProvider:
        def fetch_opinion_text(self, opinion_id):
            seen_ids.append(opinion_id)
            return "The Court holds that the Board erred in weighing the evidence."

    def recording_url_fetch(url, max_pages=0):
        fetched_urls.append(url)
        return "fallback body"

    monkeypatch.setattr(
        "va_legal_agent.deep_read.CourtListenerProvider", lambda: _FakeProvider()
    )
    monkeypatch.setattr("va_legal_agent.deep_read.fetch_full_text", recording_url_fetch)

    case = _case(
        url="https://www.courtlistener.com/opinion/12345/x/",
        courtlistener_opinion_id="12345",
    )

    result = deep_read_case(case, "tinnitus")

    assert "Holdings: The Court holds that the Board erred" in result
    assert fetched_urls == []  # the WAF-blocked URL was never scraped
    assert seen_ids == [12345]  # the string id is coerced to int for the API


def test_deep_read_case_falls_back_to_url_when_api_text_empty(monkeypatch):
    """Empty API text falls through to the generic URL fetch."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fetched_urls: list[str] = []

    class _FakeProvider:
        def fetch_opinion_text(self, opinion_id):
            return "   "

    def recording_url_fetch(url, max_pages=0):
        fetched_urls.append(url)
        return "The Court holds that the Board erred in weighing the evidence."

    monkeypatch.setattr(
        "va_legal_agent.deep_read.CourtListenerProvider", lambda: _FakeProvider()
    )
    monkeypatch.setattr("va_legal_agent.deep_read.fetch_full_text", recording_url_fetch)

    case = _case(url="https://www.courtlistener.com/opinion/12345/x/", courtlistener_opinion_id="12345")

    result = deep_read_case(case, "tinnitus")

    assert "Holdings: The Court holds that the Board erred" in result
    assert fetched_urls == [case.url]


def test_deep_read_case_falls_back_to_url_on_api_error(monkeypatch, caplog):
    """An API error degrades to the URL fetch rather than failing the case."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fetched_urls: list[str] = []

    class _FakeProvider:
        def fetch_opinion_text(self, opinion_id):
            raise ValueError("bad id")

    def recording_url_fetch(url, max_pages=0):
        fetched_urls.append(url)
        return "The Court holds that the Board erred in weighing the evidence."

    monkeypatch.setattr(
        "va_legal_agent.deep_read.CourtListenerProvider", lambda: _FakeProvider()
    )
    monkeypatch.setattr("va_legal_agent.deep_read.fetch_full_text", recording_url_fetch)

    case = _case(url="https://www.courtlistener.com/opinion/12345/x/", courtlistener_opinion_id="12345")

    result = deep_read_case(case, "tinnitus")

    assert "Holdings: The Court holds that the Board erred" in result
    assert fetched_urls == [case.url]
    assert any(
        r.getMessage().startswith("CourtListener opinion text fetch failed")
        and "bad id" in r.getMessage()
        and case.url in r.getMessage()
        for r in caplog.records
    )


def test_deep_read_case_returns_empty_on_fetch_failure(monkeypatch, caplog):
    def failing_fetch(url, max_pages=0):
        raise FetchError("Failed to fetch")

    monkeypatch.setattr("va_legal_agent.deep_read.fetch_full_text", failing_fetch)

    assert deep_read_case(_case(), "tinnitus") == ""
    # The warning names the URL and error verbatim (prefix + both args).
    assert any(
        r.getMessage().startswith("Deep-read fetch failed")
        and "https://uscourts.cavc.gov/fountain" in r.getMessage()
        and "Failed to fetch" in r.getMessage()
        for r in caplog.records
    )


def test_deep_read_case_returns_empty_on_blank_body(monkeypatch):
    monkeypatch.setattr("va_legal_agent.deep_read.fetch_full_text", lambda url, max_pages=0: "   ")
    assert deep_read_case(_case(), "tinnitus") == ""


def test_deep_read_case_synthesizes_full_text(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = (
        "The Court holds that the Board erred in weighing the evidence.\n\n"
        "The decision is vacated and remanded. See 38 U.S.C. § 5107."
    )
    monkeypatch.setattr("va_legal_agent.deep_read.fetch_full_text", lambda url, max_pages=0: body)

    result = deep_read_case(_case(), "tinnitus")

    assert "Holdings: The Court holds that the Board erred" in result
    assert "Outcome: vacated and remanded" in result


def test_deep_read_case_synthesis_carries_title_and_issue(monkeypatch):
    """The LLM reduce prompt receives the case title and issue (None mutants)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: dict[str, object] = {}

    def recording_call(messages):
        captured["messages"] = messages
        return "summary"

    monkeypatch.setattr("va_legal_agent.deep_read.call_openai", recording_call)
    body = "The Court holds that the Board erred in weighing the evidence."
    monkeypatch.setattr("va_legal_agent.deep_read.fetch_full_text", lambda url, max_pages=0: body)

    deep_read_case(_case(), "tinnitus")

    assert "Fountain v. McDonald" in captured["messages"][1]["content"]
    assert "tinnitus" in captured["messages"][1]["content"]


def test_deep_read_case_passes_deep_read_pages_setting(monkeypatch):
    monkeypatch.setenv("DEEP_READ_PAGES", "7")
    seen: dict[str, object] = {}

    def recording_fetch(url, max_pages=0):
        seen["max_pages"] = max_pages
        return "The Court holds that the Board erred in weighing the evidence."

    monkeypatch.setattr("va_legal_agent.deep_read.fetch_full_text", recording_fetch)

    deep_read_case(_case(), "tinnitus")

    assert seen["max_pages"] == 7


def test_deep_read_cases_sets_summary_in_place(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = "The Court holds that the Board erred in weighing the evidence."
    monkeypatch.setattr("va_legal_agent.deep_read.fetch_full_text", lambda url, max_pages=0: body)

    cases = [_case(title="A v. VA"), _case(title="B v. VA")]

    deep_read_cases(cases, "tinnitus")

    assert all(case.deep_summary for case in cases)
    assert "Holdings: The Court holds that the Board erred" in cases[0].deep_summary


def test_deep_read_cases_forwards_issue_to_deep_read_case(monkeypatch):
    """The issue is threaded to each per-case deep read (the None-issue mutant)."""
    issues: list[str] = []

    def recording_deep_read_case(case, issue):
        issues.append(issue)
        return "summary"

    monkeypatch.setattr("va_legal_agent.deep_read.deep_read_case", recording_deep_read_case)

    deep_read_cases([_case(title="A v. VA"), _case(title="B v. VA")], "tinnitus")

    assert issues == ["tinnitus", "tinnitus"]


def test_deep_read_cases_respects_limit(monkeypatch):
    monkeypatch.setenv("DEEP_READ_LIMIT", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "va_legal_agent.deep_read.fetch_full_text",
        lambda url, max_pages=0: "The Court holds that the Board erred.",
    )

    cases = [_case(title="A v. VA"), _case(title="B v. VA")]

    deep_read_cases(cases, "tinnitus")

    assert cases[0].deep_summary
    assert cases[1].deep_summary == ""


def test_deep_read_cases_skips_cases_without_url(monkeypatch):
    fetched: list[str] = []

    def recording_fetch(url, max_pages=0):
        fetched.append(url)
        return "The Court holds that the Board erred."

    monkeypatch.setattr("va_legal_agent.deep_read.fetch_full_text", recording_fetch)

    cases = [_case(title="No URL", url=""), _case(title="Has URL")]

    deep_read_cases(cases, "tinnitus", limit=2)

    assert fetched == ["https://uscourts.cavc.gov/fountain"]
    assert cases[0].deep_summary == ""
    assert cases[1].deep_summary


def test_deep_read_cases_keeps_empty_summary_on_fetch_failure(monkeypatch):
    def failing_fetch(url, max_pages=0):
        raise FetchError("Failed to fetch")

    monkeypatch.setattr("va_legal_agent.deep_read.fetch_full_text", failing_fetch)

    cases = [_case(title="A v. VA")]

    deep_read_cases(cases, "tinnitus")

    assert cases[0].deep_summary == ""
