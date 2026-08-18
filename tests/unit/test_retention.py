"""NFR-003d: documents must not be kept for ever, and deletions are audited.

The retention period is configuration, not code — these tests pass the period
in explicitly rather than depending on whatever app.yaml currently says.
"""

# 1. Standard library imports
from datetime import UTC, datetime, timedelta

# 2. Third-party imports
import pytest

# 3. Internal imports
from sentineliq import service
from sentineliq.components.database import repository as repo
from sentineliq.config import load_app_config

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


@pytest.fixture
def session(engine):
    repo.create_all(engine)
    factory = repo.session_factory(engine)
    with repo.session_scope(factory) as db:
        yield db


def add_document(session, tenant_id, name, path, age_days=0):
    """Store a document row, optionally backdated."""
    document = repo.add_document(
        session, tenant_id, "Vendor", name, f"sha-{name}-{tenant_id}", 10, str(path)
    )
    if age_days:
        document.created_at = datetime.now(UTC) - timedelta(days=age_days)
    session.flush()
    return document


# ------------------------------------------------------------ the policy


def test_retention_is_configurable():
    """The period is read from app.yaml, not hard-coded."""
    assert hasattr(load_app_config().retention, "document_days")


def test_no_configured_period_means_nothing_is_deleted(session, tmp_path):
    path = tmp_path / "keep.txt"
    path.write_text("x")
    document = add_document(session, TENANT_A, "keep.txt", path, age_days=999)

    assert service.purge_expired_documents(session, TENANT_A, None) == []
    assert repo.get_document(session, TENANT_A, document.id) is not None
    assert path.exists()


# ----------------------------------------------------------- the sweep


def test_a_document_past_the_period_is_deleted(session, tmp_path):
    path = tmp_path / "old.txt"
    path.write_text("x")
    document = add_document(session, TENANT_A, "old.txt", path, age_days=40)

    deleted = service.purge_expired_documents(session, TENANT_A, 30)

    assert deleted == [document.id]
    assert repo.get_document(session, TENANT_A, document.id) is None


def test_a_document_inside_the_period_is_kept(session, tmp_path):
    path = tmp_path / "recent.txt"
    path.write_text("x")
    document = add_document(session, TENANT_A, "recent.txt", path, age_days=5)

    assert service.purge_expired_documents(session, TENANT_A, 30) == []
    assert repo.get_document(session, TENANT_A, document.id) is not None


def test_zero_days_deletes_everything_stored(session, tmp_path):
    path = tmp_path / "now.txt"
    path.write_text("x")
    document = add_document(session, TENANT_A, "now.txt", path, age_days=0)
    # Backdate by a second so "now" is unambiguously before the cutoff.
    document.created_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()

    assert service.purge_expired_documents(session, TENANT_A, 0) == [document.id]


def test_the_file_on_disk_goes_with_the_row(session, tmp_path):
    """Uploaded files must not accumulate after their record is gone."""
    path = tmp_path / "old.txt"
    path.write_text("confidential")
    add_document(session, TENANT_A, "old.txt", path, age_days=40)

    service.purge_expired_documents(session, TENANT_A, 30)
    assert not path.exists()


def test_the_chunks_go_too(session, tmp_path):
    path = tmp_path / "old.txt"
    path.write_text("x")
    document = add_document(session, TENANT_A, "old.txt", path, age_days=40)
    repo.add_chunks(
        session, TENANT_A, document.id, [{"chunk_id": "old_0000", "text": "secret"}]
    )

    service.purge_expired_documents(session, TENANT_A, 30)
    assert repo.list_chunks(session, TENANT_A, document.id) == []


# ------------------------------------------------- isolation and auditing


def test_the_sweep_never_touches_another_tenant(session, tmp_path):
    mine = tmp_path / "mine.txt"
    theirs = tmp_path / "theirs.txt"
    mine.write_text("x")
    theirs.write_text("x")
    add_document(session, TENANT_A, "mine.txt", mine, age_days=40)
    other = add_document(session, TENANT_B, "theirs.txt", theirs, age_days=40)

    service.purge_expired_documents(session, TENANT_A, 30)

    assert repo.get_document(session, TENANT_B, other.id) is not None
    assert theirs.exists()


def test_every_purge_is_written_to_the_audit_log(session, tmp_path):
    path = tmp_path / "old.txt"
    path.write_text("x")
    document = add_document(session, TENANT_A, "old.txt", path, age_days=40)

    service.purge_expired_documents(session, TENANT_A, 30, actor="retention-job")

    entries = repo.list_audit(session, TENANT_A)
    deletions = [e for e in entries if e.action == "delete_document"]
    assert [e.target for e in deletions] == [document.id]
    assert deletions[0].actor == "retention-job"


def test_the_audit_entry_never_holds_document_text(session, tmp_path):
    path = tmp_path / "old.txt"
    path.write_text("HIGHLY CONFIDENTIAL CLAUSE")
    add_document(session, TENANT_A, "old.txt", path, age_days=40)

    service.purge_expired_documents(session, TENANT_A, 30)

    for entry in repo.list_audit(session, TENANT_A):
        assert "CONFIDENTIAL" not in (entry.detail or "")
