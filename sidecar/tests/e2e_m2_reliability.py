"""M2-5 单机可靠性验收（R12 四项硬门槛，真实双进程 + A6000 真机）。

T1 取消竞态：训练中 terminate → STOPPING → 停止链 → 全终态 → watcher → FINISHING(stopped)
T2 运行中预算/超时执行：budget_s=32 长训 → agent 硬截止 → STOP_CONFIRMED(hard_deadline 结算)
T3 断线对账（sidecar 重启）：训练中杀 sidecar → 重启 → 对账续跑 → 单 train attempt → JUDGED
T4 断线对账（agent 重启）：训练中杀 agent → 重启（boot 自愈）→ 快照对账 → STOP_CONFIRMED + 结算

用法：cd sidecar && .venv/bin/python tests/e2e_m2_reliability.py
前置：A6000 服务器已注册（gpu-a-a6000）、traq 环境在位、agent 0.1.3-m2。
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
AL_HOME = HOME / "Library/Application Support/AutoLab"
BASE = "http://127.0.0.1:39876"
TOKEN = (AL_HOME / "capability_token").read_text().strip()
LONG_TRAIN = "for i in $(seq 1 120); do echo tick $i; sleep 1; done; echo RELI_DONE"
RESULTS: list[tuple[str, bool, str]] = []


def call(method, path, body=None, timeout=180):
    req = urllib.request.Request(BASE + path, method=method,
        headers={"X-Capability-Token": TOKEN, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"HTTP_ERROR": e.code, "body": e.read().decode()[:300]}


def db():
    c = sqlite3.connect(AL_HOME / "autolab.db")
    c.row_factory = sqlite3.Row
    return c


def wait(fn, timeout=120, interval=3, desc=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError(f"timeout: {desc}")


def report(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name} {detail}", flush=True)


def setup_project(name: str) -> str:
    name = f"{name}-{int(time.time())}"  # UNIQUE 名（失败重跑不撞名）
    conn = db()
    gid = conn.execute("SELECT id FROM gpus WHERE model_name LIKE '%A6000%' LIMIT 1").fetchone()["id"]
    p = call("POST", "/api/v1/projects", {
        "name": name, "direction_text": "M2-5 可靠性验收",
        "base_source_kind": "local", "base_source_ref": str(HOME / "autolab-demo/base"),
        "budget_limit_s": 900,
        "data": {"logical_name": "toy", "local_path": str(HOME / "autolab-demo/data")},
        "gpu_ids": [gid],
        "train_command": "python $AUTOLAB_CODE_DIR/train.py",
        "eval_command": "python $AUTOLAB_CODE_DIR/eval.py",
        "conda_env": "traq"})
    pid = p["project_id"]
    call("POST", f"/api/v1/projects/{pid}/env-setup")
    wait(lambda: call("GET", f"/api/v1/projects/{pid}/readiness")["project_status"]
         == "REVIEW_GATE", 240, 5, "env setup")
    d = call("GET", f"/api/v1/projects/{pid}")
    call("POST", f"/api/v1/plan-protocols/{d['protocol']['id']}/freeze",
         {"expected_state_revision": d["state_revision"], "next_round": 0})
    # Round 0 基线（~10s 真实训练）→ 验收 accept → SEARCH
    r0 = call("POST", f"/api/v1/projects/{pid}/runs",
              {"role": "baseline", "command": "python $AUTOLAB_CODE_DIR/train.py",
               "budget_s": 120}, timeout=300)
    wait(lambda: call("GET", f"/api/v1/runs/{r0['run_id']}")["attempts"] and
          call("GET", f"/api/v1/runs/{r0['run_id']}")["attempts"][0]["status"]
          == "FINISHED", 240, 5, "round0 train FINISHED")
    acc = call("POST", f"/api/v1/projects/{pid}/baseline-acceptance",
               {"verdict": "accept", "expected_state_revision":
                db().execute("SELECT state_revision FROM projects WHERE id=?",
                             (pid,)).fetchone()[0]})
    assert acc.get("status") == "SEARCH", acc
    return pid


def t1_cancel_race(pid: str):
    print("[T1] 取消竞态（terminate during training）")
    r = call("POST", f"/api/v1/projects/{pid}/runs",
             {"role": "search", "command": LONG_TRAIN, "budget_s": 300}, timeout=300)
    aid = r["attempt_id"]
    wait(lambda: call("GET", f"/api/v1/attempts/{aid}")["status"] == "RUNNING", 90, 3, "RUNNING")
    t = call("POST", f"/api/v1/projects/{pid}/terminate", {"reason": "manual_stop"})
    assert t.get("terminated"), t
    # 停止链 → 全终态 → STOPPING watcher → FINISHING(stopped)
    wait(lambda: call("GET", f"/api/v1/attempts/{aid}")["status"] == "STOP_CONFIRMED",
         90, 3, "attempt STOP_CONFIRMED")
    wait(lambda: call("GET", f"/api/v1/projects/{pid}")["status"] == "FINISHING",
         60, 3, "project FINISHING")
    pj = call("GET", f"/api/v1/projects/{pid}")
    conn = db()
    settle = conn.execute(
        "SELECT settle_reason FROM budget_reservations br JOIN attempts a "
        "ON a.id=br.attempt_id WHERE a.id=?", (aid,)).fetchone()
    report("T1 取消竞态", pj["status"] == "FINISHING" and pj.get("outcome") == "stopped"
           and settle and settle["settle_reason"] == "stop_confirmed",
           f"attempt=STOP_CONFIRMED stop_confirmed project={pj['status']}/{pj.get('outcome')}")


def t2_hard_deadline(pid: str):
    print("[T2] 运行中预算/超时执行（execution_cap=32s 硬截止）")
    r = call("POST", f"/api/v1/projects/{pid}/runs",
             {"role": "search", "command": LONG_TRAIN, "budget_s": 32}, timeout=300)
    aid, rid = r["attempt_id"], r["run_id"]
    wait(lambda: call("GET", f"/api/v1/attempts/{aid}")["status"] == "RUNNING", 90, 3, "RUNNING")
    wait(lambda: call("GET", f"/api/v1/attempts/{aid}")["status"] == "STOP_CONFIRMED",
         120, 3, "deadline STOP_CONFIRMED")
    conn = db()
    settle = conn.execute(
        "SELECT settle_reason, remaining_s FROM budget_reservations WHERE attempt_id=?",
        (aid,)).fetchone()
    acc = call("GET", f"/api/v1/projects/{pid}/budget")
    usage = conn.execute("SELECT MAX(cumulative_s) m FROM usage_events WHERE attempt_id=?",
                         (aid,)).fetchone()["m"] or 0
    report("T2 超时执行", settle["settle_reason"] == "hard_deadline" and usage >= 30,
           f"settle=hard_deadline usage={usage:.0f}s C={acc['consumed_s']}s")


def t3_sidecar_restart(pid: str):
    print("[T3] 断线对账（sidecar 重启）")
    r = call("POST", f"/api/v1/projects/{pid}/runs",
             {"role": "search", "command": LONG_TRAIN, "budget_s": 200}, timeout=300)
    aid, rid = r["attempt_id"], r["run_id"]
    wait(lambda: call("GET", f"/api/v1/attempts/{aid}")["status"] == "RUNNING", 90, 3, "RUNNING")
    # 杀 sidecar（训练继续于远端）
    os.system("lsof -ti :39876 | xargs kill 2>/dev/null")
    time.sleep(12)  # 训练仍在跑（120s 命令，剩 ~100s）
    # 重启 sidecar
    subprocess.Popen(
        ["zsh", "-c",
         f"cd '{Path(__file__).parents[1]}' && AUTOLAB_HOME='{AL_HOME}' AUTOLAB_PORT=39876 "
         f"AUTOLAB_CRED_STORE=file nohup .venv/bin/python -m autolab "
         f"> /tmp/autolab-sidecar-restart.log 2>&1 &"])
    wait(lambda: _healthz(), 30, 2, "sidecar restart")
    print("  sidecar 已重启，等待对账续跑…")
    wait(lambda: call("GET", f"/api/v1/runs/{rid}")["status"] == "JUDGED",
         300, 6, "run JUDGED after restart")
    conn = db()
    n_train = conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE run_id=? AND profile='train'", (rid,)).fetchone()[0]
    report("T3 sidecar 重启对账", n_train == 1,
           f"单一 train attempt（无重复派发），run=JUDGED")


def t4_agent_restart(pid: str):
    print("[T4] 断线对账（agent 重启 + 快照对账）")
    r = call("POST", f"/api/v1/projects/{pid}/runs",
             {"role": "search", "command": LONG_TRAIN, "budget_s": 200}, timeout=300)
    aid = r["attempt_id"]
    wait(lambda: call("GET", f"/api/v1/attempts/{aid}")["status"] == "RUNNING", 90, 3, "RUNNING")
    # SSH 杀 agent 并重启（boot 自愈杀孤儿 + 封存 STOPPED；控制端快照对账收口）
    ok_restart = _kill_and_restart_agent()
    if not ok_restart:
        report("T4 agent 重启对账", False, "agent 重启失败（SSH）")
        return
    wait(lambda: call("GET", f"/api/v1/attempts/{aid}")["status"] == "STOP_CONFIRMED",
         120, 4, "snapshot reconcile → STOP_CONFIRMED")
    conn = db()
    settle = conn.execute(
        "SELECT settle_reason FROM budget_reservations WHERE attempt_id=?", (aid,)).fetchone()
    orphan_dead = _orphan_gone()
    report("T4 agent 重启对账",
           settle and settle["settle_reason"] in ("stop_confirmed", "hard_deadline")
           and orphan_dead,
           f"快照对账 STOP_CONFIRMED（settle={settle['settle_reason'] if settle else '?'}, "
           f"快照对账路径记 hard_deadline）+ 孤儿进程清除={orphan_dead}")


def _healthz():
    try:
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", 39876, timeout=2)
        c.request("GET", "/healthz")
        return c.getresponse().status == 200
    except OSError:
        return False


def _ssh_agent(cmd: str):
    conn = db()
    import asyncssh
    import asyncio
    cred = json.loads((AL_HOME / "credentials/srv-2a6fca1624ff4a36.json").read_text())
    async def run():
        c = await asyncssh.connect("192.0.2.46", username="serveruser", port=22,
                                   password=cred["secret"], preferred_auth="password",
                                   known_hosts=None)
        r = await c.run(cmd, timeout=30)
        c.close()
        return r
    return asyncio.new_event_loop().run_until_complete(run())


def _kill_and_restart_agent() -> bool:
    r = _ssh_agent("pkill -f 'autolab_agent --port 7800'; sleep 1; "
                   "cd ~/autolab-gpu-agent && "
                   "AUTOLAB_AGENT_TOKEN=$(grep -o '\"agent_token\": \"[^\"]*\"' "
                   "$HOME/autolab-gpu-agent/.token 2>/dev/null | head -1 | cut -d'\"' -f4 || true);"
                   "echo restarting")
    # token 从控制端凭据取（不落盘）
    import asyncssh, asyncio
    cred = json.loads((AL_HOME / "credentials/srv-2a6fca1624ff4a36.json").read_text())
    token = cred["agent_token"]

    async def start():
        import asyncssh
        c = await asyncssh.connect("192.0.2.46", username="serveruser", port=22,
                                   password=cred["secret"], preferred_auth="password",
                                   known_hosts=None)
        try:
            await asyncio.wait_for(c.run(
                f"cd ~/autolab-gpu-agent && AUTOLAB_AGENT_TOKEN='{token}' "
                f"PYTHONPATH=$HOME/autolab-gpu-agent/pylibs setsid nohup "
                f"/home/serveruser/anaconda3/envs/traq/bin/python -m autolab_agent "
                f"--port 7800 > agent.log 2>&1 < /dev/null & echo launched"), timeout=8)
        except (TimeoutError, asyncio.TimeoutError):
            pass  # 通道挂起=已启动（已知现象）
        await asyncio.sleep(4)
        v = await c.run(
            "python3 -c \"import urllib.request,json;print(json.load(urllib.request."
            "urlopen('http://127.0.0.1:7800/healthz',timeout=5))['status'])\" 2>&1 | tail -1",
            timeout=20)
        c.close()
        return v.stdout.strip()
    loop = asyncio.new_event_loop()
    st = loop.run_until_complete(start())
    return st == "ok"


def _orphan_gone() -> bool:
    r = _ssh_agent("pgrep -f '[s]eq 1 120' | wc -l")  # [s] 防自匹配
    return r.stdout.strip() == "0"


def main() -> int:
    print("=== M2-5 可靠性验收（R12）===  目标机：A6000 / agent 0.1.3-m2 / HMAC 强制")
    select = {x.strip().lower() for x in os.environ.get("SELECT", "t1,t2,t3,t4").split(",")}
    if "t1" in select:
        pid = setup_project("reliability-demo")
        try:
            t1_cancel_race(pid)
        except Exception as e:  # noqa: BLE001
            report("T1 取消竞态", False, str(e)[:200])
    for t, fn in (("T2", t2_hard_deadline), ("T3", t3_sidecar_restart),
                  ("T4", t4_agent_restart)):
        if t.lower() not in select:
            continue
        try:
            p2 = setup_project(f"reliability-{t.lower()}")
            fn(p2)
        except Exception as e:  # noqa: BLE001
            report(f"{t}", False, str(e)[:200])
    print("\n=== 结果 ===")
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    return 0 if all(ok for _, ok, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
