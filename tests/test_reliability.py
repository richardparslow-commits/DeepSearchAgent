"""Tests for source-reliability classification (va_legal_agent.reliability)."""

from va_legal_agent.reliability import (
    AUTHORITY_AGENCY_GUIDANCE,
    AUTHORITY_BINDING,
    AUTHORITY_PERSUASIVE,
    AUTHORITY_SECONDARY,
    AUTHORITY_UNVERIFIED,
    RELIABILITY_OFFICIAL,
    RELIABILITY_SECONDARY,
    RELIABILITY_UNKNOWN,
    authority_label,
    classify_authority_tier,
    classify_source,
    summarize_authority_reliability,
)


def test_classify_source_unknown_for_empty_urls():
    assert classify_source("") == RELIABILITY_UNKNOWN
    assert classify_source(None) == RELIABILITY_UNKNOWN
    assert classify_source("   ") == RELIABILITY_UNKNOWN


def test_classify_source_official_for_primary_domains():
    for url in (
        "https://www.courtlistener.com/opinion/123/x/",
        "https://uscourts.cavc.gov/opinions/1",
        "https://cafc.uscourts.gov/opinions/2",
        "https://caafc.uscourts.gov/opinions/3",
        "https://www.supremecourt.gov/opinions/21-123",
        "https://www.bva.va.gov/vetapp25/Files6/A1.txt",
        "https://www.va.gov/foo",
    ):
        assert classify_source(url) == RELIABILITY_OFFICIAL


def test_classify_source_secondary_for_other_domains():
    assert classify_source("https://example.com/law-firm/summary") == RELIABILITY_SECONDARY
    assert classify_source("https://news.example.com/story") == RELIABILITY_SECONDARY


def test_classify_source_is_case_insensitive():
    assert classify_source("HTTPS://WWW.COURTLISTENER.COM/OPINION/1/") == RELIABILITY_OFFICIAL


def test_classify_source_rejects_substring_lookalikes():
    # Domains that merely CONTAIN an official domain as a substring (typo,
    # suffix, or third-party host) must not be stamped official.
    for url in (
        "https://va.gov.bad.com/x",
        "https://evilva.gov/x",
        "https://notva.gov/x",
        "https://courtlistener.com.evil.net/x",
        "https://www.supremecourt.gov.evil.com/x",
        "https://bva.va.gov.attacker.io/x",
    ):
        assert classify_source(url) == RELIABILITY_SECONDARY, url


def test_classify_source_accepts_subdomains_of_official_domains():
    # Any subdomain of an official domain is still primary authority.
    for url in (
        "https://opinions.courtlistener.com/x",
        "https://deep.sub.uscourts.cavc.gov/x",
        "https://api.cafc.uscourts.gov/x",
        "https://sub.va.gov/x",
    ):
        assert classify_source(url) == RELIABILITY_OFFICIAL, url


def test_classify_source_handles_missing_scheme_and_ports():
    # Scheme-less URLs (as providers may return) and explicit ports still
    # resolve to the correct host.
    assert classify_source("uscourts.cavc.gov/opinions/1") == RELIABILITY_OFFICIAL
    assert classify_source("va.gov/foo") == RELIABILITY_OFFICIAL
    assert classify_source("example.com/foo") == RELIABILITY_SECONDARY
    assert classify_source("https://www.va.gov:8443/x") == RELIABILITY_OFFICIAL


def test_classify_source_strips_userinfo():
    # ``urlparse().hostname`` strips userinfo, so credentials never leak into
    # the host comparison.
    assert classify_source("https://user:pass@va.gov/x") == RELIABILITY_OFFICIAL
    assert classify_source("https://user@example.com/x") == RELIABILITY_SECONDARY


def test_authority_tier_requires_official_source_for_binding_status():
    assert classify_authority_tier(
        "https://uscourts.cavc.gov/opinions/1",
        "Court of Appeals for Veterans Claims",
        True,
    ) == (AUTHORITY_BINDING, True)
    # The same court label on a secondary host is never binding.
    assert classify_authority_tier(
        "https://example.com/summary",
        "Court of Appeals for Veterans Claims",
        True,
    ) == (AUTHORITY_SECONDARY, False)
    assert classify_authority_tier(
        "", "Court of Appeals for Veterans Claims", True
    ) == (AUTHORITY_UNVERIFIED, False)


def test_authority_tier_distinguishes_persuasive_and_agency_material():
    assert classify_authority_tier(
        "https://www.courtlistener.com/opinion/1",
        "Board of Veterans' Appeals",
        None,
    ) == (AUTHORITY_PERSUASIVE, True)
    assert classify_authority_tier(
        "https://www.courtlistener.com/opinion/2",
        "Court of Appeals for Veterans Claims",
        None,
    ) == (AUTHORITY_PERSUASIVE, True)
    assert classify_authority_tier(
        "https://www.va.gov/vetapp25/Files6/A1.txt",
        "Veterans law research result",
        None,
    ) == (AUTHORITY_AGENCY_GUIDANCE, True)


def test_authority_labels_and_summary_cover_official_secondary_and_unknown():
    assert "Official Court Transcript" in authority_label(AUTHORITY_BINDING, True)
    assert "Secondary Summary" in authority_label(AUTHORITY_SECONDARY, False)
    summary = summarize_authority_reliability(
        [AUTHORITY_BINDING, AUTHORITY_PERSUASIVE, AUTHORITY_SECONDARY, AUTHORITY_UNVERIFIED]
    )
    assert summary.total == 4
    assert summary.official_count == 2
    assert summary.secondary_count == 1
    assert summary.unknown_count == 1
    assert summary.official_primary_binding_count == 1
    assert summary.official_primary_persuasive_count == 1


def test_classify_authority_tier_supreme_court_binding_without_precedential_flag():
    # The Supreme Court branch returns binding regardless of the precedential
    # designation (reliability.py line 117).
    from va_legal_agent.topics import COURT_SUPREME

    assert classify_authority_tier(
        "https://www.supremecourt.gov/opinions/21-1.pdf", COURT_SUPREME
    ) == (AUTHORITY_BINDING, True)


def test_classify_authority_tier_official_non_courtlistener_host_falls_through():
    # An official primary URL with no court label whose host is neither
    # CourtListener nor va.gov lands on the fallthrough (reliability.py line 136).
    assert classify_authority_tier("https://uscourts.cavc.gov/opinions/1") == (
        AUTHORITY_AGENCY_GUIDANCE,
        True,
    )


def test_authority_label_persuasive_agency_and_generic_official_badges():
    assert authority_label(AUTHORITY_PERSUASIVE, True) == (
        "Official Court Transcript / Primary Authority (persuasive)"
    )
    assert authority_label(AUTHORITY_AGENCY_GUIDANCE, True) == (
        "Official Agency Guidance / Primary Authority"
    )
    assert authority_label("custom_tier", True) == "Official Primary Authority"
    assert authority_label(AUTHORITY_BINDING, True) == (
        "Official Court Transcript / Primary Authority (binding)"
    )
    assert authority_label(AUTHORITY_SECONDARY, False) == (
        "Secondary Summary / Non-Binding Source"
    )
    assert authority_label("custom_tier", False) == "Unverified Source"

