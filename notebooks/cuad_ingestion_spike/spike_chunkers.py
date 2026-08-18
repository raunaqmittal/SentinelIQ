"""Candidate chunking strategies under evaluation.

EXPERIMENT CODE. These are the strategies compared in the Stage 3 spike; the
winning one moves into `sentineliq/components/ingestion/chunker.py` once the
retrieval evaluation fixes the chunk size. Nothing here is production code.

All chunkers take a token `offsets` list (from a fast tokenizer's
`offset_mapping`) and return half-open character spans into the source text.
"""

import bisect
import re

CharSpan = tuple[int, int]
TokenOffsets = list[tuple[int, int]]

#: Line-start patterns that mark a new clause in commercial contracts.
#: Measured on the CUAD sample: numbering carries the signal, not typography
#: (only 4/18 documents had >3 distinct font sizes).
CLAUSE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*ARTICLE\s+([IVXLC]+|\d+)\b", re.I),
    re.compile(r"^\s*SECTION\s+(\d+(\.\d+)*)\b", re.I),
    re.compile(r"^\s*(\d+(\.\d+){1,3})\.?\s+\S"),
    re.compile(r"^\s*(\d{1,2})\.\s+[A-Z]"),
    re.compile(r"^\s*\(([a-z]{1,2})\)\s+\S"),
    re.compile(r"^\s*(EXHIBIT|SCHEDULE|APPENDIX|ANNEX)\s+[A-Z0-9]", re.I),
]


def clause_boundaries(text: str) -> list[int]:
    """Return character offsets where a new clause appears to start."""
    bounds = {0, len(text)}
    for match in re.finditer(r"^.*$", text, re.M):
        line = match.group(0)
        if not line.strip():
            continue
        if any(pattern.match(line) for pattern in CLAUSE_PATTERNS):
            bounds.add(match.start())
    return sorted(bounds)


def _token_index(offsets: TokenOffsets, char_offset: int) -> int:
    """Index of the first token starting at or after `char_offset`."""
    return bisect.bisect_left([start for start, _ in offsets], char_offset)


def _to_char_spans(
    offsets: TokenOffsets, token_spans: list[CharSpan]
) -> list[CharSpan]:
    """Convert half-open token-index spans into character spans."""
    return [
        (offsets[start][0], offsets[end - 1][1])
        for start, end in token_spans
        if end > start
    ]


def chunk_fixed(offsets: TokenOffsets, target: int, overlap: int) -> list[CharSpan]:
    """Blind fixed-size token windows with overlap — the baseline strategy."""
    token_spans: list[CharSpan] = []
    step = max(target - overlap, 1)
    position = 0
    total = len(offsets)
    while position < total:
        end = min(position + target, total)
        token_spans.append((position, end))
        if end >= total:
            break
        position += step
    return _to_char_spans(offsets, token_spans)


def _clause_units(text: str, offsets: TokenOffsets) -> list[CharSpan]:
    """Clause boundaries expressed as half-open token-index spans."""
    bounds = clause_boundaries(text)
    units: list[CharSpan] = []
    for start_char, end_char in zip(bounds, bounds[1:], strict=False):
        start = _token_index(offsets, start_char)
        end = _token_index(offsets, end_char)
        if end > start:
            units.append((start, end))
    return units


def chunk_clause_aware(
    text: str, offsets: TokenOffsets, target: int, max_tokens: int
) -> list[CharSpan]:
    """One chunk per clause; oversized clauses split into `target` children.

    Emits many very small chunks (median ~80 tokens on the CUAD sample), which
    is why `chunk_clause_packed` supersedes it.
    """
    token_spans: list[CharSpan] = []
    for start, end in _clause_units(text, offsets):
        if end - start <= max_tokens:
            token_spans.append((start, end))
            continue
        cursor = start
        while cursor < end:
            token_spans.append((cursor, min(cursor + target, end)))
            cursor += target
    return _to_char_spans(offsets, token_spans)


def chunk_clause_packed(
    text: str,
    offsets: TokenOffsets,
    target: int,
    max_tokens: int,
    min_tokens: int = 120,
) -> list[CharSpan]:
    """Respect clause boundaries, packing small adjacent clauses up to `target`.

    Best evidence containment measured in the spike (98.0% at 500 tokens)
    without the tiny-fragment problem of `chunk_clause_aware`.
    """
    token_spans: list[CharSpan] = []
    buffer: CharSpan | None = None

    for start, end in _clause_units(text, offsets):
        if end - start > max_tokens:
            # 1. flush what is buffered, then split the oversized clause
            if buffer is not None:
                token_spans.append(buffer)
                buffer = None
            cursor = start
            while cursor < end:
                token_spans.append((cursor, min(cursor + target, end)))
                cursor += target
            continue
        # 2. otherwise accumulate until adding this clause would exceed target
        if buffer is None:
            buffer = (start, end)
        elif end - buffer[0] <= target:
            buffer = (buffer[0], end)
        else:
            token_spans.append(buffer)
            buffer = (start, end)
    if buffer is not None:
        token_spans.append(buffer)

    # 3. absorb any residual chunk that is too small to retrieve well
    merged: list[CharSpan] = []
    for span in token_spans:
        too_small = span[1] - span[0] < min_tokens
        if merged and too_small and span[1] - merged[-1][0] <= max_tokens:
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)
    return _to_char_spans(offsets, merged)


def containment(spans: list[CharSpan], chunks: list[CharSpan]) -> dict[str, int]:
    """Count how many evidence spans survive chunking intact.

    A span split across chunks can never be retrieved as complete evidence,
    which makes this the decisive metric for comparing strategies.
    """
    result = {"whole": 0, "split2": 0, "split3plus": 0, "unplaced": 0}
    for start, end in spans:
        if any(cs <= start and end <= ce for cs, ce in chunks):
            result["whole"] += 1
            continue
        overlapping = sum(1 for cs, ce in chunks if cs < end and start < ce)
        if overlapping == 0:
            result["unplaced"] += 1
        elif overlapping >= 3:
            result["split3plus"] += 1
        else:
            result["split2"] += 1
    return result


def token_length(offsets: TokenOffsets, span: CharSpan) -> int:
    """Number of tokens inside a character span."""
    return _token_index(offsets, span[1]) - _token_index(offsets, span[0])
