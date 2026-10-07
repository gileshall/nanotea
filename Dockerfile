# syntax=docker/dockerfile:1
# The service, ffmpeg, and (WHISPER=1) openai-whisper on CPU to transcribe recordings. KOKORO=1 adds the
# in-process kokoro voice's package; its model files are fetched into /cache by `nanotea kokoro download`, never here.
# docs/service.md#docker

FROM python:3.12-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.7 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Whisper has its own venv: big, rarely changes, and transcribe.sh takes the python to use.
ARG WHISPER=1
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$WHISPER" = 1 ]; then \
        uv venv /opt/whisper \
        && uv pip install --python /opt/whisper/bin/python torch --index-url https://download.pytorch.org/whl/cpu \
        && uv pip install --python /opt/whisper/bin/python openai-whisper; \
    else \
        mkdir /opt/whisper; \
    fi

ARG KOKORO=0
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project $([ "$KOKORO" = 1 ] && echo --extra kokoro)
COPY README.md LICENSE ./
COPY nanotea nanotea
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable $([ "$KOKORO" = 1 ] && echo --extra kokoro)


FROM python:3.12-slim-trixie
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# /data: the service's data_dir. /cache: whisper's models. /config: mounted read-only.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin nanotea \
    && mkdir /data /cache /config \
    && chown nanotea /data \
    && chmod 1777 /cache
COPY --from=build /opt/whisper /opt/whisper
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH HOME=/home/nanotea XDG_CACHE_HOME=/cache PYTHONUNBUFFERED=1 \
    NANOTEA_CONFIG=/config/config.toml
USER nanotea
VOLUME /data
# The icon is served before any pairing or source check, so it shows only that the service answers.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; from nanotea.config import load_config as c; u.urlopen(f'http://127.0.0.1:{c()[\"port\"]}/favicon.svg', timeout=5)"]
ENTRYPOINT ["nanotea"]
CMD ["serve"]
