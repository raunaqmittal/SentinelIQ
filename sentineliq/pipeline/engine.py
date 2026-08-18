"""Deterministic synthesis and decision logic.

**No LLM runs here, by design (ADR-021).** The decision step is a Flow step, not
an agent: an LLM-authored verdict is neither auditable nor reachable-proof
against prompt injection, and ADR-010 makes determinism a security property.

Agents produce findings; this module turns findings into the final answer.
"""

import logging
import re

from sentineliq.config import RiskRulesConfig

logger = logging.getLogger(__name__)

CITATION_PATTERN = re.compile(r"\[([^\[\]]{3,200})\]")
INJECTION_MARKER = "INJECTION ATTEMPT DETECTED"

#: The five risk categories of FR-015, in formula order. Category scores and
#: config weights both use these names, so a typo fails loudly instead of
#: silently scoring zero.
CATEGORIES = ("compliance", "security", "financial", "contract", "evidence_quality")

#: Decisions that mean a human must look at the investigation (FR-019).
HUMAN_REVIEW_DECISIONS = ("ESCALATE", "REJECT")


def cited_ids(text: str) -> list[str]:
    """Chunk ids cited in a piece of text, in order, without duplicates."""
    seen: list[str] = []
    for match in CITATION_PATTERN.findall(text):
        cited = match.strip()
        if cited not in seen:
            seen.append(cited)
    return seen


def synthesise(specialist: str, verification: str, supplied: list[str]) -> dict:
    """Combine a specialist draft and the Red-Team verification, deterministically.

    Rules, in order:
    1. The Red-Team's corrected answer wins — it is the verified one.
    2. Citations are kept only if they refer to evidence actually supplied.
    3. An injection flag from either agent is surfaced, never suppressed.
    """
    answer = (verification or specialist or "").strip()
    injection_flagged = INJECTION_MARKER in (specialist + verification).upper()

    allowed = set(supplied)
    citations = [c for c in cited_ids(answer) if c in allowed]
    dropped = [c for c in cited_ids(answer) if c not in allowed]
    if dropped:
        logger.info("Dropped citations not in supplied evidence", extra={"count": len(dropped)})

    return {
        "answer": answer,
        "citations": citations,
        "dropped_citations": dropped,
        "injection_flagged": injection_flagged,
    }


def severity_score(label: str, rules: RiskRulesConfig) -> float:
    """Turn an agent's LOW/MEDIUM/HIGH label into its configured number.

    The agent only picks a label; the number comes from `risk_rules.yaml`, so
    the LLM never decides a score (ADR-021).
    """
    key = label.strip().upper()
    if key not in rules.severity:
        raise ValueError(f"unknown severity label: {label!r}")
    return rules.severity[key]


def category_score(labels: list[str], rules: RiskRulesConfig) -> float:
    """Mean severity of every finding in one category.

    Averaging deliberately smooths one bad finding out, which is why
    contradictions, injections and missing documents are carried separately as
    escalation signals instead of being folded into this number.
    """
    if not labels:
        return 0.0
    scores = [severity_score(label, rules) for label in labels]
    return round(sum(scores) / len(scores), 2)


def evidence_quality_score(
    retrieval_rate: float,
    citation_accuracy: float,
    citation_validity: float,
    rules: RiskRulesConfig,
) -> float:
    """Combine the three evidence signals into one 0–100 score.

    All three inputs are rates in 0–1; the result is scaled to 0–100 to match
    the other category scores. Weights come from `risk_rules.yaml`.
    """
    weights = rules.evidence_quality
    combined = (
        retrieval_rate * weights.retrieval_rate
        + citation_accuracy * weights.citation_accuracy
        + citation_validity * weights.citation_validity
    )
    return round(combined * 100, 2)


def evidence_risk(quality: float) -> float:
    """Turn an evidence-*quality* score into the evidence-*risk* the formula needs.

    FR-015 sums five risk scores where higher is worse, but evidence quality is
    measured the other way round: well-evidenced findings score high. Feeding
    the quality straight in would penalise good evidence, so it is inverted
    here. The quality number is kept for the report, where "90 quality" is what
    a reader expects to see.
    """
    return round(100 - quality, 2)


def overall_score(category_scores: dict[str, float], rules: RiskRulesConfig) -> float:
    """Weighted sum of the five category scores (FR-015).

    Every category is required and every score is 0–100. Missing or
    out-of-range input is an error, not something to guess a default for.
    """
    weights = rules.weights.as_dict()
    total = 0.0
    for category in CATEGORIES:
        if category not in category_scores:
            raise ValueError(f"missing category score: {category}")
        score = category_scores[category]
        if not 0 <= score <= 100:
            raise ValueError(f"{category} score must be 0-100, got {score}")
        total += score * weights[category]
    return round(total, 2)


def decide(score: float, rules: RiskRulesConfig) -> tuple[str, str]:
    """Map a 0–100 score to its risk band and decision.

    Bands are read lowest `max` first, so the config may list them in any
    order. Returns (risk_level, decision).
    """
    bands = sorted(rules.thresholds.items(), key=lambda item: item[1].max)
    for level, band in bands:
        if score <= band.max:
            return level, band.decision
    raise ValueError(f"no threshold band covers score {score}")


def score_investigation(
    category_scores: dict[str, float],
    rules: RiskRulesConfig,
    contradiction_found: bool = False,
    missing_critical_docs: bool = False,
    confidences: list[float] | None = None,
) -> dict:
    """Turn category scores into the final risk verdict (FR-015, FR-019).

    Pure arithmetic — same input always gives the same output (NFR-004), and
    no LLM is involved anywhere in this path (ADR-021).

    `confidences` are the per-agent confidence values; the overall confidence
    is their mean, or None when no agent supplied one.
    """
    score = overall_score(category_scores, rules)
    risk_level, decision = decide(score, rules)

    escalate = decision in HUMAN_REVIEW_DECISIONS
    if contradiction_found and rules.escalation.always_escalate_on_contradiction:
        escalate = True
    if missing_critical_docs and (
        rules.escalation.always_escalate_on_missing_critical_docs
    ):
        escalate = True

    confidence = round(sum(confidences) / len(confidences), 3) if confidences else None

    return {
        "overall_score": score,
        "risk_level": risk_level,
        "category_scores": {c: category_scores[c] for c in CATEGORIES},
        "recommendation": decision,
        "confidence": confidence,
        "escalate": escalate,
        "contradiction_found": contradiction_found,
        "missing_critical_docs": missing_critical_docs,
    }
