# IncidentPilot webhook receiver.
#
# git is a hard runtime dependency, not a build tool: the agent shells out to it to read
# the repository under investigation, which is mounted read-only at /repo.

FROM python:3.13-slim AS build

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .


FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /install /usr/local

WORKDIR /app
COPY runbooks ./runbooks
COPY data/topology.json ./data/topology.json

RUN useradd --create-home --uid 10001 pilot \
    && mkdir -p /app/out /app/state \
    && chown -R pilot:pilot /app

# The mounted repo is owned by the host user, not by `pilot`. Without this, git refuses
# to read it with "detected dubious ownership".
ENV GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=safe.directory \
    GIT_CONFIG_VALUE_0=*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    INCIDENTPILOT_REPO=/repo \
    INCIDENTPILOT_RUNBOOKS=/app/runbooks \
    INCIDENTPILOT_TOPOLOGY=/app/data/topology.json \
    INCIDENTPILOT_INDEX=/app/state/runbooks.db \
    INCIDENTPILOT_OUT=/app/out

USER pilot
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["incidentpilot", "serve", "--host", "0.0.0.0", "--port", "8080"]
