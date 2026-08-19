"""Turn an uploaded document into a retrieval source, scoped to itself.

An uploaded file goes: stored file -> loader -> chunker -> its own FAISS and
BM25 indexes -> the frozen `search.retrieve` pipeline. Nothing here changes
retrieval; it only decides which chunks the indexes are built from.

Scoping is structural, exactly as it is for vendors in `investigation.py`: the
indexes are built from one document's chunks, so a query against them cannot
return another document's text. The cache key carries the tenant as well as the
document, so one tenant's context can never be handed to another.

The models (embedder, cross-encoder, tokenizer) cost minutes and gigabytes to
load, so they are loaded once and shared. The per-document indexes are small
and are built on first use.
"""

# 1. Standard library imports
import logging
import threading
from pathlib import Path

# 3. Internal imports
from sentineliq.components.evaluation.retrieval_eval import LOADERS
from sentineliq.components.ingestion.chunker import chunk_document
from sentineliq.components.models.schemas import Chunk
from sentineliq.components.retrieval import dense, reranker, sparse
from sentineliq.config import RetrievalConfig
from sentineliq.exceptions import DocumentLoadError
from sentineliq.pipeline import flow

logger = logging.getLogger(__name__)

# 4. Constants
#: How many documents' indexes to keep in memory. A demo works on one document
#: at a time; the oldest is dropped rather than growing without limit.
MAX_CACHED_DOCUMENTS = 8

_models: dict = {}
_indexes: dict[tuple[str, str], tuple] = {}
_lock = threading.Lock()


def load_document(path: Path, document_id: str):
    """Load one uploaded file, picking the loader by extension.

    The document ID is the database ID of the upload, so chunk IDs are unique
    across tenants and cannot collide with the benchmark corpus.

    Raises:
        DocumentLoadError: The extension has no loader, or the file cannot be
            parsed.
    """
    loader = LOADERS.get(path.suffix.lower())
    if loader is None:
        raise DocumentLoadError(f"No loader for {path.suffix} files: {path.name}")
    return loader(path, document_id=document_id)


def chunk_upload(path: Path, document_id: str, config: RetrievalConfig) -> list[Chunk]:
    """Load and chunk one uploaded document, using the frozen chunk settings."""
    document = load_document(path, document_id)
    chunks = chunk_document(
        document,
        token_counter(config),
        chunk_size=config.chunking.chunk_size,
        chunk_overlap=config.chunking.chunk_overlap,
    )
    logger.info(
        "Chunked an upload",
        extra={"document_id": document_id, "chunks": len(chunks)},
    )
    return chunks


def token_counter(config: RetrievalConfig):
    """Count tokens the way the embedding model does.

    Loaded once: the chunk boundaries must match the tokenizer the frozen
    chunk_size of 512 was measured against.
    """
    with _lock:
        if "tokenizer" not in _models:
            from transformers import AutoTokenizer

            # Hardcode the tokenizer to the frozen baseline model. If this uses
            # config.dense.model, it will crash when Voyage models are configured
            # because they do not exist on the Hugging Face hub.
            _models["tokenizer"] = AutoTokenizer.from_pretrained("BAAI/bge-base-en-v1.5")
        tokenizer = _models["tokenizer"]
    return lambda text: len(tokenizer.encode(text, add_special_tokens=False))


def load_models(config: RetrievalConfig) -> dict:
    """The embedder and cross-encoder, loaded once and shared across calls."""
    with _lock:
        if "embedder" not in _models:
            logger.info(
                "Loading retrieval models",
                extra={
                    "embed_model": config.dense.model,
                    "reranker_model": config.reranker.model,
                },
            )
            _models["embedder"] = dense.load_model(config.dense.model)
            _models["cross_encoder"] = reranker.load_model(
                config.reranker.model,
                fp16=config.reranker.fp16,
            )
        return {
            "embedder": _models["embedder"],
            "cross_encoder": _models["cross_encoder"],
        }


def build_indexes(chunks: list[Chunk], embedder) -> tuple:
    """FAISS and BM25 over these chunks and nothing else."""
    return dense.build_index(embedder, chunks), sparse.build_index(chunks)


def union_context(
    tenant_id: str,
    documents: list[tuple[str, Path]],
    config: RetrievalConfig,
    llm=None,
) -> flow.RunContext:
    """A RunContext whose indexes cover every document in `documents`, unioned.

    This is one document for the single-document case (Q&A, or an investigation
    with only one document type uploaded) and several for a company with
    Contract + Financial + Security all supplied — same function either way, so
    there is one context-builder, not two.

    Built on first use and cached per (tenant, exact set of document ids), so a
    second question against the same set does not re-embed it, and a changed
    set of documents gets a fresh index rather than a stale one.
    """
    models = load_models(config)
    key = (tenant_id, tuple(sorted(document_id for document_id, _ in documents)))

    with _lock:
        cached = _indexes.get(key)
    if cached is None:
        chunks = []
        for document_id, stored_path in documents:
            chunks.extend(chunk_upload(stored_path, document_id, config))
        faiss_index, bm25_index = build_indexes(chunks, models["embedder"])
        cached = (chunks, faiss_index, bm25_index)
        with _lock:
            _indexes[key] = cached
            while len(_indexes) > MAX_CACHED_DOCUMENTS:
                _indexes.pop(next(iter(_indexes)))

    chunks, faiss_index, bm25_index = cached
    return flow.RunContext(
        config=config,
        embedder=models["embedder"],
        faiss_index=faiss_index,
        bm25_index=bm25_index,
        cross_encoder=models["cross_encoder"],
        chunks=chunks,
        llm=llm,
    )


def document_context(
    tenant_id: str,
    document_id: str,
    stored_path: Path,
    config: RetrievalConfig,
    llm=None,
) -> flow.RunContext:
    """A RunContext for one document. A convenience wrapper around `union_context`."""
    return union_context(tenant_id, [(document_id, stored_path)], config, llm)


def clear_cache() -> None:
    """Forget every cached index. Used by tests and after a document is deleted."""
    with _lock:
        _indexes.clear()
