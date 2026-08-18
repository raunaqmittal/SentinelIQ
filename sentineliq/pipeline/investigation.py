"""Run a whole investigation for one vendor, end to end.

vendor -> its questions -> dossier-scoped retrieval -> specialist -> Red-Team
-> severity -> score bridge -> deterministic engine -> cited report.

Retrieval is scoped by **building the indexes from that vendor's chunks only**,
not by filtering results afterwards. A vendor's index physically cannot contain
another vendor's text, so cross-vendor leakage is impossible rather than merely
unlikely (the shape NFR-003a needs later). The frozen benchmark indexes under
`artifacts/` are untouched.
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
        investigation_id=investigation_id(dossier["vendor_name"], questions),
        report_version=REPORT_VERSION,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        vendor=dossier["vendor_name"],
        injection_flagged=any(f["injection_flagged"] for f in findings),
        contradiction_questions=contradictions,
        evidence_quality=scored["evidence_quality"],
        evidence_signals=signals,
        findings_per_category=scored["findings_per_category"],
        findings=findings,
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
