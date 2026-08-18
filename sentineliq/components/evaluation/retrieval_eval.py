"""Turn the evaluation set into chunk-level relevance judgements.

`ground_truth.json` stores evidence as character spans into the text the
project loaders produce, so re-tuning `chunk_size` never invalidates it.
Retrieval metrics need chunk IDs, so they are derived here at evaluation time.
"""

import json
import logging
import math
from collections.abc import Callable
from pathlib import Path

from sentineliq.components.ingestion.chunker import chunk_document
from sentineliq.components.ingestion.loader import load_docx, load_pdf, load_txt
from sentineliq.components.models.schemas import Chunk, LoadedDocument
from sentineliq.exceptions import DocumentLoadError

logger = logging.getLogger(__name__)

LOADERS = {".pdf": load_pdf, ".txt": load_txt, ".docx": load_docx}


def load_document(path: Path) -> LoadedDocument:
    """Load one corpus document, picking the loader by file extension.

    The document ID is the file stem, so chunk IDs stay stable across runs.
    """
    loader = LOADERS.get(path.suffix)
    if loader is None:
        raise DocumentLoadError(f"No loader for {path.suffix} files: {path.name}")
    return loader(path, document_id=path.stem)


def chunk_corpus(
    directory: Path,
    count_tokens: Callable[[str], int],
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    """Load and chunk every supported document in a directory."""
    chunks = []
    for path in sorted(directory.iterdir()):
        if path.suffix in LOADERS:
            document = load_document(path)
            chunks.extend(
                chunk_document(
                    document,
                    count_tokens,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                )
            )
    logger.info("Chunked corpus", extra={"chunks": len(chunks)})
    return chunks


def chunks_for_span(
    chunks: list[Chunk], document_name: str, char_start: int, char_end: int
) -> list[str]:
    """IDs of every chunk overlapping an evidence span.

    Chunks overlap, so one span can sit in more than one chunk. Retrieving
    any of them counts as finding the evidence.
    """
    return [
        chunk.chunk_id
        for chunk in chunks
        if chunk.document_name == document_name
        and chunk.char_start < char_end
        and char_start < chunk.char_end
    ]


def load_relevance(path: Path, chunks: list[Chunk]) -> dict[str, list[str]]:
    """Map each question to the chunk IDs that answer it.

    Questions whose answer is a structured XBRL value carry no evidence spans
    and are skipped — they are not retrieval questions.

    Raises:
        ValueError: An evidence span matches no chunk, which would silently
            make that question unscoreable.
    """
    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]

    relevance = {}
    for entry in entries:
        if "evidence" not in entry:
            continue
        ids: list[str] = []
        for evidence in entry["evidence"]:
            found = chunks_for_span(
                chunks,
                evidence["document_name"],
                evidence["char_start"],
                evidence["char_end"],
            )
            if not found:
                raise ValueError(
                    f"{entry['question_id']}: no chunk covers "
                    f"{evidence['document_name']} "
                    f"[{evidence['char_start']}:{evidence['char_end']}]"
                )
            ids.extend(id for id in found if id not in ids)
        relevance[entry["question_id"]] = ids

    logger.info("Loaded relevance judgements", extra={"questions": len(relevance)})
    return relevance


def recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    """Share of the relevant chunks that appear in the top k results."""
    if not relevant:
        return 0.0
    found = [chunk_id for chunk_id in retrieved[:k] if chunk_id in relevant]
    return len(found) / len(relevant)


def precision_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    """Share of the top k results that are relevant (FR-020).

    The counterpart to `recall_at_k`: recall asks how much of the answer was
    found, precision asks how much of what was returned was worth reading.

    Divided by how many results there actually are, capped at k — a run that
    returns 3 chunks when k is 10 should not be punished for the 7 it never
    returned.
    """
    window = retrieved[:k]
    if not window:
        return 0.0
    found = [chunk_id for chunk_id in window if chunk_id in relevant]
    return len(found) / len(window)


def context_precision(retrieved: list[str], relevant: list[str], k: int) -> float:
    """Rank-weighted precision over the top k (FR-020).

    The standard formulation: precision@i is measured at every position i that
    holds a relevant chunk, and those values are averaged. Relevant chunks near
    the top therefore score higher than the same chunks near the bottom, which
    is what matters when the top k become an LLM's context window.

    Differs from `average_precision` only in the denominator — this divides by
    how many relevant chunks are reachable inside k, so a question with more
    relevant chunks than k can still reach 1.0. (`average_precision` divides by
    all of them, by definition of MAP.)
    """
    if not relevant:
        return 0.0

    hits = 0
    total = 0.0
    for position, chunk_id in enumerate(retrieved[:k], start=1):
        if chunk_id in relevant:
            hits += 1
            total += hits / position

    reachable = min(k, len(relevant))
    return total / reachable


def reciprocal_rank(retrieved: list[str], relevant: list[str]) -> float:
    """1/rank of the first relevant result, or 0.0 if none was retrieved."""
    for position, chunk_id in enumerate(retrieved, start=1):
        if chunk_id in relevant:
            return 1 / position
    return 0.0


def ndcg_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    """Ranking quality in the top k, where hitting a higher rank scores more.

    Every relevant chunk counts the same, so the best possible ranking is all
    of them at the top — but only `k` of them can fit in the top k. The ideal
    is therefore capped at `k`, otherwise a question with more relevant chunks
    than `k` could never reach 1.0 no matter how perfect the ranking
    (CUAD questions have up to 20).
    """
    if not relevant:
        return 0.0

    gain = 0.0
    for position, chunk_id in enumerate(retrieved[:k], start=1):
        if chunk_id in relevant:
            gain += 1 / math.log2(position + 1)

    reachable = min(k, len(relevant))
    best = sum(1 / math.log2(position + 1) for position in range(1, reachable + 1))
    return gain / best


def average_precision(retrieved: list[str], relevant: list[str]) -> float:
    """Precision averaged over the positions where a relevant chunk was found.

    Rewards putting relevant chunks early. MAP is the mean of this across
    questions.
    """
    if not relevant:
        return 0.0

    hits = 0
    total = 0.0
    for position, chunk_id in enumerate(retrieved, start=1):
        if chunk_id in relevant:
            hits += 1
            total += hits / position
    return total / len(relevant)


def evaluate_retrieval(
    retrieved_by_question: dict[str, list[str]],
    relevance: dict[str, list[str]],
    k: int,
) -> dict:
    """Score a whole retrieval run.

    Returns Recall@K, Precision@K, MRR, NDCG@K, MAP and Context Precision.

    Only questions present in both inputs are scored, so a question without
    relevance judgements is skipped rather than counted as a miss.
    """
    # 1. Keep only the questions we can actually score.
    question_ids = [q for q in retrieved_by_question if q in relevance]
    if not question_ids:
        raise ValueError("no questions have both results and relevance judgements")

    # 2. Score each question.
    recalls = []
    precisions_at_k = []
    ranks = []
    ndcgs = []
    precisions = []
    context_precisions = []
    for question_id in question_ids:
        retrieved = retrieved_by_question[question_id]
        relevant = relevance[question_id]
        recalls.append(recall_at_k(retrieved, relevant, k))
        precisions_at_k.append(precision_at_k(retrieved, relevant, k))
        ranks.append(reciprocal_rank(retrieved, relevant))
        ndcgs.append(ndcg_at_k(retrieved, relevant, k))
        precisions.append(average_precision(retrieved, relevant))
        context_precisions.append(context_precision(retrieved, relevant, k))

    # 3. Average across questions.
    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 4)

    return {
        "n": len(question_ids),
        "k": k,
        "recall_at_k": mean(recalls),
        "precision_at_k": mean(precisions_at_k),
        "mrr": mean(ranks),
        "ndcg_at_k": mean(ndcgs),
        "map": mean(precisions),
        "context_precision": mean(context_precisions),
    }
