"""Unit tests for the deterministic decision engine (FR-015, FR-019, NFR-004)."""

import pytest

from sentineliq.config import load_risk_rules
from sentineliq.pipeline import engine

RULES = load_risk_rules()


def scores(compliance=0, security=0, financial=0, contract=0, evidence_quality=0):
    """A full set of category scores, so tests only name what they care about."""
    return {
        "compliance": compliance,
        "security": security,
        "financial": financial,
        "contract": contract,
        "evidence_quality": evidence_quality,
    }


def test_weights_are_loaded_from_config_not_hardcoded():
    weights = RULES.weights.as_dict()
    assert sum(weights.values()) == pytest.approx(1.0)
    assert set(weights) == set(engine.CATEGORIES)


def test_overall_score_applies_the_documented_formula():
    # 80*0.25 + 60*0.30 + 40*0.20 + 20*0.15 + 100*0.10 = 59.0
    result = engine.overall_score(
        scores(80, 60, 40, 20, 100),
        RULES,
    )
    assert result == pytest.approx(59.0)


def test_all_zero_scores_give_zero_and_all_max_give_one_hundred():
    assert engine.overall_score(scores(), RULES) == 0.0
    everything = scores(100, 100, 100, 100, 100)
    assert engine.overall_score(everything, RULES) == pytest.approx(100.0)


def test_a_missing_category_is_an_error_not_a_silent_zero():
    incomplete = scores()
    del incomplete["security"]
    with pytest.raises(ValueError, match="missing category score: security"):
        engine.overall_score(incomplete, RULES)


@pytest.mark.parametrize("bad", [-1, 101])
def test_scores_outside_zero_to_one_hundred_are_rejected(bad):
    with pytest.raises(ValueError, match="must be 0-100"):
        engine.overall_score(scores(compliance=bad), RULES)


@pytest.mark.parametrize(
    "score,expected_level,expected_decision",
    [
        (0, "low", "APPROVE"),
        (30, "low", "APPROVE"),  # boundary belongs to the band it caps
        (30.1, "medium", "APPROVE_WITH_CONDITIONS"),
        (55, "medium", "APPROVE_WITH_CONDITIONS"),
        (75, "high", "ESCALATE"),
        (75.1, "critical", "REJECT"),
        (100, "critical", "REJECT"),
    ],
)
def test_thresholds_map_scores_to_the_configured_decision(
    score, expected_level, expected_decision
):
    level, decision = engine.decide(score, RULES)
    assert (level, decision) == (expected_level, expected_decision)


def test_same_input_always_gives_the_same_output():
    given = scores(70, 45, 30, 10, 55)
    first = engine.score_investigation(given, RULES)
    second = engine.score_investigation(given, RULES)
    assert first == second


def test_output_has_every_field_fr015_requires():
    result = engine.score_investigation(scores(), RULES, confidences=[0.9, 0.7])
    for field in (
        "overall_score",
        "category_scores",
        "recommendation",
        "confidence",
        "escalate",
    ):
        assert field in result


def test_low_risk_is_approved_and_needs_no_human():
    result = engine.score_investigation(scores(compliance=10, security=10), RULES)
    assert result["recommendation"] == "APPROVE"
    assert result["escalate"] is False


def test_high_score_escalates_on_its_own():
    result = engine.score_investigation(scores(100, 100, 100, 100, 0), RULES)
    assert result["recommendation"] == "REJECT"
    assert result["escalate"] is True


def test_a_contradiction_escalates_even_when_the_score_is_low():
    result = engine.score_investigation(scores(), RULES, contradiction_found=True)
    assert result["recommendation"] == "APPROVE"  # the score itself is unchanged
    assert result["escalate"] is True
    assert result["contradiction_found"] is True


def test_missing_critical_documents_escalate_even_when_the_score_is_low():
    result = engine.score_investigation(scores(), RULES, missing_critical_docs=True)
    assert result["escalate"] is True


def test_confidence_is_the_mean_of_what_the_agents_reported():
    result = engine.score_investigation(scores(), RULES, confidences=[1.0, 0.5, 0.6])
    assert result["confidence"] == pytest.approx(0.7)


def test_confidence_is_none_when_no_agent_reported_one():
    assert engine.score_investigation(scores(), RULES)["confidence"] is None
