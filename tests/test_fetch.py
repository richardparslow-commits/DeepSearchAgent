"""Tests for case-detail extraction (va_legal_agent.fetch)."""

import ipaddress
import socket

import pytest
import requests

from va_legal_agent.fetch import (
    FetchError,
    _extract_pdf_text,
    _read_response_body,
    _validate_fetch_url,
    extract_appellant_role,
    extract_case_details,
    extract_citation,
    extract_decision_date,
    extract_docket,
    extract_citation_treatments,
    extract_holding_sentence,
    extract_holding_sentences,
    extract_judge,
    extract_legal_standard,
    extract_outcome,
    extract_precedential_status,
    extract_statutes,
    fetch_case_details,
    fetch_full_text,
)


_PUBLIC_TEST_ADDRESS = "93.184.216.34"


@pytest.fixture(autouse=True)
def _stub_public_case_dns(monkeypatch):
    """Keep fetch tests offline while still exercising literal-address checks."""
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if host == "localhost":
                return real_getaddrinfo(host, port, *args, **kwargs)
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (_PUBLIC_TEST_ADDRESS, port),
                )
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr("va_legal_agent.fetch.socket.getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("See Fountain v. McDonald, 23 Vet.App. 1 (2011).", "23 Vet.App. 1"),
        ("Citing Smith v. Wilkie, 912 F.3d 1306 (Fed. Cir. 2019).", "912 F.3d 1306"),
        ("The court decided 590 F.4th 1300 last term.", "590 F.4th 1300"),
        ("Argued in 500 U.S. 123 and related orders.", "500 U.S. 123"),
        ("Westlaw cite 2011 WL 123456 applies.", "2011 WL 123456"),
        ("Citation Nr: 2100634, Board decision.", "BVA Citation Nr. 2100634"),
        ("Citation Nr. 2100634, Board decision.", "BVA Citation Nr. 2100634"),
        ("Citation Nr.: A25049742, Board decision.", "BVA Citation Nr. A25049742"),
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


def test_extract_decision_date_prefers_decided_over_filing_date():
    # Court pages frequently show the filing date first; the decision date is
    # the authority's date and must win despite document order.
    text = "Filed: January 2, 2024\nDecided: January 3, 2023"
    assert extract_decision_date(text) == "2023-01-03"


def test_extract_decision_date_prefers_issued_over_filing_date():
    # The full hint hierarchy applies: issued outranks filed even when filed
    # appears first on the page.
    text = "Filed: January 2, 2024\nIssued: January 3, 2024"
    assert extract_decision_date(text) == "2024-01-03"


def test_extract_decision_date_sept_and_invalid_day():
    # "Sept" is normalized to "Sep" before parsing.
    assert extract_decision_date("Decided: Sept 5, 2020.") == "2020-09-05"
    # An out-of-range day matches the date pattern but fails to parse -> "".
    assert extract_decision_date("Decided: Sept 40, 2020.") == ""


class FakePageResponse:
    def __init__(self, text="", content=b"", status_code=200, headers=None):
        self.text = text
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.url = "https://example.com/test"

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=8192):
        if self.content:
            yield self.content
        elif self.text:
            yield self.text.encode(self.encoding)


HTML_PAGE = """
<html><head>
<title>Fountain v. McDonald, 23 Vet.App. 1 (2011)</title>
<meta name="description" content="The Board must provide adequate reasons and bases.">
</head><body>
<h1>Fountain v. McDonald</h1>
<p>Decided: March 15, 2011. Cite as 23 Vet.App. 1.</p>
</body></html>
"""


def test_parsers_never_crash_on_adversarial_input():
    """Deterministic fuzz: every parser must tolerate garbage/truncated text."""
    import random
    import string as string_module

    from va_legal_agent.fetch import (
        extract_citation,
        extract_decision_date,
        extract_docket,
        extract_holding_sentence,
        extract_judge,
        extract_outcome,
        extract_statutes,
    )

    random.seed(42)
    parsers = [
        extract_citation,
        extract_decision_date,
        extract_docket,
        extract_holding_sentence,
        extract_judge,
        extract_outcome,
        extract_statutes,
    ]
    alphabet = string_module.printable + "§·–—\u2019\u201c\u201d"
    base = (
        "38 U.S.C. § 5107(b) and 38 C.F.R. § 3.303 apply; the Board is VACATED and "
        "REMANDED. Decided: Sept 5, 2020. Docket No. 19-4433. Before Judge Mary J. "
        "Smith. The Court holds that the Board erred. 23 Vet.App. 1 (2011), "
        "2011 WL 123456, Citation Nr: 2100634."
    )
    for i in range(2000):
        text = "".join(
            random.choice(alphabet) for _ in range(random.randint(0, 400))
        )
        if i % 7 == 0:
            text = base[: random.randint(0, len(base))] + text
        for parser in parsers:
            parser(text)  # must never raise on arbitrary input


def test_fetch_case_details_from_html(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text=HTML_PAGE, headers={"Content-Type": "text/html"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/fountain.html")

    assert details["citation"] == "23 Vet.App. 1"
    assert details["decision_date"] == "2011-03-15"
    assert details["holding"] == "The Board must provide adequate reasons and bases."


def test_fetch_case_details_from_plain_text(monkeypatch):
    bva_text = (
        "Citation Nr: A25049742\nDecision Date: 06/05/25\n\n"
        "DOCKET NO. 220606-252507\nDATE: June 5, 2025\n\nORDER\n\n"
        "Entitlement to service connection for tinnitus is granted.\n"
        "38 U.S.C. 1110, 5107; 38 C.F.R. 3.303.\n"
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text=bva_text, headers={"Content-Type": "text/plain"}
        ),
    )

    details = fetch_case_details("https://www.va.gov/vetapp25/Files6/A25049742.txt")

    assert details["citation"] == "BVA Citation Nr. A25049742"
    assert details["decision_date"] == "2025-06-05"
    assert details["docket"] == "220606-252507"
    assert details["statutes"]  # 38 U.S.C. / 38 C.F.R. sections extracted


def test_fetch_case_details_handles_unparseable_pdf(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
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
        "appellant_role": "unknown",
        "legal_standard": "",
        "citation_treatments": [],
        "precedential": None,
    }


def test_fetch_case_details_raises_fetch_error_on_network_failure(monkeypatch):
    def failing_get(url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr("va_legal_agent.fetch.cffi_requests.get", failing_get)

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
        ("38 C.F.R. § 3.303 applies; again, 38 C.F.R. § 3.303.", ["38 C.F.R. § 3.303"]),
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


def test_extract_statutes_labels_mixed_us_usca_and_cfr_correctly():
    text = "See 38 U.S.C. 1110 and 38 U.S.C.A. 5107 and 38 C.F.R. 3.303."

    assert extract_statutes(text) == [
        "38 U.S.C. § 1110",
        "38 U.S.C.A. § 5107",
        "38 C.F.R. § 3.303",
    ]


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
        # Secretary cross-appeal patterns
        ("The Secretary's cross-appeal is affirmed.", "secretary"),
        ("The Secretary appealed the grant.", "secretary"),
        ("appellant, the Secretary, argues that.", "secretary"),
        ("The VA is the appellant in this case.", "secretary"),
        # Veteran appeal patterns
        ("The veteran's appeal is denied.", "veteran"),
        ("The veteran appealed the Board's denial.", "veteran"),
        ("appellant, the veteran, contends that.", "veteran"),
        # Ambiguous / unknown
        ("The appellant filed a brief.", "unknown"),
        ("No appeal language here.", "unknown"),
    ],
)
def test_extract_appellant_role(text, expected):
    assert extract_appellant_role(text) == expected


def test_extract_appellant_role_secretary_wins_over_veteran():
    # When both appear, Secretary is checked first (cross-appeal is the
    # disambiguating signal: a veteran can appeal AND the Secretary can
    # cross-appeal, but the Secretary's cross-appeal determines the
    # direction of "affirmed" / "dismissed").
    text = "The veteran appealed the denial. The Secretary's cross-appeal is affirmed."
    assert extract_appellant_role(text) == "secretary"


def test_extract_case_details_includes_appellant_role():
    text = "The Secretary's cross-appeal is affirmed."
    details = extract_case_details(text)
    assert details["appellant_role"] == "secretary"
    assert details["outcome"] == "affirmed"


def test_extract_case_details_includes_legal_standard():
    details = extract_case_details("We review the Board's findings for clear error.")
    assert details["legal_standard"] == "clear error"


def test_extract_case_details_no_legal_standard():
    details = extract_case_details("The veteran served in Vietnam.")
    assert details["legal_standard"] == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Clear error variants
        ("We review the Board's findings for clear error.", "clear error"),
        ("The Board's finding was not clearly erroneous.", "clear error"),
        ("Under the clearly erroneous standard, we affirm.", "clear error"),
        # De novo variants
        ("We apply de novo review to the legal conclusions.", "de novo"),
        ("Legal questions are reviewed de novo.", "de novo"),
        ("This court reviews de novo questions of law.", "de novo"),
        # Abuse of discretion
        ("The agency's decision was not an abuse of discretion.", "abuse of discretion"),
        # Arbitrary and capricious
        ("The rule was arbitrary and capricious.", "arbitrary and capricious"),
        ("The rulemaking was arbitrary, capricious, and unlawful.", "arbitrary and capricious"),
        # Substantial evidence
        ("There is substantial evidence to support the finding.", "substantial evidence"),
        ("A reasonable mind might accept the evidence.", "substantial evidence"),
        # Harmless error
        ("The error was harmless.", "harmless error"),
        ("No reversible error was committed.", "harmless error"),
        # Prejudicial error
        ("The error was a prejudicial error.", "prejudicial error"),
        ("The error was so significant as to have affected the outcome.", "prejudicial error"),
        # Deferential
        ("We will not disturb the Board's credibility determination.", "deferential (will not disturb)"),
        # Plenary review
        ("We exercise plenary review over the statutory question.", "plenary review"),
        # Independent review
        ("We exercise independent review of the administrative record.", "independent review"),
        # No standard
        ("Procedural history only.", ""),
        ("", ""),
        ("   ", ""),
    ],
)
def test_extract_legal_standard(text, expected):
    assert extract_legal_standard(text) == expected


def test_extract_legal_standard_returns_first_match_only():
    """Only the first matched standard is returned, not the rest."""
    text = "We review for clear error. The court also reviews de novo."
    assert extract_legal_standard(text) == "clear error"


def test_extract_legal_standard_uses_earliest_document_match():
    # The later clear-error reference must not beat the opening de novo review.
    text = (
        "We review legal questions de novo. "
        "The Board factual findings require clear error."
    )
    assert extract_legal_standard(text) == "de novo"


def test_extract_legal_standard_keeps_earlier_clear_error_over_negated_de_novo():
    # A later negated "de novo" mention must not displace the standard the
    # court actually applied earlier in the sentence.
    text = (
        "We review the Board factual findings for clear error "
        "and do not review de novo."
    )
    assert extract_legal_standard(text) == "clear error"


# ── extract_precedential_status ─────────────────────────────────────────────


def test_extract_precedential_status_unknown_for_empty_text():
    """Empty and whitespace-only text has unknown, not presumptively valid, status."""
    assert extract_precedential_status("") is None
    assert extract_precedential_status("   ") is None


def test_extract_precedential_status_not_for_publication():
    assert extract_precedential_status("NOT FOR PUBLICATION") is False
    assert extract_precedential_status("not for publication") is False
    assert extract_precedential_status("   NOT FOR PUBLICATION   ") is False


def test_extract_precedential_status_non_precedential():
    assert extract_precedential_status("NON-PRECEDENTIAL") is False
    assert extract_precedential_status("nonprecedential") is False
    assert extract_precedential_status("NOT PRECEDENTIAL") is False


def test_extract_precedential_status_unpublished():
    assert extract_precedential_status("unpublished") is False
    assert extract_precedential_status("The decision is unpublished and cannot be cited.") is False


def test_extract_precedential_status_do_not_cite():
    assert extract_precedential_status("DO NOT CITE THIS CASE") is False
    assert extract_precedential_status("Do not cite as precedent") is False


def test_extract_precedential_status_memorandum_decision():
    assert extract_precedential_status("MEMORANDUM DECISION") is False
    assert extract_precedential_status("This is a memorandum decision of the Board.") is False


def test_extract_precedential_status_single_judge():
    assert extract_precedential_status("SINGLE JUDGE DESIGNATION") is False
    assert extract_precedential_status("single judge disposition") is False


def test_extract_precedential_status_reads_single_judge_heading():
    """A single-judge heading marks the disposition non-precedential.

    Real orders are headed "SINGLE JUDGE DECISION" or carry a
    "Before: Single Judge" caption; the older marker required the words
    "designation"/"disposition" after "single judge" and missed both.
    """
    assert (
        extract_precedential_status("SINGLE JUDGE DECISION\nThe appeal is denied.")
        is False
    )
    assert (
        extract_precedential_status("Before: Single Judge\nThe appeal is denied.")
        is False
    )
    # Prose that mentions a single-judge decision must not demote a published
    # opinion (the marker is line-anchored, and only the header is scanned).
    assert (
        extract_precedential_status(
            "SMITH v. WILKIE\n30 Vet.App. 123\n\n"
            "The Board's single judge decision was affirmed."
        )
        is True
    )


def test_extract_precedential_status_no_precedential_value():
    assert extract_precedential_status("no precedential value") is False
    assert extract_precedential_status("No precedential effect.") is False


def test_extract_precedential_status_non_citable():
    assert extract_precedential_status("non-citable") is False
    assert extract_precedential_status("noncitable") is False


def test_extract_precedential_status_this_order_not_citable():
    assert extract_precedential_status("This order is not to be cited as precedent.") is False
    assert extract_precedential_status("This order is not citable as binding.") is False


def test_extract_precedential_status_normal_opinion_is_precedential():
    """A normal published opinion without any non-precedential marker is precedential."""
    text = (
        "The Court holds that the Board erred in weighing the evidence. "
        "The decision is vacated and remanded. See 38 U.S.C. § 5107."
    )
    assert extract_precedential_status(text) is True


def test_extract_precedential_status_citation_containing_not_is_precedential():
    """A citation containing the word 'not' is not a non-precedential marker."""
    text = "See Notgrass v. Wilkie, 30 Vet. App. 123 (2018)."
    assert extract_precedential_status(text) is True


def test_extract_case_details_includes_precedential():
    details = extract_case_details(
        "The Board erred in weighing the evidence. The decision is vacated and remanded."
    )
    assert details["precedential"] is True


def test_extract_case_details_flags_non_precedential():
    details = extract_case_details(
        "NOT FOR PUBLICATION. The Board erred in weighing the evidence."
    )
    assert details["precedential"] is False


def test_extract_case_details_empty_body_has_unknown_precedential():
    details = extract_case_details("")
    assert details["precedential"] is None


def test_extract_precedential_status_did_not_serve_as_precedent():
    assert extract_precedential_status(
        "The decision did not serve as precedent."
    ) is False


# ---------------------------------------------------------------------------
# Citation treatment extraction
# ---------------------------------------------------------------------------


def test_extract_citation_treatments_basic_detection():
    assert extract_citation_treatments(
        "We overruled the decision in Smith v. Wilkie, 30 Vet.App. 1."
    ) == [{"cited_case": "Smith v. Wilkie", "treatment": "overruled"}]


def test_extract_citation_treatments_distinguished():
    assert extract_citation_treatments(
        "We distinguished the case of Miller v. VA on its facts."
    ) == [{"cited_case": "Miller v. VA", "treatment": "distinguished"}]


def test_extract_citation_treatments_followed():
    assert extract_citation_treatments(
        "We follow our prior holding in Jones v. Brown, 29 Vet.App."
    ) == [{"cited_case": "Jones v. Brown", "treatment": "followed"}]


def test_extract_citation_treatments_declined_to_follow():
    assert extract_citation_treatments(
        "We declined to follow the decision in Green v. United States, 25 Vet.App."
    ) == [{"cited_case": "Green v. United States", "treatment": "declined to follow"}]


def test_extract_citation_treatments_questioned():
    assert extract_citation_treatments(
        "The court questioned the holding in Hamilton v. McDonald."
    ) == [{"cited_case": "Hamilton v. McDonald", "treatment": "questioned"}]


def test_extract_citation_treatments_reaffirmed():
    assert extract_citation_treatments(
        "We reaffirmed Garcia v. Wilkie which controls."
    ) == [{"cited_case": "Garcia v. Wilkie", "treatment": "reaffirmed"}]


def test_extract_citation_treatments_abrogated():
    assert extract_citation_treatments(
        "Congress abrogated the decision in Allen v. Principi."
    ) == [{"cited_case": "Allen v. Principi", "treatment": "abrogated"}]


def test_extract_citation_treatments_criticized():
    assert extract_citation_treatments(
        "The panel criticized the decision in Brown v. West."
    ) == [{"cited_case": "Brown v. West", "treatment": "criticized"}]


def test_extract_citation_treatments_covered_range_prevents_double_fire():
    """'declined to follow' must not also fire the plain 'follow' term."""
    result = extract_citation_treatments(
        "We declined to follow the decision in Green v. United States."
    )
    treatments = [r["treatment"] for r in result]
    assert "followed" not in treatments
    assert "declined to follow" in treatments


def test_extract_citation_treatments_nearest_case_name_chosen():
    """Each treatment word pairs with the nearest case, not all in window."""
    result = extract_citation_treatments(
        "We distinguished Smith v. Wilkie but followed Jones v. Brown."
    )
    assert {"cited_case": "Smith v. Wilkie", "treatment": "distinguished"} in result
    assert {"cited_case": "Jones v. Brown", "treatment": "followed"} in result
    assert {"cited_case": "Jones v. Brown", "treatment": "distinguished"} not in result


def test_extract_citation_treatments_no_case_name_nearby():
    """Treatment word without a nearby case name is ignored."""
    assert extract_citation_treatments(
        "We followed the reasoning of the lower court."
    ) == []


def test_extract_citation_treatments_empty_or_whitespace():
    assert extract_citation_treatments("") == []
    assert extract_citation_treatments("   ") == []


def test_extract_citation_treatments_deduplicates():
    """Multiple mentions of the same (case, treatment) pair dedupe."""
    result = extract_citation_treatments(
        "We distinguished Smith v. Wilkie. Smith v. Wilkie was also distinguished later."
    )
    assert len(result) == 1
    assert result[0] == {"cited_case": "Smith v. Wilkie", "treatment": "distinguished"}


def test_extract_citation_treatments_multi_word_second_party():
    result = extract_citation_treatments(
        "We follow the reasoning of Brown v. West Publishing Co."
    )
    assert len(result) == 1
    assert result[0]["cited_case"] == "Brown v. West Publishing Co"


def test_extract_citation_treatments_year_parenthetical():
    result = extract_citation_treatments(
        "Overruled in part by Smith v. Wilkie (2024)."
    )
    assert result[0]["cited_case"] == "Smith v. Wilkie"


def test_extract_citation_treatments_capped_at_max():
    """Maximum of 8 treatments extracted."""
    text = " ".join(
        f"We distinguished Case{i} v. Defendant{i}." for i in range(20)
    )
    assert len(extract_citation_treatments(text)) == 8


def test_extract_case_details_includes_citation_treatments():
    details = extract_case_details(
        "We distinguished the decision in Smith v. Wilkie. "
        "We review for clear error."
    )
    assert details["citation_treatments"] == [
        {"cited_case": "Smith v. Wilkie", "treatment": "distinguished"}
    ]


def test_extract_case_details_no_citation_treatments():
    details = extract_case_details("Procedural history only.")
    assert details["citation_treatments"] == []


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
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
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


def test_fetch_case_details_pdf_reads_all_pages(monkeypatch):
    # Enrichment used to stop at page four, losing a holding on page five.
    pages = [
        _FakePdfPage("Page one"),
        _FakePdfPage("Page two"),
        _FakePdfPage("Page three"),
        _FakePdfPage("Page four"),
        _FakePdfPage("We hold that the Board erred in its nexus analysis."),
        _FakePdfPage("ORDER"),
    ]
    monkeypatch.setattr("pypdf.PdfReader", lambda stream: _FakePdfReader(pages))
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/pdf"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/long.pdf")

    assert "We hold that the Board erred in its nexus analysis." in details["holding"]


def test_fetch_case_details_from_pdf(monkeypatch):
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    pdf_bytes = buffer.getvalue()

    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=pdf_bytes, headers={"Content-Type": "application/pdf"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/blank.pdf")

    assert details["citation"] == ""
    assert details["decision_date"] == ""


def test_fetch_pdf_skips_pages_that_fail_extraction(monkeypatch):
    class _BadPage:
        def extract_text(self):
            raise RuntimeError("page corrupt")

    class _BadReader:
        pages = [_BadPage(), _BadPage()]

    monkeypatch.setattr("pypdf.PdfReader", lambda stream: _BadReader())
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/pdf"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/bad-pages.pdf")

    assert details["citation"] == ""  # unreadable pages are skipped, not fatal


def test_fetch_case_details_handles_corrupt_pdf(monkeypatch, caplog):
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"definitely not a pdf",
            headers={"Content-Type": "application/pdf"},
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/broken.pdf")

    assert details["citation"] == ""  # unparseable PDF yields empty details
    assert any("Could not extract PDF text" in r.message for r in caplog.records)


def test_fetch_skips_empty_stream_chunks(monkeypatch):
    class ChunkedResponse(FakePageResponse):
        def iter_content(self, chunk_size=8192):
            yield b""  # an empty chunk must not abort the read
            yield self.content

    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: ChunkedResponse(
            content=b"<html><body><p>No holding sentence here.</p></body></html>",
            headers={"Content-Type": "text/html"},
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/empty-chunk.html")

    assert details["holding"] == ""  # body still read past the empty chunk


def test_fetch_case_details_rejects_oversized_content_length(monkeypatch):
    monkeypatch.setenv("MAX_FETCH_BYTES", "10")
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"",
            headers={"Content-Type": "application/pdf", "Content-Length": "11"},
        ),
    )

    with pytest.raises(FetchError, match="cap"):
        fetch_case_details("https://uscourts.cavc.gov/huge.pdf")


def test_extract_citation_scotus_reporter():
    assert extract_citation("Citing 123 S. Ct. 456 last term.") == "123 S. Ct. 456"


def test_extract_decision_date_boundary_with_hint_near_start():
    # The context window starts at max(0, start-40); a hint at position 0 must
    # still be seen for a date within 40 chars, preferring it over a later date.
    text = "Decided: March 15, 2011. Filed January 3, 2024."
    assert extract_decision_date(text) == "2011-03-15"


def test_extract_decision_date_hint_at_41_chars_back():
    # A hint exactly 41 chars before the date is OUTSIDE the 40-char window, so
    # the hinted date is not preferred and the first date is returned instead.
    text = "January 1, 2020. " + "x" * 5 + " Decided " + "x" * 32 + " March 3, 2021."
    assert extract_decision_date(text) == "2020-01-01"


def test_extract_docket_collapses_multiple_spaces():
    assert extract_docket("Docket No. 12-34  567  was assigned.") == "12-34 567"


def test_extract_judge_collapses_multiple_spaces():
    assert extract_judge("Before Judge Mary   J.  Smith, the court held.") == "Mary J. Smith"


def test_normalize_statute_value_strips_whitespace_and_parenthetical():
    # Internal whitespace and a trailing parenthetical are both normalized away.
    assert extract_statutes("See 38 U.S.C. § 1114 (iv) (West).") == ["38 U.S.C. § 1114"]


def test_normalize_statute_value_preserves_two_letter_subsection():
    # (aa)/(bb) are CFR paragraph identifiers, not annotator parentheticals.
    assert extract_statutes("38 C.F.R. § 3.1(aa)") == ["38 C.F.R. § 3.1(aa)"]
    assert extract_statutes("38 C.F.R. § 3.1(bb)") == ["38 C.F.R. § 3.1(bb)"]
    assert extract_statutes("38 C.F.R. § 3.1(aa) (West)") == [
        "38 C.F.R. § 3.1(aa)"
    ]


def test_normalize_statute_value_preserves_attached_subsections():
    # Attached parentheticals are nested subsections even when Roman or
    # numeric; only whitespace-separated parentheticals are annotations.
    assert extract_statutes("38 U.S.C. § 5107(b)(1)(A)(iv)") == [
        "38 U.S.C. § 5107(b)(1)(A)(iv)"
    ]
    assert extract_statutes("38 C.F.R. § 3.1(aa)(1)(i)") == [
        "38 C.F.R. § 3.1(aa)(1)(i)"
    ]


def test_extract_holding_sentence_keeps_exactly_min_words():
    # A holding with exactly MIN words is kept; the <= mutant would drop it.
    assert extract_holding_sentence("We hold that the VA erred.") == "We hold that the VA erred."


def test_extract_holding_sentence_skips_fragment_then_returns_real():
    # A short fragment must be skipped, not abort the search for a real holding.
    text = "We hold that so. We hold that the Board erred in weighing the evidence."
    assert extract_holding_sentence(text) == "We hold that the Board erred in weighing the evidence."


def test_extract_holding_sentence_rsplit_uses_first_semicolon():
    # Only the FIRST semicolon cuts the holding; rsplit would cut at the last.
    text = "We hold that the Board erred in this matter; Y; Z."
    assert extract_holding_sentence(text) == "We hold that the Board erred in this matter."


def test_extract_holding_sentences_returns_all_holdings():
    # A decision with two holdings must return both, not just the first.
    text = (
        "We hold that the Board erred in its nexus analysis. "
        "We also hold that the benefit of the doubt rule was not applied."
    )
    holdings = extract_holding_sentences(text)
    assert len(holdings) == 2
    assert holdings[0] == "We hold that the Board erred in its nexus analysis."
    assert holdings[1] == "We also hold that the benefit of the doubt rule was not applied."


def test_extract_holding_sentences_single_holding():
    # One holding returns a one-element list (not a string).
    text = "The Court holds that the Board erred in weighing the evidence."
    holdings = extract_holding_sentences(text)
    assert holdings == ["The Court holds that the Board erred in weighing the evidence."]


def test_extract_holding_sentences_no_holdings():
    assert extract_holding_sentences("No holdings here at all.") == []


def test_extract_holding_sentences_skips_fragments_keeps_reals():
    # Fragments below the word threshold are skipped, real holdings are kept.
    text = (
        "We hold that so. "  # fragment
        "We hold that the Board erred in weighing the evidence. "
        "We also find that the duty to assist was violated."
    )
    holdings = extract_holding_sentences(text)
    assert len(holdings) == 2
    assert "Board erred" in holdings[0]
    assert "duty to assist" in holdings[1]


def test_extract_holding_sentences_caps_at_max():
    # A decision with many holdings returns at most _MAX_HOLDING_SENTENCES.
    from va_legal_agent.fetch import _MAX_HOLDING_SENTENCES

    parts = [f"We hold that element number {i} was properly analyzed." for i in range(10)]
    holdings = extract_holding_sentences(" ".join(parts))
    assert len(holdings) == _MAX_HOLDING_SENTENCES
    # Document order is preserved.
    assert "element number 0" in holdings[0]


def test_extract_holding_sentences_semicolon_cuts_each():
    # Each holding is independently cut at its first semicolon.
    text = (
        "We hold that the nexus was adequate; see also Jones. "
        "We hold that the Board erred in its analysis; remand follows."
    )
    holdings = extract_holding_sentences(text)
    assert len(holdings) == 2
    assert holdings[0] == "We hold that the nexus was adequate."
    assert holdings[1] == "We hold that the Board erred in its analysis."


def test_extract_case_details_joins_multiple_holdings():
    # The public extract_case_details joins all holdings into one holding field.
    text = (
        "We hold that the Board erred in its nexus analysis. "
        "We also hold that the benefit of the doubt rule was not applied."
    )
    details = extract_case_details(text)
    assert details["holding"] == (
        "We hold that the Board erred in its nexus analysis. "
        "We also hold that the benefit of the doubt rule was not applied."
    )


class _FakePdfPage:
    def __init__(self, text="", raises=False):
        self._text = text
        self._raises = raises

    def extract_text(self):
        if self._raises:
            raise RuntimeError("page corrupt")
        return self._text


class _FakePdfReader:
    def __init__(self, pages):
        self.pages = pages


def test_extract_pdf_text_joins_pages(monkeypatch):
    monkeypatch.setattr(
        "pypdf.PdfReader", lambda stream: _FakePdfReader([_FakePdfPage("A"), _FakePdfPage("B")])
    )
    assert _extract_pdf_text(b"pdf") == "A B"


def test_extract_pdf_text_caps_at_four_pages(monkeypatch):
    monkeypatch.setattr(
        "pypdf.PdfReader",
        lambda stream: _FakePdfReader(
            [_FakePdfPage(str(i)) for i in range(5)]
        ),
    )
    assert _extract_pdf_text(b"pdf") == "0 1 2 3"  # 5th page not read


def test_extract_pdf_text_skips_bad_pages(monkeypatch):
    monkeypatch.setattr(
        "pypdf.PdfReader",
        lambda stream: _FakePdfReader([_FakePdfPage(raises=True), _FakePdfPage("B")]),
    )
    assert _extract_pdf_text(b"pdf") == "B"  # failing page skipped, later pages read


def test_extract_pdf_text_uses_single_space_join(monkeypatch):
    monkeypatch.setattr(
        "pypdf.PdfReader", lambda stream: _FakePdfReader([_FakePdfPage("A"), _FakePdfPage("B")])
    )
    assert _extract_pdf_text(b"pdf") == "A B"  # not "AXX XXB"


def test_extract_pdf_text_empty_pages(monkeypatch):
    monkeypatch.setattr(
        "pypdf.PdfReader", lambda stream: _FakePdfReader([_FakePdfPage("")])
    )
    assert _extract_pdf_text(b"pdf") == ""


def test_read_response_body_passes_chunk_size(monkeypatch):
    seen: dict[str, object] = {}

    class RecordingResponse:
        headers = {}
        url = "https://example.com/rec"

        def iter_content(self, chunk_size=8192):
            seen["chunk_size"] = chunk_size
            return iter([b"abc"])

    body = _read_response_body(RecordingResponse(), 100)
    assert body == b"abc"
    assert seen["chunk_size"] == 64 * 1024


def test_read_response_body_exact_max_bytes_accepted():
    class Response:
        headers = {}
        url = "https://example.com/exact"

        def iter_content(self, chunk_size=8192):
            return iter([b"12345"])

    assert _read_response_body(Response(), 5) == b"12345"


def test_read_response_body_cumulative_over_max_raises():
    class Response:
        headers = {}
        url = "https://example.com/cum"

        def iter_content(self, chunk_size=8192):
            return iter([b"12345", b"67890"])

    with pytest.raises(FetchError, match="cap"):
        _read_response_body(Response(), 8)


def test_read_response_body_skips_empty_chunk_then_reads_rest():
    class Response:
        headers = {}
        url = "https://example.com/empty-first"

        def iter_content(self, chunk_size=8192):
            return iter([b"", b"abc"])

    assert _read_response_body(Response(), 100) == b"abc"


def test_read_response_body_joins_chunks_exactly():
    class Response:
        headers = {}
        url = "https://example.com/join"

        def iter_content(self, chunk_size=8192):
            return iter([b"ab", b"cd"])

    assert _read_response_body(Response(), 100) == b"abcd"  # b"".join, not a separator


def test_fetch_case_details_requests_exact_args(monkeypatch):
    seen: dict[str, object] = {}

    def recording_get(url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["timeout"] = timeout
        seen["stream"] = stream
        seen["impersonate"] = impersonate
        seen["allow_redirects"] = allow_redirects
        return FakePageResponse(
            text="<html><body><p>No holding sentence here.</p></body></html>",
            headers={"Content-Type": "text/html"},
        )

    monkeypatch.setattr("va_legal_agent.fetch.cffi_requests.get", recording_get)

    fetch_case_details("https://uscourts.cavc.gov/exact.html")

    assert seen["url"] == "https://uscourts.cavc.gov/exact.html"
    # No explicit User-Agent: the chrome impersonation supplies a browser UA,
    # and overriding it with the app's bot-identifying USER_AGENT would
    # re-trigger the WAF block the impersonation is meant to bypass.
    assert seen["headers"] is None
    assert seen["timeout"] == 20
    assert seen["stream"] is True
    # The fetch layer must impersonate a browser like the search layer does,
    # else protected result pages (CAVC, DDG targets) return WAF challenges.
    assert seen["impersonate"] == "chrome"
    assert seen["allow_redirects"] is False


@pytest.mark.filterwarnings("ignore::curl_cffi.utils.CurlCffiWarning")
def test_fetch_impersonation_never_sends_bot_user_agent(monkeypatch):
    """The on-the-wire User-Agent is curl_cffi's Chrome UA, never the bot UA.

    ``impersonate="chrome"`` reproduces the Chrome TLS handshake, but an
    explicit ``User-Agent`` header overrides the impersonated browser header —
    the exact override that re-triggers the WAF block impersonation exists to
    bypass. Pin this at the header level by capturing the real User-Agent a
    loopback server receives from the un-mocked fetch call.
    """
    import http.server
    import threading

    # The hermetic fixture clears SEARCH_HTTP_PROXY (the app's own setting),
    # but curl_cffi/libcurl also honors the standard proxy env vars; clear
    # those too so the request reaches the loopback server directly.
    for var in (
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)

    # Private-address blocking is covered by the dedicated SSRF tests; this
    # test isolates the on-the-wire User-Agent and needs a loopback server.
    monkeypatch.setattr("va_legal_agent.fetch._validate_fetch_url", lambda url: None)

    received: dict[str, str | None] = {"user_agent": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            received["user_agent"] = self.headers.get("User-Agent")
            body = b"<html><body><p>No holding sentence here.</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # keep test output clean

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        fetch_case_details(f"http://127.0.0.1:{server.server_port}/test.html")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    ua = received["user_agent"]
    assert ua is not None, "the loopback server received the request"
    # Browser Chrome UA on the wire...
    assert "Chrome" in ua
    # ...and never the app's bot-identifying USER_AGENT, which would re-trigger
    # the WAF/TLS challenge the impersonation exists to bypass.
    assert "VA-Legal-Agent" not in ua


def test_fetch_case_details_passes_proxy_when_configured(monkeypatch):
    monkeypatch.setenv("SEARCH_HTTP_PROXY", "http://user:pass@proxy.example:8080")
    captured: dict[str, object] = {}

    def fake_get(url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None):
        captured["proxies"] = proxies
        return FakePageResponse(
            text="<html><body><p>No holding sentence here.</p></body></html>",
            headers={"Content-Type": "text/html"},
        )

    monkeypatch.setattr("va_legal_agent.fetch.cffi_requests.get", fake_get)

    fetch_case_details("https://uscourts.cavc.gov/proxied.html")

    assert captured["proxies"] == {
        "http": "http://user:pass@proxy.example:8080",
        "https": "http://user:pass@proxy.example:8080",
    }


def test_fetch_full_text_passes_proxy_when_configured(monkeypatch):
    monkeypatch.setenv("SEARCH_HTTP_PROXY", "http://proxy.example:8080")
    captured: dict[str, object] = {}

    def fake_get(url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None):
        captured["proxies"] = proxies
        return FakePageResponse(
            text="<html><body>Opinion text here.</body></html>",
            headers={"Content-Type": "text/html"},
        )

    monkeypatch.setattr("va_legal_agent.fetch.cffi_requests.get", fake_get)

    fetch_full_text("https://uscourts.cavc.gov/proxied-opinion.html")

    assert captured["proxies"] == {
        "http": "http://proxy.example:8080",
        "https": "http://proxy.example:8080",
    }


def test_fetch_case_details_content_type_pdf_without_pdf_extension(monkeypatch):
    # Content-Type alone marks a response as PDF (mutants reading the header
    # with the wrong key or default would fall through to HTML parsing).
    seen: dict[str, object] = {}

    def recording_reader(stream):
        seen["stream"] = stream
        return _FakePdfReader([_FakePdfPage("Cite as 23 Vet.App. 1")])

    monkeypatch.setattr("pypdf.PdfReader", recording_reader)
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/pdf"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/no-extension")
    assert details["citation"] == "23 Vet.App. 1"
    assert seen["stream"].getvalue() == b"pdf-bytes"  # real body passed to the reader


def test_fetch_case_details_query_string_pdf(monkeypatch):
    monkeypatch.setattr(
        "pypdf.PdfReader", lambda stream: _FakePdfReader([_FakePdfPage("Cite as 23 Vet.App. 1")])
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/pdf"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/case.pdf?download=1")
    assert details["citation"] == "23 Vet.App. 1"


def test_fetch_case_details_pdf_uppercase_extension(monkeypatch):
    monkeypatch.setattr(
        "pypdf.PdfReader", lambda stream: _FakePdfReader([_FakePdfPage("Cite as 23 Vet.App. 1")])
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/pdf"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/case.PDF")
    assert details["citation"] == "23 Vet.App. 1"


def test_fetch_case_details_encoding_falls_back_to_utf8(monkeypatch):
    # A response without an encoding attribute uses utf-8.
    class NoEncodingResponse(FakePageResponse):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            del self.encoding

        def iter_content(self, chunk_size=8192):
            yield b"Decided: March 15, 2011."  # bytes already utf-8

    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: NoEncodingResponse(
            headers={"Content-Type": "text/plain"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/no-encoding.txt")
    assert details["decision_date"] == "2011-03-15"


def test_fetch_case_details_decode_replaces_bad_bytes(monkeypatch):
    class Latin1Response(FakePageResponse):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.encoding = "latin-1"
            self.content = b"Decided: March 15, 2011. \xff\xfe"

    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: Latin1Response(
            headers={"Content-Type": "text/plain"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/latin1.txt")
    assert details["decision_date"] == "2011-03-15"  # bad bytes replaced, not fatal


def test_fetch_case_details_plain_text_path_taken_without_txt_extension(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text="Citation Nr: 2100634", headers={"Content-Type": "text/plain"}
        ),
    )

    details = fetch_case_details("https://www.va.gov/decision/2100634")
    assert details["citation"] == "BVA Citation Nr. 2100634"


def test_fetch_case_details_title_is_used_when_present(monkeypatch):
    html = "<html><head><title>Doe v. McDonough, 23 Vet.App. 1 (2011)</title></head><body><p>No citations here.</p></body></html>"
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text=html, headers={"Content-Type": "text/html"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/title-only.html")
    assert details["citation"] == "23 Vet.App. 1"


def test_fetch_case_details_meta_description_holding_fallback(monkeypatch):
    html = (
        "<html><head>"
        '<meta name="description" content="The Board must provide adequate reasons and bases.">'
        "</head><body><p>Procedural history only.</p></body></html>"
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text=html, headers={"Content-Type": "text/html"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/meta-only.html")
    assert details["holding"] == "The Board must provide adequate reasons and bases."


def test_fetch_case_details_og_description_holding_fallback(monkeypatch):
    html = (
        "<html><head>"
        '<meta property="og:description" content="The Court holds that the Board erred.">'
        "</head><body><p>Procedural history only.</p></body></html>"
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text=html, headers={"Content-Type": "text/html"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/og-meta.html")
    assert details["holding"] == "The Court holds that the Board erred."


def test_normalize_statute_value_strips_uppercase_parenthetical():
    # The trailing-parenthetical strip matches both cases; a lowercase-only
    # character class would leave "(IV)" behind.
    assert extract_statutes("See 38 U.S.C. § 1114 (IV) (West).") == ["38 U.S.C. § 1114"]


def test_extract_pdf_text_passes_real_stream_to_reader(monkeypatch):
    seen: dict[str, object] = {}

    def recording_reader(stream):
        seen["stream"] = stream
        return _FakePdfReader([_FakePdfPage("A")])

    monkeypatch.setattr("pypdf.PdfReader", recording_reader)

    assert _extract_pdf_text(b"pdf") == "A"
    assert seen["stream"].getvalue() == b"pdf"  # real content bytes, not empty/None


def test_extract_pdf_text_corrupt_logs_exception_text(monkeypatch, caplog):
    def crashing_reader(stream):
        raise ValueError("corrupt pdf payload")

    monkeypatch.setattr("pypdf.PdfReader", crashing_reader)

    assert _extract_pdf_text(b"junk") == ""
    assert any("corrupt pdf payload" in r.message for r in caplog.records)
    assert any(not r.message.startswith("XX") for r in caplog.records)


def test_fetch_case_details_without_content_type_header(monkeypatch):
    # No Content-Type: the header default must be "" so the HTML path still runs.
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text="<html><body><p>No holding sentence here.</p></body></html>", headers={}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/no-content-type.html")
    assert details["holding"] == ""


def test_fetch_case_details_pdf_by_url_with_query_string_no_pdf_header(monkeypatch):
    # PDF detected from the URL alone (no application/pdf content-type): the
    # url_path split must strip the query string and keep the .pdf suffix.
    monkeypatch.setattr(
        "pypdf.PdfReader", lambda stream: _FakePdfReader([_FakePdfPage("Cite as 23 Vet.App. 1")])
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/octet-stream"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/case.pdf?download=1")
    assert details["citation"] == "23 Vet.App. 1"


def test_fetch_case_details_latin1_holding_preserved(monkeypatch):
    holding = "The Board must provide adequate reasons and bases for caf\u00e9 decisions."
    content = f"We hold that {holding}.".encode("latin-1")

    class Latin1Response(FakePageResponse):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.encoding = "latin-1"
            self.content = content

    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: Latin1Response(
            headers={"Content-Type": "text/plain"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/latin1-holding.txt")
    assert details["holding"] == f"We hold that {holding}."


def test_fetch_case_details_invalid_utf8_under_utf8_replaced(monkeypatch):
    class Utf8Response(FakePageResponse):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.encoding = "utf-8"
            self.content = b"Decided: March 15, 2011. \xff\xfe"

    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: Utf8Response(
            headers={"Content-Type": "text/plain"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/bad-utf8.txt")
    assert details["decision_date"] == "2011-03-15"  # replaced, not fatal


def test_fetch_case_details_text_plain_without_txt_extension_and_tags(monkeypatch):
    # Content-Type text/plain alone takes the text path; HTML-tagged docket
    # numbers must NOT be parsed as HTML (which would strip the tags and find
    # the docket).
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text="DOCKET NO. <b>19-4433</b> and Citation Nr: 2100634",
            headers={"Content-Type": "text/plain"},
        ),
    )

    details = fetch_case_details("https://www.va.gov/decision/2100634")
    assert details["docket"] == ""  # raw text path: tags are literal text
    assert details["citation"] == "BVA Citation Nr. 2100634"


def test_fetch_case_details_txt_extension_triggers_text_path_without_content_type(monkeypatch):
    # A .txt URL takes the text path even when the content-type is not
    # text/plain; HTML-tagged dockets must not be parsed as HTML.
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text="DOCKET NO. <b>19-4433</b> and Citation Nr: 2100634",
            headers={"Content-Type": "text/html"},
        ),
    )

    details = fetch_case_details("https://www.va.gov/decision/2100634.txt")
    assert details["docket"] == ""  # raw text path via extension


def test_fetch_case_details_meta_description_after_charset_meta(monkeypatch):
    # The description meta must be found by attrs, not by "first meta" — a
    # charset meta before it must not win.
    html = (
        "<html><head><meta charset=\"utf-8\">"
        '<meta name="description" content="The Board must provide adequate reasons and bases.">'
        "</head><body><p>Procedural history only.</p></body></html>"
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text=html, headers={"Content-Type": "text/html"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/meta-charset-first.html")
    assert details["holding"] == "The Board must provide adequate reasons and bases."


def test_fetch_case_details_og_description_after_charset_meta(monkeypatch):
    html = (
        "<html><head><meta charset=\"utf-8\">"
        '<meta property="og:description" content="The Court holds that the Board erred.">'
        "</head><body><p>Procedural history only.</p></body></html>"
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text=html, headers={"Content-Type": "text/html"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/og-charset-first.html")
    assert details["holding"] == "The Court holds that the Board erred."


def test_fetch_case_details_meta_with_empty_content(monkeypatch):
    html = (
        "<html><head>"
        '<meta name="description" content="">'
        "</head><body><p>Procedural history only.</p></body></html>"
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text=html, headers={"Content-Type": "text/html"}
        ),
    )

    details = fetch_case_details("https://uscourts.cavc.gov/empty-meta.html")
    assert details["holding"] == ""  # empty meta content stays empty


def test_fetch_case_details_rejects_oversized_body(monkeypatch):
    monkeypatch.setenv("MAX_FETCH_BYTES", "10")
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"",
            headers={"Content-Type": "application/pdf", "Content-Length": "10"},
        ),
    )

    # Content-Length exactly at the cap is allowed (the > mutant would reject).
    fetch_case_details("https://uscourts.cavc.gov/at-cap.pdf")


def test_fetch_case_details_rejects_undeclared_oversized_body(monkeypatch):
    monkeypatch.setenv("MAX_FETCH_BYTES", "10")
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"x" * 11,
            headers={"Content-Type": "application/pdf"},
        ),
    )

    with pytest.raises(FetchError, match="cap"):
        fetch_case_details("https://uscourts.cavc.gov/huge.pdf")


def test_fetch_full_text_from_html_returns_whole_body(monkeypatch):
    html = (
        "<html><head><title>Fountain v. McDonald</title></head>"
        "<body><p>First paragraph of the decision.</p>"
        "<p>Second paragraph, far beyond the snippet.</p></body></html>"
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text=html, headers={"Content-Type": "text/html"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/fountain.html")

    assert "First paragraph of the decision." in text
    assert "Second paragraph, far beyond the snippet." in text


def test_fetch_full_text_from_plain_text_returns_verbatim(monkeypatch):
    bva_text = (
        "Citation Nr: A25049742\n\nORDER\n\nEntitlement to service connection "
        "for tinnitus is granted.\n"
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text=bva_text, headers={"Content-Type": "text/plain"}
        ),
    )

    text = fetch_full_text("https://www.va.gov/vetapp25/Files6/A25049742.txt")

    assert text == bva_text


def test_fetch_full_text_from_pdf_reads_all_pages(monkeypatch):
    monkeypatch.setattr(
        "pypdf.PdfReader",
        lambda stream: _FakePdfReader([_FakePdfPage("Page one"), _FakePdfPage("Page two")]),
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/pdf"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/full.pdf")

    assert text == "Page one Page two"


def test_fetch_full_text_pdf_respects_max_pages(monkeypatch):
    monkeypatch.setattr(
        "pypdf.PdfReader",
        lambda stream: _FakePdfReader([_FakePdfPage(str(i)) for i in range(4)]),
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/pdf"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/full.pdf", max_pages=2)

    assert text == "0 1"


def test_fetch_full_text_raises_on_network_failure(monkeypatch):
    def failing_get(url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr("va_legal_agent.fetch.cffi_requests.get", failing_get)

    with pytest.raises(FetchError, match="Failed to fetch"):
        fetch_full_text("https://uscourts.cavc.gov/unreachable")


def test_fetch_full_text_rejects_oversized_body(monkeypatch):
    monkeypatch.setenv("MAX_FETCH_BYTES", "10")
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"x" * 11,
            headers={"Content-Type": "text/plain"},
        ),
    )

    with pytest.raises(FetchError, match="cap"):
        fetch_full_text("https://uscourts.cavc.gov/huge.txt")


def test_fetch_full_text_requests_exact_args(monkeypatch):
    """The request carries url, User-Agent, timeout, and stream=True."""
    seen: dict[str, object] = {}

    def recording_get(url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["timeout"] = timeout
        seen["stream"] = stream
        seen["impersonate"] = impersonate
        seen["allow_redirects"] = allow_redirects
        return FakePageResponse(
            text="<html><body><p>No holding sentence here.</p></body></html>",
            headers={"Content-Type": "text/html"},
        )

    monkeypatch.setattr("va_legal_agent.fetch.cffi_requests.get", recording_get)

    fetch_full_text("https://uscourts.cavc.gov/exact.html", timeout=42)

    assert seen["url"] == "https://uscourts.cavc.gov/exact.html"
    assert seen["headers"] is None
    assert seen["timeout"] == 42
    assert seen["stream"] is True
    assert seen["impersonate"] == "chrome"
    assert seen["allow_redirects"] is False


def test_fetch_full_text_timeout_defaults_from_settings(monkeypatch):
    """Without an explicit timeout, REQUEST_TIMEOUT_SECONDS supplies it."""
    seen: dict[str, object] = {}

    def recording_get(url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None):
        seen["timeout"] = timeout
        return FakePageResponse(
            text="<html><body><p>x</p></body></html>", headers={"Content-Type": "text/html"}
        )

    monkeypatch.setattr("va_legal_agent.fetch.cffi_requests.get", recording_get)

    fetch_full_text("https://uscourts.cavc.gov/default-timeout.html")

    assert seen["timeout"] == 20


def test_fetch_full_text_pdf_by_content_type_without_extension(monkeypatch):
    """PDF content-type alone routes to PDF extraction (or/and + string mutants)."""
    seen: dict[str, object] = {}

    def recording_reader(stream):
        seen["stream"] = stream
        return _FakePdfReader([_FakePdfPage("Cite as 23 Vet.App. 1")])

    monkeypatch.setattr("pypdf.PdfReader", recording_reader)
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/pdf"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/no-ext")

    assert text == "Cite as 23 Vet.App. 1"
    assert seen["stream"].getvalue() == b"pdf-bytes"  # real body passed to the reader


def test_fetch_full_text_content_type_key_exact(monkeypatch):
    """The header is read with the exact 'Content-Type' key (key mutants)."""
    monkeypatch.setattr(
        "pypdf.PdfReader", lambda stream: _FakePdfReader([_FakePdfPage("Cite as 23 Vet.App. 1")])
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/pdf"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/key.html")
    assert text == "Cite as 23 Vet.App. 1"


def test_fetch_full_text_without_content_type_header_takes_html_path(monkeypatch):
    """No Content-Type header: the default must be '' so HTML parsing still runs."""
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text="<html><body><p>First paragraph.</p></body></html>", headers={}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/no-content-type.html")

    assert "First paragraph." in text


def test_fetch_full_text_query_string_pdf(monkeypatch):
    """A query string after the .pdf suffix does not defeat PDF detection."""
    monkeypatch.setattr(
        "pypdf.PdfReader", lambda stream: _FakePdfReader([_FakePdfPage("Cite as 23 Vet.App. 1")])
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/pdf"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/case.pdf?download=1")
    assert text == "Cite as 23 Vet.App. 1"


def test_fetch_full_text_lowercases_url_for_pdf_detection(monkeypatch):
    """A .PDF uppercase extension is detected after lowercasing the URL path."""
    monkeypatch.setattr(
        "pypdf.PdfReader", lambda stream: _FakePdfReader([_FakePdfPage("Cite as 23 Vet.App. 1")])
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/pdf"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/case.PDF")
    assert text == "Cite as 23 Vet.App. 1"


def test_fetch_full_text_uses_response_encoding(monkeypatch):
    """A response's encoding attribute is honored (not always utf-8)."""
    class Latin1Response(FakePageResponse):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.encoding = "latin-1"
            self.content = "caf\u00e9 decision text".encode("latin-1")

    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: Latin1Response(
            headers={"Content-Type": "text/plain"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/latin1.txt")

    assert "caf\u00e9 decision text" in text  # decoded with the response's encoding


def test_fetch_full_text_encoding_falls_back_to_utf8(monkeypatch):
    """A response without an encoding attribute uses utf-8."""
    class NoEncodingResponse(FakePageResponse):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            del self.encoding

        def iter_content(self, chunk_size=8192):
            yield "plain text body".encode("utf-8")

    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: NoEncodingResponse(
            headers={"Content-Type": "text/plain"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/no-encoding.txt")
    assert "plain text body" in text


def test_fetch_full_text_decode_replaces_bad_bytes(monkeypatch):
    class BadBytesResponse(FakePageResponse):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.content = b"plain body \xff\xfe"

    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: BadBytesResponse(
            headers={"Content-Type": "text/plain"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/bad-bytes.txt")
    assert "plain body" in text  # bad bytes replaced, not fatal


def test_fetch_full_text_plain_text_without_txt_extension(monkeypatch):
    """Content-Type text/plain alone takes the text path (or/and mutant)."""
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text="Citation Nr: 2100634", headers={"Content-Type": "text/plain"}
        ),
    )

    text = fetch_full_text("https://www.va.gov/decision/2100634")
    assert text == "Citation Nr: 2100634"


def test_fetch_full_text_plain_text_without_content_type_via_txt_extension(monkeypatch):
    """A .txt URL takes the text path even without a text/plain content-type."""
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text="Citation Nr: 2100634", headers={"Content-Type": "text/html"}
        ),
    )

    text = fetch_full_text("https://www.va.gov/decision/2100634.txt")
    assert text == "Citation Nr: 2100634"


def test_fetch_full_text_html_joins_blocks_with_single_space_stripped(monkeypatch):
    html = "<html><body><p>  First paragraph.  </p><p>Second paragraph.</p></body></html>"
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text=html, headers={"Content-Type": "text/html"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/joined.html")

    assert "First paragraph." in text
    assert "Second paragraph." in text
    assert "  " not in text  # strip=True and single-space separator


def test_fetch_full_text_html_multiple_blocks_single_space_separator(monkeypatch):
    """get_text uses a single space separator (not XX XX or a dropped one)."""
    html = "<html><body><p>A</p><p>B</p></body></html>"
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text=html, headers={"Content-Type": "text/html"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/blocks.html")
    assert text == "A B"


def test_fetch_full_text_pdf_detected_by_url_query_string_only(monkeypatch):
    """A .pdf URL with a query string is PDF even without a PDF content-type."""
    monkeypatch.setattr(
        "pypdf.PdfReader", lambda stream: _FakePdfReader([_FakePdfPage("Cite as 23 Vet.App. 1")])
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/octet-stream"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/case.pdf?download=1")
    assert text == "Cite as 23 Vet.App. 1"


def test_fetch_full_text_pdf_detected_by_url_extension_only(monkeypatch):
    """A .pdf URL alone is PDF (endswith .pdf, not a decorated variant)."""
    monkeypatch.setattr(
        "pypdf.PdfReader", lambda stream: _FakePdfReader([_FakePdfPage("Cite as 23 Vet.App. 1")])
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/octet-stream"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/case.pdf")
    assert text == "Cite as 23 Vet.App. 1"


def test_fetch_full_text_pdf_detected_by_uppercase_url_extension(monkeypatch):
    """A .PDF URL is detected after lowercasing (upper() would miss it)."""
    monkeypatch.setattr(
        "pypdf.PdfReader", lambda stream: _FakePdfReader([_FakePdfPage("Cite as 23 Vet.App. 1")])
    )
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            content=b"pdf-bytes", headers={"Content-Type": "application/octet-stream"}
        ),
    )

    text = fetch_full_text("https://uscourts.cavc.gov/case.PDF")
    assert text == "Cite as 23 Vet.App. 1"


def test_fetch_full_text_text_plain_path_keeps_raw_tags(monkeypatch):
    """The text/plain path returns the body verbatim, not HTML-parsed."""
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text="DOCKET NO. <b>19-4433</b> and Citation Nr: 2100634",
            headers={"Content-Type": "text/plain"},
        ),
    )

    text = fetch_full_text("https://www.va.gov/decision/2100634")

    # The raw text (with the literal <b> tags) is returned, not the parsed
    # text -- so the or/and, key-case, and content-type-string mutants that
    # fall through to HTML parsing produce a different result.
    assert text == "DOCKET NO. <b>19-4433</b> and Citation Nr: 2100634"


def test_fetch_full_text_txt_extension_path_keeps_raw_tags(monkeypatch):
    """A .txt URL takes the text path even with a non-text/plain content-type."""
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text="DOCKET NO. <b>19-4433</b> and Citation Nr: 2100634",
            headers={"Content-Type": "text/html"},
        ),
    )

    text = fetch_full_text("https://www.va.gov/decision/2100634.txt")

    assert text == "DOCKET NO. <b>19-4433</b> and Citation Nr: 2100634"


def test_fetch_full_text_uppercase_txt_extension(monkeypatch):
    """A .TXT URL is detected after lowercasing (the .TXT mutant would miss it)."""
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda url, headers=None, timeout=None, stream=None, impersonate=None, proxies=None, allow_redirects=None: FakePageResponse(
            text="Citation Nr: 2100634", headers={"Content-Type": "text/html"}
        ),
    )

    text = fetch_full_text("https://www.va.gov/decision/2100634.TXT")
    assert text == "Citation Nr: 2100634"


# ── Disposition/scoping accuracy regressions ────────────────────────────────


def test_extract_outcome_reads_only_the_order_disposition():
    """A granted claim must not be reported as "granted and denied" because the
    body recites the earlier regional-office denial, or because the ORDER also
    denies a collateral motion."""
    text = (
        "The Veteran served on active duty from 2001 to 2005. In a February 2019 "
        "rating decision, the regional office denied service connection for "
        "tinnitus. The Board finds that the denial was proper as to the earlier "
        "effective date but the claim is granted in part, and the matter is "
        "remanded for further development.\n\n"
        "ORDER\n\n"
        "Entitlement to service connection for tinnitus is granted.\n"
        "The motion for an extension of time is denied.\n"
    )

    assert extract_outcome(text) == "granted"

    # Without an ORDER heading, the last claim-disposition sentence still wins
    # over the recited denial.
    assert extract_outcome(
        "The regional office denied the claim. The claim is granted."
    ) == "granted"


def test_extract_outcome_reads_disposition_on_the_order_heading_line():
    """A disposition on the heading's own line is inside the ORDER block.

    ``ORDER: Granted.`` must be recognised without a line break after the
    heading; otherwise the fallback reports the earlier recited denial.
    """
    assert extract_outcome(
        "The claim for service connection was denied by the regional office.\n\n"
        "ORDER: Granted."
    ) == "granted"

    # Multi-word headings accept same-line content too.
    assert extract_outcome(
        "BACKGROUND: service connection was denied.\n\n"
        "CONCLUSIONS OF LAW: Granted."
    ) == "granted"


def test_extract_precedential_status_reads_the_header_not_the_body():
    """A published opinion that discusses an unpublished order stays precedential;
    the same marker in the header still designates the decision non-precedential."""
    body = (
        "The Court holds that the Board erred in weighing the evidence. "
        + "The Board's discussion continues. " * 120
    )
    published = (
        "SMITH v. WILKIE, 30 Vet.App. 123 (2018)\nARGUMENT\n"
        + body
        + "\nThe Board relied on an unpublished order in XYZ v. Shinseki."
    )

    assert len(published) > 1500  # the marker is beyond the header window
    assert extract_precedential_status(published) is True
    assert extract_precedential_status("NOT FOR PUBLICATION\n" + body) is False


def test_extract_citation_falls_back_to_the_body_beyond_the_header():
    """A citation past the header window is still found when the header has none."""
    text = "X" * 2100 + "\nSee Smith v. Wilkie, 30 Vet.App. 123 (2018)."
    assert extract_citation(text) == "30 Vet.App. 123"
    # The body pass tries the remaining reporter patterns and exits cleanly.
    assert extract_citation("X" * 2100 + "\n590 F.4th 1300") == "590 F.4th 1300"
    assert extract_citation("X" * 2100 + "\nno reporters here") == ""


def test_extract_outcome_collapses_duplicate_signals():
    """Repeated disposition sentences report the signal once."""
    text = "ORDER\nThe claim is granted. The claim is granted."
    assert extract_outcome(text) == "granted"


def test_extract_outcome_ignores_trailing_non_disposition_claim_sentence():
    """A later sentence that names the claim but decides nothing is not the disposition."""
    text = "The claim is granted. The Board's decision discusses benefits."
    assert extract_outcome(text) == "granted"


def test_extract_statutes_keeps_cfr_sections_under_the_cap():
    """A long U.S.C. list must not crowd every C.F.R. regulation out."""
    result = extract_statutes(
        "38 U.S.C. §§ 1110, 1131, 1151, 5103, 5107, 7104; "
        "38 C.F.R. §§ 3.303, 3.304"
    )
    assert result == [
        "38 U.S.C. § 1110",
        "38 U.S.C. § 1131",
        "38 U.S.C. § 1151",
        "38 U.S.C. § 5103",
        "38 C.F.R. § 3.303",
        "38 C.F.R. § 3.304",
    ]
    # A C.F.R.-only overflow still fills the cap with regulations.
    assert len(
        extract_statutes("38 C.F.R. §§ 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8")
    ) == 6


def test_extract_statutes_matches_plural_section_citations():
    """A plural §§ citation names every listed section, not just the first."""
    assert extract_statutes("38 U.S.C. §§ 1110, 1131") == [
        "38 U.S.C. § 1110",
        "38 U.S.C. § 1131",
    ]
    assert extract_statutes("38 C.F.R. §§ 3.303, 3.304") == [
        "38 C.F.R. § 3.303",
        "38 C.F.R. § 3.304",
    ]


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        (
            "38 U.S.C. §§ 1110, 1131, 1151; 38 C.F.R. §§ 3.303, 3.304, 3.305",
            6,
            [
                "38 U.S.C. § 1110",
                "38 U.S.C. § 1131",
                "38 U.S.C. § 1151",
                "38 C.F.R. § 3.303",
                "38 C.F.R. § 3.304",
                "38 C.F.R. § 3.305",
            ],
        ),
        (
            "38 U.S.C. §§ 1110, 1131, 1151, 5103, 5107, 7104; 38 C.F.R. § 3.303",
            6,
            [
                "38 U.S.C. § 1110",
                "38 U.S.C. § 1131",
                "38 U.S.C. § 1151",
                "38 U.S.C. § 5103",
                "38 U.S.C. § 5107",
                "38 C.F.R. § 3.303",
            ],
        ),
        (
            "38 U.S.C. §§ 1110, 1131; 38 C.F.R. §§ 3.303, 3.304",
            3,
            [
                "38 U.S.C. § 1110",
                "38 U.S.C. § 1131",
                "38 C.F.R. § 3.303",
            ],
        ),
        ("38 U.S.C. § 1110; 38 C.F.R. § 3.303", 1, ["38 U.S.C. § 1110"]),
        (
            "38 U.S.C. § 1110; 38 C.F.R. § 3.303",
            2,
            ["38 U.S.C. § 1110", "38 C.F.R. § 3.303"],
        ),
        (
            "38 U.S.C. §§ 1110, 1131; 38 C.F.R. § 3.303",
            2,
            ["38 U.S.C. § 1110", "38 C.F.R. § 3.303"],
        ),
        (
            "38 U.S.C. §§ 1110, 1131, 1151; 38 C.F.R. §§ 3.303, 3.304",
            4,
            [
                "38 U.S.C. § 1110",
                "38 U.S.C. § 1131",
                "38 U.S.C. § 1151",
                "38 C.F.R. § 3.303",
            ],
        ),
    ],
)
def test_extract_statutes_preserves_both_families_at_each_cap(text, limit, expected):
    """Neither family may crowd the other out at any shared-cap boundary."""
    assert extract_statutes(text, limit) == expected


def test_extract_citation_prefers_own_citation_over_quoted_case():
    """The decision's own header citation wins over a case quoted in the body."""
    bva = (
        "Citation Nr: 2100634\nDecision Date: 01/14/2021\n\nORDER\n"
        "The claim is granted.\n"
        "See Smith v. Wilkie, 30 Vet.App. 123 (2018).\n"
    )
    assert extract_citation(bva) == "BVA Citation Nr. 2100634"

    # A CAVC opinion's caption cite likewise beats a case quoted later.
    cavc = (
        "SMITH v. WILKIE\n30 Vet.App. 123\n\n"
        "The Board relied on Jones v. Shinseki, 25 Vet.App. 45.\n"
    )
    assert extract_citation(cavc) == "30 Vet.App. 123"

@pytest.mark.parametrize("fetcher", [fetch_full_text, fetch_case_details])
def test_fetch_rejects_non_http_url_before_network(fetcher, monkeypatch):
    """Defence in depth: no non-HTTP scheme reaches the HTTP client."""
    called = []
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda *args, **kwargs: called.append(1),
    )

    with pytest.raises(FetchError, match="non-HTTP"):
        fetcher("javascript:alert(1)")

    assert called == []


def _addr_info(address: str, port: int = 443):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]


class RedirectResponse:
    def __init__(self, location: str | None, status_code: int = 302):
        self.status_code = status_code
        self.headers = {} if location is None else {"Location": location}
        self.url = "https://public.example/start"
        self.closed = False

    def close(self):
        self.closed = True

    def raise_for_status(self):
        raise AssertionError("a redirect response must not be treated as final")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.1/internal",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
    ],
)
@pytest.mark.parametrize("fetcher", [fetch_case_details, fetch_full_text])
def test_fetch_rejects_private_destinations_before_network(fetcher, url, monkeypatch):
    called = []
    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda *args, **kwargs: called.append(args[0]),
    )

    with pytest.raises(FetchError, match="non-public address"):
        fetcher(url)

    assert called == []


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "172.16.0.1",
        "192.0.2.1",
        "192.168.1.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "ff00::1",
        "::ffff:127.0.0.1",
        "2002:7f00:1::",
    ],
)
def test_validate_fetch_url_rejects_non_public_ranges(address, monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.fetch.socket.getaddrinfo",
        lambda *args, **kwargs: _addr_info(address),
    )

    with pytest.raises(FetchError, match="non-public address"):
        _validate_fetch_url("https://public.example/case")


@pytest.mark.parametrize(
    "address",
    ["93.184.216.34", "2606:4700:4700::1111"],
)
def test_validate_fetch_url_allows_global_addresses(address, monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.fetch.socket.getaddrinfo",
        lambda *args, **kwargs: _addr_info(address),
    )

    _validate_fetch_url("https://public.example/case")


@pytest.mark.parametrize(
    "answers",
    [
        ["10.0.0.1"],
        ["93.184.216.34", "10.0.0.1"],
    ],
)
def test_validate_fetch_url_rejects_any_private_dns_answer(answers, monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.fetch.socket.getaddrinfo",
        lambda *args, **kwargs: [
            info for address in answers for info in _addr_info(address)
        ],
    )

    with pytest.raises(FetchError, match="non-public address"):
        _validate_fetch_url("https://public.example/case")


def test_validate_fetch_url_rejects_dns_failure(monkeypatch):
    def failing_getaddrinfo(*args, **kwargs):
        raise socket.gaierror("name resolution failed")

    monkeypatch.setattr("va_legal_agent.fetch.socket.getaddrinfo", failing_getaddrinfo)

    with pytest.raises(FetchError, match="Could not resolve host"):
        _validate_fetch_url("https://public.example/case")


def test_validate_fetch_url_rejects_empty_dns_answer(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.fetch.socket.getaddrinfo",
        lambda *args, **kwargs: [],
    )

    with pytest.raises(FetchError, match="Could not resolve host"):
        _validate_fetch_url("https://public.example/case")


def test_validate_fetch_url_rejects_unparseable_dns_answer(monkeypatch):
    monkeypatch.setattr(
        "va_legal_agent.fetch.socket.getaddrinfo",
        lambda *args, **kwargs: _addr_info("not-an-ip"),
    )

    with pytest.raises(FetchError, match="unparseable address"):
        _validate_fetch_url("https://public.example/case")


def test_validate_fetch_url_rejects_malformed_url():
    with pytest.raises(FetchError, match="malformed"):
        _validate_fetch_url("https://public.example:bad/case")
    with pytest.raises(FetchError, match="malformed"):
        _validate_fetch_url("https://[::1/case")
    with pytest.raises(FetchError, match="without a host"):
        _validate_fetch_url("http://:80/case")


def test_validate_fetch_url_uses_scheme_default_ports(monkeypatch):
    ports = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        ports.append(port)
        return _addr_info(_PUBLIC_TEST_ADDRESS, port)

    monkeypatch.setattr("va_legal_agent.fetch.socket.getaddrinfo", fake_getaddrinfo)

    _validate_fetch_url("https://public.example:8443/case")
    _validate_fetch_url("https://public.example/case")
    _validate_fetch_url("http://public.example/case")

    assert ports == [8443, 443, 80]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_fetch_follows_public_redirect_for_each_redirect_status(status, monkeypatch):
    responses = [
        RedirectResponse("/final", status_code=status),
        FakePageResponse(text=HTML_PAGE, headers={"Content-Type": "text/html"}),
    ]
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    monkeypatch.setattr("va_legal_agent.fetch.cffi_requests.get", fake_get)

    details = fetch_case_details("https://public.example/start")

    assert [url for url, _ in calls] == [
        "https://public.example/start",
        "https://public.example/final",
    ]
    assert all(kwargs["allow_redirects"] is False for _, kwargs in calls)
    assert details["citation"] == "23 Vet.App. 1"


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/secret",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_fetch_blocks_redirect_to_private_address_before_second_request(target, monkeypatch):
    redirect = RedirectResponse(target)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return redirect

    monkeypatch.setattr("va_legal_agent.fetch.cffi_requests.get", fake_get)

    with pytest.raises(FetchError, match="non-public address"):
        fetch_case_details("https://public.example/start")

    assert calls == ["https://public.example/start"]
    assert redirect.closed is True


def test_fetch_blocks_redirect_to_dns_name_resolving_private(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "internal.example":
            return _addr_info("10.0.0.1", port)
        return _addr_info(_PUBLIC_TEST_ADDRESS, port)

    monkeypatch.setattr("va_legal_agent.fetch.socket.getaddrinfo", fake_getaddrinfo)
    redirect = RedirectResponse("https://internal.example/secret")
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return redirect

    monkeypatch.setattr("va_legal_agent.fetch.cffi_requests.get", fake_get)

    with pytest.raises(FetchError, match="non-public address"):
        fetch_full_text("https://public.example/start")

    assert calls == ["https://public.example/start"]


@pytest.mark.parametrize(
    ("location", "message"),
    [
        (None, "no Location"),
        ("file:///etc/passwd", "non-HTTP"),
    ],
)
def test_fetch_rejects_invalid_redirect_targets(location, message, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return RedirectResponse(location)

    monkeypatch.setattr("va_legal_agent.fetch.cffi_requests.get", fake_get)

    with pytest.raises(FetchError, match=message):
        fetch_case_details("https://public.example/start")

    assert calls == ["https://public.example/start"]


def test_fetch_rejects_too_many_redirects(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return RedirectResponse("/next")

    monkeypatch.setattr("va_legal_agent.fetch.cffi_requests.get", fake_get)

    with pytest.raises(FetchError, match="Exceeded 5 redirects"):
        fetch_case_details("https://public.example/start")

    assert len(calls) == 6


def test_fetch_full_text_follows_public_redirect(monkeypatch):
    responses = [
        RedirectResponse("https://public.example/opinion", status_code=302),
        FakePageResponse(text="Full opinion body", headers={"Content-Type": "text/plain"}),
    ]

    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda *args, **kwargs: responses.pop(0),
    )

    text = fetch_full_text("https://public.example/start")

    assert text == "Full opinion body"


def test_fetch_detects_pdf_from_redirected_path(monkeypatch):
    monkeypatch.setattr(
        "pypdf.PdfReader",
        lambda stream: _FakePdfReader([_FakePdfPage("Cite as 23 Vet.App. 1")]),
    )
    responses = [
        RedirectResponse("https://public.example/opinion.pdf", status_code=301),
        FakePageResponse(
            content=b"pdf-bytes",
            headers={"Content-Type": "application/octet-stream"},
        ),
    ]

    monkeypatch.setattr(
        "va_legal_agent.fetch.cffi_requests.get",
        lambda *args, **kwargs: responses.pop(0),
    )

    details = fetch_case_details("https://public.example/start")

    assert details["citation"] == "23 Vet.App. 1"
