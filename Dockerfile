# Relay Hub — Fargate container image
# Build: docker build -t relay-hub:latest .
# Run:   relay-hub (console script) on port 8080

FROM python:3.12-slim

# Install curl (required by the ECS health check: curl -f http://localhost:8080/health)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for good practice
RUN useradd --create-home --shell /bin/bash relay

WORKDIR /app

# Copy only the files pip needs to install the package
# README.md is referenced by pyproject.toml and is required by hatchling at build time.
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install the relay package with the `serve` extra (fastapi, uvicorn, tzdata).
# The serve extra is declared in pyproject.toml [project.optional-dependencies];
# pip install .[serve] keeps web deps in one place (no hand-listed packages here).
# pip install . picks up src/relay/hub/dashboard.html via hatchling's package
# data discovery (the wheel includes all files under src/relay/).
# tzdata: python:slim has no system zoneinfo DB; the PyPI tzdata package in the
# serve extra gives zoneinfo a bundled database so RELAY_TZ works correctly.
# The `demo` extra (faker, httpx) powers the optional self-running test-env
# harness (RELAY_DEMO=true). It's small and pure-Python; bundling it keeps the
# one-command demo working off a published image without a second build target.
RUN pip install --no-cache-dir ".[serve,demo]"

# Bundle the config/ dir so the Hub can load rotations/escalation/routing from
# local YAML (RELAY_CONFIG_SOURCE=local, RELAY_CONFIG_DIR=/app/config). Only the
# .yaml files are copied (no PII — contacts live in DynamoDB).
COPY config/ ./config/

# Bundle the AI investigation skill pack so the ClaudeCodeAssistant can mount it
# (RELAY_AI_PROVIDER=claude-code reads RELAY_SKILLS_DIR). Read-only probes only.
COPY skills/ ./skills/

# Bundle the one-shot local-mock bootstrap so `docker compose up` against a
# PUBLISHED image works without mounting the repo's scripts/ dir.
COPY scripts/relay-local-bootstrap.py ./scripts/relay-local-bootstrap.py

# Bundle the entrypoint wrapper + test-env harness so RELAY_DEMO=true can
# self-populate the Hub from a published image (no repo checkout needed).
COPY scripts/relay-entrypoint.sh ./scripts/relay-entrypoint.sh
COPY tools/ ./tools/
USER root
RUN chmod +x ./scripts/relay-entrypoint.sh
USER relay

# Confirm dashboard.html was included in the installed package
# (fails the build early if the file is missing from the wheel).
RUN python -c "import relay.hub.app; import pathlib; \
    p = pathlib.Path(relay.hub.app.__file__).parent / 'dashboard.html'; \
    assert p.exists(), f'dashboard.html missing from installed package: {p}'"

ENV PYTHONUNBUFFERED=1
# Where the ClaudeCodeAssistant looks for the bundled investigation skill pack.
ENV RELAY_SKILLS_DIR=/app/skills

# Build provenance: the deploy/build script passes the git short SHA + an ISO
# build timestamp so the running container can report what it was built from
# (surfaced on the Settings screen / GET /config).
ARG RELAY_BUILD_SHA=unknown
ARG RELAY_BUILD_TIME=unknown
ENV RELAY_BUILD_SHA=${RELAY_BUILD_SHA}
ENV RELAY_BUILD_TIME=${RELAY_BUILD_TIME}

# Switch to non-root user
USER relay

EXPOSE 8080

# Entrypoint wrapper: runs `relay-hub` (exec'd as the main process for clean
# SIGTERM) and, when RELAY_DEMO=true, also launches the self-populating test-env
# harness in the background. Without RELAY_DEMO it behaves exactly like running
# relay-hub directly.
ENTRYPOINT ["/app/scripts/relay-entrypoint.sh"]
