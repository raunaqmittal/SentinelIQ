"""All API route handlers.

Every route except `/health` and `/api/auth/login` requires a bearer token, and
every one derives `tenant_id` from that token rather than from the request.
"""

# 1. Standard library imports
import logging
from pathlib import Path

# 2. Third-party imports
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

# 3. Internal imports
from sentineliq import service
from sentineliq.components.api.app import (
    get_principal,
    get_session,
    run_investigation_in_background,
)
from sentineliq.components.database import repository as repo
from sentineliq.components.evaluation import rag_eval
from sentineliq.components.models.schemas import (
    DocumentResponse,
    EvidenceResponse,
    FindingResponse,
    HealthResponse,
    InvestigationCreate,
    InvestigationResponse,
    LoginRequest,
    StatusResponse,
    TokenResponse,
)

logger = logging.getLogger(__name__)

# 4. Constants
STAGE8_RESULTS = Path("data/evaluation/stage8_baseline_results.json")
# Valid judge run, kept beside the results file rather than merged into it
# so the audited artifact is never rewritten (ADR-020).
STAGE8_REJUDGE = Path("data/evaluation/stage8_rejudge.jsonl")

router = APIRouter()


# ------------------------------------------------------------- health


def database_reachable(session: Session) -> bool:
    """Whether the database answers a trivial tenant-scoped query."""
    try:
        repo.list_investigations(session, "__healthcheck__")
        return True
    except Exception:  # noqa: BLE001 - a health check must report, never raise
        logger.error("Health check could not reach the database")
        return False


@router.get("/health", response_model=HealthResponse, tags=["health"])
def health(session: Session = Depends(get_session)) -> HealthResponse:
    """Liveness: the process is up and serving.

    Always 200 while the process can answer, and reports the database
    separately. Restarting the API would not fix an unreachable database, so
    this must not fail the container's health check — use `/ready` for that.
    """
    database = "ok" if database_reachable(session) else "unavailable"
    return HealthResponse(status="ok", database=database, version="0.1.0")


@router.get("/ready", response_model=HealthResponse, tags=["health"])
def ready(
    response: Response, session: Session = Depends(get_session)
) -> HealthResponse:
    """Readiness: the service can actually serve requests.

    503 when the database is unreachable, because every endpoint past
    `/api/auth/login` needs it. A load balancer should stop sending traffic
    here; a process supervisor should not restart the process.
    """
    reachable = database_reachable(session)
    if not reachable:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if reachable else "not ready",
        database="ok" if reachable else "unavailable",
        version="0.1.0",
    )


# --------------------------------------------------------------- auth


@router.post("/api/auth/login", response_model=TokenResponse, tags=["auth"])
def login(
    payload: LoginRequest, session: Session = Depends(get_session)
) -> TokenResponse:
    """Exchange a username and password for a short-lived token."""
    token = service.login(session, payload.username, payload.password)
    return TokenResponse(access_token=token)


# ---------------------------------------------------------- documents


@router.post(
    "/api/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["documents"],
)
async def upload_document(
    vendor_name: str = Form(...),
    upload: UploadFile = File(...),
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> DocumentResponse:
    """Upload a document. The file is validated by content before it is kept."""
    content = await upload.read()
    stored = service.store_upload(
        session,
        principal["tenant_id"],
        principal["username"],
        vendor_name,
        upload.filename or "unnamed",
        content,
    )
    # Commit before responding, so the caller can immediately read the document
    # it was just handed the id of. See `create_investigation`.
    session.commit()
    return DocumentResponse(**stored)


@router.get("/api/documents", response_model=list[DocumentResponse], tags=["documents"])
def list_documents(
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> list[DocumentResponse]:
    """Every document belonging to the caller's tenant."""
    return [
        DocumentResponse(
            document_id=document.id,
            document_name=document.document_name,
            size_bytes=document.size_bytes,
            sha256=document.sha256,
        )
        for document in repo.list_documents(session, principal["tenant_id"])
    ]


@router.delete(
    "/api/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["documents"],
)
def delete_document(
    document_id: str,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> None:
    """Delete a document, its chunks and its file. Admin only."""
    service.require_role(principal, "admin")
    service.delete_document(
        session, principal["tenant_id"], principal["username"], document_id
    )


# ----------------------------------------------------- investigations


@router.post(
    "/api/investigations",
    response_model=InvestigationResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["investigations"],
)
def create_investigation(
    payload: InvestigationCreate,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> InvestigationResponse:
    """Create a pending investigation for a vendor."""
    investigation_id = service.start_investigation(
        session, principal["tenant_id"], principal["username"], payload.vendor_name
    )
    # Commit before responding. FastAPI closes a `yield` dependency *after* the
    # response is sent, so without this the caller can hold the new id and get
    # a 404 from /status by asking before the commit lands — which is exactly
    # what the dashboard does after creating a run.
    session.commit()
    return InvestigationResponse(
        investigation_id=investigation_id,
        vendor_name=payload.vendor_name,
        status="pending",
    )


@router.get(
    "/api/investigations",
    response_model=list[InvestigationResponse],
    tags=["investigations"],
)
def list_investigations(
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> list[InvestigationResponse]:
    """Every investigation belonging to the caller's tenant."""
    return [
        InvestigationResponse(**row)
        for row in service.list_investigations(session, principal["tenant_id"])
    ]


@router.post(
    "/api/investigations/{investigation_id}/run",
    response_model=StatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["investigations"],
)
def run_investigation(
    investigation_id: str,
    request: Request,
    background: BackgroundTasks,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> StatusResponse:
    """Start an investigation and return immediately (FR-022).

    A run takes minutes, so the work happens after the response is sent and the
    caller polls `/status`. 202 with status `running` means it was accepted, not
    that it finished.

    Raises:
        HTTPException: This investigation is already running.
    """
    tenant_id = principal["tenant_id"]
    current = service.get_status(session, tenant_id, investigation_id)
    if current["status"] == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this investigation is already running",
        )

    # Marked running here, not in the background task, so a poll that arrives
    # before the task starts does not still read "pending".
    #
    # COMMIT, not flush. A flush leaves the UPDATE's row lock held for the rest
    # of the request, and FastAPI starts background tasks before it closes a
    # `yield` dependency — so the background task's own connection would wait
    # for a lock this request will not release until the task finishes.
    # That deadlocks on PostgreSQL and every other server database. SQLite
    # hides it, because StaticPool gives both sessions the same connection.
    repo.mark_running(session, tenant_id, investigation_id)
    session.commit()

    background.add_task(
        run_investigation_in_background,
        request.app,
        tenant_id,
        principal["username"],
        investigation_id,
    )
    return StatusResponse(**service.get_status(session, tenant_id, investigation_id))


@router.get(
    "/api/investigations/{investigation_id}/status",
    response_model=StatusResponse,
    tags=["investigations"],
)
def investigation_status(
    investigation_id: str,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> StatusResponse:
    """Where one investigation has got to."""
    return StatusResponse(
        **service.get_status(session, principal["tenant_id"], investigation_id)
    )


@router.get("/api/investigations/{investigation_id}/report", tags=["investigations"])
def investigation_report(
    investigation_id: str,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> dict:
    """The full stored report, including the "Why?" explanation."""
    return service.get_report(session, principal["tenant_id"], investigation_id)


@router.get(
    "/api/investigations/{investigation_id}/findings",
    response_model=list[FindingResponse],
    tags=["investigations"],
)
def investigation_findings(
    investigation_id: str,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> list[FindingResponse]:
    """Every finding of one investigation, with its evidence."""
    report = service.get_report(session, principal["tenant_id"], investigation_id)
    return [FindingResponse(**finding) for finding in report["findings"]]


@router.get(
    "/api/investigations/{investigation_id}/evidence",
    response_model=list[EvidenceResponse],
    tags=["investigations"],
)
def investigation_evidence(
    investigation_id: str,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> list[EvidenceResponse]:
    """Every citation of one investigation, traceable to document and page."""
    return [
        EvidenceResponse(
            chunk_id=item.chunk_id,
            document_name=item.document_name,
            page_start=item.page_start,
            page_end=item.page_end,
        )
        for item in repo.list_evidence(
            session, principal["tenant_id"], investigation_id
        )
    ]


@router.delete(
    "/api/investigations/{investigation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["investigations"],
)
def delete_investigation(
    investigation_id: str,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> None:
    """Delete an investigation with its findings and evidence. Admin only."""
    service.require_role(principal, "admin")
    service.delete_investigation(
        session, principal["tenant_id"], principal["username"], investigation_id
    )


# --------------------------------------------------------- evaluations


@router.get("/api/evaluations", tags=["evaluations"])
def evaluations(principal: dict = Depends(get_principal)) -> dict:
    """The stored reliability numbers for the dashboard.

    Read-only and identical for every tenant: these describe the system's own
    measured quality, not any tenant's data.
    """
    if not STAGE8_RESULTS.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no evaluation results have been recorded yet",
        )
    return rag_eval.load_reliability_summary(STAGE8_RESULTS, STAGE8_REJUDGE)
