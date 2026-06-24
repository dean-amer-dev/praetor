FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 appuser
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/ common/
COPY agents/phase25-smoke/ agents/phase25-smoke/
RUN mkdir -p /scratch && chown appuser:appuser /scratch
ENV PYTHONUNBUFFERED=1
ENV SCRATCH_DIR=/scratch
USER appuser
CMD ["python", "-m", "agents.phase25_smoke.worker"]
