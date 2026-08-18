"""API tests: auth, RBAC, tenant isolation and the investigation endpoints.

The investigation runner is stubbed, so nothing here calls an LLM or spends
quota. The database is a fresh in-memory SQLite per test.
"""

# 1. Standard library imports
import os

# 2. Third-party imports
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

# 3. Internal imports
from sentineliq import service
from sentineliq.components.database import repository as repo

os.environ.setdefault("SECRET_KEY", "test-secret-key")

VERDICT = {
    "investigation_id": "abc123",
    "report_version": "1",
    "generated_at": "2026-08-16T00:00:00+00:00",
    "vendor": "Meridian CloudWorks",
    "overall_score": 41.48,
    "risk_level": "medium",
    "recommendation": "APPROVE_WITH_CONDITIONS",
    "confidence": None,
    "escalate": True,
    "contradiction_found": True,
    "contradiction_questions": ["Q001"],
    "missing_critical_docs": False,
    "injection_flagged": False,
    "category_scores": {
        "compliance": 80.0,
        "security": 20.0,
        "financial": 50.0,
        "contract": 20.0,
        "evidence_quality": 9.75,
    },
    "evidence_quality": 90.25,
    "evidence_signals": {"retrieval_rate": 1.0},
    "findings_per_category": {"compliance": 1},
    "findings": [
        {
            "question_id": "Q001",
            "question": "SOC 2 valid?",
            "category": "compliance",
            "severity": "HIGH",
            "contradiction": True,
            "answer": "The certificate expired on 15 March 2024.",
            "evidence": [
                {
                    "chunk_id": "meridian_soc2_summary_0000",
                    "document_name": "meridian_soc2_summary.txt",
                    "page_start": None,
                    "page_end": None,
                }
            ],
        }
    ],
    "why": [],
}


@pytest.fixture
def client(monkeypatch, db_url):
    """An API client on an empty test database with a stubbed runner."""
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("DATABASE_URL", db_url)

    from sentineliq.components.api.app import create_app

    app = create_app()
    app.state.runner = lambda vendor_name: dict(VERDICT, vendor=vendor_name)

    # Two tenants, three users, so isolation and RBAC can both be exercised.
    with repo.session_scope(app.state.session_factory) as session:
        service.register_user(session, "tenant-a", "alice", "pw-alice", "analyst")
        service.register_user(session, "tenant-a", "admin-a", "pw-admin", "admin")
        service.register_user(session, "tenant-b", "bob", "pw-bob", "analyst")

    with TestClient(app) as test_client:
        yield test_client


def token(client, username, password):
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(client, username, password):
    return {"Authorization": f"Bearer {token(client, username, password)}"}


def make_investigation(client, headers, vendor="Meridian CloudWorks"):
    response = client.post(
        "/api/investigations", json={"vendor_name": vendor}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["investigation_id"]


# --------------------------------------------------------------- health


def test_health_reports_ok_without_a_token(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["database"] == "ok"


# ----------------------------------------------------------------- auth


def test_login_returns_a_bearer_token(client):
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": "pw-alice"}
    )
    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"


def test_a_wrong_password_is_rejected(client):
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong"}
    )
    assert response.status_code == 401


def test_an_unknown_user_gets_the_same_message_as_a_wrong_password(client):
    unknown = client.post(
        "/api/auth/login", json={"username": "nobody", "password": "x"}
    )
    wrong = client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong"}
    )
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_every_protected_endpoint_refuses_an_anonymous_caller(client):
    assert client.get("/api/investigations").status_code == 401
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/evaluations").status_code == 401
    assert (
        client.post("/api/investigations", json={"vendor_name": "X"}).status_code == 401
    )


def test_a_tampered_token_is_rejected(client):
    good = token(client, "alice", "pw-alice")
    tampered = good[:-4] + "aaaa"
    response = client.get(
        "/api/investigations", headers={"Authorization": f"Bearer {tampered}"}
    )
    assert response.status_code == 401


def test_a_token_never_contains_the_password_hash(client):
    claims = service.decode_token(token(client, "alice", "pw-alice"))
    assert set(claims) == {"sub", "tenant_id", "username", "role", "exp"}


# ------------------------------------------------------ investigations


def test_an_investigation_runs_and_stores_its_report(client):
    headers = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, headers)

    # 202 and `running`: the run is accepted, not finished (FR-022). The test
    # client drains background tasks before returning, so by the next request
    # the report is already stored.
    run = client.post(f"/api/investigations/{investigation_id}/run", headers=headers)
    assert run.status_code == 202
    assert run.json()["status"] == "running"

    report = client.get(
        f"/api/investigations/{investigation_id}/report", headers=headers
    )
    assert report.status_code == 200
    assert report.json()["recommendation"] == "APPROVE_WITH_CONDITIONS"
    assert report.json()["escalate"] is True


def test_the_run_is_committed_as_running_before_the_work_starts(client):
    """This is what makes polling work.

    The request marks the row `running` and commits before returning 202, so
    the background task starts against an already-committed status. A client
    that polls the instant it gets its 202 therefore sees `running`, never a
    stale `pending`.

    Checked from inside the runner rather than with a second concurrent
    request: the test client serialises requests through one portal, so a
    concurrent poll would deadlock against the background task it is waiting
    for.
    """
    headers = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, headers)
    seen = {}

    def observe(vendor_name):
        with repo.session_scope(client.app.state.session_factory) as session:
            row = repo.get_investigation(session, "tenant-a", investigation_id)
            seen["status_while_running"] = row.status
        return dict(VERDICT, vendor=vendor_name)

    client.app.state.runner = observe

    accepted = client.post(
        f"/api/investigations/{investigation_id}/run", headers=headers
    )
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "running"
    assert seen["status_while_running"] == "running"


def test_running_an_investigation_that_is_already_running_is_refused(client):
    headers = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, headers)

    # Put it in the state a run in progress leaves behind, without needing a
    # concurrent request to hold it there.
    with repo.session_scope(client.app.state.session_factory) as session:
        repo.mark_running(session, "tenant-a", investigation_id)

    second = client.post(f"/api/investigations/{investigation_id}/run", headers=headers)
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]


def test_a_completed_investigation_can_be_run_again(client):
    """409 is for a run in progress, not a permanent lock."""
    headers = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, headers)

    first = client.post(f"/api/investigations/{investigation_id}/run", headers=headers)
    assert first.status_code == 202

    again = client.post(f"/api/investigations/{investigation_id}/run", headers=headers)
    assert again.status_code == 202


def test_running_another_tenants_investigation_is_a_404(client):
    alice = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, alice)

    bob = auth(client, "bob", "pw-bob")
    assert (
        client.post(
            f"/api/investigations/{investigation_id}/run", headers=bob
        ).status_code
        == 404
    )


def test_running_an_investigation_that_does_not_exist_is_a_404(client):
    headers = auth(client, "alice", "pw-alice")
    assert (
        client.post(
            "/api/investigations/does-not-exist/run", headers=headers
        ).status_code
        == 404
    )


def test_status_moves_from_pending_to_complete(client):
    headers = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, headers)

    before = client.get(
        f"/api/investigations/{investigation_id}/status", headers=headers
    )
    assert before.json()["status"] == "pending"

    client.post(f"/api/investigations/{investigation_id}/run", headers=headers)
    after = client.get(
        f"/api/investigations/{investigation_id}/status", headers=headers
    )
    assert after.json()["status"] == "complete"


def test_findings_and_evidence_are_returned_with_document_and_page(client):
    headers = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, headers)
    client.post(f"/api/investigations/{investigation_id}/run", headers=headers)

    findings = client.get(
        f"/api/investigations/{investigation_id}/findings", headers=headers
    ).json()
    assert findings[0]["severity"] == "HIGH"
    assert findings[0]["contradiction"] is True

    evidence = client.get(
        f"/api/investigations/{investigation_id}/evidence", headers=headers
    ).json()
    assert evidence[0]["document_name"] == "meridian_soc2_summary.txt"


def test_a_failing_run_is_recorded_as_failed(client):
    headers = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, headers)

    def broken(vendor_name):
        raise RuntimeError("retrieval exploded")

    client.app.state.runner = broken
    # The caller is not left holding the exception — the run is asynchronous,
    # so the failure is recorded on the investigation and read back by /status.
    accepted = client.post(
        f"/api/investigations/{investigation_id}/run", headers=headers
    )
    assert accepted.status_code == 202

    client.app.state.runner = lambda vendor_name: VERDICT
    status_response = client.get(
        f"/api/investigations/{investigation_id}/status", headers=headers
    )
    assert status_response.json()["status"] == "failed"
    assert "retrieval exploded" in status_response.json()["error"]


def test_a_report_that_does_not_exist_is_a_404(client):
    headers = auth(client, "alice", "pw-alice")
    response = client.get("/api/investigations/no-such-id/report", headers=headers)
    assert response.status_code == 404


# ------------------------------------------------- tenant isolation (API)


def test_tenant_b_cannot_see_tenant_as_investigations(client):
    alice = auth(client, "alice", "pw-alice")
    make_investigation(client, alice)

    bob = auth(client, "bob", "pw-bob")
    assert client.get("/api/investigations", headers=bob).json() == []


def test_tenant_b_cannot_read_tenant_as_report(client):
    alice = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, alice)
    client.post(f"/api/investigations/{investigation_id}/run", headers=alice)

    bob = auth(client, "bob", "pw-bob")
    for suffix in ["report", "status", "findings"]:
        response = client.get(
            f"/api/investigations/{investigation_id}/{suffix}", headers=bob
        )
        assert response.status_code == 404, suffix


def test_tenant_b_cannot_run_tenant_as_investigation(client):
    alice = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, alice)

    bob = auth(client, "bob", "pw-bob")
    response = client.post(f"/api/investigations/{investigation_id}/run", headers=bob)
    assert response.status_code == 404


def test_tenant_b_cannot_read_tenant_as_evidence(client):
    alice = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, alice)
    client.post(f"/api/investigations/{investigation_id}/run", headers=alice)

    bob = auth(client, "bob", "pw-bob")
    evidence = client.get(
        f"/api/investigations/{investigation_id}/evidence", headers=bob
    )
    assert evidence.json() == []


# ------------------------------------------------------------- RBAC


def test_an_analyst_cannot_delete_an_investigation(client):
    alice = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, alice)

    response = client.delete(f"/api/investigations/{investigation_id}", headers=alice)
    assert response.status_code == 403


def test_an_admin_can_delete_an_investigation(client):
    alice = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, alice)

    admin = auth(client, "admin-a", "pw-admin")
    response = client.delete(f"/api/investigations/{investigation_id}", headers=admin)
    assert response.status_code == 204
    assert client.get("/api/investigations", headers=alice).json() == []


def test_an_admin_of_another_tenant_still_cannot_delete(client):
    alice = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, alice)

    # bob is only an analyst, so promote the check: a tenant-b admin is the
    # real test that role does not override tenant.
    with repo.session_scope(client.app.state.session_factory) as session:
        service.register_user(session, "tenant-b", "admin-b", "pw-b", "admin")

    admin_b = auth(client, "admin-b", "pw-b")
    response = client.delete(f"/api/investigations/{investigation_id}", headers=admin_b)
    assert response.status_code == 404
    assert len(client.get("/api/investigations", headers=alice).json()) == 1


# --------------------------------------------------------- documents


def test_uploading_a_document_stores_it(client, tmp_path):
    headers = auth(client, "alice", "pw-alice")
    response = client.post(
        "/api/documents",
        data={"vendor_name": "Meridian CloudWorks"},
        files={"upload": ("policy.txt", b"Vendor shall retain records.", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["document_name"] == "policy.txt"

    listed = client.get("/api/documents", headers=headers).json()
    assert len(listed) == 1


def test_uploading_an_executable_renamed_to_pdf_is_rejected(client):
    headers = auth(client, "alice", "pw-alice")
    response = client.post(
        "/api/documents",
        data={"vendor_name": "Meridian CloudWorks"},
        files={"upload": ("invoice.pdf", b"MZ\x90\x00\x03", "application/pdf")},
        headers=headers,
    )
    assert response.status_code == 400
    assert "not a real .pdf" in response.json()["detail"]
    assert client.get("/api/documents", headers=headers).json() == []


def test_tenant_b_cannot_see_tenant_as_documents(client):
    alice = auth(client, "alice", "pw-alice")
    client.post(
        "/api/documents",
        data={"vendor_name": "Meridian CloudWorks"},
        files={"upload": ("policy.txt", b"Vendor shall retain records.", "text/plain")},
        headers=alice,
    )
    bob = auth(client, "bob", "pw-bob")
    assert client.get("/api/documents", headers=bob).json() == []


# ------------------------------------------------------- evaluations


def test_evaluations_returns_the_recorded_reliability_numbers(client):
    headers = auth(client, "alice", "pw-alice")
    response = client.get("/api/evaluations", headers=headers)
    assert response.status_code == 200
    assert "computed" in response.json()


def upload(client, headers, name, body, vendor="Meridian CloudWorks"):
    return client.post(
        "/api/documents",
        data={"vendor_name": vendor},
        files={"upload": (name, body, "text/plain")},
        headers=headers,
    )


def test_re_uploading_the_same_file_returns_the_existing_document(client):
    """FR-001: duplicates are detected by hash and handled gracefully."""
    headers = auth(client, "alice", "pw-alice")
    body = b"Vendor shall retain records for seven years."

    first = upload(client, headers, "policy.txt", body)
    second = upload(client, headers, "policy-copy.txt", body)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["document_id"] == second.json()["document_id"]
    assert len(client.get("/api/documents", headers=headers).json()) == 1


def test_a_different_file_is_still_stored_separately(client):
    headers = auth(client, "alice", "pw-alice")
    upload(client, headers, "a.txt", b"Vendor shall retain records for seven years.")
    upload(client, headers, "b.txt", b"A completely different policy document here.")
    assert len(client.get("/api/documents", headers=headers).json()) == 2


def test_the_same_file_uploaded_by_two_tenants_is_not_shared(client):
    """Deduplication must never reach across a tenant boundary."""
    body = b"Vendor shall retain records for seven years."
    alice = auth(client, "alice", "pw-alice")
    bob = auth(client, "bob", "pw-bob")

    first = upload(client, alice, "policy.txt", body)
    second = upload(client, bob, "policy.txt", body)

    assert first.json()["document_id"] != second.json()["document_id"]
    assert len(client.get("/api/documents", headers=alice).json()) == 1
    assert len(client.get("/api/documents", headers=bob).json()) == 1


# ---------------------------------------------- health, readiness, db down


def test_ready_reports_ready_when_the_database_answers(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["database"] == "ok"


def test_ready_needs_no_token(client):
    assert client.get("/ready").status_code == 200


def break_the_database(client, monkeypatch):
    """Make every repository read raise as a dead connection would."""
    from sentineliq.components.api import routes

    def dead(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection closed"))

    monkeypatch.setattr(routes.repo, "list_investigations", dead)


def test_ready_is_503_when_the_database_is_unreachable(client, monkeypatch):
    break_the_database(client, monkeypatch)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_health_stays_200_when_the_database_is_unreachable(client, monkeypatch):
    """Liveness must not fail — restarting the API would not fix the database."""
    break_the_database(client, monkeypatch)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "unavailable"


def test_a_dead_database_mid_request_is_a_503_not_a_500(client, monkeypatch):
    from sentineliq.components.api import routes

    def dead(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection closed"))

    monkeypatch.setattr(routes.service, "list_investigations", dead)
    headers = auth(client, "alice", "pw-alice")

    response = client.get("/api/investigations", headers=headers)
    assert response.status_code == 503
    assert "retry" in response.json()["detail"]


def test_a_database_error_never_leaks_the_connection_string(client, monkeypatch):
    from sentineliq.components.api import routes

    def dead(*args, **kwargs):
        raise OperationalError(
            "SELECT 1", {}, Exception("postgresql://user:hunter2@db:5432/sentineliq")
        )

    monkeypatch.setattr(routes.service, "list_investigations", dead)
    headers = auth(client, "alice", "pw-alice")

    body = client.get("/api/investigations", headers=headers).text
    assert "hunter2" not in body
    assert "postgresql://" not in body


def test_run_does_not_hold_a_row_lock_while_the_background_task_works(client):
    """Regression: /run used to deadlock on PostgreSQL.

    The route marked the investigation `running` with a flush, which keeps the
    UPDATE's row lock for the rest of the request. FastAPI starts background
    tasks before it closes a `yield` dependency, so the task's own connection
    waited for a lock the request would not release until the task finished.

    In-memory SQLite cannot reproduce it at all — StaticPool hands both
    sessions the same connection, and a second engine would be a second, empty
    database — so this only runs against a real server. On SQLite the
    committed-before-the-task property is covered by
    `test_the_run_is_committed_as_running_before_the_work_starts`.
    """
    if os.environ["DATABASE_URL"].startswith("sqlite"):
        pytest.skip("needs a server database with real separate connections")

    headers = auth(client, "alice", "pw-alice")
    investigation_id = make_investigation(client, headers)
    seen = {}

    def observe(vendor_name):
        # A separate engine, so this cannot see the request's uncommitted work.
        engine = repo.build_engine(os.environ["DATABASE_URL"])
        factory = repo.session_factory(engine)
        with repo.session_scope(factory) as session:
            row = repo.get_investigation(session, "tenant-a", investigation_id)
            seen["visible_status"] = row.status if row else None
        engine.dispose()
        return dict(VERDICT, vendor=vendor_name)

    client.app.state.runner = observe
    client.post(f"/api/investigations/{investigation_id}/run", headers=headers)

    assert seen["visible_status"] == "running", (
        "the request must commit before the background task starts, "
        "or the two will deadlock on a server database"
    )


# --------------------------------------------------- read-your-own-writes


def test_a_new_investigation_is_readable_immediately(client):
    """The caller must be able to use the id it was just given.

    FastAPI closes a `yield` dependency after the response is sent, so a route
    that leaves the commit to the dependency hands back an id that is not
    visible yet. The dashboard creates a run and polls `/status` straight
    after, so this was a real 404.
    """
    headers = auth(client, "alice", "pw-alice")

    for _ in range(5):
        created = client.post(
            "/api/investigations",
            json={"vendor_name": "Meridian CloudWorks"},
            headers=headers,
        )
        investigation_id = created.json()["investigation_id"]
        status_response = client.get(
            f"/api/investigations/{investigation_id}/status", headers=headers
        )
        assert status_response.status_code == 200, status_response.text
        assert status_response.json()["status"] == "pending"


def test_an_uploaded_document_is_listed_immediately(client):
    headers = auth(client, "alice", "pw-alice")
    uploaded = client.post(
        "/api/documents",
        headers=headers,
        files={"upload": ("policy.txt", b"Vendor policy text. " * 20, "text/plain")},
        data={"vendor_name": "Meridian CloudWorks"},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["document_id"]

    listed = client.get("/api/documents", headers=headers).json()
    assert document_id in {d["document_id"] for d in listed}
