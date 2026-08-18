"""Unit tests for config loading."""

import pytest

from sentineliq.config import load_retrieval_config, load_risk_rules


def test_load_retrieval_config_reads_chunking_values():
    config = load_retrieval_config()
    assert config.chunking.chunk_size == 512
    assert config.chunking.chunk_overlap == 64


def test_load_retrieval_config_reads_all_sections():
    config = load_retrieval_config()
    assert config.rrf.k == 60
    assert config.reranker.top_n == 5
    assert config.query_router.enable_web_search is False


def test_load_risk_rules_reads_weights_thresholds_and_escalation():
    rules = load_risk_rules()
    assert rules.weights.security == 0.30
    assert rules.thresholds["low"].decision == "APPROVE"
    assert rules.escalation.always_escalate_on_contradiction is True


def test_load_risk_rules_reads_the_severity_and_evidence_quality_settings():
    rules = load_risk_rules()
    assert rules.severity == {"LOW": 20, "MEDIUM": 50, "HIGH": 80}
    assert rules.evidence_quality.retrieval_rate == 0.50


def test_load_risk_rules_rejects_weights_that_do_not_sum_to_one(tmp_path):
    bad = tmp_path / "risk_rules.yaml"
    bad.write_text(
        "weights: {compliance: 0.5, security: 0.5, financial: 0.5, "
        "contract: 0.5, evidence_quality: 0.5}\n"
        "severity: {LOW: 20, MEDIUM: 50, HIGH: 80}\n"
        "evidence_quality: {retrieval_rate: 0.5, citation_accuracy: 0.3, "
        "citation_validity: 0.2}\n"
        "thresholds: {low: {max: 100, decision: APPROVE}}\n"
        "escalation: {always_escalate_on_contradiction: true, "
        "always_escalate_on_missing_critical_docs: true}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must sum to 1.0"):
        load_risk_rules(bad)
