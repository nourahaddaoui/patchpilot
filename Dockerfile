# PatchPilot hosted demo (deployed on Render: see render.yaml).
#
#   docker build -t patchpilot .
#   docker run --rm -p 8000:8000 patchpilot      # then open http://localhost:8000
#
# The demo runs the eval scenarios (eval/scenarios.py): the real agent, tools, checkpoint,
# recovery and stop rule, with trivy, pip and pytest simulated per scenario. So the image
# needs Python, git and bash (sandbox/reset.sh), but not Trivy or its 1.6 GB database, and it
# never runs a stranger's code. Runs as a non-root user; only port $PORT is exposed.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    LANGSMITH_TRACING=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 patchpilot && chown -R patchpilot /app
USER patchpilot

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"
CMD ["python", "-m", "web"]
