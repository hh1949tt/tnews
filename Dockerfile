FROM python:3.12-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 构建指纹：CI 传入 tag 对应的 commit 与构建时间，运行时可从 /health 读到，
# 用来判断「我 pull 的到底是不是新镜像」。手动 docker build 不传就是 unknown。
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ENV TNEWS_BUILD_SHA=$GIT_SHA \
    TNEWS_BUILD_TIME=$BUILD_TIME

# 只装 Python 依赖，不需要 ffmpeg（音频拼接在纯 Python 里完成）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web
COPY config.yaml ./

RUN mkdir -p /app/data/audio

EXPOSE 18282

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:18282/health', timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18282"]
