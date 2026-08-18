"""CLI: the Stage 9 multi-agent comparison over the Stage 8 question set.

Usage:
    python scripts/evaluate.py                 # run every unfinished question
    python scripts/evaluate.py --limit 2       # cheap trial
    python scripts/evaluate.py --list          # show what is done and what is left

The single-agent side is NOT re-run. `data/evaluation/stage8_baseline_records.jsonl`
already holds it for these same 35 questions, and this script reads that file to
get the questions, so both sides are answered from one identical list with one
identical set of relevance labels.

Records are appended one at a time, so a run stopped by a rate limit, a crash or
Ctrl-C keeps everything it had already finished. Restarting skips the questions
that already have a successful record.

Every LLM call costs quota. A daily rate limit stops the run cleanly: swap
GROQ_API_KEY by hand and start it again.
"""

import argparse
import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from transformers import AutoTokenizer

from sentineliq.components.evaluation import rag_eval
from sentineliq.components.evaluation.retrieval_eval import chunk_corpus
from sentineliq.components.retrieval import dense, reranker, sparse
from sentineliq.config import load_app_config, load_retrieval_config
from sentineliq.pipeline import flow
from sentineliq.utils import configure_logging

logger = logging.getLogger("evaluate")

DOCUMENTS = Path("data/raw/documents")
BASELINE = Path("data/evaluation/stage8_baseline_records.jsonl")
RECORDS = Path("data/evaluation/stage9_records.jsonl")

ABSTENTION = "NOT FOUND IN EVIDENCE"


def load_baseline(path: Path = BASELINE) -> list[dict]:
    """The Stage 8 records, which are also the question list for this run."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def completed_ids(path: Path = RECORDS) -> set[str]:
    """Question ids that already have a successful record.

    A record carrying an `error` does not count, so a question that failed on an
    earlier attempt is tried again instead of being skipped for ever.
    """
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not record.get("error"):
            done.add(record["question_id"])
    return done


def append_record(path: Path, record: dict) -> None:
    """Append one record and put it on disk before the next question starts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def evidence_text(chunks_by_id: dict, supplied: list[str]) -> str:
    """The text of the chunks the agents were shown, for numeric grounding."""
    return "\n".join(chunks_by_id[c].text for c in supplied if c in chunks_by_id)


def score_record(result: dict, question: dict, chunks_by_id: dict) -> dict:
    """The same deterministic metrics the Stage 8 records carry."""
    supplied = result["supplied"]
    relevant = question["relevant"]
    answer = result["answer"]
    abstained = answer.strip().upper().startswith(ABSTENTION)

    return {
        "question_id": question["question_id"],
        "question": question["question"],
        "clause_type": question["clause_type"],
        "contract": question["contract"],
        "family": question["family"],
        "answerable": question["answerable"],
        "path": "multi_agent",
        "relevant": relevant,
        "supplied": supplied,
        "answer": answer,
        "citations": result["citations"],
        "dropped_citations": result["dropped_citations"],
        "injection_flagged": result["injection_flagged"],
        "specialist": result["specialist"],
        "abstained": abstained,
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "retrieval_ms": result["retrieval_ms"],
        "generation_ms": result["generation_ms"],
        "citation_validity": rag_eval.citation_validity(result["citations"], supplied),
        "citation_accuracy": rag_eval.citation_accuracy(result["citations"], relevant),
        "numeric_grounding": rag_eval.numeric_grounding(
            answer, evidence_text(chunks_by_id, supplied)
        ),
        "abstention_correct": rag_eval.abstention_correct(abstained, question["answerable"]),
        "retrieval_hit": any(c in relevant for c in supplied),
    }


def run_one(context, question: dict, chunks_by_id: dict) -> dict:
    """Answer one question through the frozen multi-agent path and score it."""
    started = time.perf_counter()
    result = flow.investigate(context, question["question"], question["clause_type"])
    result["generation_ms"] = round((time.perf_counter() - started) * 1000)
    result.setdefault("retrieval_ms", 0)
    return score_record(result, question, chunks_by_id)


def build_context(config, app) -> flow.RunContext:
    """Load the corpus and the models once, exactly as the investigation CLI does."""
    tokenizer = AutoTokenizer.from_pretrained(config.dense.model)
    chunks = chunk_corpus(
        DOCUMENTS,
        lambda text: len(tokenizer.encode(text, add_special_tokens=False)),
        chunk_size=config.chunking.chunk_size,
        chunk_overlap=config.chunking.chunk_overlap,
    )
    # The indexes cover the whole corpus, not one vendor. The Stage 8 records
    # show corpus-wide retrieval — C0143 is a contract question whose evidence
    # includes JNJ_item_7_0007 — so scoping here would change the comparison.
    embedder = dense.load_model(config.dense.model)
    return flow.RunContext(
        config=config,
        embedder=embedder,
        faiss_index=dense.build_index(embedder, chunks),
        bm25_index=sparse.build_index(chunks),
        cross_encoder=reranker.load_model(config.reranker.model, fp16=config.reranker.fp16),
        chunks=chunks,
        llm=flow.build_llm(
            app.llm.model, os.environ["GROQ_API_KEY"], app.llm.temperature, app.llm.base_url
        ),
    )


def main() -> None:
    """Run the questions that have no successful record yet."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="Only run the first N unfinished questions")
    parser.add_argument("--list", action="store_true", help="Show progress and exit")
    parser.add_argument("--records", type=Path, default=RECORDS)
    args = parser.parse_args()

    configure_logging("INFO")
    load_dotenv()

    questions = load_baseline()
    done = completed_ids(args.records)
    todo = [q for q in questions if q["question_id"] not in done]

    if args.list:
        print(f"{len(done)} done, {len(todo)} left, {len(questions)} total")
        print("left:", ", ".join(q["question_id"] for q in todo) or "none")
        return

    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("Nothing to do — every question already has a record.")
        return

    logger.info("%d questions to run (%d already done)", len(todo), len(done))
    config = load_retrieval_config()
    context = build_context(config, load_app_config())
    chunks_by_id = {chunk.chunk_id: chunk for chunk in context.chunks}

    for number, question in enumerate(todo, start=1):
        qid = question["question_id"]
        logger.info("[%d/%d] %s", number, len(todo), qid)
        try:
            record = run_one(context, question, chunks_by_id)
        except Exception as error:  # noqa: BLE001 - the record must say what failed
            append_record(args.records, {"question_id": qid, "error": f"{type(error).__name__}: {error}"})
            if "429" in str(error) or "RateLimit" in type(error).__name__:
                print(
                    f"\nStopped at {qid}: rate limited. "
                    f"{number - 1} question(s) saved this run. "
                    "Replace GROQ_API_KEY and run this script again to continue."
                )
                return
            raise
        append_record(args.records, record)

    print(f"\nDone. {len(todo)} question(s) written to {args.records}")


if __name__ == "__main__":
    main()
