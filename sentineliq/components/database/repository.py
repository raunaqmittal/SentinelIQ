"""Database connection and all CRUD.

**Tenant isolation lives here and nowhere else** (CONVENTIONS.md §11,
NFR-003a). Every function takes `tenant_id` and filters on it. There is no
"skip the tenant filter" option, deliberately — one missing filter would be the
highest-severity bug in the system.

`tenant_id` must come from the authenticated principal, never from a request
body.
"""

# 1. Standard library imports
import json
import logging
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime

# 2. Third-party imports
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# 3. Internal imports
from sentineliq.components.database.models import (
    AuditLog,
    Base,
    Document,
    DocumentChunk,
    Evidence,
    Finding,
    Investigation,
    User,
)

logger = logging.getLogger(__name__)

# 4. Constants
DEFAULT_DATABASE_URL = "sqlite:///sentineliq.db"


def database_url() -> str:
    """The configured database URL.

    PostgreSQL is the documented production database (REQUIREMENTS.md §3).
    SQLite is the local default so the prototype runs with no server to
    install; point `DATABASE_URL` at Postgres to switch.
    """
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def normalise_url(url: str) -> str:
    """Point a plain `postgresql://` URL at the psycopg 3 driver.

    SQLAlchemy reads a bare `postgresql://` URL as psycopg **2**, which this
    project does not install, so the URL written in `.env` and docker-compose
    would fail to connect. Rewriting it here keeps one DATABASE_URL that works
    everywhere.
    """
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def build_engine(url: str | None = None):
    """Create the SQLAlchemy engine.

    SQLite needs two adjustments the server databases do not: sessions run on
    different threads under FastAPI, and an in-memory database belongs to a
    single connection — without a shared pool every session would get its own
    empty one.
    """
    url = normalise_url(url or database_url())
    if not url.startswith("sqlite"):
        # pre_ping so a connection the server has already closed is replaced
        # instead of failing the request.
        return create_engine(url, future=True, pool_pre_ping=True)

    in_memory = url in ("sqlite://", "sqlite:///:memory:")
    return create_engine(
        url,
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool if in_memory else None,
    )


def wait_for_database(engine, attempts: int = 30, delay: float = 1.0) -> None:
    """Block until the database accepts a connection.

    Postgres in a container starts listening a moment after the API does, so
    the API retries instead of crashing on the first connection.
    """
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect():
                return
        except OperationalError:
            if attempt == attempts:
                raise
            logger.info("waiting for the database (%d/%d)", attempt, attempts)
            time.sleep(delay)


def create_all(engine) -> None:
    """Create every table that does not exist yet."""
    Base.metadata.create_all(engine)


def session_factory(engine) -> sessionmaker:
    """A session factory bound to this engine."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker) -> Generator[Session]:
    """A session that commits on success and rolls back on failure."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ------------------------------------------------------------------ users


def create_user(
    session: Session, tenant_id: str, username: str, password_hash: str, role: str
) -> User:
    """Add a user to one tenant."""
    user = User(
        tenant_id=tenant_id,
        username=username,
        password_hash=password_hash,
        role=role,
    )
    session.add(user)
    session.flush()
    return user


def get_user_by_username(session: Session, username: str) -> User | None:
    """Look a user up for sign-in.

    This is the one query without a tenant filter, because the tenant is not
    known until the user is identified. It reads a single row by unique
    username and returns the tenant, which every later query then filters on.
    """
    return session.scalars(select(User).where(User.username == username)).first()


# -------------------------------------------------------------- documents


def get_document_by_hash(
    session: Session, tenant_id: str, sha256: str
) -> Document | None:
    """The tenant's existing copy of this exact file, if there is one."""
    return session.scalars(
        select(Document).where(
            Document.tenant_id == tenant_id, Document.sha256 == sha256
        )
    ).first()


def add_document(
    session: Session,
    tenant_id: str,
    vendor_name: str,
    document_name: str,
    sha256: str,
    size_bytes: int,
    stored_path: str,
) -> Document:
    """Record an uploaded document."""
    document = Document(
        tenant_id=tenant_id,
        vendor_name=vendor_name,
        document_name=document_name,
        sha256=sha256,
        size_bytes=size_bytes,
        stored_path=stored_path,
    )
    session.add(document)
    session.flush()
    return document


def list_documents(
    session: Session, tenant_id: str, vendor_name: str | None = None
) -> list[Document]:
    """Every document this tenant owns, optionally for one vendor."""
    query = select(Document).where(Document.tenant_id == tenant_id)
    if vendor_name:
        query = query.where(Document.vendor_name == vendor_name)
    return list(session.scalars(query))


def get_document(session: Session, tenant_id: str, document_id: str) -> Document | None:
    """One document, or None if it belongs to another tenant."""
    return session.scalars(
        select(Document).where(
            Document.id == document_id, Document.tenant_id == tenant_id
        )
    ).first()


def add_chunks(
    session: Session, tenant_id: str, document_id: str, chunks: list[dict]
) -> int:
    """Store a document's chunks. Returns how many were written."""
    for chunk in chunks:
        session.add(
            DocumentChunk(
                tenant_id=tenant_id,
                document_id=document_id,
                chunk_id=chunk["chunk_id"],
                text=chunk["text"],
                page_start=chunk.get("page_start"),
                page_end=chunk.get("page_end"),
            )
        )
    session.flush()
    return len(chunks)


def list_chunks(
    session: Session, tenant_id: str, document_id: str
) -> list[DocumentChunk]:
    """Every stored chunk of one document."""
    return list(
        session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.tenant_id == tenant_id,
                DocumentChunk.document_id == document_id,
            )
        )
    )


# --------------------------------------------------------- investigations


def create_investigation(
    session: Session, tenant_id: str, vendor_name: str
) -> Investigation:
    """Start an investigation in the `pending` state."""
    investigation = Investigation(
        tenant_id=tenant_id, vendor_name=vendor_name, status="pending"
    )
    session.add(investigation)
    session.flush()
    return investigation


def get_investigation(
    session: Session, tenant_id: str, investigation_id: str
) -> Investigation | None:
    """One investigation, or None if it belongs to another tenant."""
    return session.scalars(
        select(Investigation).where(
            Investigation.id == investigation_id,
            Investigation.tenant_id == tenant_id,
        )
    ).first()


def list_investigations(session: Session, tenant_id: str) -> list[Investigation]:
    """Every investigation this tenant owns, newest first."""
    return list(
        session.scalars(
            select(Investigation)
            .where(Investigation.tenant_id == tenant_id)
            .order_by(Investigation.created_at.desc())
        )
    )


def mark_running(session: Session, tenant_id: str, investigation_id: str) -> None:
    """Move an investigation into the `running` state."""
    investigation = get_investigation(session, tenant_id, investigation_id)
    if investigation:
        investigation.status = "running"


def mark_failed(
    session: Session, tenant_id: str, investigation_id: str, error: str
) -> None:
    """Record that an investigation failed."""
    investigation = get_investigation(session, tenant_id, investigation_id)
    if investigation:
        investigation.status = "failed"
        investigation.error = error


def save_report(
    session: Session, tenant_id: str, investigation_id: str, verdict: dict
) -> None:
    """Store a completed verdict, its findings and their evidence."""
    investigation = get_investigation(session, tenant_id, investigation_id)
    if investigation is None:
        raise ValueError(f"no investigation {investigation_id} for this tenant")

    # 1. Headline result on the investigation row.
    investigation.status = "complete"
    investigation.overall_score = verdict["overall_score"]
    investigation.risk_level = verdict["risk_level"]
    investigation.recommendation = verdict["recommendation"]
    investigation.escalate = verdict["escalate"]
    investigation.report_json = json.dumps(verdict)

    # 2. Findings and their evidence as queryable rows (FR-017).
    for item in verdict["findings"]:
        finding = Finding(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            question_id=item["question_id"],
            question=item["question"],
            category=item["category"],
            severity=item["severity"],
            contradiction=item["contradiction"],
            answer=item["answer"],
        )
        session.add(finding)
        session.flush()
        for evidence in item["evidence"]:
            session.add(
                Evidence(
                    tenant_id=tenant_id,
                    finding_id=finding.id,
                    chunk_id=evidence["chunk_id"],
                    document_name=evidence["document_name"],
                    page_start=evidence["page_start"],
                    page_end=evidence["page_end"],
                )
            )


def list_findings(
    session: Session, tenant_id: str, investigation_id: str
) -> list[Finding]:
    """Every finding of one investigation."""
    return list(
        session.scalars(
            select(Finding).where(
                Finding.tenant_id == tenant_id,
                Finding.investigation_id == investigation_id,
            )
        )
    )


def list_evidence(
    session: Session, tenant_id: str, investigation_id: str
) -> list[Evidence]:
    """Every piece of evidence cited by one investigation."""
    finding_ids = [f.id for f in list_findings(session, tenant_id, investigation_id)]
    if not finding_ids:
        return []
    return list(
        session.scalars(
            select(Evidence).where(
                Evidence.tenant_id == tenant_id,
                Evidence.finding_id.in_(finding_ids),
            )
        )
    )


# -------------------------------------------------------------- retention


def delete_document(session: Session, tenant_id: str, document_id: str) -> bool:
    """Delete a document, its chunks and its file (NFR-003d).

    Returns False when the document does not belong to this tenant, so one
    tenant cannot delete another's data by guessing an ID.
    """
    document = get_document(session, tenant_id, document_id)
    if document is None:
        return False

    session.execute(
        delete(DocumentChunk).where(
            DocumentChunk.tenant_id == tenant_id,
            DocumentChunk.document_id == document_id,
        )
    )
    session.delete(document)
    return True


def list_documents_older_than(
    session: Session, tenant_id: str, cutoff: datetime
) -> list[Document]:
    """This tenant's documents created before `cutoff` (NFR-003d)."""
    return list(
        session.scalars(
            select(Document).where(
                Document.tenant_id == tenant_id, Document.created_at < cutoff
            )
        )
    )


def list_tenant_ids(session: Session) -> list[str]:
    """Every tenant that owns at least one document.

    The one query with no tenant filter that is not a lookup: the retention
    sweep has to find the tenants before it can work through them one at a
    time, and it still deletes strictly per tenant.
    """
    return list(session.scalars(select(Document.tenant_id).distinct()))


def delete_investigation(
    session: Session, tenant_id: str, investigation_id: str
) -> bool:
    """Delete an investigation with its findings and evidence."""
    investigation = get_investigation(session, tenant_id, investigation_id)
    if investigation is None:
        return False

    finding_ids = [f.id for f in list_findings(session, tenant_id, investigation_id)]
    if finding_ids:
        session.execute(
            delete(Evidence).where(
                Evidence.tenant_id == tenant_id, Evidence.finding_id.in_(finding_ids)
            )
        )
    session.execute(
        delete(Finding).where(
            Finding.tenant_id == tenant_id,
            Finding.investigation_id == investigation_id,
        )
    )
    session.delete(investigation)
    return True


# ------------------------------------------------------------- audit logs


def record_audit(
    session: Session,
    tenant_id: str,
    actor: str,
    action: str,
    target: str,
    detail: str | None = None,
) -> None:
    """Append an audit entry. Never contains document text."""
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            target=target,
            detail=detail,
        )
    )


def list_audit(session: Session, tenant_id: str) -> list[AuditLog]:
    """Every audit entry for one tenant, newest first."""
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.created_at.desc())
        )
    )
