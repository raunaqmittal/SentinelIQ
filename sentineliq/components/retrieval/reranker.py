"""Cross-encoder reranking of a retrieved candidate pool.

Dense and BM25 are bi-encoders: they score query and chunk separately, which is
fast enough for the whole corpus but shallow. A cross-encoder reads the query
and the chunk together, so it judges relevance far better — but it must score
every pair, so it only ever sees the candidates retrieval already shortlisted.
"""

import logging

from sentineliq.components.models.schemas import Chunk

logger = logging.getLogger(__name__)


def load_model(name: str, *, fp16: bool = False) -> "CrossEncoder":
    """Load a cross-encoder reranking model by name."""
    import torch
    from sentence_transformers import CrossEncoder

    kwargs = {"torch_dtype": torch.float16} if fp16 else {}
    return CrossEncoder(name, max_length=512, model_kwargs=kwargs)


def rerank(
    model,
    chunks: list[Chunk],
    query: str,
    candidate_ids: list[str],
    top_n: int,
) -> list[tuple[str, float]]:
    """Re-score candidate chunk IDs against the query, best first.

    Returns ``(chunk_id, score)``. Scores are raw model logits (local) or
    relevance scores (hosted): they are only comparable within one query's
    results.
    """
    texts = {chunk.chunk_id: chunk.text for chunk in chunks}
    # Annotated because `PairInput` is a union and `list` is invariant, so a
    # plain list of tuples does not match `predict`'s signature.
    pairs = [(query, texts[chunk_id]) for chunk_id in candidate_ids]
    scores = model.predict(pairs)
    ranked = sorted(zip(candidate_ids, scores), key=lambda pair: pair[1], reverse=True)
    return [(chunk_id, float(score)) for chunk_id, score in ranked[:top_n]]
