"""Parse cached SEC EDGAR files.

Downloading lives in `edgar.py`; this module only reads files already on disk.
The 10-K gives narrative text for the RAG pipeline (Item 1A Risk Factors,
Item 7 MD&A) and the XBRL facts give a table of numbers for structured queries.
"""

import json
import logging
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from bs4 import BeautifulSoup

from sentineliq.components.ingestion.loader import compute_sha256
from sentineliq.components.models.schemas import FinancialFact, LoadedDocument
from sentineliq.exceptions import DocumentLoadError

logger = logging.getLogger(__name__)

# A section runs from its own heading to the heading of the next item.
SECTION_BOUNDS = {
    "item_1a": ("item 1a. risk factors", "item 1b. unresolved"),
    "item_7": ("item 7. management", "item 7a. quantitative"),
}
MIN_SECTION_CHARS = 500

#: Filings cite other sections in double quotes, e.g. see "Item 1A. Risk Factors".
QUOTE_CHARS = '"“”'
QUOTE_WINDOW = 8


def _html_to_text(path: Path) -> str:
    """Extract readable text from a filing, dropping markup and blank runs."""
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text)


def _fuzzy_pattern(phrase: str) -> str:
    """Regex matching `phrase` even split by line breaks between letters.

    Real filings render headings with a big drop-cap first letter, so
    "RISK FACTORS" can come out as "R\\nISK FACTORS" in the extracted text.
    """
    return r"\s*".join(re.escape(char) for char in phrase if not char.isspace())


def _is_quoted(text: str, start: int, end: int) -> bool:
    """True if the heading text sits inside quotes, so it is a cross-reference.

    A filing cites its own sections in quotes — `see "Item 1A. Risk Factors"` —
    and those citations read exactly like the heading itself. The quotes are
    what tells them apart.
    """
    nearby = text[max(0, start - QUOTE_WINDOW) : start] + text[end : end + QUOTE_WINDOW]
    return any(char in QUOTE_CHARS for char in nearby)


def _find_section(text: str, start_phrase: str, end_phrase: str) -> str:
    """Return the text between two headings.

    Both phrases also appear in the table of contents and in cross-references,
    so quoted matches are skipped, and of the rest the longest section wins —
    a table-of-contents entry is only a few characters long.
    """
    end_pattern = _fuzzy_pattern(end_phrase)
    best = ""

    for match in re.finditer(_fuzzy_pattern(start_phrase), text, re.IGNORECASE):
        if _is_quoted(text, match.start(), match.end()):
            continue
        end_match = re.search(end_pattern, text[match.start() :], re.IGNORECASE)
        if end_match is None:
            continue
        section = text[match.start() : match.start() + end_match.start()].strip()
        if len(section) > len(best):
            best = section

    if not best:
        raise DocumentLoadError(f"Section heading not found: {start_phrase}")
    return best


def load_10k_sections(path: Path) -> dict[str, LoadedDocument]:
    """Load Item 1A and Item 7 from a cached 10-K as separate documents.

    Each section becomes its own LoadedDocument so it can be chunked and cited
    independently. Filings are HTML, not paged, so `pages` is empty.

    Raises:
        DocumentLoadError: The file is missing or a section is absent or empty.
    """
    if not path.is_file():
        raise DocumentLoadError(f"No such filing: {path}")

    text = _html_to_text(path)
    sha256 = compute_sha256(path)

    sections = {}
    for name, (start_phrase, end_phrase) in SECTION_BOUNDS.items():
        section_text = _find_section(text, start_phrase, end_phrase)
        if len(section_text) < MIN_SECTION_CHARS:
            raise DocumentLoadError(f"{name} in {path.name} is too short to be real")

        sections[name] = LoadedDocument(
            document_id=str(uuid.uuid4()),
            document_name=f"{path.name}#{name}",
            text=section_text,
            pages=[],
            sha256=sha256,
            loaded_at=datetime.now(UTC),
        )

    logger.info(
        "Loaded 10-K sections",
        extra={"filing": path.name, "sections": list(sections)},
    )
    return sections


def _fact_from_row(
    cik: str, entity_name: str, concept: str, label: str, unit: str, row: dict
) -> FinancialFact:
    """Build one FinancialFact from a single raw XBRL row."""
    return FinancialFact(
        cik=cik,
        entity_name=entity_name,
        concept=concept,
        label=label,
        unit=unit,
        value=row["val"],
        period_start=date.fromisoformat(row["start"]) if "start" in row else None,
        period_end=date.fromisoformat(row["end"]),
        fiscal_year=row["fy"],
        form=row["form"],
    )


def _facts_for_concept(
    cik: str, entity_name: str, concept: str, details: dict, form: str
) -> list[FinancialFact]:
    """Build every fact reported for one concept (e.g. "Revenues")."""
    label = details.get("label") or concept
    facts = []
    for unit, rows in details["units"].items():
        for row in rows:
            if row.get("form") == form:
                facts.append(
                    _fact_from_row(cik, entity_name, concept, label, unit, row)
                )
    return facts


def load_company_facts(path: Path, *, form: str = "10-K") -> list[FinancialFact]:
    """Load XBRL company facts as a flat table of reported numbers.

    Only US-GAAP facts are kept; `dei` holds filing metadata, not financials.
    The default form filter keeps annual figures, which is what trend
    comparisons across vendors need.

    Raises:
        DocumentLoadError: The file is missing or is not a company-facts file.
    """
    if not path.is_file():
        raise DocumentLoadError(f"No such facts file: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    if "facts" not in data:
        raise DocumentLoadError(f"{path.name} is not a company facts file")

    cik = str(data["cik"]).zfill(10)
    entity_name = data["entityName"]

    facts = []
    for concept, details in data["facts"].get("us-gaap", {}).items():
        facts.extend(_facts_for_concept(cik, entity_name, concept, details, form))

    logger.info("Loaded company facts", extra={"cik": cik, "facts": len(facts)})
    return facts
