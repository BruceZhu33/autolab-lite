#!/usr/bin/env bash
# 一键打开 AutoLab 薄控制台（真实 Runner + Control + Web 三进程）。
# 看板会先跑完一个 3-seed 计划（recently_completed），并留下一个待批准的
# DRAFT 计划——用页面上的「批准」按钮、粘贴 spec hash 亲自走一遍 owner gate。
#
# 用法：scripts/demo-web.sh [--auto]   # --auto 连待批准计划也自动批掉
set -uo pipefail
cd "$(dirname "$0")/.."

AUTO="${1:-}"
WORK="$(mktemp -d /tmp/autolab-web-XXXXXX)"
PROJ="$WORK/work/demo"; OUT="$WORK/out"; mkdir -p "$PROJ" "$OUT"
DB="$WORK/control.db"; CREDS="$WORK/credentials.json"
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
RTOK="$(python3 -c 'import secrets;print(secrets.token_hex(16))')"
RHMAC="$(python3 -c 'import secrets;print(secrets.token_hex(16))')"
OWNER_TOKEN="owner-$(python3 -c 'import secrets;print(secrets.token_hex(8))')"
AGENT_TOKEN="agent-$(python3 -c 'import secrets;print(secrets.token_hex(8))')"
API_PORT=8710; WEB_PORT=3430

cat > "$PROJ/train.py" <<'PY'
import json, os, sys, time
seed = int(sys.argv[sys.argv.index("--seed") + 1])
out = os.environ["AUTOLAB_OUTPUT_DIR"]; os.makedirs(out, exist_ok=True)
for i in range(3):
    print(f"[seed {seed}] log line {i}", flush=True); time.sleep(0.4)
json.dump({"val_dice": round(0.80 + seed/100, 4)}, open(f"{out}/metrics.json", "w"))
open(f"{out}/model.ckpt", "wb").write(f"weights-{seed}".encode())
print("done", flush=True)
PY

cat > "$WORK/runner.json" <<EOF
{"listen_host":"127.0.0.1","listen_port":$PORT,"token":"$RTOK","manifest_hmac_key":"$RHMAC",
 "allowed_work_roots":["$WORK/work"],"allowed_output_roots":["$OUT"],
 "data_dir":"$WORK/runner-data","gpu_slots":1,"test_mode":true}
EOF
chmod 600 "$WORK/runner.json"

uv run python - "$CREDS" "$RTOK" "$RHMAC" "$OWNER_TOKEN" "$AGENT_TOKEN" <<'PY'
import sys
from autolab_control.credentials import CredentialStore
from autolab_control.http_api import API_REF
creds = CredentialStore(sys.argv[1])
creds.put("demo-local:runner", runner_token=sys.argv[2], manifest_hmac_key=sys.argv[3])
creds.put_entry(API_REF, {"owner_token": sys.argv[4], "agent_token": sys.argv[5]})
PY

cleanup() { [[ -n "${WEB_PID:-}" ]] && kill "$WEB_PID" 2>/dev/null || true
            [[ -n "${CTRL_PID:-}" ]] && kill "$CTRL_PID" 2>/dev/null || true
            [[ -n "${RUN_PID:-}" ]] && kill "$RUN_PID" 2>/dev/null || true; }
trap cleanup EXIT

echo "== 工作目录: $WORK"
uv run autolab-runner --config "$WORK/runner.json" > "$WORK/runner.log" 2>&1 & RUN_PID=$!
uv run autolab-control serve --db "$DB" --credentials "$CREDS" \
  --poll-interval 0.2 --http-port $API_PORT > "$WORK/control.log" 2>&1 & CTRL_PID=$!
for _ in $(seq 1 100); do
  python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:$PORT/healthz',timeout=1)" 2>/dev/null && break
  sleep 0.1
done

export AUTOLAB_DB="$DB" AUTOLAB_CREDENTIALS="$CREDS"
SRV=$(uv run autolab server add-local --name demo-local --runner-port "$PORT" \
  --token "$RTOK" --hmac-key "$RHMAC" --gpu-slots 1 \
  --work-root "$WORK/work" --output-root "$OUT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["server"]["id"])')
PID=$(uv run autolab project add --name demo --server "$SRV" --workdir "$PROJ" --output-root "$OUT" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["project"]["id"])')
uv run autolab research init "$PROJ" --project-id "$PID" >/dev/null
uv run autolab experiment create "$PROJ" --title "seed sweep (auto run)" \
  --hypothesis "Seeds 1-3 all reach val_dice >= 0.81." --change "first run" \
  --baseline "none yet" --metrics "demo, val_dice, maximize" \
  --success "every seed >= 0.81" --risk "inspect metrics.json" >/dev/null

mkplan() { uv run autolab plan create --project-id "$PID" --experiment-id "$1" \
  --command "python train.py --seed {{seed}}" --matrix '{"seed":[1,2,3]}' \
  --timeout 120 --max-parallel 1 --artifact metrics.json --artifact '*.ckpt'; }

# 计划 A：自动批准并执行（看板会走到 recently_completed）
A=$(mkplan EXP-0001)
APID=$(echo "$A" | python3 -c 'import json,sys;print(json.load(sys.stdin)["plan"]["id"])')
AUTOLAB_TEST_MODE=1 uv run autolab plan approve "$APID" --expect-spec-hash \
  "$(echo "$A" | python3 -c 'import json,sys;print(json.load(sys.stdin)["plan"]["spec_hash"])')" >/dev/null

# 计划 B：留在 needs_plan，等你在页面上亲手批准（--auto 则自动批掉）
uv run autolab experiment create "$PROJ" --title "rerun sweep (awaiting you)" \
  --hypothesis "Same config reproduces." --change "rerun" --baseline "EXP-0001" \
  --metrics "demo, val_dice, maximize" --success "reproduces >= 0.81" \
  --risk "check drift" --parent EXP-0001 >/dev/null
B=$(mkplan EXP-0002)
if [[ "$AUTO" == "--auto" ]]; then
  AUTOLAB_TEST_MODE=1 uv run autolab plan approve \
    "$(echo "$B" | python3 -c 'import json,sys;print(json.load(sys.stdin)["plan"]["id"])')" \
    --expect-spec-hash "$(echo "$B" | python3 -c 'import json,sys;print(json.load(sys.stdin)["plan"]["spec_hash"])')" >/dev/null
fi

echo "== 启动 Web（http://127.0.0.1:${WEB_PORT}）"
( cd apps/web && [[ -f .next/BUILD_ID ]] || npm run build >/dev/null 2>&1
  npm run start > "$WORK/web.log" 2>&1 ) & WEB_PID=$!

cat <<EOF

──────────────────────────────────────────────────
 控制台:   http://127.0.0.1:$WEB_PORT
 API:      http://127.0.0.1:$API_PORT
 owner 码: $OWNER_TOKEN
 agent 码: $AGENT_TOKEN
 打开页面后粘贴 owner 码；看板「批准」按钮要求
 粘贴完整 spec hash（页面提示里有）。
 数据目录: ${WORK}（退出后保留，rm -rf 清理）
──────────────────────────────────────────────────
EOF
wait $WEB_PID
