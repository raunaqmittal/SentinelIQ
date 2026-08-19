"""INT8 quantization of the retrieval models — EXPERIMENTAL, not production.

The production loaders in `sentineliq/components/retrieval/` are not touched.
This module only takes an already-loaded model and returns a quantized copy, so
deleting `scripts/experiments/` restores the baseline exactly.

Two arms, both measured rather than assumed:

- `int8_linear`  quantizes `nn.Linear` only. This is the standard, best
  supported path, and it leaves the reranker's 256M-parameter embedding table
  (45% of the model) at full precision.
- `int8_all`     also quantizes `nn.Embedding`, which needs the non-default
  `float_qparams_weight_only_qconfig` — plain qint8 raises an AssertionError.

Quantization holds the float and the quantized copy in memory at the same time,
which peaked at ~6.5 GB for the reranker. That is why a deployment would build
the artifact offline; here every arm runs in its own process anyway.
"""

import torch
from torch.ao.quantization import (
    default_dynamic_qconfig,
    float_qparams_weight_only_qconfig,
    quantize_dynamic,
)

ARMS = ("fp32", "int8_linear", "int8_all")


def quantize(module: torch.nn.Module, arm: str) -> torch.nn.Module:
    """Return `module` quantized for the given arm, or unchanged for fp32."""
    if arm == "fp32":
        return module
    if arm == "int8_linear":
        return quantize_dynamic(module, {torch.nn.Linear}, dtype=torch.qint8)
    if arm == "int8_all":
        qconfig = {
            torch.nn.Linear: default_dynamic_qconfig,
            torch.nn.Embedding: float_qparams_weight_only_qconfig,
        }
        return quantize_dynamic(module, qconfig, dtype=torch.qint8)
    raise ValueError(f"unknown arm: {arm}")


def quantize_embedder(embedder, arm: str):
    """Quantize the transformer inside a SentenceTransformer, in place.

    Assign through `.model`, not `.auto_model`. In sentence-transformers 5.x
    `auto_model` is a read-only property that reads back from `.model`, so
    assigning to it silently registers a dead second submodule and leaves the
    real weights at FP32 — the first run of this experiment did exactly that
    and reported an unquantized embedder as quantized.
    """
    embedder[0].model = quantize(embedder[0].model, arm)
    return embedder


def is_quantized(module: torch.nn.Module) -> bool:
    """Whether dynamic quantization actually replaced anything.

    Type names are not a reliable test: the dynamic quantized linear class is
    also called `Linear`, so it is indistinguishable from `torch.nn.Linear` by
    name. `LinearPackedParams` only exists in a quantized module, and the count
    of remaining float parameters is the ground truth.
    """
    return any(type(m).__name__ == "LinearPackedParams" for m in module.modules())


def float_parameters(module: torch.nn.Module) -> int:
    """Parameters still stored as floats. Drops sharply once quantized."""
    return sum(p.numel() for p in module.parameters())


def quantize_reranker(cross_encoder, arm: str):
    """Quantize the transformer inside a CrossEncoder, in place.

    It must be assigned through `cross_encoder[0]`, the Transformer wrapper.
    `CrossEncoder.model` is a read-through property on an `nn.Sequential`, so
    assigning to it registers a *second* child module, which then receives the
    tokenizer's feature dict during forward and raises an AttributeError.
    """
    cross_encoder[0].model = quantize(cross_encoder[0].model, arm)
    return cross_encoder


def weights_mb(module: torch.nn.Module) -> float:
    """Serialized size of the weights, which is what a deployment ships."""
    import io

    buffer = io.BytesIO()
    torch.save(module.state_dict(), buffer)
    return round(buffer.getbuffer().nbytes / 1e6, 1)
