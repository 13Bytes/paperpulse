FROM python:3.11-slim

ENV CODEX_HOME=/app/.codex

# Install supercronic for cron-in-Docker and Codex CLI for optional summaries.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.29/supercronic-linux-amd64 \
    && curl -fsSL "$SUPERCRONIC_URL" -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic \
    && mkdir -p /opt/codex-install "$CODEX_HOME" \
    && curl -fsSL https://chatgpt.com/codex/install.sh -o /tmp/install-codex.sh \
    && CODEX_HOME=/opt/codex-install CODEX_NON_INTERACTIVE=1 CODEX_INSTALL_DIR=/usr/local/bin sh /tmp/install-codex.sh \
    && rm -f /tmp/install-codex.sh \
    && apt-get remove -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: run the summariser once (useful for one-shot / dev invocations).
# Override CMD in docker-compose to use supercronic for scheduled runs.
CMD ["python", "-m", "api.main"]
