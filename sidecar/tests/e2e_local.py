"""M1 单机纵切端到端验收（真实进程：sidecar + gpu-agent，local 传输模式）。

流程：agent 启动 → 服务器注册(local) → GPU 手工登记 → 连通性测试 → 立项（BASE 导入
+ 数据登记 + 卡池）→ 环境搭建（venv 兜底）→ 协议冻结（T-FREEZE 全量校验）→ 手动触发
训练（T-AUTHORIZE-DISPATCH）→ 监视（T-INGEST 事件流）→ 终态断言（预算结算/日志归档/
卡释放/状态机推进）。

用法：cd sidecar && AUTOLAB_E2E=1 .venv/bin/python tests/e2e_local.py
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
SIDECAR_DIR = ROOT / "sidecar"
AGENT_DIR = ROOT / "gpu-agent"
E2E_HOME = Path(os.environ.get("AUTOLAB_E2E_HOME", "/tmp/autolab-e2e"))
TEST_HOME = E2E_HOME / "home"          # LocalTransport 的 ~ 与 agent 输出根
AGENT_PORT = int(os.environ.get("AUTOLAB_E2E_AGENT_PORT", "7891"))
SIDECAR_PORT = int(os.environ.get("AUTOLAB_E2E_SIDECAR_PORT", "39873"))
AGENT_TOKEN = "e2e-agent-token-123"

BASE_DIR = E2E_HOME / "fixtures" / "base"
DATA_DIR = E2E_HOME / "fixtures" / "data"
TRAIN_CMD = 'for i in 1 2 3 4 5; do echo "epoch $i loss $(awk "BEGIN{print 1-$i*0.15}")"; sleep 1; done; echo TRAIN_DONE'


def log(msg: str):
    print(f"[e2e] {msg}", flush=True)


def wait_for(fn, timeout: float = 60, interval: float = 0.5, desc: str = ""):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timeout waiting for: {desc or fn} (last={last})")


def make_fixtures():
    shutil.rmtree(E2E_HOME, ignore_errors=True)
    BASE_DIR.mkdir(parents=True)
    DATA_DIR.mkdir(parents=True)
    (BASE_DIR / "README.md").write_text("# BASE\n", encoding="utf-8")
    (BASE_DIR / "train.py").write_text(
        "print('placeholder train entry')\n", encoding="utf-8")
    (DATA_DIR / "train.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    (DATA_DIR / "val.csv").write_text("a,b\n5,6\n", encoding="utf-8")


def start_agent() -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"AUTOLAB_AGENT_TOKEN": AGENT_TOKEN,
                "AUTOLAB_AGENT_HOME": str(E2E_HOME / "agent-home"),
                "HOME": str(TEST_HOME)})
    proc = subprocess.Popen(
        [str(AGENT_DIR / ".venv" / "bin" / "python"), "-m", "autolab_agent",
         "--port", str(AGENT_PORT)],
        cwd=AGENT_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    wait_for(lambda: _agent_up(), timeout=15, desc="agent /healthz")
    log(f"agent up on 127.0.0.1:{AGENT_PORT}")
    return proc


def _agent_up():
    try:
        r = httpx.get(f"http://127.0.0.1:{AGENT_PORT}/healthz", timeout=2)
        return r.status_code == 200 and r.json().get("status") == "ok"
    except httpx.HTTPError:
        return False


def start_sidecar() -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"AUTOLAB_HOME": str(E2E_HOME / "sidecar-home"),
                "AUTOLAB_PORT": str(SIDECAR_PORT),
                "AUTOLAB_CRED_STORE": "file"})
    proc = subprocess.Popen(
        [str(SIDECAR_DIR / ".venv" / "bin" / "python"), "-m", "autolab"],
        cwd=SIDECAR_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    runtime = Path(env["AUTOLAB_HOME"]) / "runtime.json"
    wait_for(lambda: runtime.exists() and _sidecar_up(runtime), timeout=20, desc="sidecar /healthz")
    log(f"sidecar up on 127.0.0.1:{SIDECAR_PORT}")
    return proc


def _sidecar_up(runtime: Path):
    try:
        r = httpx.get(f"http://127.0.0.1:{SIDECAR_PORT}/healthz", timeout=2)
        return r.status_code == 200 and r.json().get("db_ok")
    except httpx.HTTPError:
        return False


class Api:
    def __init__(self):
        rt = json.loads((E2E_HOME / "sidecar-home" / "runtime.json").read_text())
        self.base = f"http://127.0.0.1:{rt['port']}"
        self.token = (E2E_HOME / "sidecar-home" / "capability_token").read_text().strip()
        self.c = httpx.Client(base_url=self.base, timeout=30,
                              headers={"X-Capability-Token": self.token})

    def req(self, method: str, path: str, **kw):
        r = self.c.request(method, path, **kw)
        if r.status_code >= 400:
            raise AssertionError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json()


def main() -> int:
    make_fixtures()
    TEST_HOME.mkdir(parents=True, exist_ok=True)
    agent = start_agent()
    sidecar = start_sidecar()
    ok = False
    try:
        api = Api()

        # ---- 鉴权面：/healthz 免token，/api/v1 无 token 401 ----
        anon = httpx.get(f"http://127.0.0.1:{SIDECAR_PORT}/api/v1/meta", timeout=5)
        assert anon.status_code == 401, f"expected 401, got {anon.status_code}"
        meta = api.req("GET", "/api/v1/meta")
        assert meta["app"] == "AutoLab"
        log("auth: /healthz open, /api/v1 token-guarded ✓")

        # ---- 服务器注册（local 模式）----
        srv = api.req("POST", "/api/v1/servers", json={
            "name": "dev-local", "transport_mode": "local", "agent_port": AGENT_PORT,
            "agent_token": AGENT_TOKEN})
        server_id = srv["server_id"]
        gpu = api.req("POST", f"/api/v1/servers/{server_id}/gpus", json={
            "gpu_index": 0, "model_name": "cpu-fake-4090", "vram_mb": 24576})
        ct = api.req("POST", f"/api/v1/servers/{server_id}/connectivity-test")
        assert ct["ssh"]["ok"], ct
        probe = api.req("POST", f"/api/v1/servers/{server_id}/agent/probe")
        assert probe["ok"], probe
        assert probe["healthz"]["agent_instance_id"]
        log(f"server registered + agent probed (instance {probe['healthz']['agent_instance_id'][:12]}) ✓")

        # ---- 立项 ----
        proj = api.req("POST", "/api/v1/projects", json={
            "name": "e2e-m1", "direction_text": "M1 纵切验收",
            "base_source_kind": "local", "base_source_ref": str(BASE_DIR),
            "budget_limit_s": 600,
            "data": {"logical_name": "toy", "local_path": str(DATA_DIR)},
            "gpu_ids": [gpu["gpu_id"]],
            "train_command": TRAIN_CMD})
        pid = proj["project_id"]
        log(f"project created: {pid} (proto {proj['protocol_id'][:14]}…) ✓")

        # ---- 环境搭建 ----
        api.req("POST", f"/api/v1/projects/{pid}/env-setup")
        wait_for(lambda: api.req("GET", f"/api/v1/projects/{pid}/readiness")
                 .get("project_status") == "REVIEW_GATE", timeout=180, desc="env setup→REVIEW_GATE")
        detail = api.req("GET", f"/api/v1/projects/{pid}")
        assert all(r["status"] == "READY" for r in detail["replicas"])
        assert detail["env_versions"] and detail["env_versions"][0]["status"] == "READY"
        assert detail["split_validations"] and all(
            s["status"] == "PASSED" for s in detail["split_validations"])
        log("env setup: replicas READY + env READY + split validations PASSED ✓")

        # ---- 冻结（T-FREEZE 全量校验）----
        rev = detail["state_revision"]
        frz = api.req("POST", f"/api/v1/plan-protocols/{proj['protocol_id']}/freeze",
                      json={"expected_state_revision": rev, "next_round": 0})
        assert frz["project_status"] == "BASELINE_ACCEPTANCE"
        log("plan frozen → BASELINE_ACCEPTANCE ✓")

        # ---- 手动触发一轮训练 ----
        run = api.req("POST", f"/api/v1/projects/{pid}/runs", json={
            "role": "baseline", "command": TRAIN_CMD, "budget_s": 120})
        attempt_id, dispatch_id = run["attempt_id"], run["dispatch_id"]
        log(f"run {run['run_id'][:14]}… dispatched (attempt {attempt_id[:14]}…, "
            f"authorized {run['authorized_s']}s + margin {run['margin_s']}s) ✓")

        def attempt_status():
            return api.req("GET", f"/api/v1/attempts/{attempt_id}")["status"]
        wait_for(lambda: attempt_status() == "RUNNING", timeout=30, desc="attempt RUNNING")
        log("attempt RUNNING (T-INGEST: ack→remote_start→running) ✓")

        logs = api.req("GET", f"/api/v1/attempts/{attempt_id}/logs", params={"tail": 50})
        assert logs["source"] == "agent"
        wait_for(lambda: "epoch" in "".join(logs.get("lines", [])) or
                 api.req("GET", f"/api/v1/attempts/{attempt_id}/logs", params={"tail": 50})
                 .get("lines"), timeout=30, desc="log tail content")
        log("log tail streaming ✓")

        # ---- 终态 ----
        wait_for(lambda: attempt_status() in ("FINISHED", "FAILED"), timeout=60,
                 desc="attempt terminal")
        final = api.req("GET", f"/api/v1/attempts/{attempt_id}")
        assert final["status"] == "FINISHED", final
        assert final["usage_events"], "usage events recorded"
        run_detail = api.req("GET", f"/api/v1/runs/{run['run_id']}")
        assert run_detail["status"] == "EVAL_QUEUED", run_detail["status"]
        budget = api.req("GET", f"/api/v1/projects/{pid}/budget")
        assert budget["reserved_s"] == 0, f"settled: {budget}"
        assert budget["consumed_s"] > 0, budget
        assert budget["consumed_s"] + budget["reserved_s"] <= budget["limit_s"]
        assert run_detail["artifacts"], "log artifact archived"
        archived = api.req("GET", f"/api/v1/attempts/{attempt_id}/logs", params={"tail": 100})
        assert archived["source"] == "archive"
        gpus = api.req("GET", f"/api/v1/servers/{server_id}/gpus")
        assert gpus[0]["status"] == "IDLE", gpus
        log(f"terminal: attempt FINISHED, run EVAL_QUEUED, budget C={budget['consumed_s']}s "
            f"R={budget['reserved_s']}s, log archived, gpu IDLE ✓")

        # ---- 预算硬上限：C+R≤B 派发拒绝路径 ----
        acc = api.req("GET", f"/api/v1/projects/{pid}/budget")
        red = api.req("POST", f"/api/v1/projects/{pid}/budget/reduction", json={
            "new_limit_s": acc["consumed_s"], "idempotency_key": "e2e-red-1",
            "expected_budget_revision": acc["revision"]})
        assert red["phase"] == "achieved", red
        try:
            api.req("POST", f"/api/v1/projects/{pid}/runs", json={
                "role": "baseline", "command": TRAIN_CMD, "budget_s": 500})
            raise AssertionError("dispatch beyond budget should fail")
        except AssertionError as e:
            if "should fail" in str(e):
                raise
        except Exception:
            pass  # 422 BUDGET_INSUFFICIENT 预期
        log("budget invariant: post-reduction dispatch rejected (C+R≤B) ✓")

        ok = True
        return 0
    finally:
        for p in (sidecar, agent):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        time.sleep(1)
        for p in (sidecar, agent):
            if p.poll() is None:
                p.kill()
        log(f"result: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    sys.exit(main())
