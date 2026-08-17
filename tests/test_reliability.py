"""Tests for source-reliability classification (va_legal_agent.reliability)."""

from va_legal_agent.reliability import (
    RELIABILITY_OFFICIAL,
    RELIABILITY_SECONDARY,
    RELIABILITY_UNKNOWN,
    classify_source,
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
