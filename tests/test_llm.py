"""Tests for the optional LLM interpretation layer (va_legal_agent.llm)."""

import json
import sys
import types

import pytest

from va_legal_agent.llm import (
    _build_messages,
    _build_reasoning_messages,
    _parse_reasoning_response,
    interpret_cases,
    reason_cases,
)
from va_legal_agent.models import CaseRecord, CitationTreatment


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
    """Point the ``openai`` import at a fake client so call_openai's real body runs."""
    fake = _FakeOpenAI(**kwargs)
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=fake))
    return fake


def test_interpret_returns_none_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    called: list[object] = []

    def should_not_run(messages):
        called.append(messages)
        return "must never be reached"

    monkeypatch.setattr("va_legal_agent.llm.call_openai", should_not_run)

    assert interpret_cases("tinnitus", "Compensation", [_case()]) is None
    # A missing key short-circuits before any LLM call: pin that call_openai is
    # never invoked (otherwise the guard's `or` could become `and` and the
    # function would fall through to a real client call with no key).
    assert called == []


def test_interpret_returns_none_for_empty_cases(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert interpret_cases("tinnitus", "Compensation", []) is None


def test_interpret_returns_llm_text(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("va_legal_agent.llm.call_openai", lambda messages: "Fountain requires reasons and bases.")

    result = interpret_cases("tinnitus", "Compensation", [_case()])

    assert result == "Fountain requires reasons and bases."


def test_interpret_falls_back_on_api_failure(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def failing_call(messages):
        raise RuntimeError("API timeout")

    monkeypatch.setattr("va_legal_agent.llm.call_openai", failing_call)

    assert interpret_cases("tinnitus", "Compensation", [_case()]) is None


def test_call_openai_success_with_real_client_body(monkeypatch):
    """Exercise call_openai's real body: client construction + completion request."""
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

    monkeypatch.setattr("va_legal_agent.llm.call_openai", recording_call)

    interpret_cases("tinnitus", "Compensation", [_case()])

    assert "Benefit type: Compensation" in captured["messages"][1]["content"]


def test_build_reasoning_messages_include_every_case_and_json_instructions():
    messages = _build_reasoning_messages("service connection", "Compensation", [_case()], 700)

    assert messages[0]["role"] == "system"
    assert "reconciled_principles" in messages[0]["content"]
    assert "contradictions" in messages[0]["content"]
    assert "case_a" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    user_text = messages[1]["content"]
    assert "Fountain v. McDonald" in user_text
    assert "[23 Vet.App. 1]" in user_text
    assert "The Board must provide reasons and bases." in user_text  # holding
    assert "Benefit type: Compensation" in user_text
    assert "Return only the JSON object" in user_text


def test_build_reasoning_messages_include_outcome_and_statutes():
    case = _case()
    case.outcome = "Granted"
    case.statutes = ["38 U.S.C. 5107"]

    messages = _build_reasoning_messages("tinnitus", "Compensation", [case], 700)
    user_text = messages[1]["content"]

    assert "Outcome: Granted" in user_text
    assert "Statutes: 38 U.S.C. 5107" in user_text


def test_build_reasoning_messages_include_legal_standard_when_present():
    """Legal standard is surfaced in the reasoning prompt when extracted."""
    case = _case()
    case.legal_standard = "clear error"

    messages = _build_reasoning_messages("tinnitus", "Compensation", [case], 700)
    user_text = messages[1]["content"]

    assert "Standard of review: clear error" in user_text


def test_build_reasoning_messages_omit_legal_standard_when_empty():
    """Empty legal_standard is omitted from the prompt (no empty line)."""
    case = _case()  # legal_standard defaults to ""

    messages = _build_reasoning_messages("tinnitus", "Compensation", [case], 700)
    user_text = messages[1]["content"]

    assert "Standard of review" not in user_text


def test_build_reasoning_messages_include_citation_treatments_when_present():
    """Citation treatments are surfaced in the reasoning prompt."""
    case = _case()
    case.citation_treatments = [
        CitationTreatment(cited_case="Smith v. Wilkie", treatment="distinguished"),
        CitationTreatment(cited_case="Jones v. Brown", treatment="overruled"),
    ]

    messages = _build_reasoning_messages("tinnitus", "Compensation", [case], 700)
    user_text = messages[1]["content"]

    assert "Treatment of prior authority:" in user_text
    assert "Smith v. Wilkie: distinguished" in user_text
    assert "Jones v. Brown: overruled" in user_text


def test_build_reasoning_messages_omit_citation_treatments_when_empty():
    """Empty treatments are omitted."""
    case = _case()  # citation_treatments defaults to []

    messages = _build_reasoning_messages("tinnitus", "Compensation", [case], 700)
    user_text = messages[1]["content"]

    assert "Treatment of prior authority" not in user_text


def test_build_reasoning_messages_flags_non_precedential():
    """Non-precedential decisions are flagged in the reasoning prompt."""
    case = _case()
    case.precedential = False

    messages = _build_reasoning_messages("tinnitus", "Compensation", [case], 700)
    user_text = messages[1]["content"]

    assert "non-precedential" in user_text
    assert "persuasive authority only" in user_text


def test_build_reasoning_messages_no_flag_when_precedential():
    """Precedential decisions (the default) get no flag."""
    case = _case()  # precedential=True by default

    messages = _build_reasoning_messages("tinnitus", "Compensation", [case], 700)
    user_text = messages[1]["content"]

    assert "non-precedential" not in user_text
    assert "persuasive authority only" not in user_text


def test_build_reasoning_messages_prefers_deep_summary():
    """A deep-read summary replaces the holding/snippet in the reasoning prompt."""
    case = _case()
    case.deep_summary = "Deep full-text summary of the decision."

    messages = _build_reasoning_messages("tinnitus", "Compensation", [case], 700)
    user_text = messages[1]["content"]

    assert "Holding: Deep full-text summary of the decision." in user_text
    assert "reasons and bases" not in user_text  # the snippet/holding are shadowed


def test_build_reasoning_messages_falls_back_without_deep_summary():
    """Without a deep summary the holding, then the snippet, is used."""
    case = _case()
    case.holding = ""

    messages = _build_reasoning_messages("tinnitus", "Compensation", [case], 700)
    user_text = messages[1]["content"]

    assert "Holding: Service connection requires competent evidence." in user_text  # snippet


def test_build_reasoning_messages_number_cases_and_budget_words():
    first, second = _case(), _case()
    second.title = "Second v. VA"

    messages = _build_reasoning_messages("tinnitus", "Compensation", [first, second], 700)
    user_text = messages[1]["content"]

    assert "1. Fountain v. McDonald" in user_text
    assert "2. Second v. VA" in user_text
    assert "under 175 words" in user_text  # max_tokens // 4


def test_build_reasoning_messages_system_prompt_is_exact():
    messages = _build_reasoning_messages("tinnitus", "Compensation", [_case()], 700)

    # Pinned verbatim: the prompt is the contract for the LLM's JSON output.
    assert messages[0]["content"] == (
        "You are a meticulous U.S. veterans-law research analyst. Given the claim issue "
        "and the retrieved decisions, reconcile the holdings into coherent guidance. "
        "Respond with ONLY a JSON object with exactly these keys:\n"
        '{"reconciled_principles": ["..."], "contradictions": [{"statement": "...", '
        '"case_a": "...", "case_b": "..."}], "synthesis": "..."}\n'
        "Rules:\n"
        "- reconciled_principles: legal principles that hold across the cases, each a "
        "complete sentence that cites the supporting case(s) inline, e.g. 'Service "
        "connection requires a nexus opinion (Buchanan v. Nicholson).'\n"
        "- contradictions: genuine conflicts between two decisions on the same point "
        "(differing holdings or outcomes). Use the exact case titles for case_a and "
        "case_b. Empty list if the cases agree.\n"
        "- synthesis: a paragraph under 250 words reconciling the holdings into guidance "
        "for the claim, citing the case(s) behind each claim you make.\n"
        "Do not invent cases, holdings, citations, or statutes. Do not make any claim "
        "the listed cases do not support."
    )


def test_build_reasoning_messages_user_message_is_exact():
    """The user message pins the case-line formatting for every optional field."""
    full = CaseRecord(
        title="Alpha v. VA",
        court="CAVC",
        citation="1 Vet.App. 1",
        decision_date="2020-01-01",
        holding="Alpha holding.",
        outcome="Granted",
        statutes=["38 U.S.C. 5107", "38 C.F.R. 4.1"],
    )
    bare = CaseRecord(title="Beta v. VA", court="CAVC")  # every optional field empty

    messages = _build_reasoning_messages("tinnitus", "Compensation", [full, bare], 700)

    assert messages[1]["content"] == (
        "Claim issue: tinnitus\nBenefit type: Compensation\n\nCases:\n"
        "1. Alpha v. VA (CAVC) [1 Vet.App. 1] -- decided 2020-01-01\n"
        "   Holding: Alpha holding.\n"
        "   Outcome: Granted\n"
        "   Statutes: 38 U.S.C. 5107, 38 C.F.R. 4.1\n\n"
        "2. Beta v. VA (CAVC)\n"
        "   Holding: (no holding extracted)\n\n"
        "Keep the synthesis under 175 words. Return only the JSON object."
    )


def test_build_reasoning_messages_word_budget_has_a_floor():
    # A tiny token budget still yields a readable word cap (max(max_tokens // 4, 50)).
    messages = _build_reasoning_messages("tinnitus", "Compensation", [_case()], 100)

    assert "under 50 words" in messages[1]["content"]


def test_parse_reasoning_response_parses_full_json():
    text = json.dumps(
        {
            "reconciled_principles": ["Nexus required (Fountain v. McDonald)."],
            "contradictions": [
                {"statement": "Split on the standard.", "case_a": "A v. VA", "case_b": "B v. VA"}
            ],
            "synthesis": "A controls.",
        }
    )

    result = _parse_reasoning_response(text)

    assert result is not None
    assert result.reconciled_principles == ["Nexus required (Fountain v. McDonald)."]
    assert result.synthesis == "A controls."
    assert len(result.contradictions) == 1
    assert result.contradictions[0].statement == "Split on the standard."
    assert result.contradictions[0].case_a == "A v. VA"
    assert result.contradictions[0].case_b == "B v. VA"

def test_parse_reasoning_response_handles_code_fences_and_prose():
    payload = '{"synthesis": "A controls."}'

    fenced = f'```json\n{payload}\n```'
    assert _parse_reasoning_response(fenced).synthesis == "A controls."

    # A fence without the ``json`` language tag still parses.
    plain_fenced = f'```\n{payload}\n```'
    assert _parse_reasoning_response(plain_fenced).synthesis == "A controls."

    prose = f'Here is the result:\n{payload}\nHope that helps.'
    assert _parse_reasoning_response(prose).synthesis == "A controls."

    # Braces that enclose broken JSON (not just trailing prose) yield None.
    assert _parse_reasoning_response("prefix {oops not json} suffix") is None


def test_parse_reasoning_response_skips_malformed_contradictions():
    text = json.dumps(
        {
            "contradictions": [
                "not-a-dict",  # a non-dict item must not stop later items
                {"statement": "ok", "case_a": "A", "case_b": "B"},
                {"statement": "missing sides"},  # no case_a/case_b -> skipped
                {"statement": "", "case_a": "", "case_b": ""},  # empty -> skipped
                {"case_a": "A", "case_b": "B"},  # no statement -> skipped
                {"statement": "s", "case_b": "B"},  # no case_a -> skipped
                {"statement": "s", "case_a": "A"},  # no case_b -> skipped
                {"case_b": "only-b"},  # nothing but one side -> skipped
            ]
        }
    )

    result = _parse_reasoning_response(text)

    assert result is not None
    assert len(result.contradictions) == 1  # only the fully-populated one survives
    assert result.contradictions[0].case_a == "A"
    assert result.contradictions[0].case_b == "B"


def test_parse_reasoning_response_skips_non_string_principles():
    text = json.dumps({"reconciled_principles": ["A real principle.", 5, {"x": 1}]})

    result = _parse_reasoning_response(text)

    assert result.reconciled_principles == ["A real principle."]  # non-str items dropped


def test_parse_reasoning_response_block_extraction_edge_cases():
    # Trailing prose after a leading brace: the first-to-last brace block is used.
    assert _parse_reasoning_response('{"synthesis": "A"} trailing').synthesis == "A"
    # A non-whitespace char right after the closing brace must not break the block.
    assert _parse_reasoning_response('prefix {"synthesis": "A"}x').synthesis == "A"
    # Nested braces: the block spans to the *last* closing brace.
    assert _parse_reasoning_response(
        'prefix {"synthesis": "A", "nested": {"b": 1}}'
    ).synthesis == "A"
    # Two separate objects: only the first-to-last span is considered; if it is
    # not valid JSON the whole thing is rejected (the earlier object wins).
    assert _parse_reasoning_response(
        'junk {"synthesis": "A"} trailing {"synthesis": "B"}'
    ) is None


def test_parse_reasoning_response_returns_none_on_garbage():
    assert _parse_reasoning_response("") is None
    assert _parse_reasoning_response("   ") is None
    assert _parse_reasoning_response("not json at all") is None
    assert _parse_reasoning_response("[1, 2, 3]") is None  # not a dict
    assert _parse_reasoning_response('{"unrelated": true}') is None  # no usable keys


def test_reason_cases_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    called: list[object] = []

    def should_not_run(messages):
        called.append(messages)
        return "never"

    monkeypatch.setattr("va_legal_agent.llm.call_openai", should_not_run)

    assert reason_cases("tinnitus", "Compensation", [_case()]) is None
    assert called == []


def test_reason_cases_returns_none_for_empty_cases(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    assert reason_cases("tinnitus", "Compensation", []) is None


def test_reason_cases_disabled_by_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_REASONING", "0")
    called: list[object] = []

    def should_not_run(messages):
        called.append(messages)
        return "never"

    monkeypatch.setattr("va_legal_agent.llm.call_openai", should_not_run)

    assert reason_cases("tinnitus", "Compensation", [_case()]) is None
    assert called == []


def test_reason_cases_returns_parsed_result(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    payload = json.dumps({"reconciled_principles": ["P"], "synthesis": "S"})
    captured: dict[str, object] = {}

    def recording_call(messages):
        captured["messages"] = messages
        return payload

    monkeypatch.setattr("va_legal_agent.llm.call_openai", recording_call)

    result = reason_cases("tinnitus", "Compensation", [_case()])

    assert result is not None
    assert result.reconciled_principles == ["P"]
    assert result.synthesis == "S"
    # The reasoning prompt (not a placeholder) is what reaches the client, and
    # the issue/claim type are threaded through to it.
    assert "reconciled_principles" in captured["messages"][0]["content"]
    assert "tinnitus" in captured["messages"][1]["content"]
    assert "Compensation" in captured["messages"][1]["content"]


def test_reason_cases_falls_back_on_api_failure(monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def failing_call(messages):
        raise RuntimeError("API timeout")

    monkeypatch.setattr("va_legal_agent.llm.call_openai", failing_call)

    assert reason_cases("tinnitus", "Compensation", [_case()]) is None
    assert any(
        r.getMessage() == "LLM reasoning failed; using template synthesis: API timeout"
        for r in caplog.records
    )


def test_reason_cases_returns_none_on_empty_response(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("va_legal_agent.llm.call_openai", lambda messages: "")

    assert reason_cases("tinnitus", "Compensation", [_case()]) is None


def test_reason_cases_handles_missing_openai_package(monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", None)  # openai not importable

    assert reason_cases("tinnitus", "Compensation", [_case()]) is None
    assert any(
        r.getMessage()
        == "OPENAI_API_KEY is set but the 'openai' package is not installed "
        "(pip install openai). Skipping LLM reasoning."
        for r in caplog.records
    )
