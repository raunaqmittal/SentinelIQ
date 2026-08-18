from pathlib import Path

import nbformat as nbf

nb = nbf.v4.new_notebook()

# Markdown Intro
nb.cells.append(nbf.v4.new_markdown_cell("""# SentinelIQ — CUAD Ingestion Spike Experiments

This notebook consolidates all the ingestion and extraction experiments into a single, structured format. It replaces the individual `spike_01` through `spike_07` scripts while preserving their exact logic, metrics, and JSON outputs.
"""))

# Cell 1: Setup and Imports
nb.cells.append(nbf.v4.new_code_cell("""import json
import os
import sys
import zipfile
import random
import statistics
import re
from collections import Counter
from pathlib import Path

import pymupdf
import pandas as pd
from rapidfuzz import fuzz
from transformers import AutoTokenizer

# Suppress HuggingFace warnings
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"

# Ensure we can import the local spike helpers
sys.path.insert(0, str(Path.cwd() / "notebooks/cuad_ingestion_spike"))
from spike_lib import normalize_with_map, page_ranges, page_for_offset, load_sample_documents, RESULTS_DIR, SAMPLE_DIR, CUAD_ZIP
from spike_chunkers import chunk_clause_packed, chunk_fixed, chunk_clause_aware, containment, token_length

# Configuration
pd.set_option('display.max_rows', 50)
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 1000)

RESULTS_DIR.mkdir(parents=True, exist_ok=True)
SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

print("Setup complete. Ready to run experiments.")"""))

# Cell 2: Experiment 1
nb.cells.append(nbf.v4.new_markdown_cell("""## 1. Document Extraction and Stratified Sampling (spike_01_extract.py)
This section reads the raw CUAD zip, stratifies the contracts, and extracts exactly 18 PDFs and their `.txt` counterparts to the local sample directory, generating the `manifest.json`."""))

nb.cells.append(nbf.v4.new_code_cell("""SEED = 42
random.seed(SEED)
SAMPLE_SIZE = 18

if not CUAD_ZIP.exists():
    raise FileNotFoundError(f"Missing {CUAD_ZIP}. Download from https://zenodo.org/records/4599830")

with zipfile.ZipFile(CUAD_ZIP) as archive:
    names = archive.namelist()
    pdfs, txts = {}, {}
    for n in names:
        if n.endswith("/"): continue
        stem, ext = os.path.splitext(os.path.basename(n))
        stem = stem.strip()
        ext = ext.lower()
        if n.startswith("CUAD_v1/full_contract_pdf/") and ext == ".pdf":
            pdfs[stem] = n
        elif n.startswith("CUAD_v1/full_contract_txt/") and ext == ".txt":
            txts[stem] = n

    print(f"pdf stems: {len(pdfs)}   txt stems: {len(txts)}")
    both = sorted(set(pdfs) & set(txts))
    print(f"stems with BOTH pdf and txt: {len(both)}")
    print(f"pdf-only: {len(set(pdfs)-set(txts))}   txt-only: {len(set(txts)-set(pdfs))}")

    master_json = json.loads(archive.read("CUAD_v1/CUAD_v1.json"))
    ann = {d["title"]: d for d in master_json["data"]}
    print(f"\\nJSON version: {master_json.get('version')}   entries: {len(master_json['data'])}")
    print(f"annotation titles: {len(ann)}")
    print(f"titles matching a pdf stem exactly: {len(set(ann) & set(pdfs))}")
    
    unmatched = sorted(set(ann) - set(pdfs))
    print(f"titles NOT matching a pdf stem: {len(unmatched)}")
    
    by_group = {}
    for stem in both:
        parts = pdfs[stem].split("/")
        group = f"{parts[2]}/{parts[3]}" if len(parts) > 4 else parts[2]
        by_group.setdefault(group, []).append(stem)
        
    rng = random.Random(SEED)
    groups = sorted(by_group)
    rng.shuffle(groups)
    sample = []
    gi = 0
    while len(sample) < SAMPLE_SIZE:
        g = groups[gi % len(groups)]
        pool = [s for s in by_group[g] if s not in sample and s in ann]
        if pool:
            sample.append(rng.choice(sorted(pool)))
        gi += 1
        if gi > 1000: break

    manifest = []
    for stem in sample:
        pdf_dst = SAMPLE_DIR / f"{stem}.pdf"
        txt_dst = SAMPLE_DIR / f"{stem}.txt"
        pdf_dst.write_bytes(archive.read(pdfs[stem]))
        txt_dst.write_bytes(archive.read(txts[stem]))
        n_para = len(ann[stem]["paragraphs"])
        n_qas = sum(len(p["qas"]) for p in ann[stem]["paragraphs"])
        manifest.append({
            "stem": stem,
            "pdf_size": pdf_dst.stat().st_size,
            "txt_size": len(txt_dst.read_bytes())
        })
        
manifest_path = SAMPLE_DIR / "manifest.json"
manifest_path.write_text(json.dumps(manifest, indent=2))

df_manifest = pd.DataFrame(manifest)
print(f"Extracted {len(manifest)} sampled contracts to {SAMPLE_DIR}.")
display(df_manifest.head())"""))

# Cell 3: Experiment 2
nb.cells.append(nbf.v4.new_markdown_cell("""## 2. Text Alignment & PDF Extractability (spike_02_align.py)
We compare the PyMuPDF-extracted text against the original CUAD `.txt` files. We check for scanned PDFs (image-count signal), length ratios, and map the expert clause annotations to exact PDF character offsets."""))

nb.cells.append(nbf.v4.new_code_cell("""FUZZ_THRESHOLD = 90.0

with zipfile.ZipFile(CUAD_ZIP) as archive:
    raw = json.loads(archive.read("CUAD_v1/CUAD_v1.json"))
ann = {d["title"]: d for d in raw["data"]}

doc_rows = []
ann_rows = []

for m in manifest:
    stem = m["stem"]
    pdf_path = SAMPLE_DIR / f"{stem}.pdf"
    txt_path = SAMPLE_DIR / f"{stem}.txt"
    cuad_txt = txt_path.read_text(encoding="utf-8", errors="replace")
    
    entry = ann[stem]
    para = entry["paragraphs"][0]
    context = para["context"]
    
    # Text Extraction
    doc = pymupdf.open(pdf_path)
    page_texts = [p.get_text("text") for p in doc]
    pdf_text = "\\n".join(page_texts)
    ranges = page_ranges(page_texts)
    
    chars_per_page = len(pdf_text) / max(len(page_texts), 1)
    empty_pages = sum(1 for t in page_texts if len(t.strip()) < 20)
    is_text_based = chars_per_page >= 200 and empty_pages / max(len(page_texts), 1) < 0.5
    doc.close()
    
    # Normalization & Comparison
    ctx_norm, _ = normalize_with_map(context)
    txt_norm, _ = normalize_with_map(cuad_txt)
    pdf_norm, pdf_map = normalize_with_map(pdf_text)
    sim = fuzz.ratio(ctx_norm[:200000], pdf_norm[:200000])
    
    ctx_eq_txt = (context == cuad_txt)
    ctx_eq_txt_norm = (ctx_norm == txt_norm)
    len_ratio = len(pdf_text) / max(len(cuad_txt), 1)
    
    counts = {"exact": 0, "normalized": 0, "fuzzy": 0, "failed": 0, "total": 0}
    
    for qa in para["qas"]:
        category = qa["id"].split("__")[-1] if "__" in qa["id"] else qa["question"][:40]
        for a in qa["answers"]:
            atext = a["text"]
            astart = a["answer_start"]
            counts["total"] += 1
            a_norm, _ = normalize_with_map(atext)
            method = "failed"
            found_at = None
            
            # Sanity check CUAD context
            offset_valid = (context[astart:astart+len(atext)] == atext)
            
            if atext and atext in pdf_text:
                method = "exact"
                found_at = pdf_text.index(atext)
            elif a_norm and a_norm in pdf_norm:
                method = "normalized"
                pos = pdf_norm.index(a_norm)
                found_at = pdf_map[pos] if pos < len(pdf_map) else None
            elif a_norm:
                al = fuzz.partial_ratio_alignment(a_norm, pdf_norm, score_cutoff=FUZZ_THRESHOLD)
                if al is not None:
                    method = "fuzzy"
                    found_at = pdf_map[al.dest_start] if al.dest_start < len(pdf_map) else None
            
            counts[method] += 1
            ann_rows.append({
                "stem": stem,
                "category": category,
                "answer_len": len(atext),
                "offset_valid_in_context": offset_valid,
                "method": method,
                "pdf_offset": found_at,
                "page": page_for_offset(ranges, found_at) if found_at is not None else None,
                "preview": atext[:60].replace("\\n", " ")
            })
            
    doc_rows.append({
        "stem": stem,
        "pages": len(page_texts),
        "text_based": is_text_based,
        "len_ratio_pdf_over_txt": round(len_ratio, 2),
        "ctx_eq_txt": ctx_eq_txt,
        "ctx_eq_txt_norm": ctx_eq_txt_norm,
        "pdf/txt_sim": round(sim, 1),
        **counts
    })

df_docs = pd.DataFrame(doc_rows)
df_anns = pd.DataFrame(ann_rows)

Path(RESULTS_DIR / "doc_stats.json").write_text(json.dumps(doc_rows, indent=2))
Path(RESULTS_DIR / "ann_alignment.json").write_text(json.dumps(ann_rows, indent=2))

print("=== Document Extraction Stats ===")
display(df_docs.head(10))

print("\\n=== Annotation Alignment Stats ===")
mapping_summary = df_anns['method'].value_counts().to_frame("Count")
mapping_summary['%'] = (mapping_summary['Count'] / mapping_summary['Count'].sum() * 100).round(1)
display(mapping_summary)
"""))

# Cell 4: Experiment 3
nb.cells.append(nbf.v4.new_markdown_cell("""## 3. Ambiguity and Offset Transfer (spike_03_ambiguity.py)
We measure Risk 1: does the answer string occur more than once (ambiguity)? And Risk 2: does case folding create matches that are not real?"""))

nb.cells.append(nbf.v4.new_code_cell("""def count_occurrences(hay: str, needle: str, limit: int = 50) -> list[int]:
    pos, out = 0, []
    while len(out) < limit:
        i = hay.find(needle, pos)
        if i < 0: break
        out.append(i)
        pos = i + 1
    return out

ambig_examples = []
drift_samples = []
stats = {
    "total": 0, "unique": 0, "ambiguous": 0, 
    "offset_transfer_exact": 0, "offset_transfer_close": 0, "offset_transfer_far": 0,
    "case_sensitive_match": 0, "case_folding_needed": 0, "first_match_wrong": 0
}

for m in manifest:
    stem = m["stem"]
    pdf_text = "\\n".join([p.get_text("text") for p in pymupdf.open(SAMPLE_DIR / f"{stem}.pdf")])
    context = ann[stem]["paragraphs"][0]["context"]
    
    pdf_norm_cf, _ = normalize_with_map(pdf_text, fold_case=True)
    pdf_norm_cs, _ = normalize_with_map(pdf_text, fold_case=False)
    ctx_norm_cf, ctx_map_cf = normalize_with_map(context, fold_case=True)
    ctx_inv = {orig_i: norm_i for norm_i, orig_i in enumerate(ctx_map_cf)}
    
    for qa in ann[stem]["paragraphs"][0]["qas"]:
        for a in qa["answers"]:
            atext, astart = a["text"], a["answer_start"]
            if not atext: continue
            stats["total"] += 1
            
            a_cf, _ = normalize_with_map(atext, fold_case=True)
            a_cs, _ = normalize_with_map(atext, fold_case=False)
            hits = count_occurrences(pdf_norm_cf, a_cf)
            
            if len(hits) == 1: stats["unique"] += 1
            elif len(hits) > 1:
                stats["ambiguous"] += 1
                ambig_examples.append({"stem": stem[:20]+"...", "hits": len(hits), "preview": atext[:60].replace("\\n", " ")})
                
            if a_cs and a_cs in pdf_norm_cs:
                stats["case_sensitive_match"] += 1
            elif a_cf and a_cf in pdf_norm_cf:
                stats["case_folding_needed"] += 1
                
            norm_start = ctx_inv.get(astart)
            if norm_start is not None and hits:
                if pdf_norm_cf.startswith(a_cf, norm_start):
                    stats["offset_transfer_exact"] += 1
                    best = norm_start
                else:
                    best = min(hits, key=lambda h: abs(h - norm_start))
                    if abs(best - norm_start) <= 200:
                        stats["offset_transfer_close"] += 1
                    else:
                        stats["offset_transfer_far"] += 1
                        drift_samples.append({"stem": stem[:20]+"...", "drift": best - norm_start, "preview": atext[:60].replace("\\n", " ")})
                        
                if len(hits) > 1 and hits[0] != best:
                    stats["first_match_wrong"] += 1

Path(RESULTS_DIR / "ambiguity.json").write_text(json.dumps(stats, indent=2))

print(f"Total Answers: {stats['total']} | Unique: {stats['unique']} | Ambiguous: {stats['ambiguous']}")
print(f"Case Sensitive Matches: {stats['case_sensitive_match']} | Case Folding Needed: {stats['case_folding_needed']}")
print(f"Offset Transfer -> Exact: {stats['offset_transfer_exact']} | Close (<=200): {stats['offset_transfer_close']} | Far (>200): {stats['offset_transfer_far']}")
print(f"First Match Wrong (Naive Search Fails): {stats['first_match_wrong']}")

if drift_samples:
    print("\\nFar Drift Samples (>200 chars):")
    display(pd.DataFrame(drift_samples).head(10))
"""))

# Cell 5: Experiment 4
nb.cells.append(nbf.v4.new_markdown_cell("""## 4. Document Structure & Typography (spike_04_structure.py)
Analyzes heading patterns, verifies extraction modes (text vs blocks), and checks font sizes to detect structural signals in contracts."""))

nb.cells.append(nbf.v4.new_code_cell("""PATTERNS = {
    "ARTICLE_N": re.compile(r"^\\s*ARTICLE\\s+([IVXLC]+|\\d+)\\b", re.I),
    "SECTION_N": re.compile(r"^\\s*SECTION\\s+(\\d+(\\.\\d+)*)\\b", re.I),
    "NUM_DOTTED": re.compile(r"^\\s*(\\d+(\\.\\d+){1,3})\\.?\\s+\\S"),
    "NUM_SIMPLE": re.compile(r"^\\s*(\\d{1,2})\\.\\s+[A-Z]"),
    "PAREN_ALPHA": re.compile(r"^\\s*\\(([a-z]{1,2})\\)\\s+\\S"),
    "PAREN_ROMAN": re.compile(r"^\\s*\\(([ivxl]{1,5})\\)\\s+\\S"),
    "ALLCAPS_SHORT": re.compile(r"^\\s*[A-Z][A-Z \\-&,\\.']{3,60}\\s*$"),
    "EXHIBIT": re.compile(r"^\\s*(EXHIBIT|SCHEDULE|APPENDIX|ANNEX)\\s+[A-Z0-9]", re.I),
}

mode_rows = []
pattern_counts = Counter()

for m in manifest:
    stem = m["stem"]
    doc = pymupdf.open(SAMPLE_DIR / f"{stem}.pdf")
    
    sizes, flags_bold, line_records = [], 0, []
    text_mode = "\\n".join(p.get_text("text") for p in doc)
    blocks_mode_parts = []
    
    for pno, page in enumerate(doc, start=1):
        blocks = page.get_text("blocks")
        blocks_sorted = sorted(blocks, key=lambda b: (round(b[1], 1), round(b[0], 1)))
        blocks_mode_parts.append("\\n".join(b[4] for b in blocks_sorted))
        
        d = page.get_text("dict")
        for blk in d.get("blocks", []):
            if blk.get("type") != 0: continue
            for line in blk.get("lines", []):
                spans = line.get("spans", [])
                if not spans: continue
                ltext = "".join(s["text"] for s in spans)
                lsize = max(s["size"] for s in spans)
                lbold = any(s["flags"] & (1 << 4) for s in spans)
                for s in spans:
                    sizes.append(round(s["size"], 1))
                    if s["flags"] & (1 << 4): flags_bold += 1
                if ltext.strip():
                    line_records.append({"text": ltext, "size": round(lsize, 1), "bold": lbold})
    doc.close()
    
    blocks_mode = "\\n".join(blocks_mode_parts)
    t_norm, _ = normalize_with_map(text_mode, fold_case=False)
    b_norm, _ = normalize_with_map(blocks_mode, fold_case=False)
    modes_identical = (t_norm == b_norm)
    
    doc_hits = Counter()
    for rec in line_records:
        for name, pat in PATTERNS.items():
            if pat.match(rec["text"]):
                doc_hits[name] += 1
                break
                
    for k, v in doc_hits.items():
        pattern_counts[k] += v
                
    n_heading_lines = sum(doc_hits.values())
    mode_rows.append({
        "stem": stem,
        "lines": len(line_records),
        "headings": n_heading_lines,
        "structured": n_heading_lines >= 10,
        "modes_identical": modes_identical,
        "distinct_fonts": len(set(sizes)),
        "bold_pct": round(100 * flags_bold / max(len(sizes), 1), 1),
        "top_pattern": doc_hits.most_common(1)[0][0] if doc_hits else None
    })

Path(RESULTS_DIR / "structure.json").write_text(json.dumps({"docs": mode_rows, "pattern_totals": dict(pattern_counts)}, indent=2))

df_structure = pd.DataFrame(mode_rows)
print(f"Modes Identical (text vs block): {df_structure['modes_identical'].sum()} / {len(mode_rows)}")
display(df_structure.sort_values(by="headings", ascending=False).head(15))"""))

# Cell 6: Experiment 5
nb.cells.append(nbf.v4.new_markdown_cell("""## 5. Chunking Strategy Evaluation (spike_05_chunking.py)
We calculate the actual token percentiles of the expert CUAD spans and then evaluate three chunking strategies (`fixed`, `clause_aware`, `clause_packed`) for evidence containment metrics."""))

nb.cells.append(nbf.v4.new_code_cell("""print("Loading models and documents for chunking evaluation...")
tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-small-en-v1.5")
documents = load_sample_documents(tokenizer)

span_tokens = sorted(token_length(doc.token_offsets, span) for doc in documents for span in doc.spans)
percentiles = {
    "p50": span_tokens[int(0.5 * len(span_tokens))],
    "p90": span_tokens[int(0.9 * len(span_tokens))],
    "p95": span_tokens[int(0.95 * len(span_tokens))]
}
print(f"CUAD span token length percentiles: p50={percentiles['p50']}, p90={percentiles['p90']}, p95={percentiles['p95']}\\n")

def evaluate(documents: list, strategy: str, size: int) -> dict[str, float | int | str]:
    totals = {"whole": 0, "split2": 0, "split3plus": 0, "unplaced": 0}
    lengths, tiny, over_cap = [], 0, 0
    
    for document in documents:
        if strategy == "fixed":
            chunks = chunk_fixed(document.token_offsets, size, int(size * 0.125))
        elif strategy == "clause_aware":
            chunks = chunk_clause_aware(document.text, document.token_offsets, size, size)
        elif strategy == "clause_packed":
            chunks = chunk_clause_packed(document.text, document.token_offsets, size, size)
            
        for chunk in chunks:
            length = token_length(document.token_offsets, chunk)
            lengths.append(length)
            if length < 50: tiny += 1
            if length > 512: over_cap += 1
            
        counts = containment(document.spans, chunks)
        for key in totals: totals[key] += counts[key]
            
    lengths.sort()
    span_total = max(sum(totals.values()), 1)
    return {
        "strategy": strategy, "size": size, "chunks": len(lengths),
        "median": lengths[len(lengths)//2],
        "p95": lengths[int(0.95 * len(lengths))],
        "max": lengths[-1],
        "tiny": tiny,
        "over_cap": over_cap,
        "unplaced": totals["unplaced"],
        "whole_pct": round(100 * totals["whole"] / span_total, 1),
        "split_pct": round(100 * (totals["split2"] + totals["split3plus"]) / span_total, 1)
    }

results = []
for strat in ["fixed", "clause_aware", "clause_packed"]:
    for sz in [350, 400, 500, 650]:
        results.append(evaluate(documents, strat, sz))

Path(RESULTS_DIR / "chunking_grid.json").write_text(json.dumps({
    "tokenizer": "BAAI/bge-small-en-v1.5",
    "model_token_cap": 512,
    "total_spans": len(span_tokens),
    "span_token_percentiles": percentiles,
    "results": results
}, indent=2))

df_chunking = pd.DataFrame(results)
display(df_chunking)"""))

# Cell 7: Experiment 6
nb.cells.append(nbf.v4.new_markdown_cell("""## 6. Page Boundary Behavior (spike_07_pages.py)
Checking how often chunks and CUAD span evidence cross physical page boundaries, mapping them to explicit `page_start` and `page_end` properties."""))

nb.cells.append(nbf.v4.new_code_cell("""chunks_crossing = 0
total_chunks = 0
spans_crossing = 0
total_spans = 0
samples = []

for document in documents:
    chunks = chunk_clause_packed(document.text, document.token_offsets, 500, 500)
    for start, end in chunks:
        total_chunks += 1
        if page_for_offset(document.page_ranges, start) != page_for_offset(document.page_ranges, end - 1):
            chunks_crossing += 1
            
    for span_start, span_end in document.spans:
        total_spans += 1
        first = page_for_offset(document.page_ranges, span_start)
        last = page_for_offset(document.page_ranges, span_end - 1)
        if first != last:
            spans_crossing += 1
            
        if len(samples) < 6 and 200 < (span_end - span_start) < 700:
            owning = [i for i, (s, e) in enumerate(chunks) if s <= span_start and span_end <= e]
            if owning:
                samples.append({
                    "doc": document.stem[:15],
                    "page_start": first,
                    "page_end": last,
                    "chunk_id": f"{document.stem[:8]}_{first:03d}_{owning[0]:04d}",
                    "evidence": document.text[span_start:span_end][:120].replace("\\n", " ") + "..."
                })

print(f"Chunks crossing page boundaries: {chunks_crossing} / {total_chunks} ({100*chunks_crossing/total_chunks:.1f}%)")
print(f"Spans crossing page boundaries: {spans_crossing} / {total_spans} ({100*spans_crossing/total_spans:.1f}%)")
print("\\nExample End-to-End Citations:")
df_citations = pd.DataFrame(samples)
display(df_citations)"""))

# Execute the notebook to populate outputs before saving
try:
    from nbclient import NotebookClient
    client = NotebookClient(nb, kernel_name='python3')
    print("Executing notebook to populate cell outputs...")
    client.execute()
    print("Notebook executed successfully.")
except Exception as e:
    print(f"Failed to execute notebook: {e}")

# Write notebook to file
nb_path = Path("notebooks/cuad_ingestion_spike/cuad_ingestion_tests.ipynb")
nb_path.write_text(nbf.writes(nb), encoding="utf-8")
print(f"Notebook written to {nb_path}")
