"""Unit tests for the retrieval metrics and the reliability summary (Stage 11)."""

import json
import math
from pathlib import Path

import pytest

from sentineliq.components.evaluation import rag_eval
from sentineliq.components.evaluation.retrieval_eval import (
    average_precision,
    evaluate_retrieval,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
)

RELEVANT = ["c1", "c2"]


def test_average_precision_is_one_when_all_relevant_come_first():
    assert average_precision(["c1", "c2", "c9"], RELEVANT) == 1.0


def test_average_precision_is_zero_when_nothing_relevant_is_found():
    assert average_precision(["c8", "c9"], RELEVANT) == 0.0


def test_average_precision_rewards_an_earlier_hit():
    early = average_precision(["c1", "c9", "c2"], RELEVANT)
    late = average_precision(["c9", "c8", "c1", "c2"], RELEVANT)
    assert early > late


def test_average_precision_with_no_relevant_chunks_is_zero():
    assert average_precision(["c1"], []) == 0.0


def test_evaluate_retrieval_averages_every_metric():
    retrieved = {"Q1": ["c1", "c2"], "Q2": ["c9", "c3"]}
    relevance = {"Q1": ["c1", "c2"], "Q2": ["c3"]}
    result = evaluate_retrieval(retrieved, relevance, k=5)

    assert result["n"] == 2
    assert result["k"] == 5
    # Q1 finds both, Q2 finds its one at rank 2 -> recall 1.0 on both
    assert result["recall_at_k"] == 1.0
    # Q1 first hit at rank 1, Q2 at rank 2 -> (1 + 0.5) / 2
    assert result["mrr"] == 0.75
    assert 0.0 < result["ndcg_at_k"] <= 1.0
    assert 0.0 < result["map"] <= 1.0


def test_evaluate_retrieval_skips_questions_without_judgements():
    retrieved = {"Q1": ["c1"], "Q_unlabelled": ["c5"]}
    relevance = {"Q1": ["c1"]}
    assert evaluate_retrieval(retrieved, relevance, k=5)["n"] == 1


def test_evaluate_retrieval_raises_when_nothing_can_be_scored():
    with pytest.raises(ValueError, match="no questions"):
        evaluate_retrieval({"Q1": ["c1"]}, {"Q2": ["c2"]}, k=5)


def test_the_existing_metrics_still_behave():
    # Stage 5-7 relied on these; Stage 11 must not have changed them.
    assert recall_at_k(["c1", "c2"], RELEVANT, k=2) == 1.0
    assert reciprocal_rank(["c9", "c1"], RELEVANT) == 0.5
    assert ndcg_at_k(["c1", "c2"], RELEVANT, k=2) == 1.0


@pytest.mark.parametrize("metric", [recall_at_k, ndcg_at_k])
def test_no_relevant_chunks_scores_zero_instead_of_crashing(metric):
    # `load_relevance` skips these today, but a divide-by-zero would be a
    # crash rather than a measurement.
    assert metric(["c1"], [], 5) == 0.0


def test_ndcg_can_reach_one_when_there_are_more_relevant_chunks_than_k():
    """Regression: the ideal must be capped at k.

    CUAD questions carry up to 20 relevant chunks against k=5. With an
    uncapped ideal, a perfect top-5 scored about 0.42 and NDCG could never
    reach 1.0 — which understated every such question.
    """
    many_relevant = [f"c{i}" for i in range(20)]
    perfect_top_5 = many_relevant[:5]
    assert ndcg_at_k(perfect_top_5, many_relevant, k=5) == pytest.approx(1.0)


def test_ndcg_still_penalises_a_worse_ranking_of_many_relevant_chunks():
    many_relevant = [f"c{i}" for i in range(20)]
    good = ndcg_at_k(["c0", "c1", "junk", "junk2", "c2"], many_relevant, k=5)
    bad = ndcg_at_k(["junk", "junk2", "junk3", "c0", "c1"], many_relevant, k=5)
    assert good > bad
    assert bad < 1.0


def test_ndcg_is_unchanged_when_relevant_fits_inside_k():
    # The fix must not move any figure already recorded for the exploratory
    # suite, where questions have at most 3 relevant chunks.
    assert ndcg_at_k(["c1", "c2"], RELEVANT, k=5) == pytest.approx(1.0)
    assert ndcg_at_k(["c9", "c1", "c2"], RELEVANT, k=5) == pytest.approx(
        (1 / math.log2(3) + 1 / math.log2(4)) / (1 / math.log2(2) + 1 / math.log2(3))
    )


# ---------------------------------------------------- reliability summary


def record(
    answerable=True,
    abstained=False,
    validity=1.0,
    accuracy=0.5,
    grounding=1.0,
    hit=True,
):
    """One evaluation record with only the fields the summary reads."""
    return {
        "answerable": answerable,
        "abstained": abstained,
        "abstention_correct": abstained != answerable,
        "citation_validity": validity,
        "citation_accuracy": accuracy,
        "numeric_grounding": grounding,
        "retrieval_hit": hit,
        "input_tokens": 100,
        "output_tokens": 10,
    }


RECORDS = [
    record(answerable=True, abstained=False, accuracy=0.5, grounding=1.0, hit=True),
    record(answerable=True, abstained=True, accuracy=0.0, grounding=0.0, hit=False),
    record(answerable=False, abstained=True, accuracy=0.0, grounding=0.0, hit=False),
]


def test_each_metric_is_averaged_over_its_own_group():
    summary = rag_eval.summarize_records(RECORDS)
    assert summary["groups"] == {
        "total": 3,
        "answerable": 2,
        "controls": 1,
        "answered": 1,
    }
    # accuracy and grounding: the one answered question only
    assert summary["citation_accuracy"] == 0.5
    assert summary["numeric_grounding"] == 1.0
    # validity and hit rate: both answerable questions
    assert summary["citation_validity"] == 1.0
    assert summary["retrieval_hit_rate"] == 0.5
    # abstention: controls, answerable, and everything, respectively
    assert summary["abstention_rate_controls"] == 1.0
    assert summary["false_abstention_rate"] == 0.5


def test_a_metric_with_no_rows_is_none_not_zero():
    # Every question abstained, so nothing was answered.
    only_abstentions = [record(answerable=True, abstained=True)]
    summary = rag_eval.summarize_records(only_abstentions)
    assert summary["citation_accuracy"] is None
    assert summary["abstention_rate_controls"] is None


def test_summarize_records_ignores_failed_rows():
    summary = rag_eval.summarize_records(RECORDS + [{"error": "429"}])
    assert summary["groups"]["total"] == 3


def test_summarize_records_needs_records():
    with pytest.raises(ValueError, match="no records"):
        rag_eval.summarize_records([])


def test_load_reliability_summary_reads_the_stage8_file_shape(tmp_path):
    path = tmp_path / "results.json"
    path.write_text(json.dumps({"summary": {}, "records": RECORDS}), encoding="utf-8")
    assert rag_eval.load_reliability_summary(path)["computed"]["groups"]["total"] == 3


def test_load_reliability_summary_reads_a_plain_record_list(tmp_path):
    path = tmp_path / "records.json"
    path.write_text(json.dumps(RECORDS), encoding="utf-8")
    summary = rag_eval.load_reliability_summary(path)
    assert summary["computed"]["groups"]["total"] == 3
    assert "recorded" not in summary


def test_a_stored_summary_is_returned_untouched(tmp_path):
    # The audited numbers must survive as written, never be recomputed.
    recorded = {"citation_validity": 1.0, "citation_accuracy": 0.20588}
    path = tmp_path / "results.json"
    path.write_text(
        json.dumps({"summary": {"deterministic": recorded}, "records": RECORDS}),
        encoding="utf-8",
    )
    assert rag_eval.load_reliability_summary(path)["recorded"] == recorded


# ------------------------------------------------ regression on real data

STAGE8 = Path("data/evaluation/stage8_baseline_results.json")


@pytest.mark.skipif(not STAGE8.exists(), reason="Stage 8 results not present")
def test_the_summary_reproduces_the_recorded_stage8_figures():
    """The computed numbers must equal the audited ones, exactly.

    This is the regression test for the 2026-08-16 discrepancy: the summary
    once pooled every metric over all 35 questions and disagreed with the
    recorded figures. Each metric now uses the denominator the original
    measurement used, so the two must match.
    """
    summary = rag_eval.load_reliability_summary(STAGE8)
    computed, recorded = summary["computed"], summary["recorded"]

    for metric in (
        "citation_validity",
        "citation_accuracy",
        "numeric_grounding",
        "retrieval_hit_rate",
        "abstention_rate_controls",
        "false_abstention_rate",
        "abstention_accuracy_overall",
    ):
        assert computed[metric] == pytest.approx(recorded[metric]), metric


@pytest.mark.skipif(not STAGE8.exists(), reason="Stage 8 results not present")
def test_the_recorded_group_sizes_are_the_ones_progress_documents():
    computed = rag_eval.load_reliability_summary(STAGE8)["computed"]
    assert computed["groups"] == {
        "total": 35,
        "answerable": 30,
        "controls": 5,
        "answered": 17,
    }


# --------------------------------------------------- judge availability


def test_an_invalid_judge_run_is_reported_as_unavailable():
    """A judge the run marked invalid must never be quoted as performance."""
    status = rag_eval.judge_status(
        {
            "summary": {
                "judge_model": "llama-3.1-8b-instant",
                "judge_INVALID": {
                    "reason": "the judge collapsed every score to 0.0",
                },
            }
        }
    )
    assert status["available"] is False
    assert status["model"] == "llama-3.1-8b-instant"
    assert "collapsed" in status["reason"]
    assert "scores" not in status


def test_a_valid_judge_run_reports_its_scores():
    status = rag_eval.judge_status(
        {
            "summary": {
                "judge_model": "llama-3.3-70b-versatile",
                "judge": {"faithfulness": 0.974, "relevance": 0.9},
            }
        }
    )
    assert status["available"] is True
    assert status["scores"]["faithfulness"] == 0.974


def test_no_judge_run_at_all_is_unavailable():
    assert rag_eval.judge_status({"summary": {}})["available"] is False
    assert rag_eval.judge_status([])["available"] is False


def test_the_real_stage8_file_still_marks_its_judge_invalid():
    """Guards the dashboard: if this ever flips, it must be a deliberate change."""
    path = Path("data/evaluation/stage8_baseline_results.json")
    status = rag_eval.load_reliability_summary(path)["judge"]
    assert status["available"] is False
    assert status["model"] == "llama-3.1-8b-instant"
