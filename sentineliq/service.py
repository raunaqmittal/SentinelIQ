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
import threading
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
#: Where uploaded documents are stored. Set SENTINELIQ_UPLOAD_DIR to an
#: absolute path on a mounted volume in a container — the default is
#: relative to the working directory and does not survive a restart.
UPLOAD_DIR = Path(os.environ.get("SENTINELIQ_UPLOAD_DIR", "data/uploads"))
BCRYPT_MAX_BYTES = 72  # bcrypt ignores anything past this, so cut it explicitly
#: The demo principal's role. `analyst`, never `admin`: demo callers must not
#: be able to delete anything.
DEMO_ROLE = "analyst"
DEFAULT_DEMO_TENANT = "demo-tenant"


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


def demo_tenant() -> str:
    """The tenant every demo caller works in.

    A fixed, real tenant — never None and never blank, so demo data goes
    through exactly the same repository filters as any other tenant's.
    """
    return os.environ.get("SENTINELIQ_DEMO_TENANT", DEFAULT_DEMO_TENANT).strip()


def demo_enabled() -> bool:
    """Whether an unauthenticated caller may use the API.

    Off unless `SENTINELIQ_DEMO_MODE` is explicitly true, so a deployment that
    says nothing keeps rejecting anonymous requests. A blank demo tenant also
    turns it off rather than falling through to a tenant-less principal.
    """
    wanted = os.environ.get("SENTINELIQ_DEMO_MODE", "").lower() in ("1", "true", "yes")
    return wanted and bool(demo_tenant())


def demo_principal() -> dict:
    """The principal an anonymous caller gets while demo mode is on.

    Same shape as a decoded token, so everything downstream — tenant filtering,
    RBAC, auditing — treats it identically. It holds the analyst role, so
    admin-only routes still refuse it.
    """
    return {
        "sub": "demo",
        "tenant_id": demo_tenant(),
        "username": "demo",
        "role": DEMO_ROLE,
    }


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
    document_type: str | None = None,
) -> dict:
    """Validate an uploaded file, save it, and record it.

    `document_type` is one of `investigation.DOCUMENT_TYPES`
    ("contract"/"financial"/"security") or None for an untyped upload.

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
            "document_type": existing.document_type,
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
        session,
        tenant_id,
        vendor_name,
        filename,
        digest,
        len(content),
        str(path),
        document_type,
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
        "document_type": document.document_type,
    }


def index_upload(session: Session, tenant_id: str, document_id: str) -> int:
    """Chunk a stored document so it can be retrieved from. Returns the count.

    This is what makes an upload a real retrieval source rather than a file on
    disk. The chunks are written to `document_chunks`, so retention deletes them
    with the document, and the same chunk boundaries are rebuilt for the FAISS
    and BM25 indexes at query time.

    Chunking an already-chunked document is skipped, which keeps a re-upload of
    the same file idempotent.

    Raises:
        NotFound: No such document for this tenant.
        DocumentLoadError: The file cannot be parsed or has no text.
    """
    document = repo.get_document(session, tenant_id, document_id)
    if document is None:
        raise NotFound(f"no document {document_id}")

    existing = repo.list_chunks(session, tenant_id, document_id)
    if existing:
        return len(existing)

    # Imported here: chunking pulls in the tokenizer, which is expensive and is
    # not needed by the rest of the service.
    from sentineliq.pipeline import documents as document_pipeline

    stored = Path(document.stored_path)
    try:
        chunks = document_pipeline.chunk_upload(
            stored, document_id, load_retrieval_config()
        )
    except Exception:
        # A document nothing can read is not a document. The file goes with the
        # row, which the caller's rollback removes.
        stored.unlink(missing_ok=True)
        raise
    repo.add_chunks(
        session, tenant_id, document_id, [chunk.model_dump() for chunk in chunks]
    )
    logger.info(
        "Indexed an upload",
        extra={
            "tenant_id": tenant_id,
            "document_id": document_id,
            "step": "index_upload",
            "chunks": len(chunks),
        },
    )
    return len(chunks)


def get_document(session: Session, tenant_id: str, document_id: str) -> dict:
    """One uploaded document with its chunk count.

    Raises:
        NotFound: No such document for this tenant.
    """
    document = repo.get_document(session, tenant_id, document_id)
    if document is None:
        raise NotFound(f"no document {document_id}")
    return {
        "document_id": document.id,
        "document_name": document.document_name,
        "size_bytes": document.size_bytes,
        "sha256": document.sha256,
        "document_type": document.document_type,
        "chunk_count": len(repo.list_chunks(session, tenant_id, document_id)),
    }


def get_vendor_group(session: Session, tenant_id: str, vendor_name: str) -> dict:
    """Every document uploaded for one company, grouped by type.

    Raises:
        NotFound: This tenant has no documents under this vendor name.
    """
    documents = repo.list_documents(session, tenant_id, vendor_name)
    if not documents:
        raise NotFound(f"no documents for {vendor_name!r}")
    return {
        "vendor_name": vendor_name,
        "documents": [
            {
                "document_id": d.id,
                "document_name": d.document_name,
                "document_type": d.document_type,
                "size_bytes": d.size_bytes,
                "chunk_count": len(repo.list_chunks(session, tenant_id, d.id)),
            }
            for d in documents
        ],
        "available_types": sorted(
            {d.document_type for d in documents if d.document_type}
        ),
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

    # The embedder, the reranker and the chunked corpus are the same for every
    # investigation, and loading them costs minutes and gigabytes. Build once on
    # the first run and reuse. The lock stops two concurrent runs each loading
    # their own copy. Nothing about retrieval itself changes: `run_investigation`
    # still calls `build_scoped_context`, so each vendor still gets indexes built
    # from its own chunks only (NFR-003a).
    cached: dict = {}
    lock = threading.Lock()

    def shared_context():
        from scripts.investigate import build_context

        with lock:
            if "context" not in cached:
                logger.info("Loading retrieval models and corpus (first run)")
                cached["context"] = build_context(config, load_app_config())
            return cached["context"]

    def runner(vendor_name: str) -> dict:
        from scripts.investigate import load_questions

        questions = load_questions(vendor_name, questions_path)
        if limit:
            questions = questions[:limit]
        dossier = pipeline.load_dossier(documents_dir / "dossiers.json", vendor_name)
        return pipeline.run_investigation(shared_context(), dossier, questions, rules)

    runner.shared_context = shared_context  # exposed so a test can assert reuse
    return runner


#: The one Precomputed Demo Investigation this project ships (README/PROGRESS).
DEMO_REPORT_PATH = Path("data/evaluation/demo_investigation.json")
DEMO_DOCUMENTS_DIR = Path("data/raw/documents")


def load_preloaded_demo() -> dict:
    """The precomputed Meridian CloudWorks investigation, ready to render.

    No LLM call, no database row — see `investigation.load_demo_report` for
    exactly what this replays versus what it recomputes.

    Raises:
        SentinelIQError: The demo's source documents are not present locally
            (`data/raw/` is not committed to git — see README "Data").
    """
    from sentineliq.exceptions import DocumentLoadError
    from sentineliq.pipeline import investigation

    try:
        return investigation.load_demo_report(DEMO_REPORT_PATH, DEMO_DOCUMENTS_DIR)
    except (FileNotFoundError, DocumentLoadError) as error:
        raise SentinelIQError(
            "the preloaded demo's source documents are not available in this "
            "environment (see README, section Data)"
        ) from error


def answer_demo_question(question: str, provider=None) -> dict:
    """Answer a question against the preloaded demo's real Meridian documents.

    Same retrieval -> rerank -> grounded LLM -> citations as every other Q&A
    call (`pipeline/qa.py`), just sourced from the frozen demo corpus instead
    of an upload — there is no DB row for these files, so this builds a
    `union_context` straight from disk. Not tenant-scoped: like the report
    itself, this is the same fixed showcase data for every caller.

    Raises:
        SentinelIQError: The demo's source documents are not available.
        ValueError: The question is empty.
    """
    from sentineliq.exceptions import DocumentLoadError
    from sentineliq.pipeline import documents as document_pipeline
    from sentineliq.pipeline import investigation, qa

    try:
        dossier = investigation.load_dossier(
            DEMO_DOCUMENTS_DIR / "dossiers.json", "Meridian CloudWorks"
        )
        wanted = investigation.dossier_document_ids(dossier)
        doc_specs = [
            (file.stem, file)
            for file in sorted(DEMO_DOCUMENTS_DIR.iterdir())
            if file.stem in wanted
        ]
        context = document_pipeline.union_context(
            "__demo__", doc_specs, load_retrieval_config()
        )
    except (FileNotFoundError, DocumentLoadError) as error:
        raise SentinelIQError(
            "the preloaded demo's source documents are not available in this "
            "environment (see README, section Data)"
        ) from error

    app_config = load_app_config()
    if provider is None:
        from sentineliq.components.llm.provider import GroqProvider

        groq_api_key()
        provider = GroqProvider(app_config.llm.model)

    return qa.ask(
        context,
        provider,
        question,
        temperature=app_config.llm.temperature,
        max_tokens=app_config.llm.max_tokens,
    )


def groq_api_key() -> str:
    """The LLM key, with a message a user can act on.

    Raises:
        SentinelIQError: `GROQ_API_KEY` is not set.
    """
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise SentinelIQError("GROQ_API_KEY is not set — see .env.example")
    return key


def build_document_runner(session: Session, tenant_id: str, document_id: str):
    """A `runner(name)` that investigates one uploaded document.

    Same shape as `build_pipeline_runner`'s runner, so `run_investigation`
    drives both without knowing which it has. The document is looked up here,
    while a session is open, because the run itself happens after the response.

    Raises:
        NotFound: No such document for this tenant.
    """
    document = repo.get_document(session, tenant_id, document_id)
    if document is None:
        raise NotFound(f"no document {document_id}")
    stored_path = Path(document.stored_path)
    document_name = document.document_name

    def runner(_name: str) -> dict:
        from sentineliq.config import load_document_questions
        from sentineliq.pipeline import documents as document_pipeline
        from sentineliq.pipeline import flow, investigation

        app_config = load_app_config()
        llm = flow.build_llm(
            app_config.llm.model,
            groq_api_key(),
            app_config.llm.temperature,
            app_config.llm.base_url,
        )
        context = document_pipeline.document_context(
            tenant_id, document_id, stored_path, load_retrieval_config(), llm=llm
        )
        return investigation.run_document_investigation(
            context,
            document_id,
            document_name,
            load_document_questions(),
            load_risk_rules(),
        )

    return runner


def build_vendor_runner(session: Session, tenant_id: str, vendor_name: str):
    """A `runner(name)` that investigates every document uploaded for one company.

    This is the PRIMARY investigation path: it builds one evidence context from
    every document this tenant has under `vendor_name` (contract, financial,
    security — whichever were actually uploaded) and runs the same
    specialist/Red-Team/scoring chain as `build_document_runner`, just over the
    combined evidence instead of one file.

    Raises:
        NotFound: This tenant has no documents under this vendor name.
    """
    documents = repo.list_documents(session, tenant_id, vendor_name)
    if not documents:
        raise NotFound(f"no documents for {vendor_name!r}")
    doc_specs = [(d.id, Path(d.stored_path)) for d in documents]
    available_types = {d.document_type for d in documents if d.document_type}

    def runner(_name: str) -> dict:
        from sentineliq.config import load_document_questions
        from sentineliq.pipeline import documents as document_pipeline
        from sentineliq.pipeline import flow, investigation

        app_config = load_app_config()
        llm = flow.build_llm(
            app_config.llm.model,
            groq_api_key(),
            app_config.llm.temperature,
            app_config.llm.base_url,
        )
        context = document_pipeline.union_context(
            tenant_id, doc_specs, load_retrieval_config(), llm=llm
        )
        return investigation.run_document_investigation(
            context,
            vendor_name,
            vendor_name,
            load_document_questions(),
            load_risk_rules(),
            available_types=available_types,
        )

    return runner


def answer_vendor_question(
    session: Session, tenant_id: str, vendor_name: str, question: str, provider=None
) -> dict:
    """Answer one question about every document uploaded for one company.

    Retrieval is scoped to this vendor's own documents, so an answer can only
    cite them. `provider` is injected so a test can answer without an LLM.

    Raises:
        NotFound: This tenant has no documents under this vendor name.
        ValueError: The question is empty.
    """
    documents = repo.list_documents(session, tenant_id, vendor_name)
    if not documents:
        raise NotFound(f"no documents for {vendor_name!r}")
    doc_specs = [(d.id, Path(d.stored_path)) for d in documents]

    from sentineliq.pipeline import documents as document_pipeline
    from sentineliq.pipeline import qa

    app_config = load_app_config()
    if provider is None:
        from sentineliq.components.llm.provider import GroqProvider

        groq_api_key()  # fail with a clear message before any model is loaded
        provider = GroqProvider(app_config.llm.model)

    context = document_pipeline.union_context(
        tenant_id, doc_specs, load_retrieval_config()
    )
    return qa.ask(
        context,
        provider,
        question,
        temperature=app_config.llm.temperature,
        max_tokens=app_config.llm.max_tokens,
    )


def answer_document_question(
    session: Session, tenant_id: str, document_id: str, question: str, provider=None
) -> dict:
    """Answer one question about one uploaded document.

    Retrieval is scoped to that document's own indexes, so an answer can only
    ever cite that document. `provider` is injected so a test can answer without
    calling an LLM.

    Raises:
        NotFound: No such document for this tenant.
        ValueError: The question is empty.
    """
    document = repo.get_document(session, tenant_id, document_id)
    if document is None:
        raise NotFound(f"no document {document_id}")

    from sentineliq.pipeline import documents as document_pipeline
    from sentineliq.pipeline import qa

    app_config = load_app_config()
    if provider is None:
        from sentineliq.components.llm.provider import GroqProvider

        groq_api_key()  # fail with a clear message before any model is loaded
        provider = GroqProvider(app_config.llm.model)

    context = document_pipeline.document_context(
        tenant_id,
        document_id,
        Path(document.stored_path),
        load_retrieval_config(),
    )
    return qa.ask(
        context,
        provider,
        question,
        temperature=app_config.llm.temperature,
        max_tokens=app_config.llm.max_tokens,
    )
