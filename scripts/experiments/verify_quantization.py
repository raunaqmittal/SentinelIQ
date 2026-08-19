"""Validity checks for the quantization arms — EXPERIMENTAL, evaluation only.

Answers, by inspection rather than by trusting a config string:

- is each model actually on the CPU?
- which modules were actually replaced with quantized ones, and how many?
- did the embedding table actually get quantized in the `int8_all` arm?
- do the weights contain NaN or Inf?
- do the reranker's scores stay finite, and how far do they move?

Run:  python scripts/experiments/verify_quantization.py
"""

import json
import warnings
from pathlib import Path

import torch

from scripts.experiments import quantize
from sentineliq.components.retrieval import dense, reranker
from sentineliq.config import load_retrieval_config

warnings.filterwarnings("ignore")

OUT = Path("artifacts/evaluation/quantization_validity.json")

# The quantized replacements torch swaps in. Counting these is the only honest
# way to say an arm is "INT8" — the arm name proves nothing.
QUANTIZED_TYPES = (
    "DynamicQuantizedLinear",
    "QuantizedLinear",
    "QuantizedEmbedding",
    "QuantizedEmbeddingBag",
)


def module_census(module: torch.nn.Module) -> dict:
    """Count float vs quantized modules, and the parameters still in float."""
    counts: dict[str, int] = {}
    for child in module.modules():
        name = type(child).__name__
        if name in QUANTIZED_TYPES or isinstance(child, (torch.nn.Linear, torch.nn.Embedding)):
            counts[name] = counts.get(name, 0) + 1
    float_params = sum(p.numel() for p in module.parameters())
    return {
        "modules": counts,
        "float_parameters_remaining": float_params,
        "quantized_modules": sum(
            n for name, n in counts.items() if name in QUANTIZED_TYPES
        ),
    }


def finite_check(module: torch.nn.Module) -> dict:
    """Any NaN or Inf hiding in the weights, float or quantized."""
    bad_float, bad_quant, checked = 0, 0, 0
    for tensor in list(module.parameters()) + list(module.buffers()):
        checked += 1
        if tensor.is_floating_point() and not torch.isfinite(tensor).all():
            bad_float += 1
    for child in module.modules():
        if type(child).__name__ in QUANTIZED_TYPES:
            try:
                weight = child.weight()
                if not torch.isfinite(weight.dequantize()).all():
                    bad_quant += 1
            except Exception:  # noqa: BLE001 - some variants expose no weight()
                pass
    return {
        "tensors_checked": checked,
        "non_finite_float_tensors": bad_float,
        "non_finite_quantized_modules": bad_quant,
    }


PAIRS = [
    ("What is the governing law of this agreement?",
     "This Agreement shall be governed by the laws of the State of New York."),
    ("What is the notice period for termination?",
     "Either party may terminate upon sixty (60) days prior written notice."),
    ("Are there audit rights?",
     "Supplier shall permit Customer to audit its records once per calendar year."),
    ("Is assignment permitted?",
     "Neither party may assign this Agreement without prior written consent."),
    ("What are the payment terms?",
     "Invoices are payable within thirty (30) days of receipt."),
]


def main() -> None:
    config = load_retrieval_config()
    report = {"embedder": config.dense.model, "reranker": config.reranker.model, "arms": {}}

    baseline_scores = None
    for arm in quantize.ARMS:
        embedder = dense.load_model(config.dense.model)
        cross_encoder = reranker.load_model(config.reranker.model, fp16=False)
        embedder.to("cpu")
        cross_encoder.to("cpu")
        embedder = quantize.quantize_embedder(embedder, arm)
        cross_encoder = quantize.quantize_reranker(cross_encoder, arm)

        emb_model = embedder[0].auto_model
        rer_model = cross_encoder[0].model

        scores = cross_encoder.predict(PAIRS).tolist()
        if arm == "fp32":
            baseline_scores = scores

        report["arms"][arm] = {
            "embedder_device": str(next(emb_model.parameters()).device)
            if any(True for _ in emb_model.parameters()) else "no float params",
            "reranker_device": str(next(rer_model.parameters()).device)
            if any(True for _ in rer_model.parameters()) else "no float params",
            "embedder_census": module_census(emb_model),
            "reranker_census": module_census(rer_model),
            "embedder_finite": finite_check(emb_model),
            "reranker_finite": finite_check(rer_model),
            "embedder_weights_mb": quantize.weights_mb(emb_model),
            "reranker_weights_mb": quantize.weights_mb(rer_model),
            "probe_scores": [round(s, 5) for s in scores],
            "all_scores_finite": all(
                s == s and abs(s) != float("inf") for s in scores
            ),
            "max_abs_delta_vs_fp32": (
                round(max(abs(a - b) for a, b in zip(baseline_scores, scores)), 5)
                if baseline_scores else 0.0
            ),
            "rank_order_vs_fp32_identical": (
                sorted(range(len(scores)), key=lambda i: -scores[i])
                == sorted(range(len(baseline_scores)), key=lambda i: -baseline_scores[i])
                if baseline_scores else True
            ),
        }
        del embedder, cross_encoder, emb_model, rer_model

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nWritten to {OUT}")


if __name__ == "__main__":
    main()
