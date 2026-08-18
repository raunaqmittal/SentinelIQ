"""SQLAlchemy ORM models.

Only the tables the application actually uses are defined here. Context.md §30
lists more as suggestions; a table without a writer is dead schema.

Every tenant-scoped table carries `tenant_id` and every repository query filters
by it (CONVENTIONS.md §11, NFR-003a).
"""

# 1. Standard library imports
import uuid
from datetime import datetime

# 2. Third-party imports
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    """A fresh UUID4 primary key."""
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Base class for every ORM model."""


class User(Base):
    """An account that can sign in and run investigations."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Document(Base):
    """An uploaded source document belonging to one tenant."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    vendor_name: Mapped[str] = mapped_column(String, index=True)
    document_name: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    stored_path: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentChunk(Base):
    """One retrievable chunk of a document.

    Stored so that retention can delete a document's chunks as well as the
    document itself (NFR-003d).
    """

    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    document_id: Mapped[str] = mapped_column(
        String, ForeignKey("documents.id"), index=True
    )
    chunk_id: Mapped[str] = mapped_column(String, index=True)
    text: Mapped[str] = mapped_column(Text)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Investigation(Base):
    """One due-diligence run for one vendor."""

    __tablename__ = "investigations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    vendor_name: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    overall_score: Mapped[float | None] = mapped_column(nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String, nullable=True)
    recommendation: Mapped[str | None] = mapped_column(String, nullable=True)
    escalate: Mapped[bool | None] = mapped_column(nullable=True)
    report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Finding(Base):
    """One answered question inside an investigation."""

    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    investigation_id: Mapped[str] = mapped_column(
        String, ForeignKey("investigations.id"), index=True
    )
    question_id: Mapped[str] = mapped_column(String)
    question: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String)
    severity: Mapped[str | None] = mapped_column(String, nullable=True)
    contradiction: Mapped[bool] = mapped_column(default=False)
    answer: Mapped[str] = mapped_column(Text)


class Evidence(Base):
    """One citation supporting a finding (FR-017)."""

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    finding_id: Mapped[str] = mapped_column(
        String, ForeignKey("findings.id"), index=True
    )
    chunk_id: Mapped[str] = mapped_column(String)
    document_name: Mapped[str] = mapped_column(String)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AuditLog(Base):
    """Who did what, including every deletion (NFR-003d, Context.md 26.E)."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    actor: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    target: Mapped[str] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
