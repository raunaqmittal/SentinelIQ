"""Download and cache SEC EDGAR filings and financial facts.

Acquisition only: every file is cached on disk on first download, so
evaluation runs never re-hit the API. Parsing lives in the EDGAR loader.
"""

import json
import logging
import time
from pathlib import Path

import requests

from sentineliq.config import get_sec_user_agent
from sentineliq.exceptions import EdgarFetchError

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = PROJECT_ROOT / "data" / "evaluation" / "datasets" / "edgar"

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

MIN_REQUEST_INTERVAL = 0.1  # SEC allows 10 requests/second (ADR-006)
_last_request_at = 0.0


def _get(url: str) -> bytes:
    """Fetch a URL, staying inside SEC's documented rate limit."""
    global _last_request_at
    wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()

    headers = {"User-Agent": get_sec_user_agent()}
    response = requests.get(url, headers=headers, timeout=30)
    if response.status_code != 200:
        raise EdgarFetchError(f"SEC returned {response.status_code} for {url}")
    return response.content


def _download(url: str, path: Path) -> Path:
    """Download url to path, unless it is already cached."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_get(url))
    logger.info("Downloaded EDGAR file", extra={"file": path.name})
    return path


def cik_for_ticker(ticker: str) -> str:
    """Look up a company's zero-padded 10-digit CIK by ticker symbol."""
    path = _download(TICKERS_URL, CACHE_DIR / "company_tickers.json")
    companies = json.loads(path.read_text(encoding="utf-8"))
    for company in companies.values():
        if company["ticker"] == ticker.upper():
            return str(company["cik_str"]).zfill(10)
    raise EdgarFetchError(f"No CIK found for ticker {ticker}")


def fetch_company_facts(cik: str) -> Path:
    """Download the company's XBRL facts — the structured financial branch."""
    return _download(FACTS_URL.format(cik=cik), CACHE_DIR / cik / "companyfacts.json")


def fetch_latest_10k(cik: str) -> Path:
    """Download the company's most recent 10-K document."""
    # 1. Get the filing history
    url = SUBMISSIONS_URL.format(cik=cik)
    path = _download(url, CACHE_DIR / cik / "submissions.json")
    recent = json.loads(path.read_text(encoding="utf-8"))["filings"]["recent"]

    # 2. Find the newest 10-K (filings are listed newest first)
    filings = zip(
        recent["form"],
        recent["accessionNumber"],
        recent["primaryDocument"],
        strict=True,
    )
    for form, accession, document in filings:
        if form == "10-K":
            url = ARCHIVE_URL.format(
                cik=int(cik),
                accession=accession.replace("-", ""),
                document=document,
            )
            return _download(url, CACHE_DIR / cik / document)

    raise EdgarFetchError(f"No 10-K filing found for CIK {cik}")


def fetch_vendor(ticker: str) -> tuple[Path, Path]:
    """Download a vendor's latest 10-K and XBRL facts.

    Returns:
        Paths to the cached 10-K document and the company facts JSON.
    """
    cik = cik_for_ticker(ticker)
    logger.info("Fetching EDGAR data", extra={"ticker": ticker.upper(), "cik": cik})
    return fetch_latest_10k(cik), fetch_company_facts(cik)
