# syntax=docker/dockerfile:1
#
# mimo-usage 最小依赖镜像（多阶段）。
#   构建：docker build -t mimo-usage .
#   运行：docker run -d --name mimo-usage -p 8000:8000 mimo-usage
#
# 初始化配置文件已打入镜像（/app/config.yaml，由 config.yaml.example 生成，凭据留空）。
# 服务启动后凭证/续登结果会就地写回该文件，因此想保留会话续期需挂载覆盖：
#   docker run ... -v "$PWD/config.yaml:/app/config.yaml" mimo-usage

# ---- 构建阶段：只把包编译成 wheel（含 static/ 与 _crypto_node.cjs）----
FROM python:3.13-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY mimo_usage ./mimo_usage
RUN pip wheel --no-deps --wheel-dir /dist .

# ---- 运行阶段：仅 wheel 运行时依赖 + node ----
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MIMO_CONFIG=/app/config.yaml

# node 是密码续登（三级降级第③级）的加密助手依赖；不需要该能力可去掉这层。
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=build /dist /tmp/dist
RUN pip install --no-cache-dir /tmp/dist/mimo_usage-*.whl \
    && rm -rf /tmp/dist

# 初始化配置文件打入镜像：凭据全部为空，续登换发的凭证会自动写回本文件
COPY config.yaml.example /app/config.yaml
RUN chmod 600 /app/config.yaml

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, sys, urllib.request; port=os.getenv('MIMO_PORT', '8000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % port, timeout=4).status == 200 else 1)"

CMD ["mimo-usage"]
