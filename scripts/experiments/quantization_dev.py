"""Bounded experiment: does INT8 quantization pay for itself on CUAD DEV?

EXPERIMENTAL. Nothing here is imported by the application. Deleting
`scripts/experiments/` returns SentinelIQ to its baseline with no other change.

Usage (one arm per process, so peak RSS is measured cleanly):

    python scripts/experiments/quantization_dev.py --arm fp32
    python scripts/experiments/quantization_dev.py --arm int8_linear
    python scripts/experiments/quantization_dev.py --arm int8_all
    python scripts/experiments/quantization_dev.py --compare

What is held identical across arms, because only the model precision is under
test: the corpus, the chunker (512/64), dense top_k 50, BM25 top_k 50, RRF
k=60, rerank_depth 20, the model names, the DEV question list and the relevance
judgements. All of it is read from the frozen `retrieval.yaml`.

Two deliberate differences from the frozen benchmark run, both stated in the
output file so no one quotes these numbers as the official ones:

1. **Everything runs on CPU**, including the baseline. INT8 dynamic
   quantization is CPU-only, so a GPU baseline would not be comparable. The
   recorded DEV figures were measured on GPU in fp16.
2. **The full 20 reranked candidates are kept**, not the top 5, so Recall@10
   and NDCG@10 can be computed. This is the same ordering the frozen pipeline
   produces; `top_n: 5` is where the LLM's evidence is truncated, and that
   config value is not modified.

No LLM is called. The TEST split is never evaluated.
"""

import argparse
import json
import logging
import os
import threading
import time
from pathlib import Path

# INT8 dynamic quantization runs on CPU only, so the baseline must too.
os.environ["CUDA_VISIBLE_DEVICES"] = ""

import psutil
import torch
from transformers import AutoTokenizer

from scripts.experiments import quantize
from sentineliq.components.evaluation import retrieval_eval
from sentineliq.components.retrieval import dense, reranker, search, sparse
from sentineliq.config import load_retrieval_config
from sentineliq.utils import configure_logging

logger = logging.getLogger("quantization_dev")

DOCUMENTS = Path("data/raw/documents")
QUESTIONS = Path("data/evaluation/cuad_questions.json")
GROUND_TRUTH = Path("data/evaluation/cuad_ground_truth.json")
# Experiment output lives in artifacts/, never in data/evaluation/, so the
# official records cannot be confused with these.
OUT_DIR = Path("artifacts/evaluation")


class PeakRSS:
    """Sample resident memory in the background and keep the maximum."""

    def __init__(self, interval: float = 0.25):
        self.process = psutil.Process()
        self.interval = interval
        self.peak = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, self.process.memory_info().rss)
            self._stop.wait(self.interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join()

    def now_mb(self) -> float:
        return round(self.process.memory_info().rss / 1e6, 1)

    def peak_mb(self) -> float:
        return round(max(self.peak, self.process.memory_info().rss) / 1e6, 1)


def device_of(module: torch.nn.Module) -> str:
    """Where the weights actually are. A quantized module has no float params."""
    for parameter in module.parameters():
        return str(parameter.device)
    for buffer in module.buffers():
        return str(buffer.device)
    return "unknown"


def dev_questions() -> list[dict]:
    """The 160 CUAD DEV questions. TEST is filtered out and never used."""
    entries = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    return [q for q in entries if q["split"] == "dev"]


def build_corpus(config) -> list:
    """Chunk the corpus exactly as the application does."""
    tokenizer = AutoTokenizer.from_pretrained(config.dense.model)
    return retrieval_eval.chunk_corpus(
        DOCUMENTS,
        lambda text: len(tokenizer.encode(text, add_special_tokens=False)),
        chunk_size=config.chunking.chunk_size,
        chunk_overlap=config.chunking.chunk_overlap,
    )


def run_arm(arm: str, limit: int | None) -> dict:
    """Retrieve every DEV question with one precision setting and score it."""
    config = load_retrieval_config()
    questions = dev_questions()
    if limit:
        questions = questions[:limit]

    with PeakRSS() as rss:
        baseline_rss = rss.now_mb()
        chunks = build_corpus(config)

        # --- models: loaded by the production loaders, then quantized ---------
        started = time.perf_counter()
        embedder = dense.load_model(config.dense.model)
        # fp16 is a GPU optimisation and is off for every arm on CPU, so the
        # only difference between arms is the quantization itself.
        cross_encoder = reranker.load_model(config.reranker.model, fp16=False)
        # Move to CPU explicitly. `CUDA_VISIBLE_DEVICES=""` does NOT hide the
        # GPU on Windows, and sentence-transformers then auto-selects cuda:0 —
        # which silently made the first baseline run a GPU run and crashed both
        # INT8 arms, whose kernels are CPU-only.
        embedder.to("cpu")
        cross_encoder.to("cpu")
        load_float_s = round(time.perf_counter() - started, 2)

        started = time.perf_counter()
        embedder = quantize.quantize_embedder(embedder, arm)
        cross_encoder = quantize.quantize_reranker(cross_encoder, arm)
        convert_s = round(time.perf_counter() - started, 2)

        sizes = {
            "embedder_weights_mb": quantize.weights_mb(embedder[0].auto_model),
            "reranker_weights_mb": quantize.weights_mb(cross_encoder[0].model),
        }
        rss_after_models = rss.now_mb()

        # --- indexes ---------------------------------------------------------
        started = time.perf_counter()
        faiss_index = dense.build_index(embedder, chunks)
        index_s = round(time.perf_counter() - started, 2)
        bm25_index = sparse.build_index(chunks)

        relevance = retrieval_eval.load_relevance(GROUND_TRUTH, chunks)

        # --- one query at a time --------------------------------------------
        retrieved, embed_ms, rerank_ms, total_ms = {}, [], [], []
        for number, question in enumerate(questions, start=1):
            qid, text = question["question_id"], question["question"]
            start_all = time.perf_counter()

            start = time.perf_counter()
            candidates = search.hybrid_search(
                embedder,
                faiss_index,
                bm25_index,
                chunks,
                text,
                dense_top_k=config.dense.top_k,
                sparse_top_k=config.sparse.top_k,
                rrf_k=config.rrf.k,
                top_k=config.rrf.rerank_depth,
            )
            embed_ms.append((time.perf_counter() - start) * 1000)

            start = time.perf_counter()
            # Keep all 20 so Recall@10 / NDCG@10 can be scored. The ordering is
            # the frozen pipeline's; production still truncates at top_n 5.
            ranked = reranker.rerank(
                cross_encoder, chunks, text, candidates, config.rrf.rerank_depth
            )
            rerank_ms.append((time.perf_counter() - start) * 1000)
            total_ms.append((time.perf_counter() - start_all) * 1000)

            retrieved[qid] = [chunk_id for chunk_id, _ in ranked]
            if number % 10 == 0:
                logger.info("[%s] %d/%d", arm, number, len(questions))

        peak = rss.peak_mb()
        steady = rss.now_mb()

    def mean(values):
        return round(sum(values) / len(values), 1) if values else None

    def percentile(values, share):
        ordered = sorted(values)
        return round(ordered[int(len(ordered) * share)], 1) if ordered else None

    return {
        "arm": arm,
        "n_questions": len(questions),
        # Read off a real parameter, not asserted — the first run recorded "cpu"
        # while actually executing on the GPU.
        "device": device_of(embedder[0].auto_model),
        "reranker_device": device_of(cross_encoder[0].model),
        "torch_threads": torch.get_num_threads(),
        "config": {
            "embedder": config.dense.model,
            "reranker": config.reranker.model,
            "chunk_size": config.chunking.chunk_size,
            "chunk_overlap": config.chunking.chunk_overlap,
            "dense_top_k": config.dense.top_k,
            "sparse_top_k": config.sparse.top_k,
            "rrf_k": config.rrf.k,
            "rerank_depth": config.rrf.rerank_depth,
            "production_top_n": config.reranker.top_n,
        },
        "sizes_mb": sizes | {
            "total_weights_mb": round(
                sizes["embedder_weights_mb"] + sizes["reranker_weights_mb"], 1
            )
        },
        "timing_s": {
            "load_float_models": load_float_s,
            "quantize_convert": convert_s,
            "build_faiss_index": index_s,
        },
        "latency_ms": {
            "retrieval_mean": mean(embed_ms),
            "rerank_mean": mean(rerank_ms),
            "rerank_p95": percentile(rerank_ms, 0.95),
            "end_to_end_mean": mean(total_ms),
            "end_to_end_p95": percentile(total_ms, 0.95),
        },
        "rss_mb": {
            "before_anything": baseline_rss,
            "after_models": rss_after_models,
            "peak": peak,
            "steady_end": steady,
        },
        "metrics": {
            "at_5": retrieval_eval.evaluate_retrieval(retrieved, relevance, 5),
            "at_10": retrieval_eval.evaluate_retrieval(retrieved, relevance, 10),
        },
        "retrieved": retrieved,
    }


def compare() -> dict:
    """Read the three arm files and judge them against A1-A7."""
    arms = {}
    for arm in quantize.ARMS:
        path = OUT_DIR / f"quantization_dev_{arm}.json"
        if not path.exists():
            raise FileNotFoundError(f"{path} — run --arm {arm} first")
        arms[arm] = json.loads(path.read_text(encoding="utf-8"))

    base = arms["fp32"]
    report = {"baseline": "fp32", "arms": {}}
    for arm, data in arms.items():
        overlap = []
        for qid, ranked in data["retrieved"].items():
            reference = base["retrieved"].get(qid)
            if reference is None:
                continue
            overlap.append(set(ranked[:5]) == set(reference[:5]))
        identical = round(sum(overlap) / len(overlap), 4) if overlap else None

        b, d = base["metrics"], data["metrics"]
        deltas = {
            "recall_at_5": round(d["at_5"]["recall_at_k"] - b["at_5"]["recall_at_k"], 4),
            "recall_at_10": round(
                d["at_10"]["recall_at_k"] - b["at_10"]["recall_at_k"], 4
            ),
            "mrr": round(d["at_10"]["mrr"] - b["at_10"]["mrr"], 4),
            "ndcg_at_10": round(d["at_10"]["ndcg_at_k"] - b["at_10"]["ndcg_at_k"], 4),
        }
        rss_saved = round(
            base["rss_mb"]["peak"] - data["rss_mb"]["peak"], 1
        )
        report["arms"][arm] = {
            "metrics_at_5": b["at_5"] if arm == "fp32" else d["at_5"],
            "metrics_at_10": b["at_10"] if arm == "fp32" else d["at_10"],
            "deltas_vs_fp32": deltas,
            "top5_identical_share": identical,
            "weights_mb": data["sizes_mb"]["total_weights_mb"],
            "weights_saved_mb": round(
                base["sizes_mb"]["total_weights_mb"]
                - data["sizes_mb"]["total_weights_mb"],
                1,
            ),
            "peak_rss_mb": data["rss_mb"]["peak"],
            "peak_rss_saved_mb": rss_saved,
            "rerank_mean_ms": data["latency_ms"]["rerank_mean"],
            "end_to_end_mean_ms": data["latency_ms"]["end_to_end_mean"],
            "criteria": {
                "A1_recall_at_5_within_0.010": deltas["recall_at_5"] >= -0.010,
                "A2_ndcg_at_10_within_0.010": deltas["ndcg_at_10"] >= -0.010,
                "A3_mrr_within_0.015": deltas["mrr"] >= -0.015,
                "A4_top5_identical_at_least_90pct": (
                    identical is not None and identical >= 0.90
                ),
                "A5_peak_rss_saved_at_least_800mb": rss_saved >= 800,
                "A6_rerank_latency_not_worse": (
                    data["latency_ms"]["rerank_mean"]
                    <= base["latency_ms"]["rerank_mean"]
                ),
            },
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=quantize.ARMS)
    parser.add_argument("--limit", type=int, help="Only the first N DEV questions")
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()

    configure_logging("INFO")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.compare:
        report = compare()
        path = OUT_DIR / "quantization_dev_comparison.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report["arms"], indent=2))
        print(f"\nWritten to {path}")
        return

    if not args.arm:
        parser.error("--arm is required unless --compare is given")

    result = run_arm(args.arm, args.limit)
    suffix = f"_limit{args.limit}" if args.limit else ""
    path = OUT_DIR / f"quantization_dev_{args.arm}{suffix}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    summary = {k: v for k, v in result.items() if k != "retrieved"}
    print(json.dumps(summary, indent=2))
    print(f"\nWritten to {path}")


if __name__ == "__main__":
    main()
