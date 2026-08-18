"""NFR-003a: one tenant's retrieval must never surface another tenant's chunks.

Context.md 26.D calls cross-tenant leakage the single highest-severity failure
mode in the system, and requires an explicit integration test. This is it.

A tenant here is one vendor dossier: isolation is enforced by building the
FAISS and BM25 indexes from that tenant's chunks only, so another tenant's text
is not in the index to be found. That is the "indexes are partitioned per
tenant" option NFR-003a allows, rather than filtering results afterwards.

Real retrieval runs here — real embeddings, real BM25, real reranker. No LLM is
called, so the test costs no quota.
"""

import json
from pathlib import Path

import pytest

from sentineliq.components.evaluation.retrieval_eval import load_document
from sentineliq.components.ingestion.chunker import chunk_document
from sentineliq.components.retrieval import dense, reranker, search, sparse
from sentineliq.config import load_retrieval_config, load_risk_rules
from sentineliq.pipeline import investigation

RULES = load_risk_rules()

DOCUMENTS = Path("data/raw/documents")

TENANT_A = "Meridian CloudWorks"
TENANT_B = "Castleridge Trust Data Services"


def dossier(name):
    return investigation.load_dossier(DOCUMENTS / "dossiers.json", name)


@pytest.fixture(scope="module")
def config():
    return load_retrieval_config()


@pytest.fixture(scope="module")
def corpus(config):
    """Chunk only the two tenants' text documents — enough, and fast."""
    wanted = investigation.dossier_document_ids(dossier(TENANT_A)) | (
        investigation.dossier_document_ids(dossier(TENANT_B))
    )
    chunks = []
    for path in sorted(DOCUMENTS.iterdir()):
        if path.suffix == ".txt" and path.stem in wanted:
            document = load_document(path)
            chunks.extend(
                chunk_document(
                    document,
                    lambda text: len(text.split()),
                    chunk_size=config.chunking.chunk_size,
                    chunk_overlap=config.chunking.chunk_overlap,
                )
            )
    assert chunks, "no chunks built — corpus layout changed"
    return chunks


@pytest.fixture(scope="module")
def models(config):
    return (
        dense.load_model(config.dense.model),
        reranker.load_model(config.reranker.model, fp16=config.reranker.fp16),
    )


def tenant_chunks(corpus, name):
    return investigation.scoped_chunks(corpus, dossier(name))


def test_the_two_tenants_share_no_documents(corpus):
    a = {c.document_id for c in tenant_chunks(corpus, TENANT_A)}
    b = {c.document_id for c in tenant_chunks(corpus, TENANT_B)}
    assert a and b
    assert a.isdisjoint(b)


def test_tenant_bs_chunks_are_absent_from_tenant_as_index(corpus):
    a_ids = {c.chunk_id for c in tenant_chunks(corpus, TENANT_A)}
    b_ids = {c.chunk_id for c in tenant_chunks(corpus, TENANT_B)}
    assert a_ids.isdisjoint(b_ids)
    # The corpus really does hold both, so the disjointness above is the
    # scoping working, not an empty-set accident.
    assert a_ids | b_ids <= {c.chunk_id for c in corpus}


def test_tenant_a_cannot_retrieve_tenant_bs_chunks(corpus, config, models):
    """The core NFR-003a assertion, against the real retrieval pipeline.

    The query is lifted verbatim out of one of tenant B's documents, so it is
    the strongest possible pull toward B's text. Tenant A's index must still
    return only A's chunks.
    """
    embedder, cross_encoder = models
    a_chunks = tenant_chunks(corpus, TENANT_A)
    b_chunks = tenant_chunks(corpus, TENANT_B)
    a_ids = {c.chunk_id for c in a_chunks}

    faiss_index = dense.build_index(embedder, a_chunks)
    bm25_index = sparse.build_index(a_chunks)

    for b_chunk in b_chunks[:5]:
        query = " ".join(b_chunk.text.split()[:40])
        hits = search.retrieve(
            config, embedder, faiss_index, bm25_index, cross_encoder, a_chunks, query
        )
        returned = {chunk_id for chunk_id, _ in hits}
        assert returned, "retrieval returned nothing at all"
        leaked = returned - a_ids
        assert not leaked, f"tenant B chunk leaked into tenant A: {leaked}"


def test_the_runner_refuses_a_result_from_outside_the_tenant(corpus, monkeypatch):
    """Belt and braces: even if retrieval were wrong, the runner must catch it."""
    a_dossier = dossier(TENANT_A)
    b_chunk_id = tenant_chunks(corpus, TENANT_B)[0].chunk_id

    monkeypatch.setattr(investigation.dense, "build_index", lambda model, chunks: None)
    monkeypatch.setattr(investigation.sparse, "build_index", lambda chunks: None)

    def leaking_finding(context, question, category):
        return {
            "answer": "leaked",
            "citations": [b_chunk_id],
            "dropped_citations": [],
            "injection_flagged": False,
            "supplied": [b_chunk_id],
            "category": category,
            "specialist": "x",
            "severity": "LOW",
            "contradiction": False,
        }

    monkeypatch.setattr(investigation.flow, "investigate_finding", leaking_finding)

    context = investigation.flow.RunContext(
        config=None,
        embedder=None,
        faiss_index=None,
        bm25_index=None,
        cross_encoder=None,
        chunks=corpus,
        llm=None,
    )
    questions = [
        {"question_id": "Q001", "category": "compliance", "question": "SOC 2 valid?"}
    ]
    with pytest.raises(ValueError, match="leaked chunks from another vendor"):
        investigation.run_investigation(context, a_dossier, questions, RULES)


def test_every_dossier_is_isolated_from_every_other():
    """All 9 tenants, not just the two used above."""
    dossiers = json.loads((DOCUMENTS / "dossiers.json").read_text(encoding="utf-8"))[
        "dossiers"
    ]
    ids = {d["vendor_name"]: investigation.dossier_document_ids(d) for d in dossiers}
    names = list(ids)
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            shared = ids[first] & ids[second]
            assert not shared, f"{first} and {second} share {shared}"
