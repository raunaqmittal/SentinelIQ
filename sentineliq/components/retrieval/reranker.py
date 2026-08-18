"""Cross-encoder reranking of a retrieved candidate pool.

Dense and BM25 are bi-encoders: they score query and chunk separately, which is
fast enough for the whole corpus but shallow. A cross-encoder reads the query
and the chunk together, so it judges relevance far better — but it must score
every pair, so it only ever sees the candidates retrieval already shortlisted.
"""

import logging

import torch
from sentence_transformers import CrossEncoder
from sentence_transformers.cross_encoder.model import PairInput

from sentineliq.components.models.schemas import Chunk

logger = logging.getLogger(__name__)


def load_model(name: str, *, fp16: bool = False) -> CrossEncoder:
    """Load a cross-encoder by name.

    `max_length` covers query + chunk together, so a full-size chunk gets its
    tail truncated. That is the normal trade-off for this model family.

    Args:
        name: Model id.
        fp16: Load in half precision. Halves VRAM (~1.1GB instead of ~2.2GB for
            bge-reranker-v2-m3), which is what lets it sit beside the embedding
            model on a 4GB GPU.
    """
    kwargs = {"torch_dtype": torch.float16} if fp16 else {}
    return CrossEncoder(name, max_length=512, model_kwargs=kwargs)


def rerank(
    model: CrossEncoder,
    chunks: list[Chunk],
    query: str,
    candidate_ids: list[str],
    top_n: int,
) -> list[tuple[str, float]]:
    """Re-score candidate chunk IDs against the query, best first.

    Returns `(chunk_id, score)`. Scores are raw model logits: they can be
    negative and are only comparable within one query's results.
    """
    texts = {chunk.chunk_id: chunk.text for chunk in chunks}
    # Annotated because `PairInput` is a union and `list` is invariant, so a
    # plain list of tuples does not match `predict`'s signature.
    pairs: list[PairInput] = [(query, texts[chunk_id]) for chunk_id in candidate_ids]
    scores = model.predict(pairs)
    ranked = sorted(zip(candidate_ids, scores), key=lambda pair: pair[1], reverse=True)
    return [(chunk_id, float(score)) for chunk_id, score in ranked[:top_n]]
