"""End to end over HTTP: upload -> index -> investigate -> ask.

Real routes, real service layer, real loader and chunker, real database and
real tenant filtering. Stubbed: the embedder and cross-encoder (too heavy for a
test), the LLM, and the investigation runner — so nothing here spends quota.
"""

# 1. Standard library imports
import os

# 2. Third-party imports
import pytest
from fastapi.testclient import TestClient

# 3. Internal imports
from sentineliq import service
from sentineliq.components.database import repository as repo
from sentineliq.components.llm.provider import LLMResponse
from sentineliq.pipeline import documents, qa

os.environ.setdefault("SECRET_KEY", "test-secret-key")

CONTRACT = b"""MASTER SERVICES AGREEMENT

1. Termination. Either party may terminate for convenience on ninety days
written notice to the other party.

2. Liability. The supplier's total liability is capped at the fees paid in the
twelve months before the claim.

3. Confidentiality. Each party shall notify the other within seventy-two hours
of any security breach affecting confidential information.
"""

VERDICT = {
    "investigation_id": "doc123",
    "report_version": "1",
    "generated_at": "2026-08-19T00:00:00+00:00",
    "vendor": "contract.txt",
    "subject_type": "uploaded_document",
    "generalisation_caveat": "not a validated vendor risk score",
    "overall_score": 38.0,
    "risk_level": "medium",
    "recommendation": "APPROVE_WITH_CONDITIONS",
    "confidence": None,
    "escalate": False,
    "contradiction_found": False,
    "contradiction_questions": [],
    "missing_critical_docs": False,
    "injection_flagged": False,
    "category_scores": {
        "compliance": 20.0,
        "security": 50.0,
        "financial": 50.0,
        "contract": 20.0,
        "evidence_quality": 20.0,
    },
    "evidence_quality": 80.0,
    "evidence_signals": {"retrieval_rate": 1.0},
    "findings_per_category": {"contract": 1},
    "findings": [
        {
            "question_id": "D001",
            "question": "What are the termination rights?",
            "category": "contract",
            "severity": "LOW",
            "contradiction": False,
            "answer": "Either party may terminate on ninety days notice.",
            "evidence": [
                {
                    "chunk_id": "chunk-1",
                    "document_name": "contract.txt",
                    "page_start": None,
                    "page_end": None,
                }
            ],
        }
    ],
    "why": [],
}


class FakeProvider:
    """Cites the first chunk it was given, so citations resolve for real."""

    def __init__(self, model=None):
        self.model = model

    def complete(self, system, user, *, temperature, max_tokens):
        first = user.split('<evidence id="')[1].split('"')[0]
        text = f"Either party may terminate on ninety days notice [{first}]."
        return LLMResponse(text=text, input_tokens=1, output_tokens=1, model="fake")


class FakeTokenizer:
    def encode(self, text, add_special_tokens=False):
        return text.split()


@pytest.fixture
def client(monkeypatch, db_url, tmp_path):
    """An API client whose uploads are chunked for real but never embedded."""
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(service, "UPLOAD_DIR", tmp_path / "uploads")

    # Real chunking, no model download.
    monkeypatch.setitem(documents._models, "tokenizer", FakeTokenizer())
    # No embedder, no reranker, no FAISS: this file is about the HTTP flow.
    monkeypatch.setattr(
        documents,
        "load_models",
        lambda config: {"embedder": "embedder", "cross_encoder": "cross_encoder"},
    )
    monkeypatch.setattr(
        documents, "build_indexes", lambda chunks, embedder: (None, None)
    )
    # Retrieval returns the context's own chunks, which is what makes a
    # cross-document leak visible if the wrong context is ever built.
    monkeypatch.setattr(
        qa, "retrieve_evidence", lambda context, question: context.chunks[:3]
    )
    monkeypatch.setattr("sentineliq.components.llm.provider.GroqProvider", FakeProvider)
    documents.clear_cache()

    from sentineliq.components.api.app import create_app

    app = create_app()
    app.state.document_runner = lambda name: dict(VERDICT, vendor=name)

    with repo.session_scope(app.state.session_factory) as session:
        service.register_user(session, "tenant-a", "alice", "pw-alice", "analyst")
        service.register_user(session, "tenant-b", "bob", "pw-bob", "analyst")

    with TestClient(app) as test_client:
        yield test_client

    documents.clear_cache()


def auth(client, username, password):
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def upload(client, headers, name="contract.txt", body=CONTRACT):
    return client.post(
        "/api/documents", files={"upload": (name, body)}, headers=headers
    )


# ------------------------------------------------------------- upload


def test_an_upload_is_chunked_and_reports_its_chunk_count(client):
    headers = auth(client, "alice", "pw-alice")

    response = upload(client, headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["document_name"] == "contract.txt"
    assert body["chunk_count"] >= 1


def test_an_upload_needs_no_vendor_name(client):
    """The demo uploads a document, not a vendor dossier."""
    headers = auth(client, "alice", "pw-alice")

    document_id = upload(client, headers).json()["document_id"]

    with repo.session_scope(client.app.state.session_factory) as session:
        stored = repo.get_document(session, "tenant-a", document_id)
        assert stored.vendor_name == "contract"  # the filename stem


def test_the_chunks_are_stored_against_the_document(client):
    headers = auth(client, "alice", "pw-alice")
    document_id = upload(client, headers).json()["document_id"]

    with repo.session_scope(client.app.state.session_factory) as session:
        chunks = repo.list_chunks(session, "tenant-a", document_id)
        assert chunks
        assert all(chunk.chunk_id.startswith(document_id) for chunk in chunks)
        assert repo.list_chunks(session, "tenant-b", document_id) == []


def test_a_file_that_cannot_be_parsed_is_rejected_and_not_kept(client):
    headers = auth(client, "alice", "pw-alice")

    response = upload(client, headers, name="broken.txt", body=b"\x00\x01\x02binary")

    assert response.status_code == 400
    assert client.get("/api/documents", headers=headers).json() == []


def test_an_unsupported_file_type_is_rejected(client):
    headers = auth(client, "alice", "pw-alice")

    response = upload(client, headers, name="notes.md", body=b"# hello")

    assert response.status_code == 400


def test_re_uploading_the_same_file_does_not_duplicate_its_chunks(client):
    headers = auth(client, "alice", "pw-alice")

    first = upload(client, headers).json()
    second = upload(client, headers).json()

    assert first["document_id"] == second["document_id"]
    assert first["chunk_count"] == second["chunk_count"]


def test_the_document_overview_reports_the_chunk_count(client):
    headers = auth(client, "alice", "pw-alice")
    document_id = upload(client, headers).json()["document_id"]

    response = client.get(f"/api/documents/{document_id}", headers=headers)

    assert response.status_code == 200
    assert response.json()["chunk_count"] >= 1


def test_another_tenant_cannot_read_the_document(client):
    alice = auth(client, "alice", "pw-alice")
    document_id = upload(client, alice).json()["document_id"]

    bob = auth(client, "bob", "pw-bob")
    assert client.get(f"/api/documents/{document_id}", headers=bob).status_code == 404


# ------------------------------------------------------ investigation


def test_an_uploaded_document_can_be_investigated(client):
    headers = auth(client, "alice", "pw-alice")
    document_id = upload(client, headers).json()["document_id"]

    started = client.post(f"/api/documents/{document_id}/investigate", headers=headers)
    assert started.status_code == 202
    investigation_id = started.json()["investigation_id"]
    assert started.json()["vendor_name"] == "contract.txt"

    report = client.get(
        f"/api/investigations/{investigation_id}/report", headers=headers
    )
    assert report.status_code == 200
    assert report.json()["recommendation"] == "APPROVE_WITH_CONDITIONS"
    assert report.json()["subject_type"] == "uploaded_document"


def test_the_report_carries_the_generalisation_caveat(client):
    """The score must not be presented as a validated vendor risk score."""
    headers = auth(client, "alice", "pw-alice")
    document_id = upload(client, headers).json()["document_id"]
    started = client.post(f"/api/documents/{document_id}/investigate", headers=headers)

    report = client.get(
        f"/api/investigations/{started.json()['investigation_id']}/report",
        headers=headers,
    ).json()

    assert report["generalisation_caveat"]


def test_findings_and_evidence_come_back_through_the_usual_endpoints(client):
    headers = auth(client, "alice", "pw-alice")
    document_id = upload(client, headers).json()["document_id"]
    started = client.post(f"/api/documents/{document_id}/investigate", headers=headers)
    investigation_id = started.json()["investigation_id"]

    findings = client.get(
        f"/api/investigations/{investigation_id}/findings", headers=headers
    ).json()
    evidence = client.get(
        f"/api/investigations/{investigation_id}/evidence", headers=headers
    ).json()

    assert findings[0]["question_id"] == "D001"
    assert evidence[0]["document_name"] == "contract.txt"


def test_a_failing_document_run_is_recorded_not_lost(client):
    headers = auth(client, "alice", "pw-alice")
    document_id = upload(client, headers).json()["document_id"]

    def broken(name):
        raise RuntimeError("the model was unreachable")

    client.app.state.document_runner = broken
    started = client.post(f"/api/documents/{document_id}/investigate", headers=headers)
    assert started.status_code == 202

    state = client.get(
        f"/api/investigations/{started.json()['investigation_id']}/status",
        headers=headers,
    ).json()
    assert state["status"] == "failed"
    assert "unreachable" in state["error"]


def test_another_tenant_cannot_investigate_the_document(client):
    alice = auth(client, "alice", "pw-alice")
    document_id = upload(client, alice).json()["document_id"]

    bob = auth(client, "bob", "pw-bob")
    response = client.post(f"/api/documents/{document_id}/investigate", headers=bob)
    assert response.status_code == 404


def test_investigating_a_document_that_does_not_exist_is_a_404(client):
    headers = auth(client, "alice", "pw-alice")
    assert (
        client.post("/api/documents/nope/investigate", headers=headers).status_code
        == 404
    )


# ---------------------------------------------------------------- Q&A


def ask(client, headers, document_id, question="What are the termination terms?"):
    return client.post(
        f"/api/documents/{document_id}/questions",
        json={"question": question},
        headers=headers,
    )


def test_a_question_is_answered_with_citations_into_the_document(client):
    headers = auth(client, "alice", "pw-alice")
    document_id = upload(client, headers).json()["document_id"]

    response = ask(client, headers, document_id)

    assert response.status_code == 200
    body = response.json()
    assert body["abstained"] is False
    assert body["citations"]
    assert body["citations"][0]["chunk_id"].startswith(document_id)
    assert body["citations"][0]["document_name"] == "contract.txt"
    assert body["citations"][0]["text"]


def test_several_questions_can_be_asked_about_the_same_document(client):
    headers = auth(client, "alice", "pw-alice")
    document_id = upload(client, headers).json()["document_id"]

    for question in ["Termination?", "Liability cap?", "Breach notice period?"]:
        response = ask(client, headers, document_id, question)
        assert response.status_code == 200, question
        assert response.json()["citations"]


def test_a_question_cannot_reach_another_document(client):
    """Every retrieved chunk must belong to the document that was asked about."""
    headers = auth(client, "alice", "pw-alice")
    first = upload(client, headers, name="first.txt").json()["document_id"]
    second = upload(
        client, headers, name="second.txt", body=CONTRACT.replace(b"ninety", b"thirty")
    ).json()["document_id"]

    body = ask(client, headers, second).json()

    assert body["retrieved"]
    assert all(item["chunk_id"].startswith(second) for item in body["retrieved"])
    assert all(not item["chunk_id"].startswith(first) for item in body["retrieved"])


def test_another_tenant_cannot_ask_about_the_document(client):
    alice = auth(client, "alice", "pw-alice")
    document_id = upload(client, alice).json()["document_id"]

    bob = auth(client, "bob", "pw-bob")
    assert ask(client, bob, document_id).status_code == 404


def test_an_empty_question_is_refused(client):
    headers = auth(client, "alice", "pw-alice")
    document_id = upload(client, headers).json()["document_id"]

    assert ask(client, headers, document_id, "   ").status_code == 400


def test_an_unsupported_answer_is_reported_as_insufficient_evidence(
    client, monkeypatch
):
    """A model answer citing nothing real must not be shown as an answer."""

    class Ungrounded:
        def __init__(self, model=None):
            pass

        def complete(self, system, user, *, temperature, max_tokens):
            return LLMResponse(
                text="The contract may be cancelled at any time [invented_0001].",
                input_tokens=1,
                output_tokens=1,
                model="fake",
            )

    headers = auth(client, "alice", "pw-alice")
    document_id = upload(client, headers).json()["document_id"]
    monkeypatch.setattr("sentineliq.components.llm.provider.GroqProvider", Ungrounded)

    body = ask(client, headers, document_id).json()

    assert body["abstained"] is True
    assert body["citations"] == []
    assert "does not answer" in body["answer"]


# ------------------------------------------------------- vendor groups


def stub_specialist_replies(monkeypatch):
    """Deterministic answers, no LLM call — same idea as test_investigation.py."""
    from sentineliq.pipeline import flow

    def fake_finding(context, question, category):
        cited = context.chunks[0].chunk_id if context.chunks else None
        return {
            "answer": "Stubbed answer." if cited else "NOT FOUND IN EVIDENCE",
            "citations": [cited] if cited else [],
            "dropped_citations": [],
            "injection_flagged": False,
            "supplied": [cited] if cited else [],
            "category": category,
            "specialist": flow.route_category(category).ROLE,
            "severity": "LOW" if cited else None,
            "contradiction": False,
        }

    monkeypatch.setattr(flow, "investigate_finding", fake_finding)


def upload_typed(client, headers, vendor_name, document_type, name, body=CONTRACT):
    return client.post(
        "/api/documents",
        data={"vendor_name": vendor_name, "document_type": document_type},
        files={"upload": (name, body)},
        headers=headers,
    )


def test_uploading_with_an_invalid_document_type_is_rejected(client):
    headers = auth(client, "alice", "pw-alice")

    response = upload_typed(client, headers, "Acme Corp", "not-a-real-type", "x.txt")

    assert response.status_code == 400


def test_a_vendor_group_lists_every_document_type_uploaded(client):
    headers = auth(client, "alice", "pw-alice")
    upload_typed(client, headers, "Acme Corp", "contract", "contract.txt", CONTRACT)
    upload_typed(
        client,
        headers,
        "Acme Corp",
        "financial",
        "financial.txt",
        CONTRACT.replace(b"ninety", b"sixty"),
    )

    response = client.get("/api/vendor-groups/Acme Corp", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert sorted(body["available_types"]) == ["contract", "financial"]
    assert len(body["documents"]) == 2


def test_an_unknown_vendor_group_is_a_404(client):
    headers = auth(client, "alice", "pw-alice")

    assert (
        client.get("/api/vendor-groups/Nobody Inc", headers=headers).status_code == 404
    )


def test_three_documents_run_the_full_investigation(client, monkeypatch):
    stub_specialist_replies(monkeypatch)
    headers = auth(client, "alice", "pw-alice")
    client.app.state.document_runner = None
    upload_typed(client, headers, "Full Corp", "contract", "c.txt", CONTRACT)
    upload_typed(
        client,
        headers,
        "Full Corp",
        "financial",
        "f.txt",
        CONTRACT.replace(b"ninety", b"sixty"),
    )
    upload_typed(
        client,
        headers,
        "Full Corp",
        "security",
        "s.txt",
        b"SECURITY POLICY\n\nData is encrypted at rest and in transit at all times.",
    )

    started = client.post("/api/vendor-groups/Full Corp/investigate", headers=headers)
    assert started.status_code == 202
    investigation_id = started.json()["investigation_id"]

    report = client.get(
        f"/api/investigations/{investigation_id}/report", headers=headers
    ).json()
    assert report["documents_analyzed"] == {
        "contract": True,
        "financial": True,
        "security": True,
    }
    assert report["coverage_caveat"] is None


def test_two_documents_run_the_same_pipeline_with_a_partial_caveat(client, monkeypatch):
    stub_specialist_replies(monkeypatch)
    headers = auth(client, "alice", "pw-alice")
    client.app.state.document_runner = None
    upload_typed(client, headers, "Partial Corp", "contract", "c.txt", CONTRACT)
    upload_typed(
        client,
        headers,
        "Partial Corp",
        "financial",
        "f.txt",
        CONTRACT.replace(b"ninety", b"sixty"),
    )

    started = client.post(
        "/api/vendor-groups/Partial Corp/investigate", headers=headers
    )
    investigation_id = started.json()["investigation_id"]

    report = client.get(
        f"/api/investigations/{investigation_id}/report", headers=headers
    ).json()
    assert report["documents_analyzed"]["security"] is False
    assert "not provided" in report["coverage_caveat"]


def test_one_document_runs_the_same_pipeline_with_its_own_caveat(client, monkeypatch):
    stub_specialist_replies(monkeypatch)
    headers = auth(client, "alice", "pw-alice")
    client.app.state.document_runner = None
    upload_typed(client, headers, "Solo Corp", "contract", "c.txt", CONTRACT)

    started = client.post("/api/vendor-groups/Solo Corp/investigate", headers=headers)
    investigation_id = started.json()["investigation_id"]

    report = client.get(
        f"/api/investigations/{investigation_id}/report", headers=headers
    ).json()
    assert report["documents_analyzed"]["contract"] is True
    assert report["documents_analyzed"]["financial"] is False
    assert report["coverage_caveat"] is not None


def test_investigating_an_empty_vendor_group_is_a_404(client):
    headers = auth(client, "alice", "pw-alice")

    response = client.post("/api/vendor-groups/Nobody Inc/investigate", headers=headers)

    assert response.status_code == 404


def test_qa_searches_the_whole_vendor_groups_document_set(client):
    headers = auth(client, "alice", "pw-alice")
    upload_typed(client, headers, "QA Corp", "contract", "c.txt", CONTRACT)
    upload_typed(
        client,
        headers,
        "QA Corp",
        "financial",
        "f.txt",
        CONTRACT.replace(b"ninety", b"sixty"),
    )

    response = client.post(
        "/api/vendor-groups/QA Corp/questions",
        json={"question": "termination?"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["citations"]


def test_qa_on_an_unknown_vendor_group_is_a_404(client):
    headers = auth(client, "alice", "pw-alice")

    response = client.post(
        "/api/vendor-groups/Nobody Inc/questions",
        json={"question": "termination?"},
        headers=headers,
    )

    assert response.status_code == 404


def test_another_tenant_cannot_see_or_investigate_this_vendor_group(client):
    alice = auth(client, "alice", "pw-alice")
    upload_typed(client, alice, "Private Corp", "contract", "c.txt", CONTRACT)

    bob = auth(client, "bob", "pw-bob")
    assert client.get("/api/vendor-groups/Private Corp", headers=bob).status_code == 404
    assert (
        client.post(
            "/api/vendor-groups/Private Corp/investigate", headers=bob
        ).status_code
        == 404
    )


# ---------------------------------------------------------- preloaded demo


def test_the_preloaded_demo_report_is_reachable(client):
    headers = auth(client, "alice", "pw-alice")

    response = client.get("/api/demo/meridian", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["vendor"] == "Meridian CloudWorks"
    assert body["subject_type"] == "preloaded_demo"


def test_the_preloaded_demo_needs_no_special_tenant(client):
    """Shared fixed content, like /api/evaluations — any tenant sees it."""
    bob = auth(client, "bob", "pw-bob")

    assert client.get("/api/demo/meridian", headers=bob).status_code == 200


def test_the_preloaded_demo_still_requires_authentication(client):
    assert client.get("/api/demo/meridian").status_code == 401
