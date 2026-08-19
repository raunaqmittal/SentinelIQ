"""FastAPI application: startup, dependencies and error handling.

The database session and the pipeline runner are provided as dependencies so a
test can override them and never touch a real database or an LLM.
"""

# 1. Standard library imports
import logging
import os
from collections.abc import Generator
from pathlib import Path

# 2. Third-party imports
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

# 3. Internal imports
from sentineliq import service
from sentineliq.components.database import repository as repo
from sentineliq.exceptions import DocumentLoadError, SentinelIQError
from sentineliq.utils import configure_logging

logger = logging.getLogger(__name__)

# 4. Constants
DOCUMENTS_DIR = Path("data/raw/documents")
QUESTIONS_PATH = Path("data/evaluation/questions.json")

bearer = HTTPBearer(auto_error=False)


def tracing_enabled() -> bool:
    """Whether LangSmith tracing is switched on.

    Off unless `LANGCHAIN_TRACING_V2` is explicitly true (CONVENTIONS.md §16),
    and off regardless when no API key is set — enabling it without a key only
    produces connection errors on every LLM call.
    """
    wanted = os.environ.get("LANGCHAIN_TRACING_V2", "").lower() in ("1", "true", "yes")
    has_key = bool(os.environ.get("LANGCHAIN_API_KEY"))
    if wanted and not has_key:
        logger.warning("LANGCHAIN_TRACING_V2 is set but LANGCHAIN_API_KEY is not")
        return False
    return wanted


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    # CrewAI traces itself when these are set; the guard keeps tracing off by
    # default so a missing key cannot break every request.
    if tracing_enabled():
        os.environ.setdefault("LANGCHAIN_PROJECT", "sentineliq")
        logger.info("LangSmith tracing enabled", extra={"step": "startup"})
    else:
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
    app = FastAPI(
        title="SentinelIQ",
        version="0.1.0",
        description="Evidence-backed vendor due-diligence API.",
    )

    engine = repo.build_engine()
    repo.wait_for_database(engine)
    repo.create_all(engine)
    app.state.session_factory = repo.session_factory(engine)
    # Replaced in tests with a stub so no LLM is called.
    app.state.runner = None
    # Same, for uploaded-document investigations. None means "build the real
    # one per document"; a test sets a stub here instead.
    app.state.document_runner = None

    register_error_handlers(app)

    from sentineliq.components.api.routes import router

    app.include_router(router)
    return app


def register_error_handlers(app: FastAPI) -> None:
    """Turn internal exceptions into clean HTTP responses.

    The message is the exception's own text, which never contains document
    content or secrets; tracebacks are logged, never returned.
    """

    @app.exception_handler(service.AuthError)
    async def _auth(request: Request, error: service.AuthError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": str(error)}
        )

    @app.exception_handler(service.PermissionDenied)
    async def _denied(
        request: Request, error: service.PermissionDenied
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(error)}
        )

    @app.exception_handler(service.NotFound)
    async def _missing(request: Request, error: service.NotFound) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(error)}
        )

    @app.exception_handler(DocumentLoadError)
    async def _bad_file(request: Request, error: DocumentLoadError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(error)}
        )

    @app.exception_handler(SentinelIQError)
    async def _other(request: Request, error: SentinelIQError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(error)}
        )

    @app.exception_handler(OperationalError)
    async def _database_down(request: Request, error: OperationalError) -> JSONResponse:
        """A database that went away is 503, not 500 — and the caller may retry.

        The driver's own message can carry the connection string, so it is
        logged and never returned.
        """
        logger.error(
            "Database unavailable while serving a request",
            extra={"step": "request", "status": "database_unavailable"},
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "the database is unavailable, please retry"},
        )


def get_session(request: Request) -> Generator[Session]:
    """A database session for one request."""
    factory = request.app.state.session_factory
    with repo.session_scope(factory) as session:
        yield session


def get_runner(request: Request):
    """The investigation runner, built on first use."""
    return _resolve_runner(request.app)


def run_investigation_in_background(
    app: FastAPI, tenant_id: str, actor: str, investigation_id: str
) -> None:
    """Run one investigation after the HTTP response has been sent (FR-022).

    This runs in FastAPI's own worker thread, so it needs a session of its own —
    the request's session is closed by the time this starts.

    Nothing is raised out of here. There is no caller left to receive an
    exception, and `service.run_investigation` has already recorded the failure
    on the investigation row, which is what `/status` reports.
    """
    factory = app.state.session_factory
    try:
        runner = _resolve_runner(app)
        with repo.session_scope(factory) as session:
            service.run_investigation(
                session, tenant_id, actor, investigation_id, runner
            )
    except Exception:
        # Building the runner can fail before run_investigation gets a chance
        # to mark the row, so make sure the status never sticks at "running".
        logger.exception(
            "Background investigation failed",
            extra={
                "tenant_id": tenant_id,
                "investigation_id": investigation_id,
                "step": "run_investigation_in_background",
            },
        )
        _mark_failed_quietly(factory, tenant_id, investigation_id)


def run_document_investigation_in_background(
    app: FastAPI,
    tenant_id: str,
    actor: str,
    investigation_id: str,
    document_id: str,
) -> None:
    """Investigate one uploaded document after the response has been sent.

    The same shape as `run_investigation_in_background`; only the runner
    differs, because the subject is a document rather than a curated vendor.
    """
    factory = app.state.session_factory
    try:
        with repo.session_scope(factory) as session:
            runner = app.state.document_runner or service.build_document_runner(
                session, tenant_id, document_id
            )
            service.run_investigation(
                session, tenant_id, actor, investigation_id, runner
            )
    except Exception:
        logger.exception(
            "Background document investigation failed",
            extra={
                "tenant_id": tenant_id,
                "investigation_id": investigation_id,
                "document_id": document_id,
                "step": "run_document_investigation_in_background",
            },
        )
        _mark_failed_quietly(factory, tenant_id, investigation_id)


def run_vendor_group_investigation_in_background(
    app: FastAPI,
    tenant_id: str,
    actor: str,
    investigation_id: str,
    vendor_name: str,
) -> None:
    """Investigate one company's whole document set after the response has been sent.

    Same shape as `run_document_investigation_in_background`; only the runner
    differs, because the subject is every document under `vendor_name`, not one.
    """
    factory = app.state.session_factory
    try:
        with repo.session_scope(factory) as session:
            runner = app.state.document_runner or service.build_vendor_runner(
                session, tenant_id, vendor_name
            )
            service.run_investigation(
                session, tenant_id, actor, investigation_id, runner
            )
    except Exception:
        logger.exception(
            "Background vendor-group investigation failed",
            extra={
                "tenant_id": tenant_id,
                "investigation_id": investigation_id,
                "vendor_name": vendor_name,
                "step": "run_vendor_group_investigation_in_background",
            },
        )
        _mark_failed_quietly(factory, tenant_id, investigation_id)


def _mark_failed_quietly(factory, tenant_id: str, investigation_id: str) -> None:
    """Last resort: stop the status sticking at `running` after a crash.

    `service.run_investigation` records the real error itself, so this only
    steps in when the row is still `running` — otherwise it would overwrite a
    precise message with a vague one. Any second failure here is swallowed.
    """
    try:
        with repo.session_scope(factory) as session:
            investigation = repo.get_investigation(session, tenant_id, investigation_id)
            if investigation is not None and investigation.status == "running":
                repo.mark_failed(
                    session, tenant_id, investigation_id, "the run did not complete"
                )
    except Exception:
        logger.exception("Could not record the failure of %s", investigation_id)


def _resolve_runner(app: FastAPI):
    """The investigation runner, built on first use. Stubbed in tests."""
    if app.state.runner is None:
        app.state.runner = service.build_pipeline_runner(
            DOCUMENTS_DIR, QUESTIONS_PATH, limit=None
        )
    return app.state.runner


def get_principal(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
) -> dict:
    """The authenticated caller, read from the bearer token.

    `tenant_id` comes from here and from nowhere else — never from a request
    body or query parameter (CONVENTIONS.md §16b.2).

    Demo mode is handled **here and nowhere else**, so no route has to know
    about it. A caller who presents credentials is authenticated exactly as
    before; only a caller with none is given the demo principal, and only while
    `SENTINELIQ_DEMO_MODE` is on. With it off — the default — an anonymous
    request is still rejected.

    Raises:
        HTTPException: No credentials were supplied and demo mode is off.
    """
    if credentials is None:
        if service.demo_enabled():
            return service.demo_principal()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated"
        )
    return service.decode_token(credentials.credentials)
