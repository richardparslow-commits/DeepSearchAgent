"""Focused tests for advisory CourtListener case metadata.

The fields added here are deliberately optional and best-effort: search
results carry panel names, argument dates, and docket ids when CourtListener
provides them, while a recording link requires one extra audio lookup during
top-case enrichment. None of the fields may affect ranking or authority.
"""

from __future__ import annotations

import requests

from va_legal_agent.agent import enrich_top_cases, normalize_case, score_case_relevance
from va_legal_agent.models import CaseRecord
from va_legal_agent.providers import CourtListenerProvider
from va_legal_agent.topics import COURT_CAVC


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else ""
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


def _search_item(**overrides):
    item = {
        "caseName": "Smith v. McDonough",
        "absolute_url": "/opinion/12345/smith-v-mcdonough/",
        "court_id": "cavc",
        "dateFiled": "2023-05-01",
        "docketNumber": "19-4433",
        "docket_id": 4214664,
        "judge": "Judge Mary J. Smith",
        "panel_names": ["Judge Mary J. Smith", "Judge Robert A. Lee"],
        "dateArgued": "2023-01-10",
        "citation": ["35 Vet.App. 123"],
        "opinions": [
            {
                "id": 12345,
                "snippet": "The court holds that the Board provided inadequate reasons.",
            }
        ],
    }
    item.update(overrides)
    return item


def _opinion_detail(**overrides):
    item = {
        "absolute_url": "/opinion/12345/smith-v-mcdonough/",
        "cluster": "https://www.courtlistener.com/api/rest/v4/clusters/98765/",
        "type": "020lead",
    }
    item.update(overrides)
    return item


def _cluster_detail(**overrides):
    item = {
        "id": 98765,
        "case_name": "Smith v. McDonough",
        "citations": [{"cite": "35 Vet.App. 123"}],
        "sub_opinions": [
            "https://www.courtlistener.com/api/rest/v4/opinions/12345/"
        ],
        "date_filed": "2023-05-01",
        "judges": "Judge Mary J. Smith",
        "panel": [
            "Judge Mary J. Smith",
            "https://www.courtlistener.com/api/rest/v4/people/77/",
            "Judge Robert A. Lee",
        ],
        "precedential_status": "Published",
        "docket": "https://www.courtlistener.com/api/rest/v4/dockets/55555/",
    }
    item.update(overrides)
    return item


def _docket_detail(**overrides):
    item = {
        "id": 55555,
        "docket_number": "19-4433",
        "court_id": "cavc",
        "date_argued": "2023-01-10",
        "absolute_url": "/docket/55555/smith-v-mcdonough/",
    }
    item.update(overrides)
    return item


def test_courtlistener_search_carries_panel_docket_and_argument_date(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda *args, **kwargs: FakeResponse({"results": [_search_item()]}),
    )

    result = CourtListenerProvider().search("service connection", max_results=5)[0]

    assert result["judge"] == "Judge Mary J. Smith"
    assert result["panel"] == "Judge Mary J. Smith; Judge Robert A. Lee"
    assert result["recap_docket_id"] == "4214664"
    assert result["recap_docket_url"] == (
        "https://www.courtlistener.com/docket/4214664/smith-v-mcdonough/"
    )
    assert result["oral_argument_date"] == "2023-01-10"
    assert result["oral_argument_url"] == ""


def test_courtlistener_search_leaves_optional_metadata_empty(monkeypatch):
    monkeypatch.setenv("SEARCH_RETRY_ATTEMPTS", "0")
    item = _search_item(
        panel_names=[],
        docket_id=None,
        dateArgued=None,
    )
    monkeypatch.setattr(
        "va_legal_agent.providers.requests.get",
        lambda *args, **kwargs: FakeResponse({"results": [item]}),
    )

    result = CourtListenerProvider().search("service connection", max_results=5)[0]

    assert result["panel"] == ""
    assert result["recap_docket_id"] == ""
    assert result["recap_docket_url"] == ""
    assert result["oral_argument_date"] == ""


def test_courtlistener_opinion_metadata_carries_panel_and_public_docket(monkeypatch):
    captured: list[str] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.append(url)
        if url == CourtListenerProvider.API_URL + "12345/":
            return FakeResponse(_opinion_detail())
        if url == "https://www.courtlistener.com/api/rest/v4/clusters/98765/":
            return FakeResponse(_cluster_detail())
        if url == "https://www.courtlistener.com/api/rest/v4/dockets/55555/":
            return FakeResponse(_docket_detail())
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    result = CourtListenerProvider()._get_opinion(12345)

    # Panel names are kept, people-endpoint links are not rendered as names,
    # and no extra audio request is made by the metadata chain itself.
    assert result["panel"] == "Judge Mary J. Smith; Judge Robert A. Lee"
    assert result["recap_docket_id"] == "55555"
    assert result["recap_docket_url"] == (
        "https://www.courtlistener.com/docket/55555/smith-v-mcdonough/"
    )
    assert result["oral_argument_date"] == "2023-01-10"
    assert captured == [
        CourtListenerProvider.API_URL + "12345/",
        "https://www.courtlistener.com/api/rest/v4/clusters/98765/",
        "https://www.courtlistener.com/api/rest/v4/dockets/55555/",
    ]


def test_courtlistener_opinion_metadata_builds_docket_url_without_absolute_url(
    monkeypatch,
):
    def fake_get(url, params=None, headers=None, timeout=None):
        if url == CourtListenerProvider.API_URL + "12345/":
            return FakeResponse(_opinion_detail())
        if url == "https://www.courtlistener.com/api/rest/v4/clusters/98765/":
            return FakeResponse(_cluster_detail())
        if url == "https://www.courtlistener.com/api/rest/v4/dockets/55555/":
            return FakeResponse(_docket_detail(absolute_url=""))
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    result = CourtListenerProvider()._get_opinion(12345)

    assert result["recap_docket_id"] == "55555"
    assert result["recap_docket_url"] == (
        "https://www.courtlistener.com/docket/55555/smith-v-mcdonough/"
    )


def test_courtlistener_fetch_oral_argument_returns_public_recording(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResponse(
            {
                "results": [
                    {
                        "absolute_url": "/audio/777/smith-v-mcdonough/",
                        "date_argued": "2023-01-10",
                    }
                ]
            }
        )

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)

    result = CourtListenerProvider().fetch_oral_argument("4214664")

    assert result == {
        "oral_argument_url": (
            "https://www.courtlistener.com/audio/777/smith-v-mcdonough/"
        ),
        "oral_argument_date": "2023-01-10",
    }
    assert captured["url"] == CourtListenerProvider.AUDIO_URL
    assert captured["params"]["docket"] == 4214664
    assert captured["params"]["order_by"] == "-date_created"


def test_courtlistener_fetch_oral_argument_handles_invalid_and_empty_dockets(
    monkeypatch,
):
    calls: list[str] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return FakeResponse({"results": []})

    monkeypatch.setattr("va_legal_agent.providers.requests.get", fake_get)
    provider = CourtListenerProvider()

    assert provider.fetch_oral_argument("not-a-docket") == {}
    assert calls == []
    assert provider.fetch_oral_argument("4214664") == {}


def test_enrich_courtlistener_oral_argument_lookup_is_opt_in(monkeypatch):
    case = CaseRecord(
        title="Smith v. McDonough",
        court="Court of Appeals for Veterans Claims",
        url="https://www.courtlistener.com/opinion/12345/smith-v-mcdonough/",
        courtlistener_opinion_id="12345",
        recap_docket_id="4214664",
    )
    asked: list[str] = []

    class FakeProvider:
        def fetch_opinion_text(self, opinion_id):
            return "The Court holds that the Board failed to provide adequate reasons."

        def fetch_oral_argument(self, docket_id):
            asked.append(docket_id)
            raise AssertionError("lookup must stay off by default")

    monkeypatch.setattr(
        "va_legal_agent.agent.CourtListenerProvider", lambda: FakeProvider()
    )

    enrich_top_cases([case], limit=1)

    assert asked == []
    assert case.holding
    assert case.oral_argument_url == ""


def test_enrich_courtlistener_attaches_optional_oral_argument_link(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_ORAL_ARGUMENT_LOOKUP", "1")
    case = CaseRecord(
        title="Smith v. McDonough",
        court="Court of Appeals for Veterans Claims",
        url="https://www.courtlistener.com/opinion/12345/smith-v-mcdonough/",
        courtlistener_opinion_id="12345",
        panel="Judge Mary J. Smith; Judge Robert A. Lee",
        recap_docket_id="4214664",
        recap_docket_url=(
            "https://www.courtlistener.com/docket/4214664/smith-v-mcdonough/"
        ),
        oral_argument_date="2023-01-10",
    )
    asked: list[str] = []

    class FakeProvider:
        def fetch_opinion_text(self, opinion_id):
            return "The Court holds that the Board failed to provide adequate reasons."

        def fetch_oral_argument(self, docket_id):
            asked.append(docket_id)
            return {
                "oral_argument_url": (
                    "https://www.courtlistener.com/audio/777/smith-v-mcdonough/"
                ),
                "oral_argument_date": "2023-01-10",
            }

    monkeypatch.setattr(
        "va_legal_agent.agent.CourtListenerProvider", lambda: FakeProvider()
    )

    enrich_top_cases([case], limit=1)

    assert asked == ["4214664"]
    assert case.oral_argument_url == (
        "https://www.courtlistener.com/audio/777/smith-v-mcdonough/"
    )
    assert case.oral_argument_date == "2023-01-10"
    assert case.panel == "Judge Mary J. Smith; Judge Robert A. Lee"
    assert case.recap_docket_url == (
        "https://www.courtlistener.com/docket/4214664/smith-v-mcdonough/"
    )
    assert case.holding  # text enrichment still succeeded


def test_enrich_courtlistener_oral_argument_failure_is_advisory(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_ORAL_ARGUMENT_LOOKUP", "1")
    case = CaseRecord(
        title="Smith v. McDonough",
        court="Court of Appeals for Veterans Claims",
        url="https://www.courtlistener.com/opinion/12345/smith-v-mcdonough/",
        courtlistener_opinion_id="12345",
        recap_docket_id="4214664",
        oral_argument_date="2023-01-10",
    )

    class FakeProvider:
        def fetch_opinion_text(self, opinion_id):
            return "The Court holds that the Board failed to provide adequate reasons."

        def fetch_oral_argument(self, docket_id):
            raise RuntimeError("audio endpoint unavailable")

    monkeypatch.setattr(
        "va_legal_agent.agent.CourtListenerProvider", lambda: FakeProvider()
    )

    enrich_top_cases([case], limit=1)

    assert case.holding  # the advisory failure did not discard text enrichment
    assert case.oral_argument_url == ""
    assert case.oral_argument_date == "2023-01-10"  # search metadata retained


def test_normalize_case_carries_optional_courtlistener_metadata():
    case = normalize_case(
        {
            "title": "Smith v. McDonough",
            "url": "https://www.courtlistener.com/opinion/12345/smith-v-mcdonough/",
            "panel": "Judge Mary J. Smith; Judge Robert A. Lee",
            "recap_docket_url": (
                "https://www.courtlistener.com/docket/4214664/smith-v-mcdonough/"
            ),
            "recap_docket_id": "4214664",
            "oral_argument_url": (
                "https://www.courtlistener.com/audio/777/smith-v-mcdonough/"
            ),
            "oral_argument_date": "2023-01-10",
        },
        COURT_CAVC,
        "service connection",
    )

    assert case.panel == "Judge Mary J. Smith; Judge Robert A. Lee"
    assert case.recap_docket_id == "4214664"
    assert case.recap_docket_url == (
        "https://www.courtlistener.com/docket/4214664/smith-v-mcdonough/"
    )
    assert case.oral_argument_url == (
        "https://www.courtlistener.com/audio/777/smith-v-mcdonough/"
    )
    assert case.oral_argument_date == "2023-01-10"


def test_optional_courtlistener_metadata_does_not_change_relevance():
    base = CaseRecord(
        title="Smith v. McDonough",
        court=COURT_CAVC,
        snippet="Service connection requires a nexus.",
    )
    decorated = CaseRecord(
        title="Smith v. McDonough",
        court=COURT_CAVC,
        snippet="Service connection requires a nexus.",
        panel="Judge Mary J. Smith; Judge Robert A. Lee",
        recap_docket_id="4214664",
        recap_docket_url=(
            "https://www.courtlistener.com/docket/4214664/smith-v-mcdonough/"
        ),
        oral_argument_url=(
            "https://www.courtlistener.com/audio/777/smith-v-mcdonough/"
        ),
        oral_argument_date="2023-01-10",
    )

    assert score_case_relevance(base, "service connection") == score_case_relevance(
        decorated, "service connection"
    )
