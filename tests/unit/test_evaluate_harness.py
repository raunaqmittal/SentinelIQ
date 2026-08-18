"""The Stage 9 harness must never lose a question it has already paid for.

No LLM is called: `flow.investigate` is stubbed everywhere in this file.
"""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evaluate.py"
spec = importlib.util.spec_from_file_location("evaluate_script", SCRIPT)
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)


class FakeChunk:
    def __init__(self, text):
        self.text = text


QUESTION = {
    "question_id": "C0001",
    "question": "How long do the warranties last?",
    "clause_type": "Warranty Duration",
    "contract": "some_contract.pdf",
    "family": "governance & admin",
    "answerable": True,
    "relevant": ["some_contract_0001"],
}


def fake_result(**overrides):
    result = {
        "answer": "The warranties last 12 months [some_contract_0001].",
        "citations": ["some_contract_0001"],
        "dropped_citations": [],
        "injection_flagged": False,
        "supplied": ["some_contract_0001"],
        "specialist": "Contract Analyst",
        "input_tokens": 100,
        "output_tokens": 20,
        "retrieval_ms": 5,
    }
    result.update(overrides)
    return result


def write_lines(path, records):
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


# --- 5. correct question selection -----------------------------------------


def test_questions_come_from_the_stage8_baseline(tmp_path):
    """Both sides must answer one identical list, so the questions are read
    from the single-agent records rather than assembled again."""
    path = tmp_path / "baseline.jsonl"
    write_lines(path, [QUESTION, dict(QUESTION, question_id="C0002")])

    questions = evaluate.load_baseline(path)

    assert [q["question_id"] for q in questions] == ["C0001", "C0002"]
    assert questions[0]["relevant"] == ["some_contract_0001"]


def test_the_real_baseline_holds_the_35_questions():
    assert len(evaluate.load_baseline()) == 35


# --- 1 & 2. resume logic and duplicate skipping ------------------------------


def test_completed_ids_reads_finished_questions(tmp_path):
    path = tmp_path / "records.jsonl"
    write_lines(path, [{"question_id": "C0001"}, {"question_id": "C0002"}])

    assert evaluate.completed_ids(path) == {"C0001", "C0002"}


def test_a_failed_question_is_not_treated_as_done(tmp_path):
    """An error record must be retried, not skipped for ever — the existing
    stage9 file already holds three 429 rows from an earlier attempt."""
    path = tmp_path / "records.jsonl"
    write_lines(
        path,
        [{"question_id": "C0001"}, {"question_id": "C0002", "error": "RateLimitError: 429"}],
    )

    assert evaluate.completed_ids(path) == {"C0001"}


def test_missing_records_file_means_nothing_is_done(tmp_path):
    assert evaluate.completed_ids(tmp_path / "nothing.jsonl") == set()


def test_finished_questions_are_skipped_on_a_restart(tmp_path):
    baseline = tmp_path / "baseline.jsonl"
    records = tmp_path / "records.jsonl"
    write_lines(baseline, [QUESTION, dict(QUESTION, question_id="C0002")])
    write_lines(records, [{"question_id": "C0001"}])

    questions = evaluate.load_baseline(baseline)
    done = evaluate.completed_ids(records)
    todo = [q for q in questions if q["question_id"] not in done]

    assert [q["question_id"] for q in todo] == ["C0002"]


# --- 3. incremental persistence ---------------------------------------------


def test_each_record_is_appended_not_overwritten(tmp_path):
    path = tmp_path / "records.jsonl"
    evaluate.append_record(path, {"question_id": "C0001"})
    evaluate.append_record(path, {"question_id": "C0002"})

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["question_id"] for line in lines] == ["C0001", "C0002"]


def test_a_record_is_on_disk_before_the_next_question_starts(tmp_path):
    """Written and flushed, so a kill between questions cannot lose it."""
    path = tmp_path / "records.jsonl"
    evaluate.append_record(path, {"question_id": "C0001"})

    assert json.loads(path.read_text(encoding="utf-8").strip())["question_id"] == "C0001"


# --- 4. interruption handling ------------------------------------------------


def test_records_written_before_a_crash_survive_it(tmp_path, monkeypatch):
    """Question 1 succeeds, question 2 raises. Question 1 must still be on disk
    and must be skipped when the run is restarted."""
    path = tmp_path / "records.jsonl"
    chunks = {"some_contract_0001": FakeChunk("The warranties last 12 months.")}
    calls = []

    def stub(context, question, clause_type):
        calls.append(question)
        if len(calls) == 2:
            raise RuntimeError("connection dropped")
        return fake_result()

    monkeypatch.setattr(evaluate.flow, "investigate", stub)

    first = evaluate.run_one(None, QUESTION, chunks)
    evaluate.append_record(path, first)

    with pytest.raises(RuntimeError):
        evaluate.run_one(None, dict(QUESTION, question_id="C0002"), chunks)

    assert evaluate.completed_ids(path) == {"C0001"}


def test_a_rate_limited_question_is_recorded_as_an_error(tmp_path):
    path = tmp_path / "records.jsonl"
    evaluate.append_record(path, {"question_id": "C0001", "error": "RateLimitError: 429"})

    assert evaluate.completed_ids(path) == set()
    assert json.loads(path.read_text(encoding="utf-8").strip())["error"].startswith("RateLimit")


# --- 6. record structure -----------------------------------------------------


def test_a_multi_agent_record_is_labelled_and_scored(monkeypatch):
    chunks = {"some_contract_0001": FakeChunk("The warranties last 12 months.")}
    monkeypatch.setattr(evaluate.flow, "investigate", lambda *a: fake_result())

    record = evaluate.run_one(None, QUESTION, chunks)

    assert record["path"] == "multi_agent"
    assert record["question_id"] == "C0001"
    assert record["citation_validity"] == 1.0
    assert record["retrieval_hit"] is True
    assert record["abstained"] is False
    assert record["input_tokens"] == 100
    assert record["generation_ms"] >= 0


def test_a_multi_agent_record_carries_the_baseline_fields(monkeypatch):
    """Every field the Stage 8 single-agent records use for comparison."""
    chunks = {"some_contract_0001": FakeChunk("The warranties last 12 months.")}
    monkeypatch.setattr(evaluate.flow, "investigate", lambda *a: fake_result())

    record = evaluate.run_one(None, QUESTION, chunks)
    baseline_fields = {
        "question_id", "question", "clause_type", "contract", "family",
        "answerable", "relevant", "supplied", "answer", "citations",
        "abstained", "input_tokens", "output_tokens", "citation_validity",
        "citation_accuracy", "numeric_grounding", "abstention_correct",
        "retrieval_hit",
    }

    assert baseline_fields <= set(record)


def test_an_abstention_is_detected(monkeypatch):
    chunks = {}
    monkeypatch.setattr(
        evaluate.flow,
        "investigate",
        lambda *a: fake_result(answer="NOT FOUND IN EVIDENCE", citations=[], supplied=[]),
    )

    record = evaluate.run_one(None, dict(QUESTION, answerable=False), chunks)

    assert record["abstained"] is True
    assert record["abstention_correct"] is True
