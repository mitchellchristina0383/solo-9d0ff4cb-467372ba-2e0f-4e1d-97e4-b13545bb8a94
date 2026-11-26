# 回放页镜像：单容器内运行 Flask（内置本地源站 + 缓存代理演示 + 回放引擎）
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# 单 worker：演示接口 /demo 持有进程内状态；回放本身每次从初态重建，无状态依赖。
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

CMD ["gunicorn", "--bind=0.0.0.0:8000", "--workers=1", "--timeout=60", "app.web:app"]

# 自测阶段：compose 的 replay-selftest 服务使用此 target
FROM base AS test
RUN pip install --no-cache-dir pytest==9.2.0
COPY tests/ ./tests/
CMD ["pytest", "-v", "tests"]
