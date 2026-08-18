"""Hybrid retrieval: dense + sparse, fused with RRF, then cross-encoder reranked.

`retrieve` is the selected pipeline, frozen 2026-08-15 after the DEV ablation
recorded in PROGRESS.md. Every parameter in it was chosen by measurement.
"""

import logging

import faiss
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder, SentenceTransformer

from sentineliq.components.models.schemas import Chunk
from sentineliq.components.retrieval import dense, reranker, sparse
from sentineliq.config import RetrievalConfig

logger = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]],
    k: int,
    top_k: int,
) -> list[str]:
    """Fuse ranked chunk-ID lists into one, best first.

    Each list contributes 1 / (k + rank) to a chunk's score, so a chunk found
    by both retrievers outranks one found by only a single retriever.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores, key=lambda chunk_id: scores[chunk_id], reverse=True)
    return ordered[:top_k]


def hybrid_search(
    model: SentenceTransformer,
    faiss_index: faiss.Index,
    bm25_index: BM25Okapi,
    chunks: list[Chunk],
    query: str,
    dense_top_k: int,
    sparse_top_k: int,
    rrf_k: int,
    top_k: int,
) -> list[str]:
    """Return the chunk IDs best matching the query, best first.

    `chunks` must be the same list, in the same order, used to build both
    indexes.
    """
    dense_hits = dense.search(model, faiss_index, chunks, query, dense_top_k)
    sparse_hits = sparse.search(bm25_index, chunks, query, sparse_top_k)
    # RRF uses rank only, so the scores are dropped here: dense cosine and BM25
    # scores are on different scales and cannot be compared directly.
    dense_ids = [chunk_id for chunk_id, _ in dense_hits]
    sparse_ids = [chunk_id for chunk_id, _ in sparse_hits]
    return reciprocal_rank_fusion([dense_ids, sparse_ids], rrf_k, top_k)


def retrieve(
    config: RetrievalConfig,
    model: SentenceTransformer,
    faiss_index: faiss.Index,
    bm25_index: BM25Okapi,
    cross_encoder: CrossEncoder,
    chunks: list[Chunk],
    query: str,
) -> list[tuple[str, float]]:
    """The selected retrieval pipeline. Returns `(chunk_id, score)`, best first.

    dense@50 + BM25@50 -> RRF(k=60) -> top 20 -> bge-reranker-v2-m3 -> top 5.

    The score is the cross-encoder's, comparable only within one query's
    results. `reranker.top_n` bounds how much confidential text reaches the LLM
    (Context.md 26.B), so it is a security setting as well as a quality one.
    """
    candidates = hybrid_search(
        model,
        faiss_index,
        bm25_index,
        chunks,
        query,
        dense_top_k=config.dense.top_k,
        sparse_top_k=config.sparse.top_k,
        rrf_k=config.rrf.k,
        top_k=config.rrf.rerank_depth,
    )
    return reranker.rerank(
        cross_encoder, chunks, query, candidates, config.reranker.top_n
    )
