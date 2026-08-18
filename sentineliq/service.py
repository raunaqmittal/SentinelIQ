"""Business logic between the API routes and the pipeline.

Routes do HTTP, this module does the work, the repository does the database.
Every function here takes an explicit `tenant_id` and passes it down — routes
derive it from the auth token and never from a request body (CONVENTIONS.md
§16b.2).
"""

# 1. Standard library imports
import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

# 2. Third-party imports
import bcrypt
from jose import JWTError, jwt
from sqlalchemy.orm import Session

# 3. Internal imports
from sentineliq.components.database import repository as repo
from sentineliq.components.ingestion.loader import validate_file
from sentineliq.config import load_app_config, load_retrieval_config, load_risk_rules
from sentineliq.exceptions import SentinelIQError

logger = logging.getLogger(__name__)

# 4. Constants
TOKEN_ALGORITHM = "HS256"
TOKEN_MINUTES = 30  # short-lived, per NFR-003
ROLES = ("analyst", "admin")
UPLOAD_DIR = Path("data/uploads")
BCRYPT_MAX_BYTES = 72  # bcrypt ignores anything past this, so cut it explicitly


class AuthError(SentinelIQError):
    """Sign-in failed or a token was missing, expired or malformed."""


class PermissionDenied(SentinelIQError):
    """The caller is authenticated but not allowed to do this."""


class NotFound(SentinelIQError):
    """The requested record does not exist for this tenant."""


# --------------------------------------------------------------- auth


def secret_key() -> str:
    """The token signing key.

    Raises:
        AuthError: `SECRET_KEY` is not set. There is deliberately no default —
            a hard-coded fallback would sign forgeable tokens in production.
    """
    key = os.environ.get("SECRET_KEY", "")
    if not key:
        raise AuthError("SECRET_KEY is not set — see .env.example")
    return key


def hash_password(password: str) -> str:
    """Hash a password for storage."""
    encoded = password.encode("utf-8")[:BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against its stored hash."""
    encoded = password.encode("utf-8")[:BCRYPT_MAX_BYTES]
    return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))


def create_token(user_id: str, tenant_id: str, username: str, role: str) -> str:
    """Issue a short-lived signed token carrying the tenant and role."""
    expires = datetime.now(UTC) + timedelta(minutes=TOKEN_MINUTES)
    claims = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "username": username,
        "role": role,
        "exp": expires,
    }
    return jwt.encode(claims, secret_key(), algorithm=TOKEN_ALGORITHM)


def decode_token(token: str) -> dict:
    """Read a token's claims.

    Raises:
        AuthError: The token is invalid, expired or tampered with.
    """
    try:
        return jwt.decode(token, secret_key(), algorithms=[TOKEN_ALGORITHM])
    except JWTError as error:
        raise AuthError("invalid or expired token") from error


def register_user(
    session: Session, tenant_id: str, username: str, password: str, role: str
) -> str:
    """Create a user and return its id.

    Raises:
        ValueError: The role is not one of the allowed roles.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}, expected one of {ROLES}")
    user = repo.create_user(session, tenant_id, username, hash_password(password), role)
    return user.id


def login(session: Session, username: str, password: str) -> str:
    """Check credentials and return a token.

    Raises:
        AuthError: The username is unknown or the password is wrong.
    """
    user = repo.get_user_by_username(session, username)
    if user is None or not verify_password(password, user.password_hash):
        # Same message either way, so the response cannot confirm a username.
        raise AuthError("incorrect username or password")
    return create_token(user.id, user.tenant_id, user.username, user.role)


def require_role(principal: dict, role: str) -> None:
    """Stop a caller who does not hold the required role.

    Raises:
        PermissionDenied: The principal's role is not `role`.
    """
    if principal.get("role") != role:
        raise PermissionDenied(f"this action requires the {role} role")


# ----------------------------------------------------------- documents


def store_upload(
    session: Session,
    tenant_id: str,
    actor: str,
    vendor_name: str,
    filename: str,
    content: bytes,
) -> dict:
    """Validate an uploaded file, save it, and record it.

    The size limit is checked **before** the bytes are written to disk, and the
    file type is checked by content afterwards (NFR-003, Context.md 26.H).

    Re-uploading a file this tenant already has returns the existing record
    instead of storing a second copy (FR-001). Making it idempotent is the
    graceful handling that requirement asks for: a retried upload after a
    dropped connection is the common case, and it should not create duplicates.

    Raises:
        DocumentLoadError: The file is too large or is not a supported type.
    """
    # 1. Size first, so an oversized upload never reaches the disk.
    limit = load_app_config().ingestion.max_file_bytes
    if len(content) > limit:
        raise SentinelIQError(
            f"{filename} is {len(content)} bytes, over the {limit} limit"
        )

    # 2. Already have this exact file? Return it rather than duplicating.
    digest = hashlib.sha256(content).hexdigest()
    existing = repo.get_document_by_hash(session, tenant_id, digest)
    if existing is not None:
        logger.info(
            "Duplicate upload ignored",
            extra={
                "tenant_id": tenant_id,
                "document_id": existing.id,
                "step": "store_upload",
                "status": "duplicate",
            },
        )
        return {
            "document_id": existing.id,
            "document_name": existing.document_name,
            "size_bytes": existing.size_bytes,
            "sha256": existing.sha256,
        }

    # 3. Write to a tenant-scoped directory, then validate the content.
    directory = UPLOAD_DIR / tenant_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_bytes(content)
    try:
        validate_file(path, max_bytes=limit)
    except Exception:
        path.unlink(missing_ok=True)  # never leave a rejected file behind
        raise

    # 4. Record it.
    document = repo.add_document(
        session, tenant_id, vendor_name, filename, digest, len(content), str(path)
    )
    repo.record_audit(session, tenant_id, actor, "upload_document", document.id)
    logger.info(
        "Stored upload",
        extra={
            "tenant_id": tenant_id,
            "document_id": document.id,
            "step": "store_upload",
            "status": "stored",
        },
    )
    return {
        "document_id": document.id,
        "document_name": document.document_name,
        "size_bytes": document.size_bytes,
        "sha256": document.sha256,
    }


def delete_document(
    session: Session, tenant_id: str, actor: str, document_id: str
) -> None:
    """Delete a document, its chunks and its file (NFR-003d).

    Raises:
        NotFound: No such document for this tenant.
    """
    document = repo.get_document(session, tenant_id, document_id)
    if document is None:
        raise NotFound(f"no document {document_id}")

    stored = Path(document.stored_path)
    if not repo.delete_document(session, tenant_id, document_id):
        raise NotFound(f"no document {document_id}")
    stored.unlink(missing_ok=True)

    repo.record_audit(session, tenant_id, actor, "delete_document", document_id)
    logger.info(
        "Deleted document", extra={"tenant_id": tenant_id, "document_id": document_id}
    )


def purge_expired_documents(
    session: Session, tenant_id: str, retention_days: int | None, actor: str = "system"
) -> list[str]:
    """Delete this tenant's documents past the retention period (NFR-003d).

    Returns the ids that were deleted. `retention_days` of None means keep for
    ever and nothing is touched; 0 deletes everything already stored.

    Deletion goes through `delete_document`, so chunks and the file on disk go
    with the row and every deletion is written to the audit log.
    """
    if retention_days is None:
        return []

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    expired = repo.list_documents_older_than(session, tenant_id, cutoff)

    deleted = []
    for document in expired:
        delete_document(session, tenant_id, actor, document.id)
        deleted.append(document.id)

    if deleted:
        logger.info(
            "Purged expired documents",
            extra={"tenant_id": tenant_id, "step": "retention", "count": len(deleted)},
        )
    return deleted


# ------------------------------------------------------- investigations


def list_investigations(session: Session, tenant_id: str) -> list[dict]:
    """Summaries of every investigation this tenant owns."""
    return [
        {
            "investigation_id": row.id,
            "vendor_name": row.vendor_name,
            "status": row.status,
            "overall_score": row.overall_score,
            "risk_level": row.risk_level,
            "recommendation": row.recommendation,
            "escalate": row.escalate,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in repo.list_investigations(session, tenant_id)
    ]


def get_report(session: Session, tenant_id: str, investigation_id: str) -> dict:
    """The stored report of one investigation.

    Raises:
        NotFound: No such investigation for this tenant, or it has no report.
    """
    investigation = repo.get_investigation(session, tenant_id, investigation_id)
    if investigation is None:
        raise NotFound(f"no investigation {investigation_id}")
    if not investigation.report_json:
        raise NotFound(f"investigation {investigation_id} has no report yet")
    return json.loads(investigation.report_json)


def get_status(session: Session, tenant_id: str, investigation_id: str) -> dict:
    """Where one investigation has got to.

    Raises:
        NotFound: No such investigation for this tenant.
    """
    investigation = repo.get_investigation(session, tenant_id, investigation_id)
    if investigation is None:
        raise NotFound(f"no investigation {investigation_id}")
    return {
        "investigation_id": investigation.id,
        "vendor_name": investigation.vendor_name,
        "status": investigation.status,
        "error": investigation.error,
    }


def delete_investigation(
    session: Session, tenant_id: str, actor: str, investigation_id: str
) -> None:
    """Delete an investigation with its findings and evidence.

    Raises:
        NotFound: No such investigation for this tenant.
    """
    if not repo.delete_investigation(session, tenant_id, investigation_id):
        raise NotFound(f"no investigation {investigation_id}")
    repo.record_audit(
        session, tenant_id, actor, "delete_investigation", investigation_id
    )


def run_investigation(
    session: Session,
    tenant_id: str,
    actor: str,
    investigation_id: str,
    runner,
) -> dict:
    """Run one investigation and store its report.

    `runner` is called as `runner(vendor_name)` and must return a verdict dict.
    It is injected rather than imported so a test can supply a stub and never
    call an LLM.

    Raises:
        NotFound: No such investigation for this tenant.
    """
    investigation = repo.get_investigation(session, tenant_id, investigation_id)
    if investigation is None:
        raise NotFound(f"no investigation {investigation_id}")

    # 1. Mark it running so a status poll shows progress.
    repo.mark_running(session, tenant_id, investigation_id)
    session.flush()
    started = time.perf_counter()

    # 2. Run the pipeline, recording a failure rather than losing it.
    try:
        verdict = runner(investigation.vendor_name)
    except Exception as error:
        repo.mark_failed(session, tenant_id, investigation_id, str(error)[:500])
        repo.record_audit(
            session, tenant_id, actor, "investigation_failed", investigation_id
        )
        # Commit before re-raising: the caller's session rolls back on an
        # exception, which would otherwise erase the record of the failure.
        session.commit()
        logger.error(
            "Investigation failed",
            extra={
                "tenant_id": tenant_id,
                "investigation_id": investigation_id,
                "step": "run_investigation",
                "status": "failed",
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "error": type(error).__name__,
            },
        )
        raise

    # 3. Store the report.
    repo.save_report(session, tenant_id, investigation_id, verdict)
    repo.record_audit(
        session, tenant_id, actor, "investigation_complete", investigation_id
    )
    logger.info(
        "Investigation complete",
        extra={
            "tenant_id": tenant_id,
            "investigation_id": investigation_id,
            "step": "run_investigation",
            "status": "complete",
            "duration_ms": round((time.perf_counter() - started) * 1000),
        },
    )
    return verdict


def start_investigation(
    session: Session, tenant_id: str, actor: str, vendor_name: str
) -> str:
    """Create a pending investigation and return its id."""
    investigation = repo.create_investigation(session, tenant_id, vendor_name)
    repo.record_audit(
        session, tenant_id, actor, "create_investigation", investigation.id
    )
    return investigation.id


# ------------------------------------------------- pipeline integration


def build_pipeline_runner(documents_dir: Path, questions_path: Path, limit: int | None):
    """Return a `runner(vendor_name)` that runs the real investigation pipeline.

    Imports are lazy because loading the retrieval models takes a minute and
    most API calls never need them.
    """
    from sentineliq.pipeline import investigation as pipeline

    config = load_retrieval_config()
    rules = load_risk_rules()

    def runner(vendor_name: str) -> dict:
        from scripts.investigate import build_context, load_questions

        questions = load_questions(vendor_name, questions_path)
        if limit:
            questions = questions[:limit]
        dossier = pipeline.load_dossier(documents_dir / "dossiers.json", vendor_name)
        context = build_context(config, load_app_config())
        return pipeline.run_investigation(context, dossier, questions, rules)

    return runner
