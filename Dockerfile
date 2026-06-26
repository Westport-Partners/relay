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
RUN pip install --no-cache-dir ".[serve]"

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

# Use the installed console script as the entrypoint.
# relay-hub = relay.hub.app:main (defined in pyproject.toml [project.scripts]).
ENTRYPOINT ["relay-hub"]
