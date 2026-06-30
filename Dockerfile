# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /opt/agent

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy sibling tool projects (build context is the parent directory)
COPY auto-detector-provisioner/       ./auto-detector-provisioner/
COPY o11y-usage-governance/           ./o11y-usage-governance/
COPY o11y-instrumentation-analyzer/   ./o11y-instrumentation-analyzer/
COPY splunk-o11y-health-check/        ./splunk-o11y-health-check/

# Copy agent
COPY autonomous-o11y-agent/           ./autonomous-o11y-agent/

# Install all Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        -r ./splunk-o11y-health-check/requirements-health-hub.txt && \
    pip install --no-cache-dir \
        -e "./autonomous-o11y-agent[all]"

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/mqbui1/autonomous-o11y-agent"
LABEL org.opencontainers.image.description="Autonomous Observability Agent for Splunk Observability Cloud"

# Non-root user
RUN groupadd -r agent && useradd -r -g agent -d /home/agent -m agent

WORKDIR /opt/agent

# Copy installed packages and binaries from builder
COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --from=builder /opt/agent ./

# State directory (PVC mounts here)
RUN mkdir -p /home/agent/.o11y-agent && chown -R agent:agent /home/agent /opt/agent

USER agent
WORKDIR /opt/agent/autonomous-o11y-agent

EXPOSE 4318

ENTRYPOINT ["python3", "main.py"]
