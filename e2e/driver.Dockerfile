# syntax=docker/dockerfile:1
# The e2e driver: Chromium (Playwright's image), the owner's and agents' side of a run, and everything a native
# Linux install uses: espeak-ng, openai-whisper on CPU, llm with its Ollama plugin, Caddy. e2e/ is mounted at
# /src/e2e when it runs, so editing a check needs no rebuild. Built from the repository root (e2e/README.md).

FROM mcr.microsoft.com/playwright:v1.63.0-noble
COPY --from=ghcr.io/astral-sh/uv:0.10.7 /uv /usr/local/bin/uv
COPY --from=caddy:2.10 /usr/bin/caddy /usr/local/bin/caddy
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/bin/python3.12 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# The image fetches from Azure's Ubuntu mirror, which goes unreachable for whole runs; Ubuntu's own archive instead.
COPY <<EOF /etc/apt/sources.list.d/ubuntu.sources
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: noble noble-updates noble-backports
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://security.ubuntu.com/ubuntu/
Suites: noble-security
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
RUN rm -f /etc/apt/sources.list \
    && ! grep -rn azure /etc/apt/sources.list.d \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng libnss3-tools \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/whisper \
    && uv pip install --python /opt/whisper/bin/python torch --index-url https://download.pytorch.org/whl/cpu \
    && uv pip install --python /opt/whisper/bin/python openai-whisper==20250625

RUN --mount=type=cache,target=/root/.cache/uv \
    UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin uv tool install llm==0.36 --with llm-ollama==0.17.1

# The browser is the image's; this is the same Playwright for Python.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/e2e && uv pip install --python /opt/e2e/bin/python playwright==1.63.0
COPY pyproject.toml uv.lock README.md LICENSE /src/
COPY nanotea /src/nanotea
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/e2e/bin/python /src \
    && mkdir -m 1777 /cache /work
ENV PATH=/opt/e2e/bin:$PATH XDG_CACHE_HOME=/cache PYTHONPATH=/src PYTHONUNBUFFERED=1
WORKDIR /src
