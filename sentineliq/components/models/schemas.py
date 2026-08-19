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
    """An uploaded document, as returned to the client.

    `chunk_count` is how many retrievable pieces the document was split into.
    It is None where the count was not looked up, which is the case for the
    list endpoint. `document_type` is "contract" | "financial" | "security" |
    None (untyped upload).
    """

    document_id: str
    document_name: str
    size_bytes: int
    sha256: str
    document_type: str | None = None
    chunk_count: int | None = None


class VendorDocumentSummary(BaseModel):
    """One document inside a vendor group's document list."""

    document_id: str
    document_name: str
    document_type: str | None = None
    size_bytes: int
    chunk_count: int


class VendorGroupResponse(BaseModel):
    """Every document uploaded for one company, grouped by type.

    `available_types` is the set of document types actually present — what the
    "Documents analyzed" checklist on the frontend is built from.
    """

    vendor_name: str
    documents: list[VendorDocumentSummary]
    available_types: list[str]


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


class QuestionRequest(BaseModel):
    """A free-form question about one uploaded document.

    The document is named in the URL and the tenant comes from the principal,
    so neither can be chosen in the body.
    """

    question: str


class CitationResponse(BaseModel):
    """One retrieved chunk, with the text a reader can check the answer against."""

    chunk_id: str
    document_name: str
    page_start: int | None = None
    page_end: int | None = None
    text: str


class AnswerResponse(BaseModel):
    """A grounded answer about one document.

    There is deliberately no confidence score: any number here would be
    unmeasured. `citations` is the honest signal — an answer stands on the
    chunks listed there, and `abstained` is True when the document did not
    provide enough evidence.
    """

    answer: str
    abstained: bool
    citations: list[CitationResponse] = Field(default_factory=list)
    retrieved: list[CitationResponse] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Liveness and readiness of the service.

    `demo_mode` tells the frontend whether it may call the API without signing
    in. It reports configuration, never a credential.
    """

    status: str
    database: str
    version: str
    demo_mode: bool = False
