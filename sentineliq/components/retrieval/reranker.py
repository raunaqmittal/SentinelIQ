"""Cross-encoder reranking of a retrieved candidate pool.

Dense and BM25 are bi-encoders: they score query and chunk separately, which is
fast enough for the whole corpus but shallow. A cross-encoder reads the query
and the chunk together, so it judges relevance far better — but it must score
every pair, so it only ever sees the candidates retrieval already shortlisted.

Provider abstraction
--------------------
`load_model` returns either a ``CrossEncoder`` (local, default) or a
``VoyageReranker`` (hosted, Voyage AI API). Both expose the same ``predict()``
method, so ``rerank()`` below is unchanged regardless of which provider is
active.
"""

import logging
import os

from sentineliq.components.models.schemas import Chunk

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------- adapters


class VoyageReranker:
    """Thin adapter so Voyage AI's reranking API looks like CrossEncoder.

    Implements only the ``predict(pairs)`` method that ``rerank()`` below
    calls. The rest of the pipeline — RRF, retrieval, agents — is unaware
    of the difference.

    Requires ``voyageai`` package and ``VOYAGE_API_KEY`` environment variable.
    """

    def __init__(self, model: str) -> None:
        import voyageai  # lazy import — only needed when provider == "voyage"

        self._model = model
        self._client = voyageai.Client()
        logger.info("VoyageReranker initialised", extra={"model": model})

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Re-score query–document pairs via Voyage AI API.

        Args:
            pairs: List of ``(query, document_text)`` tuples — same shape as
                   ``CrossEncoder.predict``.

        Returns:
            Relevance scores in the same order, as floats. Comparable only
            within one query's results, same as the CrossEncoder.
        """
        if not pairs:
            return []
        queries = [q for q, _ in pairs]
        documents = [d for _, d in pairs]
        # Voyage rerank takes a single query and multiple documents.
        # All pairs here share the same query (one retrieval call = one question),
        # so we can batch them in a single API call.
        query = queries[0]
        result = self._client.rerank(query, documents, model=self._model)
        # result.results is sorted by relevance; we need scores in original order.
        scores_by_index = {r.index: r.relevance_score for r in result.results}
        return [scores_by_index.get(i, 0.0) for i in range(len(documents))]


# ----------------------------------------------------------------- public API


def load_model(name: str, *, fp16: bool = False, provider: str = "local") -> object:
    """Load a reranking model.

    Args:
        name: Model identifier. For ``provider="local"`` this is a HuggingFace
              model name. For ``provider="voyage"`` this is a Voyage AI model
              name (e.g. ``"rerank-2"``).
        fp16: Half precision for the local CrossEncoder. Ignored for hosted
              providers.
        provider: ``"local"`` (default) or ``"voyage"``.

    Returns a ``CrossEncoder`` or a ``VoyageReranker`` — both expose the same
    ``predict()`` interface so callers do not need to know which.
    """
    if provider == "voyage":
        if not os.environ.get("VOYAGE_API_KEY"):
            logger.warning(
                "RETRIEVAL_RERANKER_PROVIDER=voyage but VOYAGE_API_KEY is not set "
                "— falling back to local CrossEncoder. Set VOYAGE_API_KEY to "
                "use hosted inference."
            )
        else:
            return VoyageReranker(name)
    # Local path — unchanged from the original implementation
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
