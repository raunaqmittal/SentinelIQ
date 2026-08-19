"""Tests for the configurable retrieval provider abstraction.

All tests mock the external SDKs so no API key is required and no network
calls are made. The local SentenceTransformer / CrossEncoder paths are covered
by the existing test suite; these tests focus on the provider-switch logic.
"""

import os
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from sentineliq.components.retrieval import dense, reranker


# ------------------------------------------------------------------ helpers


def _make_config(embed_provider="local", reranker_provider="local"):
    """Build a minimal RetrievalConfig with the given providers."""
    from sentineliq.config import (
        ChunkingConfig,
        DenseConfig,
        QueryRouterConfig,
        RerankerConfig,
        RetrievalConfig,
        RRFConfig,
        SparseConfig,
    )

    return RetrievalConfig(
        chunking=ChunkingConfig(chunk_size=512, chunk_overlap=64),
        dense=DenseConfig(model="voyage-law-2", top_k=50, provider=embed_provider),
        sparse=SparseConfig(top_k=50),
        rrf=RRFConfig(k=60, rerank_depth=20),
        reranker=RerankerConfig(
            model="rerank-2", fp16=False, top_n=5, provider=reranker_provider
        ),
        query_router=QueryRouterConfig(
            enable_web_search=False, enable_database=False
        ),
    )


# ------------------------------------------------- VoyageEmbedder unit tests


class TestVoyageEmbedder:
    """VoyageEmbedder calls the Voyage SDK and returns the right shape."""

    def _make_embedder(self, model="voyage-law-2"):
        """Construct a VoyageEmbedder with the SDK client patched out."""
        import voyageai

        fake_client = MagicMock()
        with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
            with patch.object(voyageai, "Client", return_value=fake_client):
                embedder = dense.VoyageEmbedder(model)
                embedder._client = fake_client
                return embedder, fake_client

    def test_encode_returns_normalised_float32_array(self):
        embedder, client = self._make_embedder()
        client.embed.return_value = MagicMock(
            embeddings=[[0.6, 0.8], [0.0, 1.0]]
        )
        result = embedder.encode(["text one", "text two"])
        assert isinstance(result, np.ndarray)
        assert result.dtype == np.float32
        assert result.shape == (2, 2)
        norms = np.linalg.norm(result, axis=1)
        np.testing.assert_allclose(norms, [1.0, 1.0], atol=1e-5)

    def test_encode_batches_at_batch_size(self):
        embedder, client = self._make_embedder()
        client.embed.return_value = MagicMock(embeddings=[[1.0, 0.0]])
        texts = ["a", "b", "c"]
        embedder.encode(texts, batch_size=2)
        # 3 texts with batch_size=2 → 2 API calls
        assert client.embed.call_count == 2

    def test_encode_single_text(self):
        embedder, client = self._make_embedder()
        client.embed.return_value = MagicMock(embeddings=[[0.0, 1.0]])
        result = embedder.encode(["single"])
        assert result.shape == (1, 2)


# ------------------------------------------------- VoyageReranker unit tests


class TestVoyageReranker:
    """VoyageReranker calls the Voyage rerank API and maps scores back correctly."""

    def _make_reranker(self, model="rerank-2"):
        import voyageai

        fake_client = MagicMock()
        with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
            with patch.object(voyageai, "Client", return_value=fake_client):
                vr = reranker.VoyageReranker(model)
                vr._client = fake_client
                return vr, fake_client

    def test_predict_returns_scores_in_original_order(self):
        vr, client = self._make_reranker()
        # Simulate Voyage returning results ranked by relevance (doc 2 highest).
        r0 = MagicMock(index=0, relevance_score=0.3)
        r1 = MagicMock(index=2, relevance_score=0.9)
        r2 = MagicMock(index=1, relevance_score=0.5)
        client.rerank.return_value = MagicMock(results=[r1, r2, r0])

        pairs = [("query", "doc0"), ("query", "doc1"), ("query", "doc2")]
        scores = vr.predict(pairs)

        # Must be in original (pairs) order, not Voyage relevance order.
        assert scores == [0.3, 0.5, 0.9]

    def test_predict_empty_pairs_returns_empty(self):
        vr, client = self._make_reranker()
        assert vr.predict([]) == []
        client.rerank.assert_not_called()

    def test_predict_passes_first_query_to_api(self):
        vr, client = self._make_reranker()
        r0 = MagicMock(index=0, relevance_score=1.0)
        client.rerank.return_value = MagicMock(results=[r0])
        vr.predict([("my question", "document text")])
        client.rerank.assert_called_once()
        assert client.rerank.call_args[0][0] == "my question"


# ------------------------------------------ provider-switch integration tests


class TestProviderSwitch:
    """load_model() returns the right adapter based on the provider argument."""

    def test_local_embedding_returns_sentence_transformer(self):
        """provider='local' loads SentenceTransformer (lazy-imported in load_model)."""
        st = MagicMock()
        with patch("sentence_transformers.SentenceTransformer", return_value=st):
            model = dense.load_model("BAAI/bge-base-en-v1.5", provider="local")
        assert model is st

    def test_voyage_embedding_returns_voyage_embedder(self):
        """provider='voyage' with a key returns a VoyageEmbedder."""
        import voyageai

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
            with patch.object(voyageai, "Client", return_value=MagicMock()):
                model = dense.load_model("voyage-law-2", provider="voyage")
        assert isinstance(model, dense.VoyageEmbedder)

    def test_voyage_embedding_falls_back_to_local_when_no_key(self):
        """provider='voyage' without a key falls back to SentenceTransformer."""
        env = {k: v for k, v in os.environ.items() if k != "VOYAGE_API_KEY"}
        st = MagicMock()
        with patch.dict(os.environ, env, clear=True):
            with patch("sentence_transformers.SentenceTransformer", return_value=st):
                model = dense.load_model("voyage-law-2", provider="voyage")
        assert model is st

    def test_local_reranker_returns_cross_encoder(self):
        """provider='local' loads CrossEncoder (lazy-imported inside load_model)."""
        ce = MagicMock()
        with patch("sentence_transformers.CrossEncoder", return_value=ce):
            with patch("torch.float16", MagicMock()):
                model = reranker.load_model(
                    "BAAI/bge-reranker-v2-m3", fp16=False, provider="local"
                )
        assert model is ce

    def test_voyage_reranker_returns_voyage_reranker(self):
        """provider='voyage' with a key returns a VoyageReranker."""
        import voyageai

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
            with patch.object(voyageai, "Client", return_value=MagicMock()):
                model = reranker.load_model("rerank-2", fp16=False, provider="voyage")
        assert isinstance(model, reranker.VoyageReranker)

    def test_voyage_reranker_falls_back_to_local_when_no_key(self):
        """provider='voyage' without a key falls back to CrossEncoder."""
        env = {k: v for k, v in os.environ.items() if k != "VOYAGE_API_KEY"}
        ce = MagicMock()
        with patch.dict(os.environ, env, clear=True):
            with patch("sentence_transformers.CrossEncoder", return_value=ce):
                with patch("torch.float16", MagicMock()):
                    model = reranker.load_model(
                        "rerank-2", fp16=False, provider="voyage"
                    )
        assert model is ce

    def test_load_retrieval_config_applies_env_var_overrides(self):
        """RETRIEVAL_EMBEDDING_PROVIDER / RETRIEVAL_RERANKER_PROVIDER env vars
        override the values in retrieval.yaml."""
        from sentineliq.config import load_retrieval_config

        with patch.dict(
            os.environ,
            {
                "RETRIEVAL_EMBEDDING_PROVIDER": "voyage",
                "RETRIEVAL_RERANKER_PROVIDER": "voyage",
            },
        ):
            config = load_retrieval_config()

        assert config.dense.provider == "voyage"
        assert config.reranker.provider == "voyage"
