#!/usr/bin/env bash
# 开发环境一键恢复:重启 demo 后,重新注册服务器/项目、跑通 Baseline、建评分 Agent、
# 迁移 GLM Key。用法:bash scripts/dev-restore.sh [旧workdir(取key用)]
set -euo pipefail
cd "$(dirname "$0")/.."

TOKEN=$(grep -oE "owner-[a-f0-9]+" /tmp/autolab-demo-*.log 2>/dev/null | tail -1 | cut -d: -f2)
if [ -z "$TOKEN" ]; then echo "找不到 owner token,先跑 demo-web.sh"; exit 1; fi
API=http://127.0.0.1:8710
AUTH="Authorization: Bearer $TOKEN"

WORK=$(ps aux | grep "autolab-control serve" | grep -v grep | grep -oE "/tmp/autolab-web-[^ /]+" | head -1)
echo "work: $WORK"

# GLM Key 迁移(从指定旧目录或最新含 key 的目录)
KEY_SRC="${1:-}"
if [ -n "$KEY_SRC" ] && [ -f "$KEY_SRC/credentials.json" ]; then
  :
else
  KEY_SRC=$(grep -l "chatagent" /tmp/autolab-web-*/credentials.json 2>/dev/null | xargs -I{} sh -c 'python3 -c "import json;d=json.load(\"open({})\".replace(\"open(\",\"open(\").replace(\")\",\"\"))) if False else None" 2>/dev/null; true'; true)
  KEY_SRC=$(for f in /tmp/autolab-web-*/credentials.json; do
    python3 -c "import json,sys;d=json.load(open('$f'));exit(0 if d.get('chatagent:api-key',{}).get('api_key') else 1)" 2>/dev/null && echo "$f" && break
  done | head -1 || true)
  # dirname "" 在 macOS 返回 "." 而非空串;空结果时直接清空,靠下方 -f 判断跳过
  KEY_SRC=$([ -n "$KEY_SRC" ] && dirname "$KEY_SRC" 2>/dev/null || true)
fi
if [ -n "$KEY_SRC" ] && [ -f "$KEY_SRC/credentials.json" ]; then
  uv run python - "$KEY_SRC" "$WORK" <<'PY'
import sys
from autolab_control.core.credentials import CredentialStore
src, dst = sys.argv[1], sys.argv[2]
try:
    key = CredentialStore(f"{src}/credentials.json").get_entry("chatagent:api-key")["api_key"]
    CredentialStore(f"{dst}/credentials.json").put_entry("chatagent:api-key", {"api_key": key})
    print("GLM key migrated")
except Exception as e:
    print("key migrate skip:", e)
PY
fi

post() { curl -s -m "${4:-120}" -X POST "$API$1" -H "$AUTH" -H "Content-Type: application/json" ${3:+-d "$3"}; }

# 服务器凭据从环境变量读(别硬编码进脚本/历史):
#   AUTOLAB_SSH_HOST / AUTOLAB_SSH_USER / AUTOLAB_SSH_PASSWORD
: "${AUTOLAB_SSH_HOST:?需要 AUTOLAB_SSH_HOST(共享 A6000 的 IP)}"
: "${AUTOLAB_SSH_USER:?需要 AUTOLAB_SSH_USER}"
: "${AUTOLAB_SSH_PASSWORD:?需要 AUTOLAB_SSH_PASSWORD}"
SRV=$(post /api/owner/servers '' "{\"name\":\"gpu-serveruser\",\"transport_mode\":\"ssh\",\"host\":\"$AUTOLAB_SSH_HOST\",\"ssh_port\":22,\"username\":\"$AUTOLAB_SSH_USER\",\"auth_kind\":\"password\",\"secret\":\"$AUTOLAB_SSH_PASSWORD\"}" 90 | python3 -c "import json,sys;print(json.load(sys.stdin)['server']['id'])")
echo "server: $SRV"
post /api/owner/projects '' "{\"name\":\"medseg-demo\",\"server_id\":\"$SRV\",\"workdir\":\"/home/serveruser/autolab-projects/demo\",\"local_workspace\":\"/Users/juan/autolab-dev/medseg-fake\"}" >/dev/null
PID=$(curl -s -H "$AUTH" $API/api/projects | python3 -c "import json,sys;print([p['id'] for p in json.load(sys.stdin) if p['name']=='medseg-demo'][0])")
echo "project: $PID"
post "/api/owner/projects/$PID/baseline-request" '' '{}' >/dev/null
post "/api/owner/projects/$PID/code/sync" '' '{"source":"local"}' 300 >/dev/null
post "/api/owner/projects/$PID/analyze" >/dev/null
post "/api/owner/projects/$PID/baseline/check" >/dev/null
post "/api/owner/projects/$PID/baseline/run" >/dev/null
sleep 12
curl -s -m 60 -H "$AUTH" "$API/api/projects/$PID/baseline" >/dev/null
post /api/owner/agents '' '{"name":"reviewer-1","system_prompt":"你是偏保守的方法审稿人,关注实验风险与算力成本"}' >/dev/null
post /api/owner/agents '' '{"name":"reviewer-2","system_prompt":"你是关注工程落地成本的审稿人,评估实现难度"}' >/dev/null
echo "owner token: $TOKEN"
curl -s -H "$AUTH" $API/api/projects | python3 -c "
import json,sys
for p in json.load(sys.stdin):
    print(p['name'], '→', p['stage'])"
