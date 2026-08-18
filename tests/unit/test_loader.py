"""Unit tests for PDF loading and page-offset mapping."""

import docx
import pymupdf
import pytest

from sentineliq.components.ingestion.loader import (
    PAGE_SEPARATOR,
    compute_sha256,
    load_docx,
    load_pdf,
    load_txt,
)
from sentineliq.exceptions import DocumentLoadError


def write_pdf(path, pages: list[str]):
    """Create a small multi-page PDF for testing."""
    document = pymupdf.open()
    for body in pages:
        page = document.new_page()
        page.insert_text((72, 72), body, fontsize=11)
    document.save(path)
    document.close()
    return path


@pytest.fixture
def two_page_pdf(tmp_path):
    return write_pdf(
        tmp_path / "contract.pdf",
        [
            "Section 4.2 Data Retention. Vendor shall retain records.",
            "Section 9.1 Governing Law. This Agreement is governed by law.",
        ],
    )


def test_load_pdf_returns_text_from_every_page(two_page_pdf):
    document = load_pdf(two_page_pdf)
    assert "Data Retention" in document.text
    assert "Governing Law" in document.text


def test_load_pdf_records_one_page_span_per_page(two_page_pdf):
    document = load_pdf(two_page_pdf)
    assert len(document.pages) == 2
    assert [page.page_number for page in document.pages] == [1, 2]


def test_page_spans_slice_the_correct_text(two_page_pdf):
    """Offsets must address real text, since citations depend on them."""
    document = load_pdf(two_page_pdf)
    first, second = document.pages
    assert "Data Retention" in document.text[first.start : first.end]
    assert "Governing Law" in document.text[second.start : second.end]


def test_page_spans_account_for_the_separator(two_page_pdf):
    document = load_pdf(two_page_pdf)
    first, second = document.pages
    assert second.start == first.end + len(PAGE_SEPARATOR)


def test_page_for_offset_maps_back_to_the_right_page(two_page_pdf):
    document = load_pdf(two_page_pdf)
    offset = document.text.index("Governing Law")
    assert document.page_for_offset(offset) == 2


def test_page_for_offset_returns_none_past_end(two_page_pdf):
    document = load_pdf(two_page_pdf)
    assert document.page_for_offset(len(document.text) + 10) is None


def test_document_name_is_the_file_name(two_page_pdf):
    assert load_pdf(two_page_pdf).document_name == "contract.pdf"


def test_document_id_is_generated_when_not_supplied(two_page_pdf):
    assert load_pdf(two_page_pdf).document_id


def test_supplied_document_id_is_preserved(two_page_pdf):
    document = load_pdf(two_page_pdf, document_id="vendor-x-001")
    assert document.document_id == "vendor-x-001"


def test_identical_files_share_a_hash(tmp_path):
    """Deduplication depends on identical content hashing identically."""
    body = ["Same content on the page."]
    first = load_pdf(write_pdf(tmp_path / "a.pdf", body))
    second = load_pdf(write_pdf(tmp_path / "b.pdf", body))
    assert first.sha256 == compute_sha256(tmp_path / "a.pdf")
    assert len(first.sha256) == 64
    assert second.sha256 == compute_sha256(tmp_path / "b.pdf")


def test_missing_file_raises_document_load_error(tmp_path):
    with pytest.raises(DocumentLoadError):
        load_pdf(tmp_path / "absent.pdf")


def test_non_pdf_content_raises_document_load_error(tmp_path):
    corrupt = tmp_path / "broken.pdf"
    corrupt.write_bytes(b"this is not a pdf")
    with pytest.raises(DocumentLoadError):
        load_pdf(corrupt)


def test_empty_pdf_raises_document_load_error(tmp_path):
    """A page-image-only PDF has no extractable text and must fail loudly."""
    document = pymupdf.open()
    document.new_page()
    document.save(tmp_path / "scanned.pdf")
    document.close()
    with pytest.raises(DocumentLoadError, match="no extractable text"):
        load_pdf(tmp_path / "scanned.pdf")


def test_page_separator_offset_belongs_to_the_preceding_page(two_page_pdf):
    """Chunks often end just past a page break; that must still report a page."""
    document = load_pdf(two_page_pdf)
    assert document.page_for_offset(document.pages[0].end) == 1


# --- TXT ---------------------------------------------------------------


@pytest.fixture
def txt_file(tmp_path):
    path = tmp_path / "policy.txt"
    path.write_text("Section 4.2 Data Retention. Vendor shall retain records.")
    return path


def test_load_txt_returns_the_file_text(txt_file):
    document = load_txt(txt_file)
    assert "Data Retention" in document.text


def test_load_txt_has_no_pages(txt_file):
    assert load_txt(txt_file).pages == []


def test_load_txt_document_name_is_the_file_name(txt_file):
    assert load_txt(txt_file).document_name == "policy.txt"


def test_load_txt_missing_file_raises_document_load_error(tmp_path):
    with pytest.raises(DocumentLoadError, match="No such document"):
        load_txt(tmp_path / "absent.txt")


def test_load_txt_empty_file_raises_document_load_error(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("  ")
    with pytest.raises(DocumentLoadError, match="no extractable text"):
        load_txt(path)


def test_load_txt_non_utf8_file_raises_document_load_error(tmp_path):
    """One undecodable upload must not crash a caller catching DocumentLoadError."""
    path = tmp_path / "latin1.txt"
    path.write_bytes("Vendor caf\xe9 agreement text.".encode("latin-1"))
    with pytest.raises(DocumentLoadError, match="not valid UTF-8"):
        load_txt(path)


# --- DOCX ----------------------------------------------------------------


def write_docx(path, paragraphs: list[str]):
    """Create a small Word document for testing."""
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    document.save(path)
    return path


@pytest.fixture
def docx_file(tmp_path):
    return write_docx(
        tmp_path / "policy.docx",
        [
            "Section 4.2 Data Retention. Vendor shall retain records.",
            "Section 9.1 Governing Law. This Agreement is governed by law.",
        ],
    )


def test_load_docx_returns_text_from_every_paragraph(docx_file):
    document = load_docx(docx_file)
    assert "Data Retention" in document.text
    assert "Governing Law" in document.text


def test_load_docx_has_no_pages(docx_file):
    assert load_docx(docx_file).pages == []


def test_load_docx_missing_file_raises_document_load_error(tmp_path):
    with pytest.raises(DocumentLoadError, match="No such document"):
        load_docx(tmp_path / "absent.docx")


def test_load_docx_corrupt_file_raises_document_load_error(tmp_path):
    corrupt = tmp_path / "broken.docx"
    corrupt.write_bytes(b"this is not a docx")
    # Since NFR-003 file validation was added this is caught by the content
    # check, before python-docx is handed the file at all. Still a
    # DocumentLoadError, but rejected earlier and for a better reason.
    with pytest.raises(DocumentLoadError, match="not a real .docx file"):
        load_docx(corrupt)


def test_load_docx_empty_file_raises_document_load_error(tmp_path):
    empty = write_docx(tmp_path / "empty.docx", [])
    with pytest.raises(DocumentLoadError, match="no extractable text"):
        load_docx(empty)
