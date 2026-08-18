"""Shared helpers: text normalization for evidence matching, and log redaction.

Text extracted from a PDF and the same text from another source rarely match
character for character. Whitespace, quotation marks, dashes, ligatures and
line-break hyphenation all differ. Matching therefore has to happen on a
normalized form, but citations must point at the *original* offsets — so the
normalizer keeps an index map back to the source.
"""

# 1. Standard library imports
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass

# 2. Constants

# Kept as readable lookup tables rather than one entry per line.
# fmt: off

#: Characters that differ between PDF extraction and plaintext exports.
CHAR_REPLACEMENTS: dict[str, str] = {
    # single quotes
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    # double quotes
    "“": '"', "”": '"', "„": '"', "‟": '"',
    # dashes and minus signs
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    # ellipsis
    "…": "...",
}

#: Ligatures expand to more than one character, so they shift the index map.
LIGATURES: dict[str, str] = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
    "ﬃ": "ffi", "ﬄ": "ffl",
}

# fmt: on

#: Zero-width and formatting characters that carry no textual meaning.
INVISIBLE_CHARS: frozenset[str] = frozenset("​‌‍﻿­")

WORD_PATTERN = re.compile(r"\w+")


@dataclass(frozen=True)
class NormalizedText:
    """Normalized text plus a map from each character back to the source.

    `source_offsets[i]` is the offset in the original string that produced
    `text[i]`. This is what lets a match found on normalized text be cited at
    exact original character positions.

    A frozen dataclass rather than a Pydantic model on purpose: the offset map
    holds one entry per character, and validating a six-figure integer list on
    every document would cost more than it protects.
    """

    text: str
    source_offsets: tuple[int, ...]

    def source_offset(self, index: int) -> int:
        """Return the source offset that produced `text[index]`."""
        if not 0 <= index < len(self.source_offsets):
            raise IndexError(
                f"index {index} outside normalized text of length "
                f"{len(self.source_offsets)}"
            )
        return self.source_offsets[index]

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        """Map a half-open normalized span to a half-open source span."""
        if start >= end:
            raise ValueError(f"empty or reversed span: start={start} end={end}")
        return self.source_offset(start), self.source_offset(end - 1) + 1

    def __len__(self) -> int:
        return len(self.text)


def _soft_hyphen_break(text: str, index: int) -> int | None:
    """Return the index just past a hyphen-at-line-break, or None.

    Detects the "reten-\\ntion" pattern so it normalizes to "retention".
    """
    if text[index] != "-":
        return None
    cursor = index + 1
    length = len(text)
    while cursor < length and text[cursor] in " \t\r":
        cursor += 1
    if cursor >= length or text[cursor] != "\n":
        return None
    cursor += 1
    while cursor < length and text[cursor] in " \t\r":
        cursor += 1
    if cursor < length and text[cursor].islower():
        return cursor
    return None


def _translate(char: str) -> str:
    """Map one source character to its normalized form (may be 0..3 chars)."""
    if char in INVISIBLE_CHARS:
        return ""
    replacement = LIGATURES.get(char) or CHAR_REPLACEMENTS.get(char)
    if replacement is not None:
        return replacement
    folded = unicodedata.normalize("NFKC", char)
    return folded if folded else char


def normalize_for_matching(text: str, *, fold_case: bool = False) -> NormalizedText:
    """Normalize text for matching while preserving source offsets.

    Collapses runs of whitespace to a single space, unifies quotes, dashes and
    ligatures, drops zero-width characters, and rejoins words hyphenated across
    a line break.

    Args:
        text: The source text, typically a PDF extraction.
        fold_case: Lower-case the output. Defaults to False — measurement on
            the CUAD sample showed 98.5% of evidence spans match without
            folding and 0% require it, so folding only adds false-positive
            risk. See `Docs/PROGRESS.md` → Ingestion Spike.

    Returns:
        A NormalizedText whose `source_offsets` map back into `text`.
    """
    chars: list[str] = []
    offsets: list[int] = []
    at_space_boundary = True  # drop leading whitespace
    index = 0
    length = len(text)

    while index < length:
        char = text[index]

        resume = _soft_hyphen_break(text, index)
        if resume is not None:
            index = resume
            continue

        if char.isspace():
            if not at_space_boundary:
                chars.append(" ")
                offsets.append(index)
                at_space_boundary = True
            index += 1
            continue

        for piece in _translate(char):
            chars.append(piece.lower() if fold_case else piece)
            offsets.append(index)
            at_space_boundary = False
        index += 1

    while chars and chars[-1] == " ":
        chars.pop()
        offsets.pop()

    return NormalizedText(text="".join(chars), source_offsets=tuple(offsets))


def find_evidence_span(
    haystack: NormalizedText,
    needle: str,
    *,
    expected_source_start: int | None = None,
    fold_case: bool = False,
) -> tuple[int, int] | None:
    """Locate `needle` in `haystack` and return its source span.

    When the same text occurs more than once — which it does for 18.6% of CUAD
    annotations — plain first-match search cites the wrong location 4.1% of the
    time. Passing `expected_source_start` selects the occurrence nearest to the
    expected position instead.

    Args:
        haystack: Normalized document text.
        needle: Raw evidence text to locate.
        expected_source_start: Approximate source offset of the expected match.
        fold_case: Must match how `haystack` was normalized.

    Returns:
        A half-open (start, end) span in source coordinates, or None.
    """
    normalized_needle = normalize_for_matching(needle, fold_case=fold_case).text
    if not normalized_needle:
        return None

    occurrences: list[int] = []
    cursor = haystack.text.find(normalized_needle)
    while cursor != -1:
        occurrences.append(cursor)
        cursor = haystack.text.find(normalized_needle, cursor + 1)

    if not occurrences:
        return None

    if expected_source_start is None or len(occurrences) == 1:
        chosen = occurrences[0]
    else:
        chosen = min(
            occurrences,
            key=lambda pos: abs(haystack.source_offset(pos) - expected_source_start),
        )
    return haystack.source_span(chosen, chosen + len(normalized_needle))


def word_count(text: str) -> int:
    """Count word-like tokens, for rough length reporting."""
    return len(WORD_PATTERN.findall(text))


# ---------------------------------------------------------------- logging


#: Log fields that would carry confidential content. Context.md 26.F allows
#: metadata only: ids, agent, step, latency, tokens, status, error.
CONFIDENTIAL_FIELDS = frozenset(
    {
        "answer",
        "chunk",
        "chunk_text",
        "completion",
        "content",
        "document_text",
        "evidence",
        "prompt",
        "question",
        "text",
        "verified",
    }
)

#: Environment variables whose *values* must never reach a log line.
SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")

REDACTED = "[REDACTED]"


def secret_values() -> list[str]:
    """Current values of the environment's secrets, longest first.

    Longest first so that a secret containing another one is masked whole
    rather than being left half-visible.
    """
    values = [
        value
        for name, value in os.environ.items()
        if value and name.upper().endswith(SECRET_ENV_SUFFIXES)
    ]
    return sorted(values, key=len, reverse=True)


class RedactingFilter(logging.Filter):
    """Strip confidential content out of log records before they are emitted.

    Two rules, both from Context.md 26.F:

    1. Any value of a secret environment variable is masked wherever it
       appears in the message or its arguments.
    2. A record field named after document content is replaced entirely — the
       point is that raw document text never reaches a log, so it is masked
       rather than truncated.

    This is a backstop. Call sites are still expected to log metadata only;
    the filter exists so that a careless one cannot leak.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = secret_values()

        if isinstance(record.msg, str):
            record.msg = _mask(record.msg, secrets)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: _mask_arg(value, secrets) for key, value in record.args.items()
                }
            else:
                record.args = tuple(_mask_arg(a, secrets) for a in record.args)

        for field in CONFIDENTIAL_FIELDS:
            if hasattr(record, field):
                setattr(record, field, REDACTED)
        return True


def _mask(text: str, secrets: list[str]) -> str:
    """Replace every secret value found in `text`."""
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return text


def _mask_arg(value: object, secrets: list[str]) -> object:
    """Mask a log argument, leaving non-strings as they are.

    Non-strings are returned untouched because converting them would break the
    format specifier they were meant for — `%d` against a stringified int
    raises, which would turn a log line into an error.
    """
    if isinstance(value, str):
        return _mask(value, secrets)
    return value


#: The metadata NFR-006 and Context.md 26.F say a structured log line carries.
#: Anything absent is simply left out rather than filled with a placeholder.
#: `document_id` and `chunk_id` are included because 26.F explicitly allows a
#: chunk to be identified by id — it is the *text* that must never be logged.
STRUCTURED_FIELDS = (
    "investigation_id",
    "tenant_id",
    "document_id",
    "chunk_id",
    "agent",
    "step",
    "duration_ms",
    "tokens",
    "status",
    "error",
)


class StructuredFormatter(logging.Formatter):
    """Render a log record as JSON with the NFR-006 metadata fields.

    Only the fields in `STRUCTURED_FIELDS` are copied across, so a stray
    `extra` carrying document text cannot reach the output even if the
    redaction filter somehow missed it. The two controls are independent on
    purpose.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", structured: bool | None = None) -> None:
    """Set up application logging with redaction switched on.

    `structured` writes JSON lines carrying the NFR-006 metadata fields; it
    defaults to the `LOG_STRUCTURED` environment variable, off unless set.

    The filter goes on every handler rather than on the root logger, because a
    logger-level filter does not apply to records propagated up from child
    loggers — which is where most of this project's logging happens.
    """
    if structured is None:
        structured = os.environ.get("LOG_STRUCTURED", "").lower() in (
            "1",
            "true",
            "yes",
        )

    logging.basicConfig(level=level, format="%(levelname)s %(name)s %(message)s")
    redactor = RedactingFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
        if structured:
            handler.setFormatter(StructuredFormatter())
