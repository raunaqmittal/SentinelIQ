"""What a DEPLOYED process would actually hold in RAM — EXPERIMENTAL.

The DEV arms quantize at start-up, so their peak RSS is dominated by holding the
float and the quantized copy at once. That is a build cost, not a deployment
cost. A real deployment would ship a pre-built artifact, so this script splits
the two:

    build  produce the quantized reranker once and save it
    load   fresh process: load only the artifact, rerank once, measure

FP32 `load` goes through the normal HuggingFace path, because memory-mapped
safetensors is what the baseline actually does — comparing a mmap'd baseline
against a fully materialised pickle is the trap this script exists to expose.

Usage:
    python scripts/experiments/deployed_rss.py build --arm int8_all
    python scripts/experiments/deployed_rss.py load  --arm int8_all
"""

import argparse
import json
import time
import warnings
from pathlib import Path

import psutil
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from scripts.experiments import quantize

warnings.filterwarnings("ignore")

MODEL = "BAAI/bge-reranker-v2-m3"
ART_DIR = Path("artifacts/evaluation/quantized_models")
OUT = Path("artifacts/evaluation/deployed_rss.json")

# A realistic reranking batch: 20 candidates, contract-length text.
QUERY = "What are the termination rights and notice periods under this agreement?"
PASSAGE = (
    "Either party may terminate this Agreement for convenience upon sixty (60) "
    "days prior written notice to the other party. In the event of a material "
    "breach, the non-breaching party may terminate upon thirty (30) days notice "
    "if the breach remains uncured. " * 8
)


def rss_mb() -> float:
    return round(psutil.Process().memory_info().rss / 1e6, 1)


def build(arm: str) -> dict:
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL, dtype=torch.float32
    ).eval()
    model = quantize.quantize(model, arm)
    ART_DIR.mkdir(parents=True, exist_ok=True)
    path = ART_DIR / f"reranker_{arm}.pt"
    torch.save(model, path)
    return {
        "arm": arm,
        "artifact": str(path),
        "disk_mb": round(path.stat().st_size / 1e6, 1),
        "build_peak_rss_mb": rss_mb(),
        "quantized": quantize.is_quantized(model),
        "float_parameters": quantize.float_parameters(model),
    }


def load(arm: str) -> dict:
    baseline = rss_mb()
    started = time.perf_counter()
    if arm == "fp32":
        # The real baseline path: memory-mapped safetensors, not a pickle.
        model = AutoModelForSequenceClassification.from_pretrained(
            MODEL, dtype=torch.float32
        ).eval()
    else:
        model = torch.load(ART_DIR / f"reranker_{arm}.pt", weights_only=False).eval()
    load_s = round(time.perf_counter() - started, 2)
    after_load = rss_mb()

    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    encoded = tokenizer(
        [QUERY] * 20, [PASSAGE] * 20, padding=True, truncation=True,
        max_length=512, return_tensors="pt",
    )
    started = time.perf_counter()
    with torch.no_grad():
        logits = model(**encoded).logits.view(-1)
    rerank_s = round(time.perf_counter() - started, 2)

    return {
        "arm": arm,
        "rss_before_load_mb": baseline,
        "rss_after_load_mb": after_load,
        "rss_after_rerank_mb": rss_mb(),
        "model_rss_cost_mb": round(after_load - baseline, 1),
        "load_s": load_s,
        "rerank_20_pairs_s": rerank_s,
        "quantized": quantize.is_quantized(model),
        "float_parameters": quantize.float_parameters(model),
        "all_finite": bool(torch.isfinite(logits).all()),
        "logit_0": round(logits[0].item(), 5),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("build", "load"))
    parser.add_argument("--arm", required=True, choices=quantize.ARMS)
    args = parser.parse_args()

    result = build(args.arm) if args.mode == "build" else load(args.arm)
    print(json.dumps(result))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    stored = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    stored.setdefault(args.arm, {})[args.mode] = result
    OUT.write_text(json.dumps(stored, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
