FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先复制构建元数据与源码，安装运行时依赖（仅 Pillow）
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && groupadd --gid 10001 icogen \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin icogen \
    && mkdir -p /data/output \
    && chown -R icogen:icogen /data

USER icogen

ENV ICOGEN_HOST=0.0.0.0 \
    ICOGEN_PORT=8765 \
    ICOGEN_OUTPUT_DIR=/data/output

EXPOSE 8765
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)"

CMD ["icogen", "serve", "--host", "0.0.0.0", "--port", "8765"]
