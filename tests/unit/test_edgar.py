"""Unit tests for SEC EDGAR acquisition. No real network calls are made."""

import json

import pytest

from sentineliq.components.ingestion import edgar
from sentineliq.exceptions import EdgarFetchError

TICKERS = {"0": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"}}

SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["8-K", "10-K", "10-K"],
            "accessionNumber": ["0000-24-1", "0000-24-2", "0000-23-9"],
            "primaryDocument": ["a.htm", "msft-2024.htm", "msft-2023.htm"],
        }
    }
}


@pytest.fixture
def fake_sec(monkeypatch, tmp_path):
    """Serve canned SEC responses and cache into a temporary directory."""
    responses = {
        edgar.TICKERS_URL: json.dumps(TICKERS),
        edgar.SUBMISSIONS_URL.format(cik="0000789019"): json.dumps(SUBMISSIONS),
        edgar.FACTS_URL.format(cik="0000789019"): '{"facts": {}}',
        edgar.ARCHIVE_URL.format(
            cik=789019, accession="0000242", document="msft-2024.htm"
        ): "<html>Item 1A. Risk Factors</html>",
    }
    calls = []

    def fake_get(url: str) -> bytes:
        calls.append(url)
        if url not in responses:
            raise EdgarFetchError(f"SEC returned 404 for {url}")
        return responses[url].encode()

    monkeypatch.setattr(edgar, "_get", fake_get)
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    return calls


def test_cik_for_ticker_returns_zero_padded_cik(fake_sec):
    assert edgar.cik_for_ticker("msft") == "0000789019"


def test_cik_for_ticker_raises_for_unknown_ticker(fake_sec):
    with pytest.raises(EdgarFetchError, match="No CIK found"):
        edgar.cik_for_ticker("NOPE")


def test_fetch_latest_10k_picks_the_newest_10k(fake_sec):
    """8-K filings must be skipped, and the first 10-K is the most recent."""
    path = edgar.fetch_latest_10k("0000789019")
    assert path.name == "msft-2024.htm"
    assert "Item 1A" in path.read_text(encoding="utf-8")


def test_fetch_latest_10k_raises_when_no_10k_exists(fake_sec, monkeypatch):
    empty = {"filings": {"recent": {k: [] for k in SUBMISSIONS["filings"]["recent"]}}}
    monkeypatch.setattr(
        edgar,
        "_get",
        lambda url: json.dumps(empty).encode(),
    )
    with pytest.raises(EdgarFetchError, match="No 10-K"):
        edgar.fetch_latest_10k("0000789019")


def test_cached_files_are_not_downloaded_twice(fake_sec):
    """Caching is what keeps evaluation runs off the SEC API."""
    edgar.fetch_company_facts("0000789019")
    edgar.fetch_company_facts("0000789019")
    assert len(fake_sec) == 1


def test_fetch_vendor_returns_filing_and_facts_paths(fake_sec):
    filing, facts = edgar.fetch_vendor("MSFT")
    assert filing.name == "msft-2024.htm"
    assert facts.name == "companyfacts.json"


def test_get_waits_between_requests_to_respect_the_rate_limit(monkeypatch):
    """SEC allows 10 requests/second; back-to-back calls must be spaced out."""

    class FakeResponse:
        status_code = 200
        content = b"{}"

    slept = []
    monkeypatch.setattr(edgar.requests, "get", lambda *a, **kw: FakeResponse())
    monkeypatch.setattr(edgar.time, "sleep", slept.append)
    monkeypatch.setattr(edgar, "get_sec_user_agent", lambda: "SentinelIQ t@e.com")

    edgar._get("https://example.com/a")
    edgar._get("https://example.com/b")

    assert slept and slept[-1] <= edgar.MIN_REQUEST_INTERVAL
