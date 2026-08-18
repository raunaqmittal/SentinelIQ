"""Spike helpers not (yet) part of production.

EXPERIMENT CODE. Text normalization now lives in `sentineliq.utils` — this
module re-exports it so the spike exercises the promoted production function
rather than a divergent copy.
"""

import json
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# The spike runs from the repo root; make the package importable either way.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sentineliq.utils import (  # noqa: E402
    NormalizedText,
    find_evidence_span,
    normalize_for_matching,
)

__all__ = [
    "NormalizedText",
    "SpikeDocument",
    "find_evidence_span",
    "load_sample_documents",
    "normalize_for_matching",
    "normalize_with_map",
    "page_ranges",
    "page_for_offset",
]

PageRange = tuple[int, int, int]
CharSpan = tuple[int, int]

CUAD_ZIP = Path("data/evaluation/datasets/CUAD_v1.zip")
SAMPLE_DIR = Path("data/evaluation/datasets/sample")
RESULTS_DIR = Path("data/evaluation/datasets/spike_results")


def normalize_with_map(text: str, fold_case: bool = True) -> tuple[str, list[int]]:
    """Legacy spike signature, kept so earlier scripts still run unchanged.

    Prefer `sentineliq.utils.normalize_for_matching`.
    """
    result = normalize_for_matching(text, fold_case=fold_case)
    return result.text, list(result.source_offsets)


def page_ranges(page_texts: list[str], sep: str = "\n") -> list[PageRange]:
    """Character range of each page inside the concatenated document text.

    Returns (page_number, start, end) triples, 1-indexed, end-exclusive.
    """
    ranges: list[PageRange] = []
    position = 0
    for index, text in enumerate(page_texts):
        start = position
        position += len(text)
        ranges.append((index + 1, start, position))
        position += len(sep)
    return ranges


def page_for_offset(ranges: list[PageRange], offset: int) -> int | None:
    """Page number containing `offset`, or the last page if past the end."""
    for page_number, start, end in ranges:
        if start <= offset < end:
            return page_number
    return ranges[-1][0] if ranges else None


@dataclass
class SpikeDocument:
    """One sampled contract: extracted text, page map and mapped evidence spans."""

    stem: str
    text: str
    page_ranges: list[PageRange]
    spans: list[CharSpan] = field(default_factory=list)
    token_offsets: list[tuple[int, int]] = field(default_factory=list)


def load_sample_documents(tokenizer: object | None = None) -> list[SpikeDocument]:
    """Load the sampled contracts with CUAD spans mapped onto PDF offsets.

    Shared by several spike steps so the extraction and mapping logic exists
    in exactly one place. Pass a fast tokenizer to also populate
    `token_offsets`.
    """
    import pymupdf

    manifest = json.loads((SAMPLE_DIR / "manifest.json").read_text())
    with zipfile.ZipFile(CUAD_ZIP) as archive:
        payload = json.loads(archive.read("CUAD_v1/CUAD_v1.json"))
    annotations = {entry["title"]: entry for entry in payload["data"]}

    documents: list[SpikeDocument] = []
    for record in manifest:
        stem = record["stem"]
        with pymupdf.open(SAMPLE_DIR / f"{stem}.pdf") as pdf:
            page_texts = [page.get_text("text") for page in pdf]
        text = "\n".join(page_texts)
        normalized = normalize_for_matching(text)

        spans: list[CharSpan] = []
        for qa in annotations[stem]["paragraphs"][0]["qas"]:
            for answer in qa["answers"]:
                span = find_evidence_span(
                    normalized,
                    answer["text"],
                    expected_source_start=answer["answer_start"],
                )
                if span is not None:
                    spans.append(span)

        token_offsets: list[tuple[int, int]] = []
        if tokenizer is not None:
            token_offsets = tokenizer(  # type: ignore[operator]
                text,
                return_offsets_mapping=True,
                add_special_tokens=False,
                truncation=False,
                verbose=False,
            )["offset_mapping"]

        documents.append(
            SpikeDocument(
                stem=stem,
                text=text,
                page_ranges=page_ranges(page_texts),
                spans=spans,
                token_offsets=token_offsets,
            )
        )
    return documents
