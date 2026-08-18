# SentinelIQ backend (FastAPI + retrieval + agents).
#
# The retrieval models are downloaded on first use, so give the container a
# persistent cache volume or the first request will be slow.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/cache/huggingface

WORKDIR /app

# System libraries PyMuPDF and torch need at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./

# CPU torch on purpose. The default Linux wheel drags in ~4 GB of CUDA
# libraries that a CPU container cannot use. Retrieval results are identical
# either way — only speed differs, so the recorded latency figures (measured on
# a GPU) do not apply to this image.
RUN pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch==2.13.0 \
    && pip install -r requirements.txt

COPY sentineliq/ ./sentineliq/
COPY scripts/ ./scripts/
COPY data/ ./data/
RUN pip install -e . --no-deps

EXPOSE 8000

# Matches the /health endpoint the API exposes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "sentineliq.components.api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
