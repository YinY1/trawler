# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────
# Trawler 镜像
# 单容器同时托管 FastAPI Web UI + 后台 cron 调度（supervisord）
# ─────────────────────────────────────────────────────────────────────

ARG PYTHON_VERSION=3.14

# ── CSS builder：用 Tailwind standalone CLI 编译 main.css，免 node ──
# 构建期镜像，不进运行镜像；产物 COPY 进运行阶段。
FROM alpine:3.20 AS css-builder
WORKDIR /build
ARG TAILWIND_VERSION=3.4.13
RUN apk add --no-cache curl \
    && curl -fsSL -o /usr/local/bin/tailwindcss \
       "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-x64" \
    && chmod +x /usr/local/bin/tailwindcss
COPY tailwind.config.js .
COPY web/src/input.css web/src/input.css
COPY web/templates/ web/templates/
RUN /usr/local/bin/tailwindcss \
    --input web/src/input.css \
    --output main.css \
    --minify

FROM python:${PYTHON_VERSION}-slim AS base

# ── 系统依赖：ffmpeg（转写必需）+ cron + supervisor + tini（PID 1）──
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        cron \
        supervisor \
        tini \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── uv（与项目一致，使用 lock 文件保证可复现）──
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# ── 先装依赖（利用 layer cache，源码改动不触发重装）──
COPY pyproject.toml uv.lock ./
# 装运行时依赖：web（FastAPI/uvicorn）+ xhs（小红书可选）
# dev 不装，transcribe 依赖 faster-whisper 已经在主依赖里
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra web --extra xhs

# ── 版本信息注入（issue #55）：构建期 ARG → ENV，运行期 os.environ 读取 ──
# 本地构建：docker build . （未传 ARG，ENV 为空 → shared/constants.py fallback 'dev'/'unknown'）
# CI 构建：workflow 传 build-args GIT_SHA/BUILD_DATE
# 在源码 COPY 之前注入，避免 build-args 变化触发源码 layer 失效导致 uv sync 重复执行
ARG GIT_SHA=""
ARG BUILD_DATE=""
ARG TRAWLER_VERSION=""
ENV TRAWLER_GIT_SHA=${GIT_SHA}
ENV TRAWLER_BUILD_DATE=${BUILD_DATE}
ENV TRAWLER_VERSION=${TRAWLER_VERSION}

# ── 项目源码 ──
COPY . .
RUN uv sync --frozen --extra web --extra xhs

# ── 拷入构建期生成的 CSS（覆盖任何残留）──
COPY --from=css-builder /build/main.css web/static/css/main.css

# ── whisper 模型预下载（可选；构建时预热，避免首次启动慢）──
# 模型默认缓存在 /root/.cache，运行时用 HF_HOME 指向持久卷
ARG DOWNLOAD_WHISPER_MODEL=0
RUN if [ "${DOWNLOAD_WHISPER_MODEL}" = "1" ]; then \
        python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu')" \
        || echo "[warn] whisper model prefetch failed, will download at runtime"; \
    fi

# ── 运行时目录与卷 ──
RUN mkdir -p /app/config /app/data /app/downloads /app/transcripts /app/hf_cache /var/log/supervisor \
    && ln -sf /app/hf_cache /root/.cache

# ── supervisor 与 cron 配置 ──
COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY docker/crontab /etc/cron.d/trawler
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 0644 /etc/cron.d/trawler \
    && chmod +x /entrypoint.sh \
    && crontab /etc/cron.d/trawler

EXPOSE 8080

# tini 处理信号，supervisor 负责拉起 uvicorn + cron
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
