"""Split a loaded document into overlapping chunks.

Recursive character splitting: cut on the largest natural boundary first
(paragraphs), fall back to smaller ones only where a piece is still too big.
Works on any text, so it stays independent of the source format.
"""

from collections.abc import Callable

from sentineliq.components.models.schemas import Chunk, LoadedDocument

SEPARATORS = ["\n\n", "\n", ". ", " "]

Span = tuple[int, int]
CountTokens = Callable[[str], int]


def _split_on(text: str, start: int, end: int, sep: str) -> list[Span]:
    """Split [start, end) on sep, keeping the separator and the offsets."""
    spans = []
    position = start
    while (found := text.find(sep, position, end)) != -1:
        spans.append((position, found + len(sep)))
        position = found + len(sep)
    spans.append((position, end))
    return [(s, e) for s, e in spans if e > s]


def _split_to_fit(text: str, size: int, count_tokens: CountTokens) -> list[Span]:
    """Split text into spans that each fit within size tokens."""
    spans = [(0, len(text))]
    for sep in SEPARATORS:
        smaller = []
        for start, end in spans:
            if count_tokens(text[start:end]) <= size:
                smaller.append((start, end))
            else:
                smaller.extend(_split_on(text, start, end, sep))
        spans = smaller
    return spans


def _overlap_start(
    text: str, chunk: Span, overlap: int, count_tokens: CountTokens
) -> int:
    """Where the next chunk starts so about `overlap` tokens are repeated.

    Steps back word by word from the end of the chunk, taking the longest
    tail that still fits the overlap budget. Returns the chunk's end when
    even one word does not fit, which means no overlap.
    """
    start, end = chunk
    position = end
    for index in range(end - 1, start, -1):
        if text[index].isspace():  # isspace, not " ": PDFs use \xa0 for spaces
            if count_tokens(text[index + 1 : end]) > overlap:
                break
            position = index + 1
    return position


def _pack(
    text: str, spans: list[Span], size: int, overlap: int, count_tokens: CountTokens
) -> list[Span]:
    """Merge small spans into chunks of at most size tokens."""
    chunks = []
    start = end = spans[0][0]
    for span in spans:
        if end > start and count_tokens(text[start : span[1]]) > size:
            chunks.append((start, end))
            start = _overlap_start(text, (start, end), overlap, count_tokens)
            if count_tokens(text[start : span[1]]) > size:
                start = span[0]  # the overlap would overflow, so drop it
        end = span[1]
    chunks.append((start, end))
    return chunks


def chunk_document(
    document: LoadedDocument,
    count_tokens: CountTokens,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    """Split a document into chunks.

    Args:
        document: The loaded source document.
        count_tokens: Token counter, matching the embedding model's tokenizer.
        chunk_size: Maximum tokens per chunk.
        chunk_overlap: Tokens of context repeated between adjacent chunks.

    Returns:
        Chunks in document order, each carrying its source offsets and pages.
    """
    if chunk_overlap >= chunk_size:
        raise ValueError(
            f"chunk_overlap ({chunk_overlap}) must be smaller "
            f"than chunk_size ({chunk_size})"
        )

    text = document.text
    # Leave room for the overlap so a single piece can never crowd it out.
    spans = _split_to_fit(text, chunk_size - chunk_overlap, count_tokens)
    packed = _pack(text, spans, chunk_size, chunk_overlap, count_tokens)

    return [
        Chunk(
            chunk_id=f"{document.document_id}_{index:04d}",
            document_id=document.document_id,
            document_name=document.document_name,
            text=text[start:end],
            char_start=start,
            char_end=end,
            page_start=document.page_for_offset(start),
            page_end=document.page_for_offset(end - 1),
        )
        for index, (start, end) in enumerate(packed)
    ]
