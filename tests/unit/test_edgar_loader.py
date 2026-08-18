"""Unit tests for parsing cached SEC EDGAR files."""

import json

import pytest

from sentineliq.components.ingestion.edgar_loader import (
    load_10k_sections,
    load_company_facts,
)
from sentineliq.exceptions import DocumentLoadError

RISK_TEXT = "Our results depend on many risks. " * 30
MDA_TEXT = "Revenue grew this year for these reasons. " * 30

# Mimics a real filing: a table of contents first, then the sections, with the
# drop-cap line break inside "RISK FACTORS" that SEC filings actually contain.
FILING_HTML = f"""
<html><body>
<p>Item 1A.</p><p>Risk Factors</p><p>14</p>
<p>Item 1B.</p><p>Unresolved Staff Comments</p><p>28</p>
<p>Item 7.</p><p>Management's Discussion</p><p>33</p>
<p>Item 7A.</p><p>Quantitative Disclosures</p><p>48</p>
<p>ITEM 1A. R</p><p>ISK FACTORS</p><p>{RISK_TEXT}</p>
<p>ITEM 1B. UNRESOLVED STAFF COMMENTS</p><p>None.</p>
<p>ITEM 7. MANAGEMENT'S DISCUSSION</p><p>{MDA_TEXT}</p>
<p>ITEM 7A. QUANTITATIVE DISCLOSURES</p><p>See above.</p>
</body></html>
"""

FACTS = {
    "cik": 789019,
    "entityName": "MICROSOFT CORPORATION",
    "facts": {
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": []}}},
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "units": {
                    "USD": [
                        {
                            "start": "2024-07-01",
                            "end": "2025-06-30",
                            "val": 281000000000,
                            "fy": 2025,
                            "form": "10-K",
                        },
                        {
                            "start": "2025-07-01",
                            "end": "2025-09-30",
                            "val": 77000000000,
                            "fy": 2026,
                            "form": "10-Q",
                        },
                    ]
                },
            },
            "CashAndCashEquivalentsAtCarryingValue": {
                "label": "Cash and Cash Equivalents",
                "units": {
                    "USD": [
                        {
                            "end": "2025-06-30",
                            "val": 94000000000,
                            "fy": 2025,
                            "form": "10-K",
                        }
                    ]
                },
            },
        },
    },
}


@pytest.fixture
def filing(tmp_path):
    path = tmp_path / "msft-2025.htm"
    path.write_text(FILING_HTML, encoding="utf-8")
    return path


@pytest.fixture
def facts_file(tmp_path):
    path = tmp_path / "companyfacts.json"
    path.write_text(json.dumps(FACTS), encoding="utf-8")
    return path


def test_load_10k_sections_extracts_both_sections(filing):
    sections = load_10k_sections(filing)
    assert set(sections) == {"item_1a", "item_7"}
    assert "many risks" in sections["item_1a"].text
    assert "Revenue grew" in sections["item_7"].text


def test_sections_stop_at_the_next_item(filing):
    """A section must not swallow the one that follows it."""
    sections = load_10k_sections(filing)
    assert "Revenue grew" not in sections["item_1a"].text
    assert "UNRESOLVED" not in sections["item_1a"].text
    assert "See above" not in sections["item_7"].text


def test_sections_are_separate_documents_from_one_file(filing):
    sections = load_10k_sections(filing)
    item_1a, item_7 = sections["item_1a"], sections["item_7"]
    assert item_1a.document_id != item_7.document_id
    assert item_1a.document_name == "msft-2025.htm#item_1a"
    assert item_1a.sha256 == item_7.sha256  # same source file
    assert item_1a.pages == []  # HTML filings have no pages


def test_sections_ignore_quoted_cross_references(tmp_path):
    """Filings cite their own sections in quotes; those are not the heading.

    Real 10-Ks say `see "Item 1A. Risk Factors" and elsewhere` inside later
    sections. Treating one of those as the heading silently returns the wrong
    text, so the quoted form must be skipped.
    """
    path = tmp_path / "crossref.htm"
    path.write_text(
        f"""
        <html><body>
        <p>Item 1A.</p><p>Risk Factors</p><p>14</p>
        <p>Item 1B.</p><p>Unresolved Staff Comments</p><p>28</p>
        <p>Item 7.</p><p>Management's Discussion</p><p>33</p>
        <p>Item 7A.</p><p>Quantitative Disclosures</p><p>48</p>
        <p>ITEM 1A. RISK FACTORS</p><p>{RISK_TEXT}</p>
        <p>ITEM 1B. UNRESOLVED STAFF COMMENTS</p><p>None.</p>
        <p>ITEM 7. MANAGEMENT'S DISCUSSION</p><p>{MDA_TEXT}</p>
        <p>For more detail see "Item 1A. Risk Factors" and elsewhere herein.</p>
        <p>ITEM 7A. QUANTITATIVE DISCLOSURES</p><p>See above.</p>
        </body></html>
        """,
        encoding="utf-8",
    )
    sections = load_10k_sections(path)
    assert "many risks" in sections["item_1a"].text
    assert "Revenue grew" not in sections["item_1a"].text


def test_load_10k_sections_raises_for_missing_file(tmp_path):
    with pytest.raises(DocumentLoadError, match="No such filing"):
        load_10k_sections(tmp_path / "nope.htm")


def test_load_10k_sections_raises_when_a_section_is_missing(tmp_path):
    path = tmp_path / "short.htm"
    path.write_text("<html><body><p>Nothing useful here.</p></body></html>")
    with pytest.raises(DocumentLoadError, match="Section heading not found"):
        load_10k_sections(path)


def test_load_company_facts_keeps_annual_us_gaap_facts(facts_file):
    facts = load_company_facts(facts_file)
    assert len(facts) == 2  # the 10-Q row and the dei taxonomy are excluded
    assert {f.concept for f in facts} == {
        "Revenues",
        "CashAndCashEquivalentsAtCarryingValue",
    }


def test_load_company_facts_reads_periods_and_values(facts_file):
    revenue = next(f for f in load_company_facts(facts_file) if f.concept == "Revenues")
    assert revenue.value == 281000000000
    assert revenue.cik == "0000789019"
    assert revenue.entity_name == "MICROSOFT CORPORATION"
    assert str(revenue.period_start) == "2024-07-01"
    assert str(revenue.period_end) == "2025-06-30"
    assert revenue.fiscal_year == 2025


def test_point_in_time_facts_have_no_start_date(facts_file):
    cash = next(
        f for f in load_company_facts(facts_file) if f.concept.startswith("Cash")
    )
    assert cash.period_start is None
    assert str(cash.period_end) == "2025-06-30"


def test_load_company_facts_can_select_another_form(facts_file):
    facts = load_company_facts(facts_file, form="10-Q")
    assert [f.value for f in facts] == [77000000000]


def test_load_company_facts_rejects_a_non_facts_file(tmp_path):
    path = tmp_path / "submissions.json"
    path.write_text('{"cik": 789019}')
    with pytest.raises(DocumentLoadError, match="not a company facts file"):
        load_company_facts(path)
