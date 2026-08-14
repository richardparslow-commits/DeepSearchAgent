"""Tests for case-detail extraction (va_legal_agent.fetch)."""

import pytest
import requests

from va_legal_agent.fetch import (
    FetchError,
    extract_citation,
    extract_decision_date,
    extract_docket,
    extract_holding_sentence,
    extract_judge,
    extract_outcome,
    extract_statutes,
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

    assert details == {
        "citation": "",
        "decision_date": "",
        "holding": "",
        "docket": "",
        "judge": "",
        "statutes": [],
        "outcome": "",
    }


def test_fetch_case_details_raises_fetch_error_on_network_failure(monkeypatch):
    def failing_get(url, headers=None, timeout=None):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr("va_legal_agent.fetch.requests.get", failing_get)

    with pytest.raises(FetchError, match="Failed to fetch"):
        fetch_case_details("https://uscourts.cavc.gov/unreachable")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Docket No. 12-34 567 was assigned by the Board.", "12-34 567"),
        ("Appeal No. 23-10567 is pending.", "23-10567"),
        ("Case No. 2023-1234, Fed. Cir.", "2023-1234"),
        ("No. 19-2233 (order issued).", "19-2233"),
        ("The docket is not mentioned here.", ""),
    ],
)
def test_extract_docket(text, expected):
    assert extract_docket(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Before Judge Mary J. Smith, the court held.", "Mary J. Smith"),
        ("Chief Judge Alan B. Ward wrote separately.", "Alan B. Ward"),
        ("PER CURIAM.", "Per Curiam"),
        ("Judge held that nothing useful followed.", ""),
        ("No judicial attribution here.", ""),
    ],
)
def test_extract_judge(text, expected):
    assert extract_judge(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "See 38 U.S.C. § 1110 and 38 U.S.C. § 5107(b).",
            ["38 U.S.C. § 1110", "38 U.S.C. § 5107(b)"],
        ),
        ("Section 38 C.F.R. § 3.303(a) governs service connection.", ["38 C.F.R. § 3.303(a)"]),
        ("38 U.S.C. § 1110 applies; again, 38 U.S.C. § 1110.", ["38 U.S.C. § 1110"]),
        ("Also 38 U.S.C. 5103A without the section mark.", ["38 U.S.C. § 5103A"]),
        ("Also cited 38 u.s.c. § 1110 in lowercase text.", ["38 U.S.C. § 1110"]),
        ("No statutory citations appear.", []),
    ],
)
def test_extract_statutes(text, expected):
    assert extract_statutes(text) == expected


def test_extract_statutes_caps_results():
    text = " and ".join(f"38 U.S.C. § {n}" for n in (1110, 1131, 1151, 5103, 5107, 7104, 7252))

    assert len(extract_statutes(text)) == 6


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The decision is vacated and remanded.", "vacated and remanded"),
        ("The Board decision is AFFIRMED.", "affirmed"),
        ("The motion was granted but the appeal denied.", "granted and denied"),
        ("vacated, remanded, and affirmed all appear.", "vacated and remanded"),
        ("No disposition language appears here.", ""),
    ],
)
def test_extract_outcome(text, expected):
    assert extract_outcome(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "We hold that the Board erred in weighing the evidence.",
            "We hold that the Board erred in weighing the evidence.",
        ),
        (
            "the Court holds that VA failed in its duty; remand follows.",
            "The Court holds that VA failed in its duty.",
        ),
        ("We   conclude\nthat the notice was adequate.", "We conclude that the notice was adequate."),
        ("We hold that so.", ""),
        ("No holding sentence here.", ""),
    ],
)
def test_extract_holding_sentence(text, expected):
    assert extract_holding_sentence(text) == expected


RICH_HTML_PAGE = """
<html><head>
<title>Doe v. McDonough</title>
<meta name="description" content="Old meta holding.">
</head><body>
<h1>Doe v. McDonough</h1>
<p>Decided: June 2, 2022. Docket No. 19-4433. Before Judge Mary J. Smith.</p>
<p>The Court holds that the Board provided inadequate reasons and bases for its decision.</p>
<p>See 38 U.S.C. § 7104(d)(1) and 38 C.F.R. § 3.303. The decision is vacated and remanded.</p>
</body></html>
"""


def test_fetch_case_details_extracts_rich_fields(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.fetch.requests.get",
        lambda url, headers=None, timeout=None: FakePageResponse(
            text=RICH_HTML_PAGE, headers={"Content-Type": "text/html"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/doe.html")

    assert details["decision_date"] == "2022-06-02"
    assert details["docket"] == "19-4433"
    assert details["judge"] == "Mary J. Smith"
    assert details["holding"] == (
        "The Court holds that the Board provided inadequate reasons and bases for its decision."
    )
    assert details["statutes"] == ["38 U.S.C. § 7104(d)(1)", "38 C.F.R. § 3.303"]
    assert details["outcome"] == "vacated and remanded"