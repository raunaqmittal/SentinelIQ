"""Uploaded documents: chunking, per-document scoping and the context cache.

The embedder and the cross-encoder are stubbed — this file is about which
chunks an index is built from, not about the models themselves.
"""

# 1. Standard library imports
from pathlib import Path

# 2. Third-party imports
import pytest

# 3. Internal imports
from sentineliq.config import load_retrieval_config
from sentineliq.exceptions import DocumentLoadError
from sentineliq.pipeline import documents

CONTRACT = """MASTER SERVICES AGREEMENT

1. Term. This agreement runs for three years from the effective date.

2. Termination. Either party may terminate for convenience on ninety days
written notice. The customer may terminate immediately for material breach.

3. Liability. The supplier's total liability is capped at the fees paid in the
twelve months before the claim. Neither party is liable for indirect loss.

4. Confidentiality. Each party shall protect the other's confidential
information using no less than reasonable care, and shall notify the other
within seventy-two hours of any breach of security affecting that information.
"""


class FakeTokenizer:
    """Counts words, so no model has to be downloaded to test chunking."""

    def encode(self, text, add_special_tokens=False):
        return text.split()


@pytest.fixture(autouse=True)
def stub_tokenizer(monkeypatch):
    """Use the word-count tokenizer and start from an empty cache."""
    monkeypatch.setitem(documents._models, "tokenizer", FakeTokenizer())
    documents.clear_cache()
    yield
    documents.clear_cache()


@pytest.fixture
def config():
    return load_retrieval_config()


@pytest.fixture
def contract(tmp_path):
    path = tmp_path / "contract.txt"
    path.write_text(CONTRACT, encoding="utf-8")
    return path


def test_an_uploaded_document_is_chunked_under_its_own_id(contract, config):
    chunks = documents.chunk_upload(contract, "doc-abc", config)

    assert chunks
    assert all(chunk.document_id == "doc-abc" for chunk in chunks)
    assert all(chunk.chunk_id.startswith("doc-abc_") for chunk in chunks)
    assert all(chunk.document_name == "contract.txt" for chunk in chunks)


def test_chunking_uses_the_frozen_settings(contract, config, monkeypatch):
    """Uploads must be chunked the same way the benchmark corpus was."""
    seen = {}

    real = documents.chunk_document

    def spy(document, count_tokens, *, chunk_size, chunk_overlap):
        seen["size"] = chunk_size
        seen["overlap"] = chunk_overlap
        return real(
            document, count_tokens, chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )

    monkeypatch.setattr(documents, "chunk_document", spy)
    documents.chunk_upload(contract, "doc-abc", config)

    assert seen["size"] == config.chunking.chunk_size
    assert seen["overlap"] == config.chunking.chunk_overlap


def test_a_file_type_with_no_loader_is_rejected(tmp_path, config):
    path = tmp_path / "notes.md"
    path.write_text("# hello", encoding="utf-8")

    with pytest.raises(DocumentLoadError):
        documents.chunk_upload(path, "doc-abc", config)


def test_an_unreadable_file_is_rejected(tmp_path, config):
    """A .txt that is really binary must not become a retrieval source."""
    path = tmp_path / "sneaky.txt"
    path.write_bytes(b"%PDF-\x00\x00binary")

    with pytest.raises(DocumentLoadError):
        documents.chunk_upload(path, "doc-abc", config)


# ------------------------------------------------------ scoped context


@pytest.fixture
def stub_models(monkeypatch):
    """No real embedder or reranker; record what the indexes were built from."""
    built = []

    monkeypatch.setattr(
        documents,
        "load_models",
        lambda config: {"embedder": "embedder", "cross_encoder": "cross_encoder"},
    )

    def build_indexes(chunks, embedder):
        built.append([chunk.chunk_id for chunk in chunks])
        return ("faiss", "bm25")

    monkeypatch.setattr(documents, "build_indexes", build_indexes)
    return built


def test_the_context_indexes_only_that_document(contract, config, stub_models):
    context = documents.document_context(
        "tenant-a", "doc-abc", contract, config, llm="llm"
    )

    assert {chunk.document_id for chunk in context.chunks} == {"doc-abc"}
    assert stub_models[0] == [chunk.chunk_id for chunk in context.chunks]
    assert context.llm == "llm"
    assert context.config is config


def test_a_second_question_reuses_the_cached_index(contract, config, stub_models):
    documents.document_context("tenant-a", "doc-abc", contract, config)
    documents.document_context("tenant-a", "doc-abc", contract, config)

    assert len(stub_models) == 1, "the document was embedded twice"


def test_the_cache_is_keyed_by_tenant_as_well_as_document(
    contract, config, stub_models
):
    """Two tenants asking about the same id must not share an index."""
    documents.document_context("tenant-a", "doc-abc", contract, config)
    documents.document_context("tenant-b", "doc-abc", contract, config)

    assert len(stub_models) == 2
    assert set(documents._indexes) == {
        ("tenant-a", ("doc-abc",)),
        ("tenant-b", ("doc-abc",)),
    }


def test_two_documents_get_separate_contexts(tmp_path, config, stub_models):
    first = tmp_path / "first.txt"
    first.write_text(CONTRACT, encoding="utf-8")
    second = tmp_path / "second.txt"
    second.write_text(CONTRACT.replace("ninety", "thirty"), encoding="utf-8")

    one = documents.document_context("tenant-a", "doc-1", first, config)
    two = documents.document_context("tenant-a", "doc-2", second, config)

    ids_one = {chunk.chunk_id for chunk in one.chunks}
    ids_two = {chunk.chunk_id for chunk in two.chunks}
    assert not ids_one & ids_two


def test_the_cache_does_not_grow_without_limit(tmp_path, config, stub_models):
    for number in range(documents.MAX_CACHED_DOCUMENTS + 3):
        path = tmp_path / f"doc{number}.txt"
        path.write_text(CONTRACT, encoding="utf-8")
        documents.document_context("tenant-a", f"doc-{number}", path, config)

    assert len(documents._indexes) == documents.MAX_CACHED_DOCUMENTS


def test_load_document_uses_the_given_id_not_the_filename(contract):
    document = documents.load_document(contract, "doc-abc")

    assert document.document_id == "doc-abc"
    assert document.document_name == "contract.txt"


def test_a_missing_file_is_rejected(config):
    with pytest.raises(DocumentLoadError):
        documents.chunk_upload(Path("does-not-exist.txt"), "doc-abc", config)


# ------------------------------------------------------- union_context


CONTRACT_B = CONTRACT.replace("ninety days", "sixty days")


def test_three_documents_produce_one_combined_context(tmp_path, config, stub_models):
    a = tmp_path / "a.txt"
    a.write_text(CONTRACT, encoding="utf-8")
    b = tmp_path / "b.txt"
    b.write_text(CONTRACT_B, encoding="utf-8")
    c = tmp_path / "c.txt"
    c.write_text(
        "SECURITY POLICY\n\nData is encrypted at rest and in transit.", encoding="utf-8"
    )

    context = documents.union_context(
        "tenant-a",
        [("doc-a", a), ("doc-b", b), ("doc-c", c)],
        config,
    )

    assert {chunk.document_id for chunk in context.chunks} == {
        "doc-a",
        "doc-b",
        "doc-c",
    }


def test_two_documents_produce_a_context_with_only_those_two(
    tmp_path, config, stub_models
):
    a = tmp_path / "a.txt"
    a.write_text(CONTRACT, encoding="utf-8")
    b = tmp_path / "b.txt"
    b.write_text(CONTRACT_B, encoding="utf-8")

    context = documents.union_context("tenant-a", [("doc-a", a), ("doc-b", b)], config)

    assert {chunk.document_id for chunk in context.chunks} == {"doc-a", "doc-b"}


def test_one_document_produces_a_context_with_just_that_one(
    contract, config, stub_models
):
    context = documents.union_context("tenant-a", [("doc-a", contract)], config)

    assert {chunk.document_id for chunk in context.chunks} == {"doc-a"}


def test_document_context_is_a_thin_wrapper_around_union_context(
    contract, config, stub_models
):
    """One implementation, not two — document_context just calls union_context."""
    single = documents.document_context("tenant-a", "doc-a", contract, config)
    documents.clear_cache()
    union = documents.union_context("tenant-a", [("doc-a", contract)], config)

    assert [c.chunk_id for c in single.chunks] == [c.chunk_id for c in union.chunks]


def test_another_vendors_documents_are_never_unioned_in(tmp_path, config, stub_models):
    """union_context only ever sees the documents it is explicitly given."""
    a = tmp_path / "a.txt"
    a.write_text(CONTRACT, encoding="utf-8")
    other_vendor = tmp_path / "other.txt"
    other_vendor.write_text(
        "A completely different company's confidential filing.", encoding="utf-8"
    )

    context = documents.union_context("tenant-a", [("doc-a", a)], config)

    assert {chunk.document_id for chunk in context.chunks} == {"doc-a"}


# ---------------------------------------------------------- document types


def test_a_missing_document_type_stores_as_none(tmp_path, config):
    """No document_type given at upload is a valid state, not an error."""
    from sentineliq.pipeline import investigation

    assert None not in investigation.DOCUMENT_TYPES
    assert investigation.DOCUMENT_TYPES == ("contract", "financial", "security")
