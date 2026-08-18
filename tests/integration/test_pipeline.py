"""End-to-end: API → service → investigation pipeline → engine → report.

The only thing stubbed is the LLM call itself (`flow.investigate_finding`) and
the index building, so no Groq quota is spent. Everything else is the real
code: real dossier scoping, the real score bridge, the real deterministic
engine, the real persistence layer and the real HTTP routes.
"""

# 1. Standard library imports
import json
import os
from pathlib import Path

# 2. Third-party imports
import pytest
from fastapi.testclient import TestClient

# 3. Internal imports
from sentineliq import service
from sentineliq.components.database import repository as repo
from sentineliq.components.models.schemas import Chunk
from sentineliq.config import load_risk_rules
from sentineliq.pipeline import flow, investigation

os.environ.setdefault("SECRET_KEY", "test-secret-key")

DOCUMENTS = Path("data/raw/documents")
VENDOR = "Meridian CloudWorks"

SEVERITY_BY_CATEGORY = {
    "compliance": ("HIGH", True),  # the planted SOC 2 contradiction
    "security": ("MEDIUM", False),
    "financial": ("MEDIUM", False),
    "contract": ("LOW", False),
}


@pytest.fixture
def real_chunks():
    """A small real corpus: the vendor's text documents, chunked for real."""
    from sentineliq.components.evaluation.retrieval_eval import load_document
    from sentineliq.components.ingestion.chunker import chunk_document

    dossier = investigation.load_dossier(DOCUMENTS / "dossiers.json", VENDOR)
    wanted = investigation.dossier_document_ids(dossier)

    chunks: list[Chunk] = []
    for path in sorted(DOCUMENTS.iterdir()):
        if path.suffix == ".txt" and path.stem in wanted:
            document = load_document(path)
            chunks.extend(
                chunk_document(
                    document,
                    lambda text: len(text.split()),
                    chunk_size=512,
                    chunk_overlap=64,
                )
            )
    assert chunks, "expected real chunks for this vendor"
    return chunks


@pytest.fixture
def client(monkeypatch, real_chunks, db_url):
    """The API, wired to the real pipeline with only the LLM replaced."""
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("DATABASE_URL", db_url)

    # 1. Stub index building and the LLM; everything else stays real.
    monkeypatch.setattr(investigation.dense, "build_index", lambda model, chunks: None)
    monkeypatch.setattr(investigation.sparse, "build_index", lambda chunks: None)

    def fake_finding(context, question, category):
        severity, contradiction = SEVERITY_BY_CATEGORY[category]
        cited = context.chunks[0].chunk_id
        answer = f"Deterministic stub answer for {category}. [{cited}]"
        result = flow.engine.synthesise(answer, answer, [cited])
        result.update(
            supplied=[cited],
            category=category,
            specialist=flow.route_category(category).ROLE,
            severity=severity,
            contradiction=contradiction,
        )
        return result

    monkeypatch.setattr(flow, "investigate_finding", fake_finding)

    # 2. The real investigation runner over the real dossier and questions.
    from scripts.investigate import load_questions
    from sentineliq.components.api.app import create_app

    app = create_app()
    rules = load_risk_rules()

    def runner(vendor_name: str) -> dict:
        dossier = investigation.load_dossier(DOCUMENTS / "dossiers.json", vendor_name)
        questions = load_questions(vendor_name)
        context = flow.RunContext(
            config=None,
            embedder=None,
            faiss_index=None,
            bm25_index=None,
            cross_encoder=None,
            chunks=real_chunks,
            llm=None,
        )
        return investigation.run_investigation(context, dossier, questions, rules)

    app.state.runner = runner

    with repo.session_scope(app.state.session_factory) as session:
        service.register_user(session, "tenant-a", "alice", "pw-alice", "analyst")

    with TestClient(app) as test_client:
        yield test_client


def sign_in(client) -> dict:
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": "pw-alice"}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_the_whole_chain_runs_from_the_api_and_stores_a_cited_report(client):
    headers = sign_in(client)

    # 1. Create.
    created = client.post(
        "/api/investigations", json={"vendor_name": VENDOR}, headers=headers
    )
    assert created.status_code == 201
    investigation_id = created.json()["investigation_id"]

    # 2. Run the real pipeline. /run is asynchronous (FR-022): it accepts with
    #    202 and `running`, and the test client drains the background task
    #    before the next request, so /status then reads `complete`.
    run = client.post(f"/api/investigations/{investigation_id}/run", headers=headers)
    assert run.status_code == 202, run.text
    assert run.json()["status"] == "running"

    status_response = client.get(
        f"/api/investigations/{investigation_id}/status", headers=headers
    )
    assert status_response.json()["status"] == "complete", status_response.text

    # 3. The stored report is complete and internally consistent.
    report = client.get(
        f"/api/investigations/{investigation_id}/report", headers=headers
    ).json()

    assert report["vendor"] == VENDOR
    assert report["investigation_id"]
    assert report["report_version"] == "1"
    assert set(report["category_scores"]) == {
        "compliance",
        "security",
        "financial",
        "contract",
        "evidence_quality",
    }
    # compliance findings are HIGH -> 80 straight from risk_rules.yaml
    assert report["category_scores"]["compliance"] == 80.0
    # the compliance contradiction must force human review
    assert report["contradiction_found"] is True
    assert report["escalate"] is True
    assert report["why"], "the report must explain itself"
    assert report["why"][0]["label"] == "CRITICAL"


def test_every_citation_belongs_to_this_vendor(client):
    headers = sign_in(client)
    created = client.post(
        "/api/investigations", json={"vendor_name": VENDOR}, headers=headers
    )
    investigation_id = created.json()["investigation_id"]
    client.post(f"/api/investigations/{investigation_id}/run", headers=headers)

    dossier = investigation.load_dossier(DOCUMENTS / "dossiers.json", VENDOR)
    allowed = investigation.dossier_document_ids(dossier)

    evidence = client.get(
        f"/api/investigations/{investigation_id}/evidence", headers=headers
    ).json()
    assert evidence
    for item in evidence:
        assert Path(item["document_name"]).stem in allowed


def test_findings_are_persisted_and_queryable(client):
    headers = sign_in(client)
    created = client.post(
        "/api/investigations", json={"vendor_name": VENDOR}, headers=headers
    )
    investigation_id = created.json()["investigation_id"]
    client.post(f"/api/investigations/{investigation_id}/run", headers=headers)

    findings = client.get(
        f"/api/investigations/{investigation_id}/findings", headers=headers
    ).json()
    assert len(findings) == 4
    categories = {f["category"] for f in findings}
    assert categories == {"compliance", "security", "financial", "contract"}
    assert any(f["contradiction"] for f in findings)


def test_the_report_survives_a_restart_of_the_process(client):
    """The report is read back out of the database, not from memory."""
    headers = sign_in(client)
    created = client.post(
        "/api/investigations", json={"vendor_name": VENDOR}, headers=headers
    )
    investigation_id = created.json()["investigation_id"]
    client.post(f"/api/investigations/{investigation_id}/run", headers=headers)

    with repo.session_scope(client.app.state.session_factory) as session:
        stored = repo.get_investigation(session, "tenant-a", investigation_id)
        assert stored.status == "complete"
        assert json.loads(stored.report_json)["vendor"] == VENDOR
