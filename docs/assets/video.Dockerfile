# syntax=docker/dockerfile:1
# Films docs/assets/video.py: Chromium (Playwright's image), ffmpeg, and Inter standing in for the system faces the
# app asks for, so the film looks the same from any machine. The repository is mounted at /src when it runs.
#
#   docker build -t nanotea-video -f docs/assets/video.Dockerfile .
#   docker run --rm -v "$PWD:/src" nanotea-video

FROM mcr.microsoft.com/playwright:v1.63.0-noble
COPY --from=ghcr.io/astral-sh/uv:0.10.7 /uv /usr/local/bin/uv
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
    && apt-get install -y --no-install-recommends ffmpeg fonts-inter fonts-jetbrains-mono \
    && rm -rf /var/lib/apt/lists/*

COPY <<EOF /etc/fonts/local.conf
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <alias binding="strong"><family>system-ui</family><prefer><family>Inter</family></prefer></alias>
  <alias binding="strong"><family>sans-serif</family><prefer><family>Inter</family></prefer></alias>
  <alias binding="strong"><family>monospace</family><prefer><family>JetBrains Mono</family></prefer></alias>
</fontconfig>
EOF
RUN fc-cache -f \
    && fc-match system-ui | grep -q Inter \
    && fc-match sans-serif | grep -q Inter \
    && fc-match monospace | grep -q "JetBrains Mono"

# The browser is the image's; this is the same Playwright for Python, with nanotea's dependencies.
COPY pyproject.toml /tmp/nanotea/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/video \
    && uv pip install --python /opt/video/bin/python -r /tmp/nanotea/pyproject.toml playwright==1.63.0
ENV PATH=/opt/video/bin:$PATH PYTHONPATH=/src HOME=/tmp
WORKDIR /src
CMD ["python", "docs/assets/video.py"]
