"""All API route handlers.

Every route except `/health`, `/ready` and `/api/auth/login` requires a
principal, and every one derives `tenant_id` from that principal rather than
from the request.

While demo mode is on, an anonymous caller is given a fixed demo principal
instead of being rejected. That decision is made once, in `app.get_principal`,
so no route here needs to know about it.
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
    run_document_investigation_in_background,
    run_investigation_in_background,
    run_vendor_group_investigation_in_background,
)
from sentineliq.components.database import repository as repo
from sentineliq.components.evaluation import rag_eval
from sentineliq.components.models.schemas import (
    AnswerResponse,
    DocumentResponse,
    EvidenceResponse,
    FindingResponse,
    HealthResponse,
    InvestigationCreate,
    InvestigationResponse,
    LoginRequest,
    QuestionRequest,
    StatusResponse,
    TokenResponse,
    VendorGroupResponse,
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
    return HealthResponse(
        status="ok",
        database=database,
        version="0.1.0",
        demo_mode=service.demo_enabled(),
    )


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
        demo_mode=service.demo_enabled(),
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
    vendor_name: str = Form(""),
    document_type: str = Form(""),
    upload: UploadFile = File(...),
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> DocumentResponse:
    """Upload a document, then chunk it so it can be retrieved from.

    The file is validated by content before it is kept. `vendor_name` is
    optional: investigating a single uploaded document does not need one, so it
    falls back to the filename. `document_type` is one of "contract",
    "financial", "security", or blank for untyped — it is what groups several
    documents into one company's evidence set for a vendor-group investigation.

    Raises:
        HTTPException: `document_type` was given and is not a recognised type.
    """
    from sentineliq.pipeline.investigation import DOCUMENT_TYPES

    if document_type and document_type not in DOCUMENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"document_type must be one of {DOCUMENT_TYPES}",
        )
    content = await upload.read()
    filename = upload.filename or "unnamed"
    stored = service.store_upload(
        session,
        principal["tenant_id"],
        principal["username"],
        vendor_name or Path(filename).stem,
        filename,
        content,
        document_type or None,
    )
    chunk_count = service.index_upload(
        session, principal["tenant_id"], stored["document_id"]
    )
    # Commit before responding, so the caller can immediately read the document
    # it was just handed the id of. See `create_investigation`.
    session.commit()
    return DocumentResponse(**stored, chunk_count=chunk_count)


@router.get(
    "/api/documents/{document_id}",
    response_model=DocumentResponse,
    tags=["documents"],
)
def get_document(
    document_id: str,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> DocumentResponse:
    """One uploaded document, with how many chunks it was split into."""
    return DocumentResponse(
        **service.get_document(session, principal["tenant_id"], document_id)
    )


@router.post(
    "/api/documents/{document_id}/investigate",
    response_model=StatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["documents"],
)
def investigate_document(
    document_id: str,
    request: Request,
    background: BackgroundTasks,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> StatusResponse:
    """Run a full investigation on one uploaded document (FR-022).

    Creates the investigation, accepts it, and does the work after the response
    — same as `/api/investigations/{id}/run`, and the result is read back
    through the same `/status`, `/report`, `/findings` and `/evidence`
    endpoints. The investigation is named after the document.
    """
    tenant_id = principal["tenant_id"]
    document = service.get_document(session, tenant_id, document_id)

    investigation_id = service.start_investigation(
        session, tenant_id, principal["username"], document["document_name"]
    )
    # Committed, not flushed — see `run_investigation` for why a held row lock
    # would deadlock the background task.
    repo.mark_running(session, tenant_id, investigation_id)
    session.commit()

    background.add_task(
        run_document_investigation_in_background,
        request.app,
        tenant_id,
        principal["username"],
        investigation_id,
        document_id,
    )
    return StatusResponse(**service.get_status(session, tenant_id, investigation_id))


@router.post(
    "/api/documents/{document_id}/questions",
    response_model=AnswerResponse,
    tags=["documents"],
)
def ask_document(
    document_id: str,
    payload: QuestionRequest,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> AnswerResponse:
    """Answer a question about one uploaded document.

    Retrieval is scoped to that document, so the answer can only cite it. When
    the evidence does not answer, the response says so rather than guessing.

    Raises:
        HTTPException: The question is empty.
    """
    try:
        result = service.answer_document_question(
            session, principal["tenant_id"], document_id, payload.question
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error
    return AnswerResponse(**result)


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
            document_type=document.document_type,
            size_bytes=document.size_bytes,
            sha256=document.sha256,
        )
        for document in repo.list_documents(session, principal["tenant_id"])
    ]


# ------------------------------------------------------- vendor groups
#
# A vendor group is every document one tenant has uploaded under one company
# name (Document.vendor_name). This is the PRIMARY investigation path: the
# original multi-document architecture, applied to whatever the caller
# actually uploaded — one, two or three of contract/financial/security.


@router.get(
    "/api/vendor-groups/{vendor_name}",
    response_model=VendorGroupResponse,
    tags=["vendor-groups"],
)
def get_vendor_group(
    vendor_name: str,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> VendorGroupResponse:
    """Every document uploaded for one company, grouped by type."""
    return VendorGroupResponse(
        **service.get_vendor_group(session, principal["tenant_id"], vendor_name)
    )


@router.post(
    "/api/vendor-groups/{vendor_name}/investigate",
    response_model=StatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["vendor-groups"],
)
def investigate_vendor_group(
    vendor_name: str,
    request: Request,
    background: BackgroundTasks,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> StatusResponse:
    """Run the full investigation over every document uploaded for one company.

    Same async shape as `/api/documents/{id}/investigate` (FR-022): accepted
    immediately, the real work happens after the response, the caller polls
    `/status`. The report is read back through the usual `/report`, `/findings`
    and `/evidence` endpoints.
    """
    tenant_id = principal["tenant_id"]
    service.get_vendor_group(session, tenant_id, vendor_name)  # 404 if empty

    investigation_id = service.start_investigation(
        session, tenant_id, principal["username"], vendor_name
    )
    repo.mark_running(session, tenant_id, investigation_id)
    session.commit()

    background.add_task(
        run_vendor_group_investigation_in_background,
        request.app,
        tenant_id,
        principal["username"],
        investigation_id,
        vendor_name,
    )
    return StatusResponse(**service.get_status(session, tenant_id, investigation_id))


@router.post(
    "/api/vendor-groups/{vendor_name}/questions",
    response_model=AnswerResponse,
    tags=["vendor-groups"],
)
def ask_vendor_group(
    vendor_name: str,
    payload: QuestionRequest,
    principal: dict = Depends(get_principal),
    session: Session = Depends(get_session),
) -> AnswerResponse:
    """Answer a question about every document uploaded for one company.

    Retrieval is scoped to this vendor's documents, so an answer can only cite
    them. When the evidence does not answer, the response says so.

    Raises:
        HTTPException: The question is empty.
    """
    try:
        result = service.answer_vendor_question(
            session, principal["tenant_id"], vendor_name, payload.question
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error
    return AnswerResponse(**result)


@router.get("/api/demo/meridian", tags=["demo"])
def preloaded_demo(principal: dict = Depends(get_principal)) -> dict:
    """The Precomputed Demo Investigation — Meridian CloudWorks, instant.

    No LLM call and no database row: this replays a real, previously-run
    investigation's findings and citations through the current deterministic
    engine (see `investigation.load_demo_report`). Not tenant-scoped — it is
    the same fixed showcase data for every caller, like `/api/evaluations`.
    """
    return service.load_preloaded_demo()


@router.post(
    "/api/demo/meridian/questions", response_model=AnswerResponse, tags=["demo"]
)
def ask_preloaded_demo(
    payload: QuestionRequest, principal: dict = Depends(get_principal)
) -> AnswerResponse:
    """Ask a question about the preloaded demo's real documents.

    Same grounded retrieval -> citation pipeline as `/api/vendor-groups/.../questions`,
    sourced from the fixed demo corpus rather than an upload.

    Raises:
        HTTPException: The question is empty.
    """
    try:
        result = service.answer_demo_question(payload.question)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error
    return AnswerResponse(**result)


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
