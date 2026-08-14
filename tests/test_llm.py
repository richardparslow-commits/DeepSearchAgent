"""Tests for the optional LLM interpretation layer (va_legal_agent.llm)."""

import pytest

from va_legal_agent.llm import _build_messages, interpret_cases
from va_legal_agent.models import CaseRecord


def _case() -> CaseRecord:
    return CaseRecord(
        title="Fountain v. McDonald",
        court="Court of Appeals for Veterans Claims",
        citation="23 Vet.App. 1",
        url="https://uscourts.cavc.gov/fountain",
        snippet="Service connection requires competent evidence.",
        holding="The Board must provide reasons and bases.",
    )


def test_interpret_returns_none_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert interpret_cases("tinnitus", "Compensation", [_case()]) is None


def test_interpret_returns_none_for_empty_cases(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert interpret_cases("tinnitus", "Compensation", []) is None


def test_interpret_returns_llm_text(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("va_legal_agent.llm._call_openai", lambda messages: "Fountain requires reasons and bases.")

    result = interpret_cases("tinnitus", "Compensation", [_case()])

    assert result == "Fountain requires reasons and bases."


def test_interpret_falls_back_on_api_failure(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def failing_call(messages):
        raise RuntimeError("API timeout")

    monkeypatch.setattr("va_legal_agent.llm._call_openai", failing_call)

    assert interpret_cases("tinnitus", "Compensation", [_case()]) is None


def test_build_messages_include_case_details():
    messages = _build_messages("service connection for tinnitus", "Compensation", [_case()])

    assert messages[0]["role"] == "system"
    user_text = messages[1]["content"]
    assert "Fountain v. McDonald" in user_text
    assert "[23 Vet.App. 1]" in user_text
    assert "reasons and bases" in user_text