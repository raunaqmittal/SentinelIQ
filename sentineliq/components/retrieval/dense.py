"""Dense retrieval: embed chunks with a sentence-transformer, search with FAISS.

Embeddings are normalized, so FAISS inner product equals cosine similarity.
"""

import logging
from pathlib import Path

import faiss
import numpy as np

from sentineliq.components.models.schemas import Chunk

logger = logging.getLogger(__name__)


def load_model(name: str) -> "SentenceTransformer":
    """Load an embedding model by name."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name)


def max_tokens(model) -> int:
    """Tokens the model really accepts.

    Read from the sentence-transformers config, not `AutoConfig`: some models
    advertise 512 there but truncate at 256 in practice.

    Raises:
        ValueError: The model does not declare a limit, so chunks could be
            silently truncated without us knowing where the cap is.
    """
    limit = model.max_seq_length
    if limit is None:
        raise ValueError(f"{model} does not declare max_seq_length")
    return limit


def embed(model, texts: list[str]) -> np.ndarray:
    """Embed texts into normalized vectors, one row per text."""
    return model.encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        batch_size=32,
    )


def build_index(model, chunks: list[Chunk]) -> faiss.Index:
    """Build a FAISS index over the chunk texts, in chunk order."""
    vectors = embed(model, [chunk.text for chunk in chunks])
    index = faiss.IndexFlatIP(vectors.shape[1])
    index.add(vectors)
    logger.info("Built dense index", extra={"chunks": len(chunks)})
    return index


def save_index(index: faiss.Index, path: Path) -> None:
    """Write the index to disk so the next run does not re-embed the corpus."""
    path.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(path))
    logger.info("Saved dense index", extra={"path": str(path)})


def load_index(path: Path) -> faiss.Index:
    """Read an index written by `save_index`.

    The chunk list must be rebuilt the same way, in the same order, as when the
    index was saved — the index stores vectors by position, not by chunk ID.
    """
    return faiss.read_index(str(path))


def search(
    model,
    index: faiss.Index,
    chunks: list[Chunk],
    query: str,
    top_k: int,
) -> list[tuple[str, float]]:
    """Return `(chunk_id, score)` most similar to the query, best first.

    The score is cosine similarity, because the vectors are normalized.

    `chunks` must be the same list, in the same order, used to build the index.
    """
    query_vector = embed(model, [query])
    scores, positions = index.search(query_vector, top_k)
    return [
        (chunks[position].chunk_id, float(score))
        for position, score in zip(positions[0], scores[0])
    ]

