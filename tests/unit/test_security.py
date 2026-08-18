"""Unit tests for file validation (NFR-003) and log redaction (NFR-006)."""

import ast
import io
import json
import logging
from pathlib import Path

import pymupdf
import pytest

from sentineliq.components.ingestion.loader import (
    SIGNATURES,
    load_pdf,
    load_txt,
    validate_file,
)
from sentineliq.components.models.schemas import InvestigationCreate
from sentineliq.exceptions import DocumentLoadError
from sentineliq.utils import (
    CONFIDENTIAL_FIELDS,
    REDACTED,
    STRUCTURED_FIELDS,
    RedactingFilter,
    StructuredFormatter,
    secret_values,
)

# ------------------------------------------------- file validation (NFR-003)


def write_pdf(path, body="Vendor shall retain records for seven years."):
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), body)
    document.save(path)
    document.close()
    return path


def test_a_real_pdf_passes(tmp_path):
    validate_file(write_pdf(tmp_path / "contract.pdf"))


def test_a_real_text_file_passes(tmp_path):
    path = tmp_path / "policy.txt"
    path.write_text("Vendor shall retain records.", encoding="utf-8")
    validate_file(path)


def test_a_missing_file_is_rejected(tmp_path):
    with pytest.raises(DocumentLoadError, match="No such document"):
        validate_file(tmp_path / "absent.pdf")


def test_an_unsupported_extension_is_rejected(tmp_path):
    path = tmp_path / "payload.exe"
    path.write_bytes(b"MZ\x90\x00")
    with pytest.raises(DocumentLoadError, match="Unsupported file type"):
        validate_file(path)


def test_a_file_over_the_size_limit_is_rejected_before_parsing(tmp_path):
    path = tmp_path / "huge.txt"
    path.write_text("x" * 5000, encoding="utf-8")
    with pytest.raises(DocumentLoadError, match="over the 1000 limit"):
        validate_file(path, max_bytes=1000)


def test_an_executable_renamed_to_pdf_is_rejected(tmp_path):
    # The whole point of checking content: the extension is attacker-chosen.
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00")  # a Windows executable
    with pytest.raises(DocumentLoadError, match="not a real .pdf file"):
        validate_file(path)


def test_a_pdf_renamed_to_docx_is_rejected(tmp_path):
    path = tmp_path / "agreement.docx"
    path.write_bytes(b"%PDF-1.7\n")
    with pytest.raises(DocumentLoadError, match="not a real .docx file"):
        validate_file(path)


def test_a_binary_file_renamed_to_txt_is_rejected(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"before\x00after")
    with pytest.raises(DocumentLoadError, match="binary, not text"):
        validate_file(path)


def test_a_non_utf8_text_file_is_rejected(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"\xff\xfe invalid")
    with pytest.raises(DocumentLoadError, match="not valid UTF-8"):
        validate_file(path)


def test_the_loaders_validate_before_parsing(tmp_path):
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"MZ\x90\x00")
    with pytest.raises(DocumentLoadError, match="not a real .pdf file"):
        load_pdf(fake_pdf)

    fake_txt = tmp_path / "fake.txt"
    fake_txt.write_bytes(b"binary\x00here")
    with pytest.raises(DocumentLoadError, match="binary, not text"):
        load_txt(fake_txt)


def test_every_supported_type_has_a_content_check():
    # .txt is validated by decoding instead of by signature.
    assert set(SIGNATURES) == {".pdf", ".docx"}


# --------------------------------------------------- log redaction (NFR-006)


@pytest.fixture
def captured():
    """A handler that keeps formatted records, with redaction switched on."""
    records = []

    class Collector(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Collector()
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("test_redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, records


def test_an_api_key_never_reaches_the_log(captured, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_supersecret_value_123")
    logger, records = captured
    logger.info("calling provider with gsk_supersecret_value_123")
    assert "gsk_supersecret_value_123" not in records[0]
    assert REDACTED in records[0]


def test_a_secret_in_a_log_argument_is_masked_too(captured, monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "tok_abc123")
    logger, records = captured
    logger.info("token was %s", "tok_abc123")
    assert "tok_abc123" not in records[0]


def test_document_text_passed_as_a_log_field_is_replaced(captured):
    logger, records = captured
    handler = logger.handlers[0]
    handler.setFormatter(logging.Formatter("%(message)s | %(text)s"))
    logger.info("chunk stored", extra={"text": "CONFIDENTIAL contract clause"})
    assert "CONFIDENTIAL" not in records[0]
    assert REDACTED in records[0]


def test_metadata_fields_are_left_alone(captured):
    logger, records = captured
    handler = logger.handlers[0]
    handler.setFormatter(
        logging.Formatter("%(message)s | %(investigation_id)s %(duration_ms)s")
    )
    logger.info(
        "step complete", extra={"investigation_id": "abc123", "duration_ms": 42}
    )
    assert "abc123" in records[0] and "42" in records[0]


def test_secret_values_ignores_non_secret_variables(monkeypatch):
    monkeypatch.setenv("SENTINELIQ_MODE", "development")
    monkeypatch.setenv("MY_API_KEY", "secret-one")
    values = secret_values()
    assert "secret-one" in values
    assert "development" not in values


def test_secrets_are_masked_longest_first(monkeypatch):
    # A short secret that is a substring of a longer one must not leave the
    # longer one partly readable.
    monkeypatch.setenv("SHORT_KEY", "abc")
    monkeypatch.setenv("LONG_KEY", "abc-def-ghi")
    values = secret_values()
    assert values.index("abc-def-ghi") < values.index("abc")


def test_a_numeric_log_argument_survives_redaction(captured):
    """Regression: the filter used to stringify every argument.

    That broke any call site using a numeric format specifier — httpx logs
    `"%s %s \\"%s %d %s\\""` with an int status, and `%d` against a string
    raises, turning a log line into a logging error.
    """
    logger, records = captured
    logger.info("request %s returned %d in %d ms", "/health", 200, 12)
    assert records[0] == "request /health returned 200 in 12 ms"


def test_a_non_string_message_object_is_not_broken(captured):
    logger, records = captured
    logger.info({"event": "started"})
    assert "started" in records[0]


# ------------------------------------------- structured logging (NFR-006)


def test_structured_output_carries_the_nfr006_metadata_fields():
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(StructuredFormatter())
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("test_structured")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info(
        "retrieval complete",
        extra={
            "investigation_id": "inv-1",
            "tenant_id": "tenant-a",
            "agent": "Compliance Analyst",
            "step": "retrieve",
            "duration_ms": 803,
            "tokens": 4184,
            "status": "ok",
        },
    )
    written = json.loads(handler.stream.getvalue())
    assert written["message"] == "retrieval complete"
    assert written["investigation_id"] == "inv-1"
    assert written["tenant_id"] == "tenant-a"
    assert written["duration_ms"] == 803
    assert written["level"] == "INFO"


def test_structured_output_drops_fields_that_are_not_metadata():
    """A stray `extra` carrying document text must not reach the output."""
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(StructuredFormatter())
    logger = logging.getLogger("test_structured_drop")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("stored", extra={"tenant_id": "t", "text": "CONFIDENTIAL clause"})
    written = json.loads(handler.stream.getvalue())
    assert "CONFIDENTIAL" not in handler.stream.getvalue()
    assert "text" not in written
    assert written["tenant_id"] == "t"


def test_every_documented_structured_field_is_supported():
    # NFR-006 requires these exactly; a missing one would be a silent gap.
    required = {
        "investigation_id",
        "tenant_id",
        "agent",
        "step",
        "duration_ms",
        "tokens",
        "status",
        "error",
    }
    assert required <= set(STRUCTURED_FIELDS)


def test_only_identifiers_are_added_beyond_the_required_fields():
    """Extras must be ids, never anything that could carry document text."""
    extras = set(STRUCTURED_FIELDS) - {
        "investigation_id",
        "tenant_id",
        "agent",
        "step",
        "duration_ms",
        "tokens",
        "status",
        "error",
    }
    assert extras == {"document_id", "chunk_id"}
    assert not extras & CONFIDENTIAL_FIELDS


# ------------------------------------------- structural guards (NFR-003a)


REPOSITORY = Path("sentineliq/components/database/repository.py")
ROUTES = Path("sentineliq/components/api/routes.py")

#: The one query that cannot filter by tenant: it is how the tenant is
#: discovered in the first place. Documented in the repository module.
#: The only repository functions allowed to run without a `tenant_id` filter.
#: Each is a deliberate exception, not an oversight:
#:  - get_user_by_username: sign-in, where the tenant is not yet known. It
#:    returns the tenant that every later query is then filtered by.
#:  - list_tenant_ids: the retention sweep has to discover the tenants before
#:    it can work through them one at a time. It returns only tenant ids —
#:    never a document, a finding or any other tenant-owned row.
TENANT_FILTER_EXEMPT = {"get_user_by_username", "list_tenant_ids"}

TENANT_MODELS = (
    "Document",
    "DocumentChunk",
    "Investigation",
    "Finding",
    "Evidence",
    "AuditLog",
    "User",
)


def public_functions(path: Path):
    """Every public top-level function in a module, with its source."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            yield node, ast.get_source_segment(source, node) or ""


def test_every_repository_query_is_tenant_filtered():
    """A repository function that forgets `tenant_id` is a cross-tenant leak.

    This is a structural guard, not a behaviour test: it fails when a *new*
    function is added without the filter, which is exactly when the mistake
    would otherwise slip through.
    """
    offenders = []
    for node, body in public_functions(REPOSITORY):
        if node.name in TENANT_FILTER_EXEMPT:
            continue
        if not any(model in body for model in TENANT_MODELS):
            continue  # does not touch tenant data
        takes_tenant = "tenant_id" in [a.arg for a in node.args.args]
        uses_tenant = "tenant_id" in body.split(")", 1)[-1]
        if not (takes_tenant and uses_tenant):
            offenders.append(node.name)
    assert not offenders, f"repository functions missing a tenant filter: {offenders}"


def test_every_route_except_health_and_login_requires_authentication():
    """Tenant comes from the token, so an unauthenticated route has no tenant.

    `health` and `ready` are the two probes: a load balancer and a container
    runtime must be able to call them without credentials, and neither reads
    any tenant's data — only whether the database answers at all.
    """
    public_routes = {"health", "ready", "login"}
    offenders = []
    for node, body in public_functions(ROUTES):
        is_route = any(
            isinstance(d, ast.Call) and "router" in ast.dump(d)
            for d in node.decorator_list
        )
        if not is_route or node.name in public_routes:
            continue
        if "get_principal" not in body:
            offenders.append(node.name)
    assert not offenders, f"routes without authentication: {offenders}"


def test_every_route_reads_tenant_id_only_from_the_principal():
    """`tenant_id` must never be client-supplied (CONVENTIONS.md §16b.2).

    Checked on the syntax tree rather than the text, so prose in a docstring
    cannot trip it and a real `payload.tenant_id` cannot hide from it.
    """
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))

    # Every `x["tenant_id"]` must read from `principal`.
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if node.slice.value == "tenant_id":
                assert (
                    isinstance(node.value, ast.Name) and node.value.id == "principal"
                ), "tenant_id read from something other than the principal"

    # And no `something.tenant_id` attribute access at all — that would be a
    # request model carrying a client-supplied tenant.
    attributes = [
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    ]
    assert "tenant_id" not in attributes


def test_the_investigation_request_schema_has_no_tenant_field():
    """A client must not be able to name the tenant it writes into."""
    assert "tenant_id" not in InvestigationCreate.model_fields
