"""Unit tests for recursive character chunking."""

from datetime import UTC, datetime

import pytest

from sentineliq.components.ingestion.chunker import chunk_document
from sentineliq.components.models.schemas import LoadedDocument, PageSpan

PAGE_SEPARATOR = "\n"


def count_words(text: str) -> int:
    """Stand-in tokenizer: one word, one token."""
    return len(text.split())


def make_document(pages: list[str], document_id: str = "doc1") -> LoadedDocument:
    text = PAGE_SEPARATOR.join(pages)
    spans = []
    start = 0
    for number, page in enumerate(pages, start=1):
        spans.append(PageSpan(page_number=number, start=start, end=start + len(page)))
        start += len(page) + len(PAGE_SEPARATOR)
    return LoadedDocument(
        document_id=document_id,
        document_name="contract.pdf",
        text=text,
        pages=spans,
        sha256="0" * 64,
        loaded_at=datetime.now(UTC),
    )


@pytest.fixture
def document():
    """Prose that splits down to sentences, as real extracted text does."""
    sentences = [f"Clause {i} states the vendor shall comply." for i in range(1, 40)]
    return make_document([" ".join(sentences)])


def test_short_document_becomes_one_chunk():
    document = make_document(["A short agreement clause."])
    chunks = chunk_document(document, count_words, chunk_size=50, chunk_overlap=5)
    assert len(chunks) == 1
    assert chunks[0].text == document.text


def test_long_document_is_split_into_several_chunks(document):
    chunks = chunk_document(document, count_words, chunk_size=50, chunk_overlap=5)
    assert len(chunks) > 1


def test_chunks_respect_the_size_limit(document):
    chunks = chunk_document(document, count_words, chunk_size=50, chunk_overlap=5)
    assert all(count_words(chunk.text) <= 50 for chunk in chunks)


def test_chunk_ids_are_sequential_and_document_scoped(document):
    chunks = chunk_document(document, count_words, chunk_size=50, chunk_overlap=5)
    assert [chunk.chunk_id for chunk in chunks[:3]] == [
        "doc1_0000",
        "doc1_0001",
        "doc1_0002",
    ]


def test_offsets_slice_back_to_the_chunk_text(document):
    """Citations depend on offsets addressing exactly the chunk's own text."""
    chunks = chunk_document(document, count_words, chunk_size=50, chunk_overlap=5)
    for chunk in chunks:
        assert document.text[chunk.char_start : chunk.char_end] == chunk.text


def test_chunks_cover_the_whole_document(document):
    """No text may be silently dropped during chunking."""
    chunks = chunk_document(document, count_words, chunk_size=50, chunk_overlap=5)
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(document.text)
    for earlier, later in zip(chunks, chunks[1:], strict=False):
        assert later.char_start <= earlier.char_end


def test_overlap_repeats_context_between_chunks(document):
    chunks = chunk_document(document, count_words, chunk_size=50, chunk_overlap=20)
    assert chunks[1].char_start < chunks[0].char_end


def test_zero_overlap_produces_adjacent_chunks(document):
    chunks = chunk_document(document, count_words, chunk_size=50, chunk_overlap=0)
    for earlier, later in zip(chunks, chunks[1:], strict=False):
        assert later.char_start == earlier.char_end


def test_overlap_works_on_coarse_text_without_sentence_breaks():
    """Overlap cuts mid-piece, so text with few natural breaks still overlaps."""
    paragraphs = [" ".join(["word"] * 30) for _ in range(4)]
    document = make_document(["\n\n".join(paragraphs)])
    chunks = chunk_document(document, count_words, chunk_size=50, chunk_overlap=20)
    assert chunks[1].char_start < chunks[0].char_end


def test_overlap_never_pushes_a_chunk_over_the_size_limit():
    """The carried-over text counts towards the chunk's budget, not on top of it."""
    paragraphs = [" ".join(["word"] * 48) for _ in range(6)]
    document = make_document(["\n\n".join(paragraphs)])
    chunks = chunk_document(document, count_words, chunk_size=50, chunk_overlap=20)
    assert all(count_words(chunk.text) <= 50 for chunk in chunks)


def test_pages_are_recorded_from_the_document():
    document = make_document(["First page text here.", "Second page text here."])
    chunks = chunk_document(document, count_words, chunk_size=4, chunk_overlap=0)
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 2


def test_chunk_spanning_two_pages_records_both():
    document = make_document(["one two three", "four five six"])
    chunks = chunk_document(document, count_words, chunk_size=100, chunk_overlap=0)
    assert len(chunks) == 1
    assert (chunks[0].page_start, chunks[0].page_end) == (1, 2)


def test_text_without_separators_is_still_split():
    """A wall of text with no paragraph or sentence breaks must not blow the limit."""
    document = make_document([" ".join(["word"] * 200)])
    chunks = chunk_document(document, count_words, chunk_size=25, chunk_overlap=0)
    assert len(chunks) > 1
    assert all(count_words(chunk.text) <= 25 for chunk in chunks)


def test_overlap_must_be_smaller_than_chunk_size(document):
    with pytest.raises(ValueError, match="must be smaller"):
        chunk_document(document, count_words, chunk_size=50, chunk_overlap=50)
