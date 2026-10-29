# ONVIF カメラエミュレーター(arm64 / amd64)
# Python(SOAP / WS-Discovery / プロセス監視)+ MediaMTX(RTSP)+ FFmpeg(libx265 / libx264)

FROM python:3.13-slim AS base

ARG TARGETARCH
ARG MEDIAMTX_VERSION=v1.21.2

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tini curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) arch=amd64 ;; \
      arm64) arch=arm64 ;; \
      *) echo "unsupported arch: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_${arch}.tar.gz" \
    | tar -xz -C /usr/local/bin mediamtx \
 && mediamtx --version

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY scripts ./scripts
COPY config.example.yaml ./
RUN uv sync --frozen --no-dev

ENV EMULATOR_RUNTIME_DIR=/run/onvif-emulator
EXPOSE 8080-8089 8090 8554 8000/udp 8001/udp 3702/udp 8189/udp 8189/tcp
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s \
  CMD curl -fsS "http://127.0.0.1:${EMULATOR_ONVIF_PORT:-8080}/healthz" >/dev/null || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "onvif_emulator"]

# ---- テスト用(pytest、onvif-zeep、WSDiscovery、ffprobe)----
FROM base AS test
RUN uv sync --frozen
COPY tests ./tests
CMD ["pytest", "-v", "-rs"]
