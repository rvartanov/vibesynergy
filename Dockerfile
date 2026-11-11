# syntax=docker/dockerfile:1
# VibeSynergy as a container: Linux, macOS and Windows (Docker Desktop, Podman, OrbStack…),
# amd64 and arm64. See INSTALL.md. All state lives in the /data volume.

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
# Dependencies first (cached while only the code changes), exactly as uv.lock pins them.
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 vibesynergy \
 && mkdir /data && chown vibesynergy:vibesynergy /data
COPY --from=build /opt/venv /opt/venv
COPY deploy/container/entrypoint.sh /usr/local/bin/vibesynergy-entrypoint
ARG VIBESYNERGY_COMMIT=""
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    VIBESYNERGY_CONTAINER=1 \
    VIBESYNERGY_COMMIT=$VIBESYNERGY_COMMIT \
    VIBESYNERGY_CONFIG=/data/config/config.toml \
    VIBESYNERGY_DATA_DIR=/data/share
LABEL org.opencontainers.image.source="https://github.com/rvartanov/vibesynergy" \
      org.opencontainers.image.description="VibeSynergy: your health and training data in one place" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision=$VIBESYNERGY_COMMIT
USER vibesynergy
WORKDIR /data
VOLUME /data
# 8447: the portal (publish it to the host's 127.0.0.1 only). 8448: the service (webhooks, MCP).
EXPOSE 8447 8448
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8448/vibesynergy/health', timeout=4)"
ENTRYPOINT ["tini", "--", "vibesynergy-entrypoint"]
CMD ["serve"]
