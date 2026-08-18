"""All Pydantic data models for SentinelIQ."""

# 1. Standard library imports
from datetime import date, datetime

# 2. Third-party imports
from pydantic import BaseModel, Field


class PageSpan(BaseModel):
    """Character range of one page inside a document's extracted text."""

    page_number: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class LoadedDocument(BaseModel):
    """A parsed source document, ready for chunking.

    `text` is the extracted text exactly as the parser produced it. It is not
    normalized or rewritten, because every downstream citation is expressed as
    a character offset into this string. Normalization happens at match time
    via `sentineliq.utils.normalize_for_matching`.
    """

    document_id: str
    document_name: str
    text: str
    pages: list[PageSpan]
    sha256: str
    loaded_at: datetime

    def page_for_offset(self, offset: int) -> int | None:
        """Page number containing `offset`, or None if it falls outside.

        The separator between two pages counts as part of the page it follows,
        so a chunk ending just past a page break still reports a page.
        """
        for page in self.pages:
            if page.start <= offset <= page.end:
                return page.page_number
        return None


class FinancialFact(BaseModel):
    """One reported financial number from a company's XBRL facts.

    A fact covers either a period (`period_start` set, e.g. yearly revenue) or
    a single date (`period_start` is None, e.g. cash on hand at year end).
    """

    cik: str
    entity_name: str
    concept: str
    label: str
    unit: str
    value: float
    period_start: date | None = None
    period_end: date
    fiscal_year: int
    form: str


class Chunk(BaseModel):
    """A retrievable piece of a document, traceable back to its source."""

    chunk_id: str
    document_id: str
    document_name: str
    text: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    page_start: int | None = None
    page_end: int | None = None


# ---------------------------------------------------------------- API


class LoginRequest(BaseModel):
    """Credentials posted to the login endpoint."""

    username: str
    password: str


class TokenResponse(BaseModel):
    """A short-lived bearer token."""

    access_token: str
    token_type: str = "bearer"


class DocumentResponse(BaseModel):
    """An uploaded document, as returned to the client."""

    document_id: str
    document_name: str
    size_bytes: int
    sha256: str


class InvestigationCreate(BaseModel):
    """Request to start an investigation.

    There is no `tenant_id` field on purpose — the tenant comes from the
    authenticated principal, never from the request body.
    """

    vendor_name: str


class InvestigationResponse(BaseModel):
    """An investigation's headline state."""

    investigation_id: str
    vendor_name: str
    status: str
    overall_score: float | None = None
    risk_level: str | None = None
    recommendation: str | None = None
    escalate: bool | None = None
    created_at: str | None = None


class StatusResponse(BaseModel):
    """Where an investigation has got to."""

    investigation_id: str
    vendor_name: str
    status: str
    error: str | None = None


class EvidenceResponse(BaseModel):
    """One citation, traceable to its document and page."""

    chunk_id: str
    document_name: str
    page_start: int | None = None
    page_end: int | None = None


class FindingResponse(BaseModel):
    """One answered question with its evidence."""

    question_id: str
    question: str
    category: str
    severity: str | None = None
    contradiction: bool = False
    answer: str
    evidence: list[EvidenceResponse] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Liveness and readiness of the service."""

    status: str
    database: str
    version: str
