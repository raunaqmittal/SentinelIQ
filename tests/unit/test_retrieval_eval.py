"""Unit tests for deriving chunk-level relevance from ground-truth spans."""

import json

import pytest

from sentineliq.components.evaluation.retrieval_eval import (
    chunk_corpus,
    chunks_for_span,
    context_precision,
    evaluate_retrieval,
    load_document,
    load_relevance,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from sentineliq.components.models.schemas import Chunk
from sentineliq.exceptions import DocumentLoadError


def count_words(text: str) -> int:
    """Stand-in tokenizer: one word, one token."""
    return len(text.split())


def make_chunk(chunk_id: str, document_name: str, start: int, end: int) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id="d",
        document_name=document_name,
        text="x" * (end - start),
        char_start=start,
        char_end=end,
    )


@pytest.fixture
def chunks():
    return [
        make_chunk("a_0000", "a.txt", 0, 100),
        make_chunk("a_0001", "a.txt", 90, 200),
        make_chunk("b_0000", "b.txt", 0, 100),
    ]


def test_span_inside_one_chunk(chunks):
    assert chunks_for_span(chunks, "a.txt", 10, 20) == ["a_0000"]


def test_span_in_the_overlap_belongs_to_both_chunks(chunks):
    """Chunks overlap, so retrieving either one finds the evidence."""
    assert chunks_for_span(chunks, "a.txt", 92, 95) == ["a_0000", "a_0001"]


def test_span_crossing_a_boundary_returns_both(chunks):
    assert chunks_for_span(chunks, "a.txt", 50, 150) == ["a_0000", "a_0001"]


def test_span_is_matched_per_document(chunks):
    assert chunks_for_span(chunks, "b.txt", 10, 20) == ["b_0000"]


def test_span_touching_a_chunk_end_does_not_match_the_next(chunks):
    """char_end is exclusive, so a span ending at 90 is not in the 90.. chunk."""
    assert chunks_for_span(chunks, "a.txt", 80, 90) == ["a_0000"]


def test_span_outside_every_chunk_returns_nothing(chunks):
    assert chunks_for_span(chunks, "a.txt", 500, 600) == []


def write_ground_truth(tmp_path, entries):
    path = tmp_path / "ground_truth.json"
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return path


def test_load_relevance_maps_questions_to_chunk_ids(tmp_path, chunks):
    path = write_ground_truth(
        tmp_path,
        [
            {
                "question_id": "Q001",
                "evidence": [
                    {"document_name": "a.txt", "char_start": 10, "char_end": 20},
                    {"document_name": "b.txt", "char_start": 5, "char_end": 8},
                ],
            }
        ],
    )
    assert load_relevance(path, chunks) == {"Q001": ["a_0000", "b_0000"]}


def test_load_relevance_skips_questions_without_evidence(tmp_path, chunks):
    """Structured XBRL answers are not retrieval questions."""
    path = write_ground_truth(
        tmp_path, [{"question_id": "Q014", "expected": {"value": 1.0}}]
    )
    assert load_relevance(path, chunks) == {}


def test_load_relevance_does_not_repeat_a_chunk_id(tmp_path, chunks):
    path = write_ground_truth(
        tmp_path,
        [
            {
                "question_id": "Q001",
                "evidence": [
                    {"document_name": "a.txt", "char_start": 10, "char_end": 20},
                    {"document_name": "a.txt", "char_start": 30, "char_end": 40},
                ],
            }
        ],
    )
    assert load_relevance(path, chunks) == {"Q001": ["a_0000"]}


def test_load_relevance_rejects_a_span_that_matches_no_chunk(tmp_path, chunks):
    """A silently unmatched span would make that question permanently unscoreable."""
    path = write_ground_truth(
        tmp_path,
        [
            {
                "question_id": "Q001",
                "evidence": [
                    {"document_name": "a.txt", "char_start": 5000, "char_end": 5010}
                ],
            }
        ],
    )
    with pytest.raises(ValueError, match="no chunk covers"):
        load_relevance(path, chunks)


def test_load_document_rejects_an_unsupported_extension(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("some text that is long enough to load")
    with pytest.raises(DocumentLoadError, match="No loader for .md"):
        load_document(path)


def test_chunk_corpus_ignores_unsupported_files(tmp_path):
    (tmp_path / "policy.txt").write_text("Vendor policy text " * 20, encoding="utf-8")
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    chunks = chunk_corpus(tmp_path, count_words, chunk_size=50, chunk_overlap=5)
    assert {chunk.document_name for chunk in chunks} == {"policy.txt"}


def test_chunk_corpus_uses_the_file_stem_as_document_id(tmp_path):
    """Stable chunk IDs across runs depend on a stable document ID."""
    (tmp_path / "policy.txt").write_text("Vendor policy text " * 20, encoding="utf-8")
    chunks = chunk_corpus(tmp_path, count_words, chunk_size=50, chunk_overlap=5)
    assert chunks[0].chunk_id == "policy_0000"


def test_recall_counts_only_the_top_k():
    assert recall_at_k(["a", "b", "c"], ["a", "c"], k=2) == 0.5


def test_recall_is_one_when_everything_relevant_is_found():
    assert recall_at_k(["a", "c", "b"], ["a", "c"], k=3) == 1.0


def test_recall_is_zero_when_nothing_relevant_is_found():
    assert recall_at_k(["x", "y"], ["a"], k=2) == 0.0


def test_reciprocal_rank_uses_the_first_relevant_result():
    assert reciprocal_rank(["x", "a", "b"], ["a", "b"]) == 0.5


def test_reciprocal_rank_is_zero_when_nothing_is_retrieved():
    assert reciprocal_rank(["x", "y"], ["a"]) == 0.0


def test_ndcg_is_one_when_relevant_results_come_first():
    assert ndcg_at_k(["a", "b", "x"], ["a", "b"], k=3) == 1.0


def test_ndcg_drops_when_relevant_results_rank_lower():
    top = ndcg_at_k(["a", "x", "y"], ["a"], k=3)
    lower = ndcg_at_k(["x", "y", "a"], ["a"], k=3)
    assert lower < top == 1.0


def test_ndcg_is_zero_when_nothing_relevant_is_retrieved():
    assert ndcg_at_k(["x", "y"], ["a"], k=2) == 0.0


def test_precision_counts_only_the_top_k():
    # 2 of the top 3 are relevant.
    assert precision_at_k(["a", "b", "x", "c"], ["a", "b", "c"], k=3) == pytest.approx(
        2 / 3
    )


def test_precision_is_one_when_every_result_is_relevant():
    assert precision_at_k(["a", "b"], ["a", "b", "c"], k=2) == 1.0


def test_precision_does_not_punish_a_run_shorter_than_k():
    """3 results, all relevant, k=10 — that is precision 1.0, not 0.3."""
    assert precision_at_k(["a", "b", "c"], ["a", "b", "c"], k=10) == 1.0


def test_precision_is_zero_when_nothing_was_retrieved():
    assert precision_at_k([], ["a"], k=5) == 0.0


def test_context_precision_is_one_when_relevant_results_come_first():
    assert context_precision(["a", "b", "x"], ["a", "b"], k=3) == 1.0


def test_context_precision_drops_when_relevant_results_rank_lower():
    top = context_precision(["a", "x", "y"], ["a"], k=3)
    lower = context_precision(["x", "y", "a"], ["a"], k=3)
    assert lower < top == 1.0


def test_context_precision_can_still_reach_one_with_more_relevant_than_k():
    """The reachable denominator is capped at k, unlike MAP's."""
    assert context_precision(["a", "b"], ["a", "b", "c", "d"], k=2) == 1.0


def test_context_precision_is_zero_when_nothing_relevant_is_retrieved():
    assert context_precision(["x", "y"], ["a"], k=2) == 0.0


def test_evaluate_retrieval_reports_every_required_metric():
    summary = evaluate_retrieval(
        {"Q1": ["a", "x"], "Q2": ["y", "b"]},
        {"Q1": ["a"], "Q2": ["b"]},
        k=2,
    )
    assert set(summary) == {
        "n",
        "k",
        "recall_at_k",
        "precision_at_k",
        "mrr",
        "ndcg_at_k",
        "map",
        "context_precision",
    }
    assert summary["n"] == 2
    assert summary["recall_at_k"] == 1.0
    # Q1 hits at rank 1, Q2 at rank 2 -> mean precision@2 of 0.5.
    assert summary["precision_at_k"] == 0.5
    assert summary["mrr"] == 0.75
