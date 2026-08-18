# SentinelIQ dashboard (Streamlit).
#
# Deliberately a separate image from the backend: the UI talks HTTP only and
# must not carry the ML stack. Keeping them apart also avoids the pandas/torch
# conflict recorded in PROGRESS.md.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY frontend/requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY frontend/app.py ./app.py

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
