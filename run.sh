#!/usr/bin/env bash
# 启动 MiMo 用量服务。运行配置与凭据集中在 config.yaml（见 config.yaml.example），
# 环境变量优先级更高，详见 .env.example。
set -euo pipefail

cd "$(dirname "$0")"

# config.yaml 是主配置载体：存在则自动指给进程（显式 MIMO_CONFIG 优先）
if [[ -z "${MIMO_CONFIG:-}" && -f config.yaml ]]; then
  export MIMO_CONFIG="$PWD/config.yaml"
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec python3 -m mimo_usage
