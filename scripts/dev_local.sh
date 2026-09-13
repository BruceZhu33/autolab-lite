#!/usr/bin/env bash
# AutoLab 本地自测：gpu-agent + sidecar + UI 一键起停（Ctrl+C 全停）
#
# 用法：
#   bash scripts/dev_local.sh                        # 默认端口/令牌，数据目录用平台默认
#   AUTOLAB_HOME=/tmp/al bash scripts/dev_local.sh   # 指定数据目录（干净重测）
#
# 前置：sidecar/.venv 与 gpu-agent/.venv 已 uv sync；ui 已 pnpm install。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

AGENT_PORT="${AUTOLAB_AGENT_PORT:-7890}"
SIDECAR_PORT="${AUTOLAB_PORT:-39876}"
UI_PORT="${UI_PORT:-3100}"
AGENT_TOKEN="${AUTOLAB_AGENT_TOKEN:-autolab-dev-token}"

if [[ -z "${AUTOLAB_HOME:-}" ]]; then
  case "$(uname -s)" in
    Darwin) export AUTOLAB_HOME="$HOME/Library/Application Support/AutoLab" ;;
    Linux)  export AUTOLAB_HOME="$HOME/.local/share/autolab" ;;
    *)      export AUTOLAB_HOME="$HOME/AppData/Roaming/AutoLab" ;;
  esac
fi

[[ -x "$ROOT/gpu-agent/.venv/bin/python" ]] || { echo "缺 gpu-agent venv：cd gpu-agent && uv sync"; exit 1; }
[[ -x "$ROOT/sidecar/.venv/bin/python"   ]] || { echo "缺 sidecar venv：cd sidecar && uv sync --extra dev"; exit 1; }
command -v pnpm >/dev/null || { echo "缺 pnpm：npm install -g pnpm"; exit 1; }

# 演示夹具（可直接在立项表单里引用；目录名带 autolab-demo，便于辨认/清理）
DEMO="$HOME/autolab-demo"
mkdir -p "$DEMO/base" "$DEMO/data"
cat > "$DEMO/base/train.py" <<'PY'
import os, time, math
print("demo train | code:", os.environ.get("AUTOLAB_CODE_DIR"),
      "data:", os.environ.get("AUTOLAB_DATA_DIR"), "out:", os.environ.get("AUTOLAB_OUT_DIR"))
for i in range(1, 11):
    print(f"epoch {i} loss {1.0/math.sqrt(i):.4f}")
    time.sleep(0.8)
print("TRAIN_DONE")
PY
printf 'a,b\n1,2\n3,4\n' > "$DEMO/data/train.csv"
printf 'a,b\n5,6\n' > "$DEMO/data/val.csv"

PIDS=()
cleanup() {
  echo; echo "[dev] stopping..."
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  lsof -ti ":${UI_PORT}" 2>/dev/null | xargs kill 2>/dev/null || true
  exit 0
}
trap cleanup EXIT INT TERM

echo "[dev] gpu-agent : 127.0.0.1:${AGENT_PORT}  (token: ${AGENT_TOKEN})"
( cd "$ROOT/gpu-agent" && AUTOLAB_AGENT_TOKEN="${AGENT_TOKEN}" \
    .venv/bin/python -m autolab_agent --port "${AGENT_PORT}" ) &
PIDS+=($!)
sleep 1

echo "[dev] sidecar   : 127.0.0.1:${SIDECAR_PORT}  (home: ${AUTOLAB_HOME})"
( cd "$ROOT/sidecar" && AUTOLAB_PORT="${SIDECAR_PORT}" AUTOLAB_CRED_STORE="${AUTOLAB_CRED_STORE:-file}" \
    .venv/bin/python -m autolab ) &
PIDS+=($!)
sleep 3

echo "[dev] ui        : http://localhost:${UI_PORT}"
( cd "$ROOT/ui" && pnpm dev --port "${UI_PORT}" ) &
PIDS+=($!)

sleep 8
echo
echo "============================================================"
echo " 打开 http://localhost:${UI_PORT} 自测："
echo "   P1 服务器页: 注册 local 模式服务器 (agent 端口 ${AGENT_PORT} / 令牌 ${AGENT_TOKEN})"
echo "   P3 立项页  : BASE=${DEMO}/base  数据=${DEMO}/data"
echo "   P5 监视页  : 触发训练看日志尾/预算/停止链"
echo " 演示夹具已生成于 ${DEMO}"
echo "============================================================"
wait
