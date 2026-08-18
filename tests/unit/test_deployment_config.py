"""Deployment-preparation behaviour (Phase 1, AWS).

Three things a cloud host needs that a laptop did not:
  - the retrieval models are loaded once, not on every /run
  - uploads go to a configurable absolute path on a mounted volume
  - half precision can be turned off on a CPU-only host

No LLM is called and no model is loaded here.
"""

import importlib
from pathlib import Path, PurePosixPath

import pytest

from sentineliq import config, service

# --- the retrieval context is built once ---------------------------------


def test_the_retrieval_context_is_built_once_not_per_run(monkeypatch, tmp_path):
    """Rebuilding per run reloaded both models and re-chunked 87 documents,
    which on a CPU host is minutes and gigabytes for every investigation."""
    builds = []

    fake_investigate = type("M", (), {})()
    fake_investigate.build_context = lambda cfg, app: builds.append(1) or "CONTEXT"
    fake_investigate.load_questions = lambda vendor, path: [{"question_id": "Q1"}]
    monkeypatch.setitem(
        __import__("sys").modules, "scripts.investigate", fake_investigate
    )

    runner = service.build_pipeline_runner(tmp_path, tmp_path / "q.json", None)

    assert builds == [], "must stay lazy — importing must not load models"

    first = runner.shared_context()
    second = runner.shared_context()

    assert first == "CONTEXT"
    assert second is first
    assert len(builds) == 1, "the models must be loaded exactly once"


def test_the_context_is_not_loaded_until_a_run_needs_it(monkeypatch, tmp_path):
    loaded = []
    fake = type("M", (), {})()
    fake.build_context = lambda cfg, app: loaded.append(1) or "CTX"
    monkeypatch.setitem(__import__("sys").modules, "scripts.investigate", fake)

    service.build_pipeline_runner(tmp_path, tmp_path / "q.json", None)

    assert loaded == []


# --- uploads live on a configurable absolute path -------------------------


def test_the_upload_directory_can_be_pointed_at_a_mounted_volume(monkeypatch):
    monkeypatch.setenv("SENTINELIQ_UPLOAD_DIR", "/var/lib/sentineliq/uploads")
    reloaded = importlib.reload(service)
    try:
        # PurePosixPath, because the container is Linux even when the test
        # runs on Windows, where a leading slash alone is not "absolute".
        assert reloaded.UPLOAD_DIR == Path("/var/lib/sentineliq/uploads")
        assert PurePosixPath(reloaded.UPLOAD_DIR.as_posix()).is_absolute()
    finally:
        monkeypatch.delenv("SENTINELIQ_UPLOAD_DIR", raising=False)
        importlib.reload(service)


def test_the_upload_directory_defaults_to_the_local_path(monkeypatch):
    monkeypatch.delenv("SENTINELIQ_UPLOAD_DIR", raising=False)
    reloaded = importlib.reload(service)

    assert reloaded.UPLOAD_DIR == Path("data/uploads")


# --- fp16 is overridable for a CPU host -----------------------------------


def test_fp16_can_be_disabled_for_a_cpu_host(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_FP16", "false")

    assert config.load_retrieval_config().reranker.fp16 is False


def test_fp16_stays_on_by_default():
    assert config.load_retrieval_config().reranker.fp16 is True


@pytest.mark.parametrize(
    "value,expected",
    [("true", True), ("1", True), ("no", False), ("FALSE", False)],
)
def test_the_fp16_override_reads_the_usual_spellings(monkeypatch, value, expected):
    monkeypatch.setenv("RETRIEVAL_FP16", value)

    assert config.load_retrieval_config().reranker.fp16 is expected


def test_the_override_cannot_change_any_frozen_retrieval_setting(monkeypatch):
    """Only fp16 is overridable. Models, chunking and k values stay frozen."""
    monkeypatch.setenv("RETRIEVAL_FP16", "false")
    monkeypatch.setenv("RETRIEVAL_CHUNK_SIZE", "128")
    monkeypatch.setenv("RETRIEVAL_MODEL", "some/other-model")

    loaded = config.load_retrieval_config()

    assert loaded.chunking.chunk_size == 512
    assert loaded.chunking.chunk_overlap == 64
    assert loaded.dense.model == "BAAI/bge-base-en-v1.5"
    assert loaded.reranker.model == "BAAI/bge-reranker-v2-m3"


# --- no secret is baked into the image ------------------------------------


def test_the_dockerignore_excludes_the_env_file():
    ignored = Path(".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in ignored
    assert ".venv/" in ignored


def test_the_dockerfile_never_copies_the_env_file():
    for path in (Path("Dockerfile"), Path("frontend/Dockerfile")):
        body = path.read_text(encoding="utf-8")
        assert ".env" not in body, f"{path} must not reference .env"


def test_the_production_compose_file_hardcodes_no_secret():
    body = Path("docker-compose.prod.yml").read_text(encoding="utf-8")

    for required in ("SECRET_KEY", "GROQ_API_KEY", "DATABASE_URL"):
        assert f"${{{required}:?" in body, f"{required} must come from the environment"
    assert "gsk_" not in body
