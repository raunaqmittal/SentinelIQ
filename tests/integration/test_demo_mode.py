"""Demo mode: an interviewer can use the API without an account.

What must stay true: demo mode is off unless it is switched on, it never grants
admin, it works in a real tenant of its own, and it changes nothing about
authenticated requests.
"""

# 1. Standard library imports
import os

# 2. Third-party imports
import pytest
from fastapi.testclient import TestClient

# 3. Internal imports
from sentineliq import service
from sentineliq.components.database import repository as repo

os.environ.setdefault("SECRET_KEY", "test-secret-key")

DEMO_TENANT = "demo-tenant"


@pytest.fixture
def client(monkeypatch, db_url):
    """An API client with demo mode OFF. Individual tests switch it on."""
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.delenv("SENTINELIQ_DEMO_MODE", raising=False)
    monkeypatch.delenv("SENTINELIQ_DEMO_TENANT", raising=False)

    from sentineliq.components.api.app import create_app

    app = create_app()
    app.state.runner = lambda vendor_name: {"vendor": vendor_name}

    with repo.session_scope(app.state.session_factory) as session:
        service.register_user(session, "tenant-a", "alice", "pw-alice", "analyst")
        service.register_user(session, "tenant-a", "admin-a", "pw-admin", "admin")

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setenv("SENTINELIQ_DEMO_MODE", "true")


def auth(client, username, password):
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# ------------------------------------------------------ demo mode off


def test_anonymous_requests_are_rejected_when_demo_mode_is_off(client):
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/investigations").status_code == 401
    assert (
        client.post("/api/investigations", json={"vendor_name": "X"}).status_code == 401
    )
    assert (
        client.post("/api/documents/any/questions", json={"question": "hi"}).status_code
        == 401
    )
    assert client.post("/api/documents/any/investigate").status_code == 401


def test_demo_mode_is_off_by_default(client):
    assert service.demo_enabled() is False
    assert client.get("/health").json()["demo_mode"] is False


@pytest.mark.parametrize("value", ["", "false", "no", "0", "maybe"])
def test_only_an_explicit_true_switches_demo_mode_on(monkeypatch, value):
    monkeypatch.setenv("SENTINELIQ_DEMO_MODE", value)
    assert service.demo_enabled() is False


def test_a_blank_demo_tenant_leaves_demo_mode_off(monkeypatch):
    """Rather than falling through to a principal with no tenant."""
    monkeypatch.setenv("SENTINELIQ_DEMO_MODE", "true")
    monkeypatch.setenv("SENTINELIQ_DEMO_TENANT", "   ")

    assert service.demo_enabled() is False


# ------------------------------------------------------- demo mode on


def test_an_anonymous_caller_can_use_the_api_in_demo_mode(client, demo_on):
    assert client.get("/api/documents").status_code == 200
    assert client.get("/api/investigations").json() == []
    assert client.get("/health").json()["demo_mode"] is True


def test_the_demo_principal_works_in_the_demo_tenant(client, demo_on):
    created = client.post("/api/investigations", json={"vendor_name": "Demo Vendor"})
    assert created.status_code == 201

    with repo.session_scope(client.app.state.session_factory) as session:
        row = repo.get_investigation(
            session, DEMO_TENANT, created.json()["investigation_id"]
        )
        assert row is not None
        assert row.tenant_id == DEMO_TENANT


def test_the_demo_tenant_can_be_configured(monkeypatch, demo_on):
    monkeypatch.setenv("SENTINELIQ_DEMO_TENANT", "interview-demo")

    assert service.demo_principal()["tenant_id"] == "interview-demo"


def test_the_demo_principal_is_never_an_admin(client, demo_on):
    assert service.demo_principal()["role"] == "analyst"

    created = client.post("/api/investigations", json={"vendor_name": "Demo Vendor"})
    investigation_id = created.json()["investigation_id"]

    assert client.delete(f"/api/investigations/{investigation_id}").status_code == 403
    assert client.delete("/api/documents/any-id").status_code == 403


def test_the_demo_principal_never_has_a_null_tenant(demo_on):
    principal = service.demo_principal()

    assert principal["tenant_id"]
    assert principal["tenant_id"] is not None


# ---------------------------------- demo mode does not weaken real auth


def test_a_real_token_still_works_and_keeps_its_own_tenant(client, demo_on):
    headers = auth(client, "alice", "pw-alice")

    created = client.post(
        "/api/investigations", json={"vendor_name": "Alice Vendor"}, headers=headers
    )
    assert created.status_code == 201

    with repo.session_scope(client.app.state.session_factory) as session:
        row = repo.get_investigation(
            session, "tenant-a", created.json()["investigation_id"]
        )
        assert row.tenant_id == "tenant-a"


def test_a_demo_caller_cannot_see_an_authenticated_tenants_data(client, demo_on):
    headers = auth(client, "alice", "pw-alice")
    client.post(
        "/api/investigations", json={"vendor_name": "Alice Vendor"}, headers=headers
    )

    assert client.get("/api/investigations").json() == []


def test_an_authenticated_tenant_cannot_see_demo_data(client, demo_on):
    client.post("/api/investigations", json={"vendor_name": "Demo Vendor"})

    headers = auth(client, "alice", "pw-alice")
    assert client.get("/api/investigations", headers=headers).json() == []


def test_a_bad_token_is_still_rejected_in_demo_mode(client, demo_on):
    """Presenting credentials means being authenticated by them, demo or not."""
    response = client.get(
        "/api/investigations", headers={"Authorization": "Bearer not-a-token"}
    )

    assert response.status_code == 401


def test_an_admin_still_has_admin_in_demo_mode(client, demo_on):
    headers = auth(client, "alice", "pw-alice")
    created = client.post(
        "/api/investigations", json={"vendor_name": "Alice Vendor"}, headers=headers
    )
    investigation_id = created.json()["investigation_id"]

    admin = auth(client, "admin-a", "pw-admin")
    assert (
        client.delete(
            f"/api/investigations/{investigation_id}", headers=admin
        ).status_code
        == 204
    )
