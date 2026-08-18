# CUAD Ingestion Spike

Experiment code behind the Stage 3 (Data Inspection) findings recorded in
`Docs/PROGRESS.md`. **Experiment only — not production code.** The findings
here inform `components/ingestion/`, which is not yet implemented.

## Run order

All data inspection and extraction experiments have been consolidated into a single Jupyter Notebook for easier execution and visualization:

```bash
jupyter notebook notebooks/cuad_ingestion_spike/cuad_ingestion_tests.ipynb
```

Run the notebook from the repository root. Requires `CUAD_v1.zip` in `data/evaluation/datasets/`. 

Results (extracted samples and metric JSONs) are written to `data/evaluation/datasets/sample/` and `data/evaluation/datasets/spike_results/` (both gitignored).

## Reusable piece

`spike_lib.normalize_with_map()` is the part worth promoting to production —
it normalizes text for matching while keeping an index map back to original
character offsets, which is what allows matching on normalized text while
citing exact source positions.

## Dependencies

`pymupdf`, `rapidfuzz`, `transformers` (tokenizer only — no PyTorch needed),
`requests`.
