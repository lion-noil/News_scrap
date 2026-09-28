# syntax=docker/dockerfile:1.6
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul \
    LANG=C.UTF-8 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HF_HOME=/opt/hf_cache \
    DEBIAN_FRONTEND=noninteractive

# 1) Python 3.11 + 기본 도구
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      tzdata ca-certificates tini wget gnupg ffmpeg software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
      python3.11 python3.11-dev python3.11-distutils \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && wget -qO /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py \
    && python3.11 /tmp/get-pip.py \
    && rm /tmp/get-pip.py

# 2) Edge 설치도 apt 캐시 재사용
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    wget -qO- https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor > /etc/apt/trusted.gpg.d/microsoft.gpg \
    && echo "deb [arch=amd64] https://packages.microsoft.com/repos/edge stable main" \
      > /etc/apt/sources.list.d/microsoft-edge.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends microsoft-edge-stable \
    && rm -rf /var/lib/apt/lists/*

# 3) Google Chrome 설치
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor \
      > /usr/share/keyrings/google-linux.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 10001 appuser
WORKDIR /app

COPY requirements.txt ./requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python -m pip install --upgrade pip && \
    python -m pip install -r requirements.txt && \
    python -m playwright install --with-deps msedge chromium

RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

RUN chown -R appuser:appuser /ms-playwright /opt/hf_cache

# 4) 구독 Claude CLI (2026-09-12) — 네이티브 바이너리를 appuser 홈에 설치.
#    인증은 app/.env 의 CLAUDE_CODE_OAUTH_TOKEN(`claude setup-token`, 1년). 모델은 CLAUDE_MODEL* (llm.py 참고).
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
USER appuser
# CLI 갱신: --build-arg CLAUDE_CLI_BUST=$(date +%s) 로 이 단계부터 캐시를 무효화해 최신 CLI를 받는다
#   (새 모델 ID — 예: claude-opus-5-5 — 는 최신 CLI만 인식. 2026-09-29)
ARG CLAUDE_CLI_BUST=0
RUN echo "cli-bust=${CLAUDE_CLI_BUST}" && curl -fsSL https://claude.ai/install.sh | bash \
    && /home/appuser/.local/bin/claude --version
USER root
ENV PATH="/home/appuser/.local/bin:${PATH}"

COPY app/ /app/
RUN chown -R appuser:appuser /app

USER appuser
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","main.py"]
