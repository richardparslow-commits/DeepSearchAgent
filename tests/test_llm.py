"""Tests for the optional LLM interpretation layer (va_legal_agent.llm)."""

import sys
import types

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


class _FakeOpenAI:
    """Stand-in for the openai.OpenAI class: callable, with chat/completions."""

    def __init__(self, content="", create_raises=None, captured=None):
        self.content = content
        self.create_raises = create_raises
        self.captured = captured
        self.client_kwargs = {}

    def __call__(self, **kwargs):
        self.client_kwargs.update(kwargs)
        return self

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        if self.captured is not None:
            self.captured.update(kwargs)
        if self.create_raises is not None:
            raise self.create_raises
        message = types.SimpleNamespace(content=self.content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


def _install_fake_openai(monkeypatch, **kwargs) -> _FakeOpenAI:
    """Point the ``openai`` import at a fake client so _call_openai's real body runs."""
    fake = _FakeOpenAI(**kwargs)
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=fake))
    return fake


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


def test_call_openai_success_with_real_client_body(monkeypatch):
    """Exercise _call_openai's real body: client construction + completion request."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example.com/v1")
    captured: dict[str, object] = {}
    fake = _install_fake_openai(
        monkeypatch, content="Fountain requires reasons and bases.", captured=captured
    )

    result = interpret_cases("tinnitus", "Compensation", [_case()])

    assert result == "Fountain requires reasons and bases."
    # Settings are wired into the client and the completion request.
    assert fake.client_kwargs["api_key"] == "test-key"
    assert fake.client_kwargs["base_url"] == "https://llm.example.com/v1"
    assert captured["model"] == "gpt-4o-mini"
    assert captured["timeout"] == 60.0
    assert captured["max_tokens"] == 700
    assert "tinnitus" in captured["messages"][1]["content"]


def test_call_openai_failure_falls_back_to_template(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _install_fake_openai(monkeypatch, create_raises=RuntimeError("API timeout"))

    assert interpret_cases("tinnitus", "Compensation", [_case()]) is None


@pytest.mark.parametrize("content", ["", None])
def test_call_openai_empty_response_returns_none(monkeypatch, content):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _install_fake_openai(monkeypatch, content=content)

    assert interpret_cases("tinnitus", "Compensation", [_case()]) is None


def test_interpret_handles_missing_openai_package(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", None)  # openai not importable

    assert interpret_cases("tinnitus", "Compensation", [_case()]) is None


def test_build_messages_include_case_details():
    messages = _build_messages("service connection for tinnitus", "Compensation", [_case()])

    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == (
        "You are a careful U.S. veterans-law research assistant. Explain how the listed "
        "decisions affect VA compensation claims on the given issue. Refer to the specific "
        "cases, stay under 200 words, and do not invent holdings or citations."
    )
    assert messages[1]["role"] == "user"
    user_text = messages[1]["content"]
    assert "Fountain v. McDonald" in user_text
    assert "[23 Vet.App. 1]" in user_text
    assert "reasons and bases" in user_text
    assert "Benefit type: Compensation" in user_text


def test_build_messages_join_cases_with_newlines():
    second = _case()

    messages = _build_messages("tinnitus", "Compensation", [_case(), second])

    user_text = messages[1]["content"]
    assert (
        "Fountain v. McDonald (Court of Appeals for Veterans Claims) [23 Vet.App. 1]: "
        "The Board must provide reasons and bases." in user_text
    )
    assert "XX" not in user_text  # case lines joined with plain newlines


def test_build_messages_without_citation_has_no_placeholder():
    case = _case()
    case.citation = ""

    messages = _build_messages("tinnitus", "Compensation", [case])

    user_text = messages[1]["content"]
    assert (
        "Fountain v. McDonald (Court of Appeals for Veterans Claims): "
        "The Board must provide reasons and bases." in user_text
    )
    assert "XXXX" not in user_text


def test_interpret_cases_passes_claim_type_through(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: dict[str, object] = {}

    def recording_call(messages):
        captured["messages"] = messages
        return "ok"

    monkeypatch.setattr("va_legal_agent.llm._call_openai", recording_call)

    interpret_cases("tinnitus", "Compensation", [_case()])

    assert "Benefit type: Compensation" in captured["messages"][1]["content"]
