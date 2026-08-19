"""Load YAML config files and .env secrets into typed objects."""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

CONFIGS_DIR = Path(__file__).parent / "configs"

load_dotenv()


class ChunkingConfig(BaseModel):
    """Chunk size and overlap, in tokens."""

    chunk_size: int
    chunk_overlap: int


class DenseConfig(BaseModel):
    """Dense (embedding + FAISS) retrieval settings."""

    model: str
    top_k: int
    # "local" uses bge-base-en-v1.5 via SentenceTransformer (default, no key needed).
    # "voyage" uses the Voyage AI API (fast hosted inference, requires VOYAGE_API_KEY).
    provider: str = "local"


class SparseConfig(BaseModel):
    """BM25 sparse retrieval settings."""

    top_k: int


class RRFConfig(BaseModel):
    """Reciprocal Rank Fusion settings."""

    k: int
    rerank_depth: int


class RerankerConfig(BaseModel):
    """Cross-encoder reranker settings."""

    model: str
    fp16: bool
    top_n: int
    # "local" uses bge-reranker-v2-m3 via CrossEncoder (default, no key needed).
    # "voyage" uses the Voyage AI rerank API (fast hosted inference, requires VOYAGE_API_KEY).
    provider: str = "local"


class QueryRouterConfig(BaseModel):
    """Query router feature flags."""

    enable_web_search: bool
    enable_database: bool


class RetrievalConfig(BaseModel):
    """Full retrieval.yaml config, mirrored field-for-field."""

    chunking: ChunkingConfig
    dense: DenseConfig
    sparse: SparseConfig
    rrf: RRFConfig
    reranker: RerankerConfig
    query_router: QueryRouterConfig


class LLMConfig(BaseModel):
    """LLM provider and model settings."""

    provider: str
    model: str
    base_url: str
    judge_model: str
    temperature: float
    max_tokens: int


class IngestionConfig(BaseModel):
    """Limits applied to a file before it is read."""

    max_file_bytes: int


class RetentionConfig(BaseModel):
    """How long uploaded documents are kept (NFR-003d).

    `document_days` is None when documents are kept indefinitely, which is the
    shipped default — no retention period is specified in the requirements.
    """

    document_days: int | None = None


class AppConfig(BaseModel):
    """The parts of app.yaml that code reads today."""

    llm: LLMConfig
    ingestion: IngestionConfig
    retention: RetentionConfig = RetentionConfig()


class RiskWeights(BaseModel):
    """Weight of each risk category, named to match FR-015's formula."""

    compliance: float
    security: float
    financial: float
    contract: float
    evidence_quality: float

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class ThresholdBand(BaseModel):
    """One risk band: scores up to `max` get this decision."""

    max: float
    decision: str


class EscalationConfig(BaseModel):
    """When a human must review regardless of the score (FR-019)."""

    always_escalate_on_contradiction: bool
    always_escalate_on_missing_critical_docs: bool


class EvidenceQualityWeights(BaseModel):
    """How the three evidence signals combine into one score."""

    retrieval_rate: float
    citation_accuracy: float
    citation_validity: float

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class RiskRulesConfig(BaseModel):
    """Full risk_rules.yaml config, mirrored field-for-field."""

    weights: RiskWeights
    severity: dict[str, float]
    evidence_quality: EvidenceQualityWeights
    thresholds: dict[str, ThresholdBand]
    escalation: EscalationConfig


def load_app_config(path: Path = CONFIGS_DIR / "app.yaml") -> AppConfig:
    """Load app.yaml. Only the sections with a current reader are typed."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig(
        llm=raw["llm"],
        ingestion=raw["ingestion"],
        retention=raw.get("retention") or {},
    )


def load_retrieval_config(
    path: Path = CONFIGS_DIR / "retrieval.yaml",
) -> RetrievalConfig:
    """Load and validate retrieval.yaml.

    `RETRIEVAL_FP16=false` turns off half precision for the reranker. It exists
    for CPU hosts: fp16 is a GPU memory optimisation, and on CPU torch runs it
    slowly or not at all. Full precision is the more exact of the two, so this
    does not degrade retrieval — the frozen benchmark figures were measured on a
    GPU in fp16 and remain the reference for GPU runs.

    Nothing else about retrieval can be overridden here. Model names, chunk
    sizes, pool depths and k values stay in the frozen YAML.
    """
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    override = os.environ.get("RETRIEVAL_FP16")
    if override is not None:
        raw["reranker"]["fp16"] = override.strip().lower() in ("1", "true", "yes")

    # Provider overrides — env vars take precedence over retrieval.yaml so a
    # demo deployment can be configured via docker-compose without editing the
    # committed YAML. The YAML default for both is "local".
    embed_provider = os.environ.get("RETRIEVAL_EMBEDDING_PROVIDER")
    if embed_provider:
        raw["dense"]["provider"] = embed_provider.strip().lower()

    reranker_provider = os.environ.get("RETRIEVAL_RERANKER_PROVIDER")
    if reranker_provider:
        raw["reranker"]["provider"] = reranker_provider.strip().lower()

    return RetrievalConfig(**raw)


def load_risk_rules(
    path: Path = CONFIGS_DIR / "risk_rules.yaml",
) -> RiskRulesConfig:
    """Load and validate risk_rules.yaml.

    The weights must sum to 1.0 — the file says so, and a set that does not
    would silently change every score, so it is checked on load rather than
    trusted.
    """
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    rules = RiskRulesConfig(**raw)

    for name, values in (
        ("risk weights", rules.weights.as_dict()),
        ("evidence_quality weights", rules.evidence_quality.as_dict()),
    ):
        total = sum(values.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"{name} must sum to 1.0, got {total}")
    return rules


def load_document_questions(
    path: Path = CONFIGS_DIR / "document_questions.yaml",
) -> list[dict]:
    """The generic questions asked of an uploaded document.

    Curated vendors get their own questions from `data/evaluation/questions.json`;
    an uploaded document has no dossier, so it gets this fixed set instead.
    """
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return list(raw["questions"])


def get_sec_user_agent() -> str:
    """Return the User-Agent SEC EDGAR requires on every request (ADR-006)."""
    user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not user_agent:
        raise ValueError("SEC_USER_AGENT is not set — see .env.example")
    return user_agent
