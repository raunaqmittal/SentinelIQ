"""CLI: run a full due-diligence investigation for one vendor.

Usage:
    python scripts/investigate.py "Meridian CloudWorks"
    python scripts/investigate.py "Meridian CloudWorks" --limit 2 --json out.json

Runs the whole chain: dossier-scoped retrieval -> specialist -> Red-Team ->
severity -> score bridge -> deterministic engine -> cited report. Every LLM call
costs quota, so `--limit` is there to keep a trial run cheap.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from transformers import AutoTokenizer

from sentineliq.components.evaluation.retrieval_eval import chunk_corpus
from sentineliq.components.retrieval import dense, reranker
from sentineliq.config import load_app_config, load_retrieval_config, load_risk_rules
from sentineliq.pipeline import flow, investigation
from sentineliq.utils import configure_logging

logger = logging.getLogger("investigate")

DOCUMENTS = Path("data/raw/documents")
QUESTIONS = Path("data/evaluation/questions.json")


def load_questions(vendor: str, path: Path = QUESTIONS) -> list[dict]:
    """The investigation questions belonging to one vendor."""
    questions = json.loads(path.read_text(encoding="utf-8"))
    return [q for q in questions if q["vendor"] == vendor]


def build_context(config, app) -> flow.RunContext:
    """Load the corpus and the models once.

    The indexes are left empty: `run_investigation` rebuilds them per vendor so
    one vendor's search cannot reach another's documents.
    """
    tokenizer = AutoTokenizer.from_pretrained(config.dense.model)
    chunks = chunk_corpus(
        DOCUMENTS,
        lambda text: len(tokenizer.encode(text, add_special_tokens=False)),
        chunk_size=config.chunking.chunk_size,
        chunk_overlap=config.chunking.chunk_overlap,
    )
    return flow.RunContext(
        config=config,
        embedder=dense.load_model(config.dense.model),
        faiss_index=None,
        bm25_index=None,
        cross_encoder=reranker.load_model(
            config.reranker.model, fp16=config.reranker.fp16
        ),
        chunks=chunks,
        llm=flow.build_llm(
            app.llm.model,
            os.environ["GROQ_API_KEY"],
            app.llm.temperature,
            app.llm.base_url,
        ),
    )


def safe_print(text: str) -> None:
    """Print text the console may not be able to encode.

    Windows consoles default to cp1252, which has no character for things like
    the narrow no-break space (\\u202f) that a model can put in an answer.
    Unencodable characters are replaced rather than raising.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def main() -> None:
    """Parse arguments and run one vendor's investigation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vendor", help='Vendor name, e.g. "Meridian CloudWorks"')
    parser.add_argument("--limit", type=int, help="Only run the first N questions")
    parser.add_argument("--json", type=Path, help="Also write the full report as JSON")
    args = parser.parse_args()

    # Redaction on before anything else runs, so no secret or document text can
    # reach a log line (NFR-006).
    configure_logging("INFO")
    load_dotenv()

    questions = load_questions(args.vendor)
    if not questions:
        raise SystemExit(f"No questions found for vendor {args.vendor!r}")
    if args.limit:
        questions = questions[: args.limit]

    config = load_retrieval_config()
    app = load_app_config()
    rules = load_risk_rules()
    dossier = investigation.load_dossier(DOCUMENTS / "dossiers.json", args.vendor)

    logger.info("%s — %d questions", args.vendor, len(questions))
    context = build_context(config, app)
    verdict = investigation.run_investigation(context, dossier, questions, rules)

    # Save before printing. A run that has already spent quota must not lose its
    # result to a display problem — which is exactly what happened on
    # 2026-08-16, when a cp1252 console could not encode a character the model
    # emitted and the process died two lines before the write.
    if args.json:
        args.json.write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        logger.info("wrote %s", args.json)
    safe_print("\n" + investigation.format_report(verdict))


if __name__ == "__main__":
    main()
