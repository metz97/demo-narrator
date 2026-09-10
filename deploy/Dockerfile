# demo-narrator web server (single-tenant). Build from the repo root:
#   docker build -f deploy/Dockerfile -t demo-narrator .
# NOTE: the image is large (~4-5 GB): PyTorch (Kokoro TTS) + Chromium.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[server,demo,kokoro]" \
    && playwright install --with-deps chromium

# Workspace (profiles/, output/, config/) is mounted at runtime — see compose
# file. Running from /workspace makes the default workspace_root "." correct.
WORKDIR /workspace

EXPOSE 8765
CMD ["demo-narrator", "serve", "--host", "0.0.0.0", "--port", "8765", \
     "--config", "/workspace/config/config.yaml"]
