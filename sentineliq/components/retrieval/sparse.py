"""Sparse retrieval: BM25 keyword search over chunk texts.

Complements dense retrieval, which misses exact tokens like clause numbers,
certification names and figures such as `713,163`.
"""

import logging
import pickle
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

from sentineliq.components.models.schemas import Chunk

logger = logging.getLogger(__name__)

# Words and numbers. Inner "," and "." are kept so `713,163` and `1.5` stay
# whole. Splitting on " " would be wrong here — CUAD PDFs separate words with
# non-breaking spaces (\xa0), which this pattern handles.
TOKEN_PATTERN = re.compile(r"\w+(?:[.,]\w+)*")


def tokenize(text: str) -> list[str]:
    """Split text into lowercase BM25 tokens."""
    return TOKEN_PATTERN.findall(text.lower())


def build_index(chunks: list[Chunk]) -> BM25Okapi:
    """Build a BM25 index over the chunk texts, in chunk order."""
    index = BM25Okapi([tokenize(chunk.text) for chunk in chunks])
    logger.info("Built sparse index", extra={"chunks": len(chunks)})
    return index


def save_index(index: BM25Okapi, path: Path) -> None:
    """Write the index to disk so the next run does not re-tokenize the corpus.

    Pickle, because `BM25Okapi` has no format of its own. Only ever load a file
    this project wrote — unpickling untrusted data executes arbitrary code.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(index))
    logger.info("Saved sparse index", extra={"path": str(path)})


def load_index(path: Path) -> BM25Okapi:
    """Read an index written by `save_index`.

    The chunk list must be rebuilt the same way, in the same order, as when the
    index was saved — the index stores documents by position, not by chunk ID.
    """
    index: BM25Okapi = pickle.loads(path.read_bytes())
    return index


def search(
    index: BM25Okapi,
    chunks: list[Chunk],
    query: str,
    top_k: int,
) -> list[tuple[str, float]]:
    """Return `(chunk_id, score)` scoring highest for the query, best first.

    BM25 scores are unbounded and only comparable within one query's results.

    `chunks` must be the same list, in the same order, used to build the index.
    """
    scores = index.get_scores(tokenize(query))
    positions = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
    return [(chunks[p].chunk_id, float(scores[p])) for p in positions[:top_k]]
