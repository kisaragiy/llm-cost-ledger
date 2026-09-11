# syntax=docker/dockerfile:1

# ---------- builder：只在这里装依赖，编译产物不带进最终镜像 ----------
FROM python:3.11-slim AS builder

WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# ---------- runtime ----------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    LEDGER_DB=/data/ledger.db \
    LEDGER_HOST=0.0.0.0 \
    LEDGER_PORT=8790

COPY --from=builder /opt/venv /opt/venv

# 非 root 运行；/data 用于挂载账本持久化
RUN useradd --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data /app/examples \
 && chown -R app:app /data /app
WORKDIR /app
COPY --chown=app:app examples ./examples
COPY --chown=app:app scripts ./scripts

USER app
VOLUME ["/data"]
EXPOSE 8790

# 健康检查不依赖 curl —— 用标准库，少一层依赖
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8790/health',timeout=3).status==200 else 1)"

CMD ["python", "-m", "llm_cost_ledger", "serve"]
