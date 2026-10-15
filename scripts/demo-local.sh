#!/usr/bin/env bash
# 一条命令体验 M1 本地闭环：起 Runner + Control 双进程，走完
# 登记服务器 → 建项目 → 写实验记录 → 建 Plan → 人工批准 → 3 个 Job 串行执行
# → 看产物和指标。
#
# 用法：
#   scripts/demo-local.sh          # 交互模式：批准时要亲手输入 spec hash 短码
#   scripts/demo-local.sh --auto   # 自动模式（测试 owner capability，无需输入）
set -euo pipefail
cd "$(dirname "$0")/.."

AUTO="${1:-}"
if [[ "$AUTO" != "--auto" && "$AUTO" != "" ]]; then
  echo "usage: scripts/demo-local.sh [--auto]" >&2; exit 2
fi

WORK="$(mktemp -d /tmp/autolab-demo-XXXXXX)"
PROJ="$WORK/work/demo"; OUT="$WORK/out"; mkdir -p "$PROJ" "$OUT"
DB="$WORK/control.db"; CREDS="$WORK/credentials.json"
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
HMAC="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"

cat > "$PROJ/train.py" <<'PY'
import json, os, sys, time
seed = int(sys.argv[sys.argv.index("--seed") + 1])
out = os.environ["AUTOLAB_OUTPUT_DIR"]; os.makedirs(out, exist_ok=True)
for epoch in range(1, 4):
    print(f"seed={seed} epoch={epoch} loss={0.5/epoch:.3f}", flush=True); time.sleep(0.2)
with open(f"{out}/metrics.json", "w") as fh:
    json.dump({"val_dice": round(0.80 + seed/100, 4)}, fh)
open(f"{out}/model.ckpt", "wb").write(f"weights-seed-{seed}".encode())
print("done", flush=True)
PY

cat > "$WORK/runner.json" <<EOF
{"listen_host":"127.0.0.1","listen_port":$PORT,"token":"$TOKEN","manifest_hmac_key":"$HMAC",
 "allowed_work_roots":["$WORK/work"],"allowed_output_roots":["$OUT"],
 "data_dir":"$WORK/runner-data","gpu_slots":1,"test_mode":true}
EOF
chmod 600 "$WORK/runner.json"

cleanup() { [[ -n "${CTRL_PID:-}" ]] && kill "$CTRL_PID" 2>/dev/null || true
            [[ -n "${RUN_PID:-}" ]] && kill "$RUN_PID" 2>/dev/null || true; }
trap cleanup EXIT

echo "== 工作目录: $WORK"
echo "== 启动 Runner (127.0.0.1:$PORT) 与 Control 调度进程"
uv run autolab-runner --config "$WORK/runner.json" > "$WORK/runner.log" 2>&1 & RUN_PID=$!
uv run autolab-control serve --db "$DB" --credentials "$CREDS" --poll-interval 0.2 \
  > "$WORK/control.log" 2>&1 & CTRL_PID=$!

for _ in $(seq 1 100); do
  python3 -c "import urllib.request as u;u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:$PORT/healthz',timeout=1)" 2>/dev/null && break
  sleep 0.1
done

export AUTOLAB_DB="$DB" AUTOLAB_CREDENTIALS="$CREDS"
say() { echo; echo "==> $*"; }

say "登记本地服务器与项目"
SRV=$(uv run autolab server add-local --name demo-local --runner-port "$PORT" \
        --token "$TOKEN" --hmac-key "$HMAC" --gpu-slots 1 \
        --work-root "$WORK/work" --output-root "$OUT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["server"]["id"])')
PID=$(uv run autolab project add --name demo --server "$SRV" \
        --workdir "$PROJ" --output-root "$OUT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["project"]["id"])')

say "初始化 .autolab 科研记录并创建实验（含事前成功判据）"
uv run autolab research init "$PROJ" --project-id "$PID" >/dev/null
uv run autolab experiment create "$PROJ" --title "seed sweep on demo dataset" \
  --hypothesis "Seeds 1-3 all reach val_dice >= 0.81." \
  --change "First run; establishes the baseline." \
  --baseline "No baseline yet." \
  --metrics "demo dataset, val split, val_dice, maximize, mean over seeds." \
  --success "every seed reaches val_dice >= 0.81" \
  --risk "If a seed fails, inspect its metrics.json first." >/dev/null
uv run autolab research validate "$PROJ" --project-id "$PID" >/dev/null && echo "research validate: OK"

say "创建 DRAFT Plan（3 seeds 矩阵）并预览"
PLAN_JSON=$(uv run autolab plan create --project-id "$PID" --experiment-id EXP-0001 \
  --command "python train.py --seed {{seed}}" --matrix '{"seed":[1,2,3]}' \
  --timeout 120 --max-parallel 1 --artifact metrics.json --artifact '*.ckpt')
PLAN=$(echo "$PLAN_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["plan"]["id"])')
HASH=$(echo "$PLAN_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["plan"]["spec_hash"])')
uv run autolab plan preview "$PLAN" | python3 -m json.tool

say "批准 Plan（owner gate）"
if [[ "$AUTO" == "--auto" ]]; then
  AUTOLAB_TEST_MODE=1 uv run autolab plan approve "$PLAN" --expect-spec-hash "$HASH" >/dev/null
  echo "auto 模式：已用测试 owner capability 批准"
else
  uv run autolab plan approve "$PLAN"    # 会打印摘要，要求输入 hash 前 12 位
fi

say "等待 3 个 Job 串行执行完成（max_parallel=1）"
for _ in $(seq 1 300); do
  DONE=$(uv run autolab job list | python3 -c 'import json,sys;j=json.load(sys.stdin)["jobs"];print(sum(1 for x in j if x["status"] in ("SUCCEEDED","FAILED","CANCELLED","TIMED_OUT")))' )
  [[ "$DONE" == "3" ]] && break
  sleep 0.5
done

say "Job 结果"
uv run autolab job list | python3 -c '
import json, sys
for j in json.load(sys.stdin)["jobs"]:
    print("  %s. %s… %s exit=%s %s->%s dispatch=%s…" % (
        j["ordinal"], j["id"][:24], j["status"], j["exit_code"],
        j["started_at"][11:19], j["finished_at"][11:19], j["dispatch_id"][:20]))'
FIRST=$(uv run autolab job list | python3 -c 'import json,sys;print(json.load(sys.stdin)["jobs"][0]["id"])')

say "产物与指标（job show ${FIRST}）"
uv run autolab job show "$FIRST" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("  observation:", d["observation"])
for a in d["artifacts"]:
    print("  %-10s %s  sha256=%s…  metrics=%s" % (
        a["kind"], a["remote_path"], a["sha256"][:16],
        a["metadata"].get("metric_values", {})))'

say "增量日志（最后 3 行）"
uv run autolab job logs "$FIRST" | tail -3

echo
echo "== 完成。数据保留在 ${WORK}（删除：rm -rf ${WORK}）"
