"""Unit tests for the score bridge, routing, dossier isolation and the runner.

Everything here is deterministic — the LLM call is replaced with a stub, so the
whole chain from findings to cited report is tested without spending quota.
"""

import pytest

from sentineliq.components.agents import compliance, financial, security
from sentineliq.components.models.schemas import Chunk
from sentineliq.config import load_risk_rules
from sentineliq.pipeline import engine, flow, investigation

RULES = load_risk_rules()

DOSSIER = {
    "vendor_name": "Meridian CloudWorks",
    "edgar_files": ["MSFT_item_1a.txt", "MSFT_financial_facts.json"],
    "security_docs": ["meridian_sla.txt"],
    "cuad_contracts": ["Some Reseller Agreement.pdf"],
}


def chunk(chunk_id, document_id):
    return Chunk(
        chunk_id=chunk_id,
        document_id=document_id,
        document_name=f"{document_id}.txt",
        text="some evidence text",
        char_start=0,
        char_end=18,
    )


# ----------------------------------------------------------- score bridge


def test_severity_labels_use_the_numbers_from_config():
    assert engine.severity_score("LOW", RULES) == 20
    assert engine.severity_score("MEDIUM", RULES) == 50
    assert engine.severity_score("HIGH", RULES) == 80


def test_severity_label_is_case_insensitive_and_trimmed():
    assert engine.severity_score(" high ", RULES) == 80


def test_an_unknown_severity_label_is_rejected():
    with pytest.raises(ValueError, match="unknown severity label"):
        engine.severity_score("CATASTROPHIC", RULES)


def test_evidence_quality_uses_the_configured_formula():
    # 0.50*1.0 + 0.30*0.5 + 0.20*1.0 = 0.85 -> 85.0
    assert engine.evidence_quality_score(1.0, 0.5, 1.0, RULES) == pytest.approx(85.0)


def test_evidence_quality_is_zero_when_nothing_was_evidenced():
    assert engine.evidence_quality_score(0.0, 0.0, 0.0, RULES) == 0.0


def test_good_evidence_becomes_low_risk_and_bad_evidence_becomes_high_risk():
    perfect = engine.evidence_quality_score(1.0, 1.0, 1.0, RULES)
    none_at_all = engine.evidence_quality_score(0.0, 0.0, 0.0, RULES)
    assert engine.evidence_risk(perfect) == 0.0
    assert engine.evidence_risk(none_at_all) == 100.0
    assert engine.evidence_risk(90.25) == pytest.approx(9.75)


def test_better_evidence_never_raises_the_overall_risk_score():
    good = dict.fromkeys(engine.CATEGORIES, 50.0)
    poor = dict(good)
    good["evidence_quality"] = engine.evidence_risk(100.0)  # perfect evidence
    poor["evidence_quality"] = engine.evidence_risk(0.0)  # no evidence at all
    assert engine.overall_score(good, RULES) < engine.overall_score(poor, RULES)


# ------------------------------------------------------------ aggregation


def test_category_score_is_the_mean_of_its_findings():
    assert engine.category_score(["LOW", "HIGH"], RULES) == pytest.approx(50.0)


def test_a_category_with_no_findings_scores_zero():
    assert engine.category_score([], RULES) == 0.0


def test_averaging_softens_a_single_high_finding():
    # The point of keeping contradictions as a separate escalation signal.
    assert engine.category_score(["HIGH", "LOW", "LOW"], RULES) == pytest.approx(40.0)


# --------------------------------------------------------------- routing


@pytest.mark.parametrize(
    "category,expected",
    [
        ("compliance", compliance),
        ("contract", compliance),  # no Contract Agent exists, by decision
        ("financial", financial),
        ("security", security),
    ],
)
def test_each_question_category_reaches_its_specialist(category, expected):
    assert flow.route_category(category) is expected


def test_an_unknown_category_is_rejected_rather_than_silently_defaulted():
    with pytest.raises(ValueError, match="unknown question category"):
        flow.route_category("marketing")


def test_cuad_clause_routing_is_unchanged():
    # The frozen Stage 8/9 path must keep behaving exactly as before.
    assert flow.route("Cap On Liability") is financial
    assert flow.route("Ip Ownership Assignment") is security
    assert flow.route("Anything Else") is compliance


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Answer here.\nSEVERITY: HIGH", "HIGH"),
        ("severity: low", "LOW"),
        ("NOT FOUND IN EVIDENCE", None),
        ("No label at all", None),
    ],
)
def test_severity_is_parsed_out_of_the_agent_reply(text, expected):
    assert flow.parse_severity(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("The policy conflicts.\nCONTRADICTION: YES", True),
        ("All consistent.\nCONTRADICTION: NO", False),
        ("contradiction: yes", True),
        ("NOT FOUND IN EVIDENCE", False),  # nothing said -> nothing raised
        ("The evidence contradicts itself but no marker", False),
    ],
)
def test_contradiction_is_only_raised_by_an_explicit_report(text, expected):
    assert flow.parse_contradiction(text) is expected


# ------------------------------------------------------- dossier isolation


def test_dossier_document_ids_are_file_stems_and_skip_xbrl_json():
    ids = investigation.dossier_document_ids(DOSSIER)
    assert ids == {"MSFT_item_1a", "meridian_sla", "Some Reseller Agreement"}


def test_scoped_chunks_keep_only_this_vendors_documents():
    corpus = [
        chunk("MSFT_item_1a_0001", "MSFT_item_1a"),
        chunk("meridian_sla_0001", "meridian_sla"),
        chunk("BA_item_7_0005", "BA_item_7"),  # another vendor
        chunk("castleridge_sla_0002", "castleridge_sla"),  # another vendor
    ]
    kept = investigation.scoped_chunks(corpus, DOSSIER)
    assert [c.chunk_id for c in kept] == ["MSFT_item_1a_0001", "meridian_sla_0001"]


def test_a_vendor_with_no_matching_chunks_is_an_error():
    with pytest.raises(ValueError, match="no chunks for vendor"):
        investigation.scoped_chunks([chunk("BA_item_7_0005", "BA_item_7")], DOSSIER)


# ------------------------------------------------- end-to-end, no LLM used


def fake_context(chunks):
    """A RunContext whose model fields are never touched by the stubbed run."""
    return flow.RunContext(
        config=None,
        embedder=None,
        faiss_index=None,
        bm25_index=None,
        cross_encoder=None,
        chunks=chunks,
        llm=None,
    )


REPLIES = {
    "compliance": ("HIGH", "SOC 2 expired."),
    "security": ("LOW", "Encryption documented."),
    "financial": ("MEDIUM", "Liability is capped."),
    "contract": ("LOW", "Termination is 30 days."),
}


def install_stub(monkeypatch, contradiction_in=()):
    """Replace index building and the LLM call with deterministic stubs."""
    monkeypatch.setattr(investigation.dense, "build_index", lambda model, chunks: None)
    monkeypatch.setattr(investigation.sparse, "build_index", lambda chunks: None)

    def fake_finding(context, question, category):
        severity, answer = REPLIES[category]
        cited = context.chunks[0].chunk_id
        return {
            "answer": answer,
            "citations": [cited],
            "dropped_citations": [],
            "injection_flagged": False,
            "supplied": [cited],
            "category": category,
            "specialist": flow.route_category(category).ROLE,
            "severity": severity,
            "contradiction": category in contradiction_in,
        }

    monkeypatch.setattr(flow, "investigate_finding", fake_finding)


@pytest.fixture
def stub_pipeline(monkeypatch):
    """The normal case: every question answered, no contradiction anywhere."""
    install_stub(monkeypatch)


QUESTIONS = [
    {"question_id": "Q001", "category": "compliance", "question": "SOC 2 valid?"},
    {"question_id": "Q007", "category": "security", "question": "Encryption at rest?"},
    {"question_id": "Q013", "category": "financial", "question": "Liability cap?"},
    {"question_id": "Q020", "category": "contract", "question": "Termination notice?"},
]


def corpus():
    return [
        chunk("meridian_sla_0001", "meridian_sla"),
        chunk("MSFT_item_1a_0001", "MSFT_item_1a"),
        chunk("BA_item_7_0005", "BA_item_7"),  # another vendor, must never appear
    ]


def test_the_whole_chain_runs_and_scores_from_findings(stub_pipeline):
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    # compliance HIGH=80, security LOW=20, financial MEDIUM=50, contract LOW=20
    assert verdict["category_scores"]["compliance"] == 80
    assert verdict["category_scores"]["security"] == 20
    assert verdict["category_scores"]["financial"] == 50
    assert verdict["category_scores"]["contract"] == 20
    # every question answered and every citation valid, no ground-truth labels
    # supplied, so accuracy contributes nothing: 0.50*1 + 0.30*0 + 0.20*1 = 0.70
    assert verdict["evidence_quality"] == pytest.approx(70.0)
    # the weighted formula gets the inverted risk, not the quality
    assert verdict["category_scores"]["evidence_quality"] == pytest.approx(30.0)
    assert verdict["recommendation"] in {b.decision for b in RULES.thresholds.values()}
    assert verdict["vendor"] == "Meridian CloudWorks"


def test_the_chain_is_deterministic(stub_pipeline):
    first = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    second = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    # `generated_at` is wall-clock and is meant to differ; everything that
    # affects the verdict must not.
    first.pop("generated_at"), second.pop("generated_at")
    assert first == second
    assert first["investigation_id"] == second["investigation_id"]


def test_citation_accuracy_is_measured_only_where_labels_exist(stub_pipeline):
    relevant = {"Q001": ["meridian_sla_0001"], "Q007": ["some_other_chunk"]}
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES, relevant=relevant
    )
    signals = verdict["evidence_signals"]
    assert signals["questions_with_labels"] == 2
    assert signals["citation_accuracy"] == pytest.approx(0.5)  # Q001 right, Q007 wrong


def test_a_leaked_chunk_from_another_vendor_stops_the_run(monkeypatch, stub_pipeline):
    def leaking_finding(context, question, category):
        return {
            "answer": "leaked",
            "citations": ["BA_item_7_0005"],
            "dropped_citations": [],
            "injection_flagged": False,
            "supplied": ["BA_item_7_0005"],  # belongs to a different vendor
            "category": category,
            "specialist": "x",
            "severity": "LOW",
            "contradiction": False,
        }

    monkeypatch.setattr(flow, "investigate_finding", leaking_finding)
    with pytest.raises(ValueError, match="leaked chunks from another vendor"):
        investigation.run_investigation(
            fake_context(corpus()), DOSSIER, QUESTIONS, RULES
        )


def test_no_contradiction_leaves_the_verdict_to_the_score_alone(stub_pipeline):
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    assert verdict["contradiction_found"] is False
    assert verdict["contradiction_questions"] == []
    assert verdict["recommendation"] == "APPROVE_WITH_CONDITIONS"
    assert verdict["escalate"] is False


def test_one_contradiction_forces_human_review_whatever_the_score(monkeypatch):
    install_stub(monkeypatch, contradiction_in={"security"})
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    assert verdict["contradiction_found"] is True
    assert verdict["contradiction_questions"] == ["Q007"]
    assert verdict["escalate"] is True
    # The recommendation still comes from the score; escalation rides alongside
    # it rather than rewriting it.
    assert verdict["recommendation"] == "APPROVE_WITH_CONDITIONS"


def test_the_contradiction_does_not_change_any_category_score(monkeypatch):
    install_stub(monkeypatch)
    clean = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    install_stub(monkeypatch, contradiction_in={"security", "compliance"})
    flagged = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    assert flagged["category_scores"] == clean["category_scores"]
    assert flagged["overall_score"] == clean["overall_score"]
    assert flagged["escalate"] is True and clean["escalate"] is False


def test_the_report_names_the_contradicted_questions(monkeypatch):
    install_stub(monkeypatch, contradiction_in={"compliance"})
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    report = investigation.format_report(verdict)
    assert "CONTRADICTION DETECTED in Q001 — human review required." in report
    assert "Human review required: YES" in report
    assert "· CONTRADICTION" in report


def test_report_shows_evidence_quality_next_to_its_inverted_risk(stub_pipeline):
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    report = investigation.format_report(verdict)
    assert "evidence quality 70.0" in report


# ------------------------------------------ report assembly + "Why?" (FR-018/016)


def test_citations_carry_the_document_not_just_a_chunk_id(stub_pipeline):
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    evidence = verdict["findings"][0]["evidence"][0]
    assert evidence["document_name"] == "meridian_sla.txt"
    assert evidence["chunk_id"] == "meridian_sla_0001"


@pytest.mark.parametrize(
    "page_start,page_end,expected",
    [
        (None, None, "Contract.pdf"),
        (17, 17, "Contract.pdf, p.17"),
        (17, 19, "Contract.pdf, pp.17-19"),
    ],
)
def test_a_citation_reads_the_way_a_reviewer_expects(page_start, page_end, expected):
    evidence = {
        "chunk_id": "x_0001",
        "document_name": "Contract.pdf",
        "page_start": page_start,
        "page_end": page_end,
    }
    assert investigation.cite(evidence) == expected


def test_the_same_investigation_always_gets_the_same_id():
    first = investigation.investigation_id("Meridian CloudWorks", QUESTIONS)
    second = investigation.investigation_id("Meridian CloudWorks", QUESTIONS[::-1])
    assert first == second  # question order must not matter
    assert investigation.investigation_id("Other Vendor", QUESTIONS) != first


def test_why_orders_contradictions_first_then_worst_severity(monkeypatch):
    install_stub(monkeypatch, contradiction_in={"contract"})
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    why = verdict["why"]
    # contract is only LOW, but it contradicts, so it leads
    assert why[0]["question_id"] == "Q020"
    assert why[0]["label"] == "CRITICAL"
    # then HIGH (compliance), MEDIUM (financial), LOW (security)
    assert [r["severity"] for r in why[1:]] == ["HIGH", "MEDIUM", "LOW"]


def test_why_labels_high_and_contradicted_findings_critical(stub_pipeline):
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    labels = {r["category"]: r["label"] for r in verdict["why"]}
    assert labels["compliance"] == "CRITICAL"  # HIGH severity
    assert labels["security"] == "WARNING"  # LOW severity


def test_why_skips_questions_that_found_nothing(monkeypatch):
    install_stub(monkeypatch)

    original = flow.investigate_finding

    def sometimes_nothing(context, question, category):
        result = original(context, question, category)
        if category == "financial":
            result.update(severity=None, answer="NOT FOUND IN EVIDENCE")
        return result

    monkeypatch.setattr(flow, "investigate_finding", sometimes_nothing)
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    assert "financial" not in {r["category"] for r in verdict["why"]}
    assert len(verdict["why"]) == 3


def test_why_is_capped_at_top_n(stub_pipeline):
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    assert len(investigation.explain(verdict, top_n=2)) == 2


def test_report_carries_the_investigation_id_and_version(stub_pipeline):
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    report = investigation.format_report(verdict)
    assert verdict["investigation_id"] in report
    assert "report v1" in report
    assert "WHY THIS RECOMMENDATION" in report


def test_report_shows_the_recommendation_scores_and_cited_findings(stub_pipeline):
    verdict = investigation.run_investigation(
        fake_context(corpus()), DOSSIER, QUESTIONS, RULES
    )
    report = investigation.format_report(verdict)
    assert "INVESTIGATION REPORT — Meridian CloudWorks" in report
    assert f"RECOMMENDATION: {verdict['recommendation']}" in report
    assert "SOC 2 expired." in report
    assert "Evidence -> meridian_sla.txt" in report
    assert "-- COMPLIANCE --" in report
