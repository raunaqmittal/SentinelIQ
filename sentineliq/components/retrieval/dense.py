"""Dense retrieval: embed chunks with a sentence-transformer, search with FAISS.

Embeddings are normalized, so FAISS inner product equals cosine similarity.

Provider abstraction
--------------------
`load_model` returns either a `SentenceTransformer` (local, default) or a
`VoyageEmbedder` (hosted, Voyage AI API).  Both objects expose the same
`encode()` method, so `embed()`, `build_index()` and `search()` below are
unchanged regardless of which provider is active.
"""

import logging
import os
from pathlib import Path

import faiss
import numpy as np

from sentineliq.components.models.schemas import Chunk

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------- adapters


class VoyageEmbedder:
    """Thin adapter so Voyage AI's embedding API looks like SentenceTransformer.

    Implements only the `encode()` method that `embed()` below calls. FAISS,
    BM25, RRF and every downstream component are unaware of the difference.

    Requires `voyageai` package and `VOYAGE_API_KEY` environment variable.
    """

    def __init__(self, model: str) -> None:
        import voyageai  # lazy import — only needed when provider == "voyage"

        self._model = model
        self._client = voyageai.Client()
        logger.info("VoyageEmbedder initialised", extra={"model": model})

    def encode(
        self,
        texts: list[str],
        normalize_embeddings: bool = True,
        convert_to_numpy: bool = True,
        batch_size: int = 32,
    ) -> np.ndarray:
        """Embed texts via Voyage AI API.

        `normalize_embeddings` is accepted for interface compatibility but
        Voyage returns normalised vectors by default, so the flag is honoured
        implicitly. `batch_size` controls how many texts are sent per request.
        """
        # Voyage recommends batches of up to 128; honour the caller's cap.
        all_vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            result = self._client.embed(batch, model=self._model)
            all_vectors.extend(result.embeddings)
        vectors = np.array(all_vectors, dtype=np.float32)
        if normalize_embeddings:
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1.0, norms)
            vectors = vectors / norms
        return vectors


# ----------------------------------------------------------------- public API


def load_model(name: str, provider: str = "local") -> object:
    """Load an embedding model.

    Args:
        name: Model identifier. For ``provider="local"`` this is a
              HuggingFace model name. For ``provider="voyage"`` this is
              a Voyage AI model name (e.g. ``"voyage-law-2"``).
        provider: ``"local"`` (default) or ``"voyage"``.

    Returns a ``SentenceTransformer`` or a ``VoyageEmbedder`` — both expose
    the same ``encode()`` interface so callers do not need to know which.
    """
    if provider == "voyage":
        if not os.environ.get("VOYAGE_API_KEY"):
            logger.warning(
                "RETRIEVAL_EMBEDDING_PROVIDER=voyage but VOYAGE_API_KEY is not set "
                "— falling back to local SentenceTransformer. Set VOYAGE_API_KEY to "
                "use hosted inference."
            )
        else:
            return VoyageEmbedder(name)
    # Local path — unchanged from the original implementation
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

