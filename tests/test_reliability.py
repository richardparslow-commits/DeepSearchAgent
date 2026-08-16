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
