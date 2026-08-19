"""Run a whole investigation for one vendor, end to end.

vendor -> its questions -> dossier-scoped retrieval -> specialist -> Red-Team
-> severity -> score bridge -> deterministic engine -> cited report.

Retrieval is scoped by **building the indexes from that vendor's chunks only**,
not by filtering results afterwards. A vendor's index physically cannot contain
another vendor's text, so cross-vendor leakage is impossible rather than merely
unlikely (the shape NFR-003a needs later). The frozen benchmark indexes under
`artifacts/` are untouched.

`run_document_investigation` runs the same chain over a single uploaded
document, whose context `pipeline/documents.py` has already scoped the same way.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from sentineliq.components.evaluation import rag_eval
from sentineliq.components.models.schemas import Chunk
from sentineliq.components.retrieval import dense, sparse
from sentineliq.config import RiskRulesConfig
from sentineliq.pipeline import engine, flow

logger = logging.getLogger(__name__)

#: Risk categories a question can carry. `evidence_quality` is not here — it is
#: measured from how well the answers were evidenced, not asked as a question.
QUESTION_CATEGORIES = ("compliance", "security", "financial", "contract")

#: The three document types the upload UI asks for, matching the three real
#: specialists (compliance also owns "contract" — flow.CATEGORY_ROUTING).
DOCUMENT_TYPES = ("contract", "financial", "security")

#: Bumped when the report's structure changes, so an old stored report can
#: still be read correctly (FR-018).
REPORT_VERSION = "1"


def load_dossier(path: Path, vendor_name: str) -> dict:
    """Find one vendor's dossier by name."""
    dossiers = json.loads(path.read_text(encoding="utf-8"))["dossiers"]
    for dossier in dossiers:
        if dossier["vendor_name"] == vendor_name:
            return dossier
    raise ValueError(f"no dossier for vendor {vendor_name!r}")


def dossier_document_ids(dossier: dict) -> set[str]:
    """Document IDs belonging to this vendor.

    A document ID is the file stem (see `retrieval_eval.load_document`), so the
    dossier's file lists convert straight into IDs. `.json` XBRL facts are not
    text documents and are never chunked, so they drop out here.
    """
    files = (
        dossier.get("edgar_files", [])
        + dossier.get("security_docs", [])
        + dossier.get("cuad_contracts", [])
    )
    return {Path(name).stem for name in files if not name.endswith(".json")}


def scoped_chunks(chunks: list[Chunk], dossier: dict) -> list[Chunk]:
    """Keep only the chunks belonging to this vendor's documents."""
    allowed = dossier_document_ids(dossier)
    scoped = [chunk for chunk in chunks if chunk.document_id in allowed]
    if not scoped:
        raise ValueError(f"no chunks for vendor {dossier['vendor_name']!r}")
    return scoped


def build_scoped_context(base: flow.RunContext, dossier: dict) -> flow.RunContext:
    """A RunContext whose indexes cover this vendor's documents and nothing else."""
    chunks = scoped_chunks(base.chunks, dossier)
    logger.info(
        "Scoped retrieval to one vendor",
        extra={"vendor": dossier["vendor_name"], "chunks": len(chunks)},
    )
    return flow.RunContext(
        config=base.config,
        embedder=base.embedder,
        faiss_index=dense.build_index(base.embedder, chunks),
        bm25_index=sparse.build_index(chunks),
        cross_encoder=base.cross_encoder,
        chunks=chunks,
        llm=base.llm,
    )


def evidence_signals(findings: list[dict], relevant: dict[str, list[str]]) -> dict:
    """The three rates the evidence-quality score is built from.

    `relevant` maps question_id to its ground-truth chunk IDs. Citation accuracy
    needs those labels, so it is measured only over the questions that have
    them.
    """
    answered = [f for f in findings if f["severity"] is not None]
    retrieval_rate = len(answered) / len(findings) if findings else 0.0

    validities = [
        rag_eval.citation_validity(f["citations"], f["supplied"]) for f in findings
    ]
    accuracies = [
        rag_eval.citation_accuracy(f["citations"], relevant[f["question_id"]])
        for f in findings
        if f["question_id"] in relevant
    ]
    return {
        "retrieval_rate": retrieval_rate,
        "citation_validity": sum(validities) / len(validities) if validities else 0.0,
        "citation_accuracy": sum(accuracies) / len(accuracies) if accuracies else 0.0,
        "questions_with_labels": len(accuracies),
    }


def category_scores(
    findings: list[dict], signals: dict, rules: RiskRulesConfig
) -> dict:
    """The five FR-015 inputs, built from the findings.

    Four come from the mean severity of that category's findings. The fifth is
    evidence quality, **inverted to a risk** before it goes into the weighted
    sum — see `engine.evidence_risk`. The quality number is returned alongside
    it for the report. A category nobody asked about scores 0 — there is no
    evidence of risk — and `covered` records that so a reader can tell an
    unasked category from a genuinely clean one.
    """
    scores = {}
    covered = {}
    for category in QUESTION_CATEGORIES:
        labels = [
            f["severity"]
            for f in findings
            if f["category"] == category and f["severity"] is not None
        ]
        scores[category] = engine.category_score(labels, rules)
        covered[category] = len(labels)

    quality = engine.evidence_quality_score(
        signals["retrieval_rate"],
        signals["citation_accuracy"],
        signals["citation_validity"],
        rules,
    )
    scores["evidence_quality"] = engine.evidence_risk(quality)
    return {
        "scores": scores,
        "findings_per_category": covered,
        "evidence_quality": quality,
    }


def evidence_detail(by_id: dict[str, Chunk], chunk_id: str) -> dict:
    """Where a citation actually points: document, page and chunk (FR-016).

    A bare chunk id is not something a reviewer can check. `page_start` is None
    for sources that have no pages, such as the extracted EDGAR text files.
    """
    chunk = by_id[chunk_id]
    return {
        "chunk_id": chunk.chunk_id,
        "document_name": chunk.document_name,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
    }


def cite(evidence: dict) -> str:
    """One evidence item as a reader sees it: `Contract.pdf, p.17`."""
    if evidence["page_start"] is None:
        return evidence["document_name"]
    pages = f"p.{evidence['page_start']}"
    if evidence["page_end"] and evidence["page_end"] != evidence["page_start"]:
        pages = f"pp.{evidence['page_start']}-{evidence['page_end']}"
    return f"{evidence['document_name']}, {pages}"


def investigation_id(vendor: str, questions: list[dict]) -> str:
    """A stable ID for this vendor-and-questions investigation (FR-018).

    Derived from the inputs rather than random, so re-running the same
    investigation reproduces the same ID — which is what NFR-004's determinism
    requirement implies for the report as well as the score.
    """
    seed = vendor + "|" + "|".join(sorted(q["question_id"] for q in questions))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def explain(verdict: dict, top_n: int = 5) -> list[dict]:
    """The ordered, cited reasons behind the recommendation (FR-016).

    Worst first: contradictions, then HIGH, MEDIUM, LOW severity. A
    contradiction or a HIGH finding is labelled CRITICAL — the things that can
    sink a vendor on their own — and everything else is a WARNING.
    """
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    reasons = []
    for finding in verdict["findings"]:
        if finding["severity"] is None:
            continue  # nothing was found, so there is nothing to explain
        critical = finding["contradiction"] or finding["severity"] == "HIGH"
        reasons.append(
            {
                "label": "CRITICAL" if critical else "WARNING",
                "category": finding["category"],
                "severity": finding["severity"],
                "contradiction": finding["contradiction"],
                "question_id": finding["question_id"],
                "reason": finding["answer"],
                "evidence": finding["evidence"],
            }
        )
    reasons.sort(key=lambda r: (not r["contradiction"], rank[r["severity"]]))
    return reasons[:top_n]


def run_investigation(
    context: flow.RunContext,
    dossier: dict,
    questions: list[dict],
    rules: RiskRulesConfig,
    relevant: dict[str, list[str]] | None = None,
) -> dict:
    """Answer every question for one vendor and produce the final verdict."""
    scoped = build_scoped_context(context, dossier)
    vendor = dossier["vendor_name"]
    return run_on_context(scoped, vendor, vendor, questions, rules, relevant)


def coverage(available_types: set[str]) -> dict:
    """Which document types are present, for the report's "Documents analyzed" line.

    `available_types` is a set of `DOCUMENT_TYPES` values actually uploaded.
    Untyped or unrecognised types are ignored here — they still feed retrieval,
    they just cannot be marked present/absent on this checklist.
    """
    documents_analyzed = {t: (t in available_types) for t in DOCUMENT_TYPES}
    missing = [t for t in DOCUMENT_TYPES if t not in available_types]
    if not missing:
        caveat = None
    elif len(missing) == len(DOCUMENT_TYPES):
        caveat = (
            "Partial evidence set: no recognised document type was supplied. "
            "Findings are limited to whatever evidence was retrieved."
        )
    else:
        named = " and ".join(m.capitalize() for m in missing)
        caveat = (
            f"Partial evidence set: {named} documentation was not provided. "
            "This assessment is based on the documents supplied. Missing "
            "document categories may contain evidence that could change the "
            "risk assessment."
        )
    return {"documents_analyzed": documents_analyzed, "coverage_caveat": caveat}


def run_document_investigation(
    scoped: flow.RunContext,
    document_id: str,
    document_name: str,
    questions: list[dict],
    rules: RiskRulesConfig,
    available_types: set[str] | None = None,
) -> dict:
    """Investigate one company's uploaded document set.

    Same agents, same Red-Team, same deterministic scoring as a vendor run —
    `scoped` was already built (by `pipeline/documents.union_context`) from
    every document the caller supplied, one FAISS/BM25 index over the union, so
    there is no dossier to filter by; the scoping already happened upstream.

    A specialist whose document type was never supplied has no matching
    evidence in the index, so it answers `NOT FOUND IN EVIDENCE` on its own —
    this is the existing abstention behaviour, not new logic. `available_types`
    only drives the report's "Documents analyzed" checklist and caveat text.

    The scores are NOT a validated vendor risk score in the sense the frozen
    benchmark used: `risk_rules.yaml` was tuned on the curated multi-document
    dossiers, and an uploaded set may still be smaller or differently composed.
    `generalisation_caveat` records that; `coverage_caveat` (from `coverage()`)
    separately records which document types were actually supplied.
    """
    verdict = run_on_context(scoped, document_id, document_name, questions, rules)
    verdict["subject_type"] = "uploaded_document"
    verdict["generalisation_caveat"] = (
        "Scored from an uploaded document set with the generic question set. "
        "The risk weights and thresholds were tuned on the curated vendor "
        "dossiers, so this is a document risk indication, not a validated "
        "vendor risk score."
    )
    verdict.update(coverage(available_types or set()))
    return verdict


def run_on_context(
    scoped: flow.RunContext,
    subject_id: str,
    subject_name: str,
    questions: list[dict],
    rules: RiskRulesConfig,
    relevant: dict[str, list[str]] | None = None,
) -> dict:
    """Answer every question against an already-scoped context.

    `subject_id` seeds the reproducible investigation ID; `subject_name` is what
    a reader sees. For a vendor both are the vendor name.
    """
    allowed_ids = {chunk.chunk_id for chunk in scoped.chunks}

    by_id = {chunk.chunk_id: chunk for chunk in scoped.chunks}

    findings = []
    for question in questions:
        result = flow.investigate_finding(
            scoped, question["question"], question["category"]
        )
        # Isolation is structural, but assert it: a citation from outside this
        # vendor's documents would be a leak, not a formatting problem.
        leaked = [c for c in result["supplied"] if c not in allowed_ids]
        if leaked:
            raise ValueError(f"retrieval leaked chunks from another vendor: {leaked}")
        result.update(
            question_id=question["question_id"],
            question=question["question"],
            evidence=[evidence_detail(by_id, c) for c in result["citations"]],
        )
        findings.append(result)

    signals = evidence_signals(findings, relevant or {})
    scored = category_scores(findings, signals, rules)

    # One contradiction anywhere is enough: the engine's escalation rule then
    # forces human review whatever the score says (FR-019).
    contradictions = [f["question_id"] for f in findings if f["contradiction"]]

    verdict = engine.score_investigation(
        scored["scores"],
        rules,
        contradiction_found=bool(contradictions),
        missing_critical_docs=False,
    )
    verdict.update(
        investigation_id=investigation_id(subject_id, questions),
        report_version=REPORT_VERSION,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        vendor=subject_name,
        injection_flagged=any(f["injection_flagged"] for f in findings),
        contradiction_questions=contradictions,
        evidence_quality=scored["evidence_quality"],
        evidence_signals=signals,
        findings_per_category=scored["findings_per_category"],
        findings=findings,
    )
    verdict["why"] = explain(verdict)
    return verdict


def load_demo_report(path: Path, documents_dir: Path) -> dict:
    """The preloaded demo investigation, recomputed through the current engine.

    `data/evaluation/demo_investigation.json` holds a REAL run's answers and
    citations for Meridian CloudWorks — captured 2026-08-16, before two engine
    fixes documented in PROGRESS.md (contradiction-escalation wiring, and the
    evidence-quality inversion). No LLM is called here: this takes those same
    answers and citations and passes them through the same `engine.py`
    functions every other investigation uses, so the score shown matches what
    the frozen scoring code computes today, not a number from before it was
    fixed. `contradiction` is left False on every finding — this run predates
    the CONTRADICTION marker, so whether the Red-Team would flag one was never
    observed, which is different from "no contradiction exists".

    Raises:
        DocumentLoadError: The vendor's source documents are not present
            (`data/raw/` is not committed to git; see README).
    """
    from transformers import AutoTokenizer

    from sentineliq.components.evaluation.retrieval_eval import load_document
    from sentineliq.components.ingestion.chunker import chunk_document
    from sentineliq.config import load_retrieval_config, load_risk_rules

    raw = json.loads(path.read_text(encoding="utf-8"))
    dossier = load_dossier(documents_dir / "dossiers.json", raw["vendor"])
    wanted = dossier_document_ids(dossier)

    config = load_retrieval_config()
    tokenizer = AutoTokenizer.from_pretrained(config.dense.model)

    def count_tokens(text):
        return len(tokenizer.encode(text, add_special_tokens=False))

    # Re-chunk the vendor's own documents with the frozen settings, so the
    # citation ids in the stored findings resolve to the same chunks they
    # pointed at when the run happened.
    by_id: dict[str, Chunk] = {}
    for file in sorted(documents_dir.iterdir()):
        if file.stem in wanted:
            document = load_document(file)
            for chunk in chunk_document(
                document,
                count_tokens,
                chunk_size=config.chunking.chunk_size,
                chunk_overlap=config.chunking.chunk_overlap,
            ):
                by_id[chunk.chunk_id] = chunk

    findings = [
        {
            **finding,
            "contradiction": False,
            "evidence": [
                evidence_detail(by_id, c) for c in finding["citations"] if c in by_id
            ],
        }
        for finding in raw["findings"]
    ]

    rules = load_risk_rules()
    signals = evidence_signals(findings, {})
    scored = category_scores(findings, signals, rules)
    verdict = engine.score_investigation(
        scored["scores"], rules, contradiction_found=False, missing_critical_docs=False
    )
    verdict.update(
        investigation_id=investigation_id(raw["vendor"], findings),
        report_version=REPORT_VERSION,
        generated_at="2026-08-16T00:00:00+00:00",  # when this run actually happened
        vendor=raw["vendor"],
        subject_type="preloaded_demo",
        injection_flagged=any(f["injection_flagged"] for f in findings),
        contradiction_questions=[],
        evidence_quality=scored["evidence_quality"],
        evidence_signals=signals,
        findings_per_category=scored["findings_per_category"],
        findings=findings,
        demo_note=(
            "Precomputed Demo Investigation — real findings and citations "
            "from an actual run (2026-08-16), re-scored through the current "
            "deterministic engine so the numbers match today's scoring code."
        ),
    )
    verdict["why"] = explain(verdict)
    return verdict


def format_report(verdict: dict) -> str:
    """The investigation report (FR-018), with the "Why?" section (FR-016)."""
    lines = [
        f"INVESTIGATION REPORT — {verdict['vendor']}",
        f"Investigation {verdict['investigation_id']} "
        f"· report v{verdict['report_version']} · {verdict['generated_at']}",
        "",
        f"RECOMMENDATION: {verdict['recommendation']}",
        f"Overall risk score: {verdict['overall_score']} ({verdict['risk_level']})",
        f"Confidence: {verdict['confidence'] or 'not measured'}",
        f"Human review required: {'YES' if verdict['escalate'] else 'no'}",
        "",
        "Category scores:",
    ]
    for category, score in verdict["category_scores"].items():
        count = verdict["findings_per_category"].get(category)
        detail = f"  ({count} findings)" if count is not None else ""
        if category == "evidence_quality":
            # This row holds the inverted risk; show the quality it came from.
            detail = f"  (risk; evidence quality {verdict['evidence_quality']})"
        lines.append(f"  {category:<17} {score:>6}{detail}")

    if verdict["contradiction_questions"]:
        found = ", ".join(verdict["contradiction_questions"])
        lines += ["", f"CONTRADICTION DETECTED in {found} — human review required."]
    if verdict["injection_flagged"]:
        lines += ["", "WARNING: an injection attempt was reported in the evidence."]

    lines += ["", f"WHY THIS RECOMMENDATION (top {len(verdict['why'])}):"]
    for position, reason in enumerate(verdict["why"], start=1):
        mark = "X" if reason["label"] == "CRITICAL" else "!"
        note = " — CONTRADICTION" if reason["contradiction"] else ""
        lines += [
            "",
            f"{position}. [{mark}] {reason['label']} · {reason['category']} · "
            f"{reason['severity']}{note}",
            f"   {reason['reason'].splitlines()[0]}",
        ]
        for evidence in reason["evidence"]:
            lines.append(f"   Evidence -> {cite(evidence)}")

    lines += ["", "FINDINGS BY CATEGORY:"]
    for category in QUESTION_CATEGORIES:
        in_category = [f for f in verdict["findings"] if f["category"] == category]
        if not in_category:
            continue
        lines += ["", f"-- {category.upper()} --"]
        for finding in in_category:
            severity = finding["severity"] or "NO ANSWER"
            flag = " · CONTRADICTION" if finding["contradiction"] else ""
            lines += [
                "",
                f"[{finding['question_id']}] {severity}{flag}",
                f"  Q: {finding['question']}",
                f"  A: {finding['answer']}",
            ]
            for evidence in finding["evidence"]:
                source = f"{cite(evidence)}  [{evidence['chunk_id']}]"
                lines.append(f"  Evidence -> {source}")
    return "\n".join(lines)
