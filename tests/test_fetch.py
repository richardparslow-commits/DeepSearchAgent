"""Tests for case-detail extraction (va_legal_agent.fetch)."""

import pytest
import requests

from va_legal_agent.fetch import (
    FetchError,
    extract_citation,
    extract_decision_date,
    fetch_case_details,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("See Fountain v. McDonald, 23 Vet.App. 1 (2011).", "23 Vet.App. 1"),
        ("Citing Smith v. Wilkie, 912 F.3d 1306 (Fed. Cir. 2019).", "912 F.3d 1306"),
        ("The court decided 590 F.4th 1300 last term.", "590 F.4th 1300"),
        ("Argued in 500 U.S. 123 and related orders.", "500 U.S. 123"),
        ("Westlaw cite 2011 WL 123456 applies.", "2011 WL 123456"),
        ("Citation Nr: 2100634, Board decision.", "BVA Citation Nr. 2100634"),
        ("No reporters here, only statutes like 38 U.S.C. 1110.", ""),
    ],
)
def test_extract_citation(text, expected):
    assert extract_citation(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Decided: March 15, 2011. The Court holds...", "2011-03-15"),
        ("Filed January 3, 2024 by the Clerk.", "2024-01-03"),
        ("Argument was held on Feb 9, 2020 in this matter.", "2020-02-09"),
        ("No dates appear in this text.", ""),
    ],
)
def test_extract_decision_date(text, expected):
    assert extract_decision_date(text) == expected


def test_extract_decision_date_prefers_hinted_date():
    text = "Received October 1, 2019. Decided: November 5, 2020. Issued to the parties."
    assert extract_decision_date(text) == "2020-11-05"


class FakePageResponse:
    def __init__(self, text="", content=b"", status_code=200, headers=None):
        self.text = text
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        return None


HTML_PAGE = """
<html><head>
<title>Fountain v. McDonald, 23 Vet.App. 1 (2011)</title>
<meta name="description" content="The Board must provide adequate reasons and bases.">
</head><body>
<h1>Fountain v. McDonald</h1>
<p>Decided: March 15, 2011. Cite as 23 Vet.App. 1.</p>
</body></html>
"""


def test_fetch_case_details_from_html(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.fetch.requests.get",
        lambda url, headers=None, timeout=None: FakePageResponse(text=HTML_PAGE, headers={"Content-Type": "text/html"}),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/fountain.html")

    assert details["citation"] == "23 Vet.App. 1"
    assert details["decision_date"] == "2011-03-15"
    assert details["holding"] == "The Board must provide adequate reasons and bases."


def test_fetch_case_details_handles_unparseable_pdf(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.fetch.requests.get",
        lambda url, headers=None, timeout=None: FakePageResponse(
            content=b"%PDF-1.4 broken bytes", headers={"Content-Type": "application/pdf"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/documents/case.pdf")

    assert details == {"citation": "", "decision_date": "", "holding": ""}


def test_fetch_case_details_raises_fetch_error_on_network_failure(monkeypatch):
    def failing_get(url, headers=None, timeout=None):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr("va_legal_agent.fetch.requests.get", failing_get)

    with pytest.raises(FetchError, match="Failed to fetch"):
        fetch_case_details("https://uscourts.cavc.gov/unreachable")