"""Repository tests, with tenant isolation as the main subject (NFR-003a).

Every one of these runs against a real in-memory SQLite database, so the
filtering being tested is the filtering the application uses.
"""

import pytest

from sentineliq.components.database import repository as repo

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


@pytest.fixture
def session(engine):
    repo.create_all(engine)
    factory = repo.session_factory(engine)
    with repo.session_scope(factory) as db:
        yield db


def make_verdict(question_id="Q001"):
    """The minimum verdict shape `save_report` needs."""
    return {
        "overall_score": 42.0,
        "risk_level": "medium",
        "recommendation": "APPROVE_WITH_CONDITIONS",
        "escalate": True,
        "findings": [
            {
                "question_id": question_id,
                "question": "SOC 2 valid?",
                "category": "compliance",
                "severity": "HIGH",
                "contradiction": True,
                "answer": "Certificate expired.",
                "evidence": [
                    {
                        "chunk_id": "meridian_sla_0001",
                        "document_name": "meridian_sla.txt",
                        "page_start": None,
                        "page_end": None,
                    }
                ],
            }
        ],
    }


# --------------------------------------------------------- connection URL


def test_a_plain_postgres_url_uses_the_psycopg_3_driver():
    """SQLAlchemy defaults `postgresql://` to psycopg 2, which is not installed."""
    assert (
        repo.normalise_url("postgresql://user:pw@db:5432/sentineliq")
        == "postgresql+psycopg://user:pw@db:5432/sentineliq"
    )


def test_an_explicit_driver_and_sqlite_urls_are_left_alone():
    assert repo.normalise_url("postgresql+psycopg://u:p@db/x") == (
        "postgresql+psycopg://u:p@db/x"
    )
    assert repo.normalise_url("sqlite://") == "sqlite://"


# ------------------------------------------------------------- documents


def test_a_tenant_only_lists_its_own_documents(session):
    repo.add_document(session, TENANT_A, "Vendor A", "a.pdf", "sha-a", 10, "/tmp/a")
    repo.add_document(session, TENANT_B, "Vendor B", "b.pdf", "sha-b", 10, "/tmp/b")

    names_a = [d.document_name for d in repo.list_documents(session, TENANT_A)]
    assert names_a == ["a.pdf"]


def test_tenant_b_cannot_fetch_tenant_as_document_by_id(session):
    document = repo.add_document(
        session, TENANT_A, "Vendor A", "a.pdf", "sha-a", 10, "/tmp/a"
    )
    assert repo.get_document(session, TENANT_A, document.id) is not None
    assert repo.get_document(session, TENANT_B, document.id) is None


def test_chunks_are_scoped_to_their_tenant(session):
    document = repo.add_document(
        session, TENANT_A, "Vendor A", "a.pdf", "sha-a", 10, "/tmp/a"
    )
    repo.add_chunks(
        session, TENANT_A, document.id, [{"chunk_id": "a_0001", "text": "secret"}]
    )
    assert len(repo.list_chunks(session, TENANT_A, document.id)) == 1
    assert repo.list_chunks(session, TENANT_B, document.id) == []


# -------------------------------------------------------- investigations


def test_a_tenant_only_lists_its_own_investigations(session):
    repo.create_investigation(session, TENANT_A, "Vendor A")
    repo.create_investigation(session, TENANT_B, "Vendor B")

    vendors = [i.vendor_name for i in repo.list_investigations(session, TENANT_A)]
    assert vendors == ["Vendor A"]


def test_tenant_b_cannot_fetch_tenant_as_investigation(session):
    investigation = repo.create_investigation(session, TENANT_A, "Vendor A")
    assert repo.get_investigation(session, TENANT_A, investigation.id) is not None
    assert repo.get_investigation(session, TENANT_B, investigation.id) is None


def test_saving_a_report_stores_findings_and_evidence(session):
    investigation = repo.create_investigation(session, TENANT_A, "Vendor A")
    repo.save_report(session, TENANT_A, investigation.id, make_verdict())

    stored = repo.get_investigation(session, TENANT_A, investigation.id)
    assert stored.status == "complete"
    assert stored.recommendation == "APPROVE_WITH_CONDITIONS"
    assert stored.escalate is True

    findings = repo.list_findings(session, TENANT_A, investigation.id)
    assert len(findings) == 1
    assert findings[0].contradiction is True
    assert len(repo.list_evidence(session, TENANT_A, investigation.id)) == 1


def test_tenant_b_cannot_read_tenant_as_findings_or_evidence(session):
    investigation = repo.create_investigation(session, TENANT_A, "Vendor A")
    repo.save_report(session, TENANT_A, investigation.id, make_verdict())

    assert repo.list_findings(session, TENANT_B, investigation.id) == []
    assert repo.list_evidence(session, TENANT_B, investigation.id) == []


def test_saving_a_report_for_another_tenant_is_refused(session):
    investigation = repo.create_investigation(session, TENANT_A, "Vendor A")
    with pytest.raises(ValueError, match="no investigation"):
        repo.save_report(session, TENANT_B, investigation.id, make_verdict())


def test_status_transitions_are_tenant_scoped(session):
    investigation = repo.create_investigation(session, TENANT_A, "Vendor A")
    repo.mark_running(session, TENANT_B, investigation.id)  # wrong tenant, no-op
    stored = repo.get_investigation(session, TENANT_A, investigation.id)
    assert stored.status == "pending"

    repo.mark_running(session, TENANT_A, investigation.id)
    stored = repo.get_investigation(session, TENANT_A, investigation.id)
    assert stored.status == "running"

    repo.mark_failed(session, TENANT_A, investigation.id, "boom")
    stored = repo.get_investigation(session, TENANT_A, investigation.id)
    assert stored.status == "failed" and stored.error == "boom"


# ------------------------------------------------------------- retention


def test_deleting_a_document_removes_its_chunks(session):
    document = repo.add_document(
        session, TENANT_A, "Vendor A", "a.pdf", "sha-a", 10, "/tmp/a"
    )
    repo.add_chunks(
        session, TENANT_A, document.id, [{"chunk_id": "a_0001", "text": "secret"}]
    )

    assert repo.delete_document(session, TENANT_A, document.id) is True
    assert repo.get_document(session, TENANT_A, document.id) is None
    assert repo.list_chunks(session, TENANT_A, document.id) == []


def test_a_tenant_cannot_delete_another_tenants_document(session):
    document = repo.add_document(
        session, TENANT_A, "Vendor A", "a.pdf", "sha-a", 10, "/tmp/a"
    )
    assert repo.delete_document(session, TENANT_B, document.id) is False
    assert repo.get_document(session, TENANT_A, document.id) is not None


def test_deleting_an_investigation_removes_findings_and_evidence(session):
    investigation = repo.create_investigation(session, TENANT_A, "Vendor A")
    repo.save_report(session, TENANT_A, investigation.id, make_verdict())

    assert repo.delete_investigation(session, TENANT_A, investigation.id) is True
    assert repo.get_investigation(session, TENANT_A, investigation.id) is None
    assert repo.list_findings(session, TENANT_A, investigation.id) == []
    assert repo.list_evidence(session, TENANT_A, investigation.id) == []


def test_a_tenant_cannot_delete_another_tenants_investigation(session):
    investigation = repo.create_investigation(session, TENANT_A, "Vendor A")
    assert repo.delete_investigation(session, TENANT_B, investigation.id) is False
    assert repo.get_investigation(session, TENANT_A, investigation.id) is not None


# ------------------------------------------------------------ audit + users


def test_audit_entries_are_tenant_scoped(session):
    repo.record_audit(session, TENANT_A, "alice", "delete_document", "doc-1")
    repo.record_audit(session, TENANT_B, "bob", "delete_document", "doc-2")

    entries = repo.list_audit(session, TENANT_A)
    assert len(entries) == 1
    assert entries[0].actor == "alice"


def test_a_user_is_found_by_username_and_carries_its_tenant(session):
    repo.create_user(session, TENANT_A, "alice", "hashed", "analyst")
    user = repo.get_user_by_username(session, "alice")
    assert user.tenant_id == TENANT_A
    assert user.role == "analyst"
    assert repo.get_user_by_username(session, "nobody") is None


# ------------------------------------------------------------- retention


def test_documents_older_than_the_cutoff_are_found(session):
    from datetime import UTC, datetime, timedelta

    document = repo.add_document(
        session, TENANT_A, "Vendor A", "old.pdf", "sha-old", 10, "/tmp/old"
    )
    document.created_at = datetime.now(UTC) - timedelta(days=40)
    session.flush()

    cutoff = datetime.now(UTC) - timedelta(days=30)
    found = repo.list_documents_older_than(session, TENANT_A, cutoff)
    assert [d.id for d in found] == [document.id]


def test_a_recent_document_is_not_expired(session):
    from datetime import UTC, datetime, timedelta

    repo.add_document(session, TENANT_A, "Vendor A", "new.pdf", "sha-new", 10, "/tmp/n")
    session.flush()
    cutoff = datetime.now(UTC) - timedelta(days=30)
    assert repo.list_documents_older_than(session, TENANT_A, cutoff) == []


def test_the_retention_sweep_never_reaches_another_tenant(session):
    from datetime import UTC, datetime, timedelta

    theirs = repo.add_document(
        session, TENANT_B, "Vendor B", "old.pdf", "sha-b", 10, "/tmp/b"
    )
    theirs.created_at = datetime.now(UTC) - timedelta(days=40)
    session.flush()

    cutoff = datetime.now(UTC) - timedelta(days=30)
    assert repo.list_documents_older_than(session, TENANT_A, cutoff) == []


def test_list_tenant_ids_reports_each_tenant_once(session):
    repo.add_document(session, TENANT_A, "V", "a.pdf", "sha-a1", 10, "/tmp/a1")
    repo.add_document(session, TENANT_A, "V", "b.pdf", "sha-a2", 10, "/tmp/a2")
    repo.add_document(session, TENANT_B, "V", "c.pdf", "sha-b1", 10, "/tmp/b1")
    session.flush()
    assert sorted(repo.list_tenant_ids(session)) == [TENANT_A, TENANT_B]
