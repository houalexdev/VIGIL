#!/usr/bin/env bash
# 启动 VIGIL 异常数据检测
set -e
cd "$(dirname "$0")"
PY=${PY:-$(command -v python3)}
PORT=${VIGIL_PORT:-8080}
echo "[vigil] python: $PY"
echo "[vigil] http://0.0.0.0:${PORT}"
exec "$PY" -m app.main
