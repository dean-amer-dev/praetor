FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 appuser
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/ common/
COPY agents/echo-agent/ agents/echo_agent/
RUN mkdir -p /scratch && chown appuser:appuser /scratch
ENV PYTHONUNBUFFERED=1
ENV SCRATCH_DIR=/scratch
USER appuser
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import importlib; importlib.import_module('agents.echo_agent.worker')" || exit 1
CMD ["python", "-m", "agents.echo_agent.worker"]
