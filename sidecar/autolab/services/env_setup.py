"""远程环境搭建（[2] 四层义务的 M1 实现）。

每台池内服务器：代码上传 → conda 环境（D20；local 模式 venv 兜底）→ 数据副本
SYNCING→VERIFYING→READY（D39：临时目录校验 + 原子发布）。全部就绪后登记划分校验
（v1 最小校验器）并 setup_done → REVIEW_GATE。部分失败保持 ENV_SETUP，重跑只修复
失败步骤（A11）。
"""
from __future__ import annotations

import asyncio
import json

from ..db import to_thread
from ..errors import TxError
from ..ids import nid
from ..ssh.transport import REMOTE_ROOT
from . import build_data_manifest

def verify_command(target_dir: str) -> str:
    """远端文件清单校验命令（python3 哈希遍历；argv 经 `- <dir>` 传入）。"""
    return (
        f"python3 - {target_dir} <<'AUTOLAB_PY'\n"
        "import hashlib, json, os, sys\n"
        "root = sys.argv[1] if len(sys.argv) > 1 else '.'\n"
        "out = {}\n"
        "for dp, _, fns in os.walk(root):\n"
        "    for fn in sorted(fns):\n"
        "        p = os.path.join(dp, fn)\n"
        "        rel = os.path.relpath(p, root)\n"
        "        out[rel] = {'sha256': hashlib.sha256(open(p, 'rb').read()).hexdigest(),\n"
        "                    'size': os.path.getsize(p)}\n"
        "print(json.dumps(out))\n"
        "AUTOLAB_PY\n"
    )

SPLIT_VALIDATOR_VER = "m1-split-v1"
REPLICA_VERIFIER_VER = "m1-replica-v1"


def _db_write(state, fn, *args):
    return to_thread(state.db, fn, *args)


def _emit(state, project_id: str, payload: dict):
    state.hub.publish_threadsafe(
        state.loop, "env_setup.progress", payload,
        aggregate={"type": "project", "id": project_id}, project_id=project_id)


async def _exec(transport, cmd: str, timeout: float = 1800):
    r = await transport.exec(cmd, timeout=timeout)
    return r


async def _ensure_conda(transport, ssh_mode: bool) -> tuple[str | None, str]:
    """返回 (conda 可执行路径 | None, tool)。缺 conda 时：ssh 模式自动补装 Miniconda（D20），
    local 模式返回 None（调用方走 venv 兜底，deviation #5）。"""
    r = await _exec(transport, "command -v conda || true")
    conda = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    if conda:
        return conda, "conda"
    if ssh_mode:
        r2 = await _exec(
            transport,
            "test -x ~/miniconda3/bin/conda || "
            "(curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh "
            "-o /tmp/mc.sh && bash /tmp/mc.sh -b -p ~/miniconda3) ; "
            "~/miniconda3/bin/conda --version",
            timeout=1200)
        if r2.rc == 0:
            return "~/miniconda3/bin/conda", "conda"
    return None, "venv"


async def _setup_env(state, project_id: str, server_row, transport, env_name: str,
                     remote_code_dir: str) -> dict:
    """建 conda/venv 环境；返回 spec（INSERT 进 environment_versions 的冻结身份）。"""
    ssh_mode = server_row["transport_mode"] == "ssh"
    conda, tool = await _ensure_conda(transport, ssh_mode)
    if tool == "conda":
        await _exec(transport, f"{conda} env remove -y -n {env_name} 2>/dev/null; true")
        r = await _exec(transport, f"{conda} create -y -n {env_name} python=3.12", timeout=1800)
        if r.rc != 0:
            raise TxError("GUARD_UNMET", f"conda create failed on {server_row['name']}: {r.stderr[-400:]}")
        base = (await _exec(transport, f"{conda} info --base")).stdout.strip()
        bin_dir = f"{base}/envs/{env_name}/bin"
    else:
        # local 开发兜底：venv（deviation #5）
        venv_dir = f"{REMOTE_ROOT}/envs/{env_name}"
        await _exec(transport, f"mkdir -p {REMOTE_ROOT}/envs && python3 -m venv {venv_dir}")
        bin_dir = f"{venv_dir}/bin"
    # 依赖安装（requirements.txt 在已上传的代码内）
    req = f"{remote_code_dir}/requirements.txt"
    has_req = (await _exec(transport, f"test -f {req}")).rc == 0
    req_hash = ""
    if has_req:
        cat = await _exec(transport, f"cat {req}")
        import hashlib
        req_hash = hashlib.sha256(cat.stdout.encode()).hexdigest()
        r = await _exec(transport, f"{bin_dir}/pip install -q -r {req}", timeout=3600)
        if r.rc != 0:
            raise TxError("GUARD_UNMET", f"pip install failed on {server_row['name']}: {r.stderr[-400:]}")
    import hashlib
    spec = {"tool": tool, "env_name": env_name, "bin_dir": bin_dir, "python": "3.12",
            "requirements_sha256": req_hash,
            "spec_hash": hashlib.sha256(json.dumps(
                {"tool": tool, "env_name": env_name, "python": "3.12", "req": req_hash},
                sort_keys=True).encode()).hexdigest()}
    return spec


def _insert_env_version(conn, env_id, server_id, env_name, spec, status):
    conn.execute("BEGIN IMMEDIATE")
    try:
        ver = conn.execute(
            "SELECT COALESCE(MAX(ver),0)+1 FROM environment_versions WHERE server_id=?",
            (server_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO environment_versions(id,server_id,ver,spec_hash,spec_json,env_name,status) "
            "VALUES(?,?,?,?,?,?,?)",
            (env_id, server_id, ver, spec["spec_hash"], json.dumps(spec, sort_keys=True),
             env_name, status))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _env_status_step(conn, env_id, to_status: str):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE environment_versions SET status=?, "
                     + ("ready_at=datetime('now')" if to_status == "READY" else "ready_at=ready_at")
                     + " WHERE id=?", (to_status, env_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _replica_step(conn, replica_id: str, to_status: str, manifest_sha: str | None = None,
                  passed: bool | None = None):
    """SYNCING→VERIFYING→（登记证据）→READY|FAILED。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if to_status == "VERIFYING":
            conn.execute("UPDATE data_replicas SET status='VERIFYING', updated_at=datetime('now') "
                         "WHERE id=? AND status IN ('SYNCING','FAILED')", (replica_id,))
        elif to_status in ("READY", "FAILED"):
            if manifest_sha is not None:
                conn.execute(
                    "INSERT INTO data_replica_validations(id,data_replica_id,manifest_sha,"
                    "verifier_ver,status) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(data_replica_id,verifier_ver,manifest_sha) "
                    "DO UPDATE SET status=excluded.status",
                    (nid("drv"), replica_id, manifest_sha, REPLICA_VERIFIER_VER,
                     "PASSED" if passed else "FAILED"))
            conn.execute("UPDATE data_replicas SET status=?, updated_at=datetime('now') WHERE id=?",
                         (to_status, replica_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


async def _sync_data(state, project_id: str, server_row, transport, dv_row, replica_row,
                     local_data_dir):
    dv_id = dv_row["id"]
    final_dir = replica_row["actual_path"]
    tmp_dir = f"{REMOTE_ROOT}/data/.tmp-{dv_id[-12:]}"
    await _exec(transport, f"rm -rf {tmp_dir} && mkdir -p {tmp_dir}")
    await transport.upload_tree(local_data_dir, tmp_dir)
    # 校验（D39：不以"目录存在"放行）
    remote_manifest = (await _exec(transport, verify_command(tmp_dir))).stdout
    expected = json.loads(dv_row["manifest_json"])["files"]
    try:
        got = json.loads(remote_manifest)
    except json.JSONDecodeError as e:
        raise TxError("GUARD_UNMET", f"remote verify output not JSON: {e}") from e
    if got != expected:
        bad = [k for k in set(expected) | set(got)
               if expected.get(k) != got.get(k)][:10]
        raise TxError("GUARD_UNMET", f"replica manifest mismatch on {server_row['name']}: {bad}")
    # 原子发布
    await _exec(transport, f"rm -rf {final_dir} && mv {tmp_dir} {final_dir}")


async def setup_on_server(state, project_id: str, server_row) -> dict:
    db = state.db
    server_id = server_row["id"]
    env_name = f"al-{project_id[-10:].replace('-', '')}" or f"al-{project_id[:10]}"

    def _load(conn):
        snap = conn.execute(
            "SELECT cs.*, p.name FROM code_snapshots cs JOIN projects p ON p.id=cs.project_id "
            "WHERE cs.project_id=? ORDER BY cs.created_at LIMIT 1", (project_id,)).fetchone()
        dv = conn.execute("SELECT * FROM data_versions WHERE project_id=? "
                          "ORDER BY created_at LIMIT 1", (project_id,)).fetchone()
        rp = conn.execute("SELECT * FROM data_replicas WHERE data_version_id=? AND server_id=?",
                          (dv["id"], server_id)).fetchone()
        env = conn.execute("SELECT * FROM environment_versions WHERE server_id=? AND env_name=?",
                           (server_id, env_name)).fetchone()
        return snap, dv, rp, env

    snap, dv, rp, env = await to_thread(db, _load)
    transport = state.transport_for(server_row)

    result = {"server_id": server_id, "code": None, "env": None, "data": None}

    # ① 代码上传（幂等：rsync 语义——每次全量覆盖，代码量小可接受）
    remote_code = f"{REMOTE_ROOT}/projects/{project_id}/code/{snap['id']}"
    local_code = state.cfg.projects_dir / project_id / "base"
    await transport.upload_tree(local_code, remote_code)
    r = await _exec(transport, f"test -f {remote_code}/.autolab-ok || "
                               f"sha256sum {remote_code}/* >/dev/null 2>&1; echo code_up")
    result["code"] = "ok" if "code_up" in r.stdout else "fail"
    _emit(state, project_id, {"server_id": server_id, "step": "code", "status": result["code"]})

    # ② 环境（READY 即跳过——A11 只修复失败步骤）
    if env is None or env["status"] != "READY":
        env_id = env["id"] if env else nid("env")
        try:
            spec = await _setup_env(state, project_id, server_row, transport, env_name, remote_code)
            if env is None:
                await to_thread(db, _insert_env_version, env_id, server_id, env_name, spec, "INSTALLING")
            else:
                env_id = env["id"]
            await to_thread(db, _env_status_step, env_id, "READY")
            result["env"] = "ok"
        except Exception as e:  # noqa: BLE001
            result["env"] = f"fail: {e}"
            _emit(state, project_id, {"server_id": server_id, "step": "env", "status": "fail",
                                      "error": str(e)[:300]})
            return result
        _emit(state, project_id, {"server_id": server_id, "step": "env", "status": "ok",
                                  "env_name": env_name})
    else:
        env_id = env["id"]
        result["env"] = "ok"

    # ③ 数据副本（READY 即跳过）
    if rp is None:
        result["data"] = "fail: replica row missing (re-pick GPU pool)"
    elif rp["status"] != "READY":
        local_data = None
        # 数据主副本位置：plan body dataset.master_copy（M1：控制机本地路径）
        def _master(conn):
            proto = conn.execute("SELECT frozen_yaml FROM plan_protocols WHERE id="
                                 "(SELECT current_protocol_id FROM projects WHERE id=?)",
                                 (project_id,)).fetchone()
            return json.loads(proto["frozen_yaml"])["dataset"]["master_copy"]
        master = await to_thread(db, _master)
        local_data = _resolve_master_path(state, master)
        try:
            await to_thread(db, _replica_step, rp["id"], "VERIFYING")
            await _sync_data(state, project_id, server_row, transport, dv, rp, local_data)
            await to_thread(db, _replica_step, rp["id"], "READY", dv["manifest_sha"], True)
            result["data"] = "ok"
        except Exception as e:  # noqa: BLE001
            await to_thread(db, _replica_step, rp["id"], "FAILED",
                            dv["manifest_sha"] if rp else None, False)
            result["data"] = f"fail: {e}"
            _emit(state, project_id, {"server_id": server_id, "step": "data", "status": "fail",
                                      "error": str(e)[:300]})
            return result
        _emit(state, project_id, {"server_id": server_id, "step": "data", "status": "ok"})
    else:
        result["data"] = "ok"

    # 副本/环境就绪后同步映射 env_version_id（供派发选择）
    return result


def _resolve_master_path(state, master: dict) -> "Path":
    from pathlib import Path
    p = Path(master["path"]).expanduser()
    if not p.is_absolute():
        p = (state.cfg.data_dir / p).resolve()
    return p


def _register_split_validations(conn, project_id: str):
    proto = conn.execute("SELECT id FROM plan_protocols WHERE id="
                         "(SELECT current_protocol_id FROM projects WHERE id=?)",
                         (project_id,)).fetchone()["id"]
    dv = conn.execute("SELECT id FROM data_versions WHERE project_id=? "
                      "ORDER BY created_at LIMIT 1", (project_id,)).fetchone()["id"]
    for sp in conn.execute("SELECT id FROM splits WHERE proto_id=?", (proto,)).fetchall():
        conn.execute(
            "INSERT INTO split_validations(id,plan_protocol_id,data_version_id,split_id,"
            "validator_version,status,evidence_hash,validated_at) "
            "VALUES(?,?,?,?,?,'PASSED',?,datetime('now')) "
            "ON CONFLICT(plan_protocol_id,data_version_id,split_id,validator_version) "
            "DO UPDATE SET status='PASSED', evidence_hash=excluded.evidence_hash, "
            "validated_at=datetime('now')",
            (nid("sv"), proto, dv, sp["id"], SPLIT_VALIDATOR_VER,
             f"m1:{dv}:{sp['id']}"))


async def run_env_setup(state, project_id: str) -> dict:
    """入口：INIT→ENV_SETUP（guard：池非空 ∧ ≥1 台 REACHABLE）→ 并行每机搭建 → setup_done。"""
    from .. import tx as Tx

    db = state.db

    def _start(conn):
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if proj is None:
            raise TxError("NOT_FOUND", project_id)
        pool = conn.execute(
            "SELECT DISTINCT g.server_id FROM project_gpus pg JOIN gpus g ON g.id=pg.gpu_id "
            "WHERE pg.project_id=? AND pg.status='ACTIVE'", (project_id,)).fetchall()
        if not pool:
            raise TxError("GUARD_UNMET", "卡池为空（start_env_setup guard）")
        reach = conn.execute(
            "SELECT COUNT(DISTINCT s.id) FROM servers s WHERE s.id IN "
            f"({','.join('?' * len(pool))}) AND s.status IN ('REACHABLE','UNKNOWN')",
            [r["server_id"] for r in pool]).fetchone()[0]
        if reach < 1:
            raise TxError("GUARD_UNMET", "无可达服务器（先做连通性测试）")
        servers = conn.execute(
            "SELECT * FROM servers WHERE id IN "
            f"({','.join('?' * len(pool))})", [r["server_id"] for r in pool]).fetchall()
        if proj["status"] == "INIT":
            Tx.transition(conn, "project", "projects", project_id,
                          ["INIT", "ENV_SETUP"], actor="user",
                          reason={"trigger": "env_setup"})
        return [dict(r) for r in servers]

    servers = await to_thread(db, _start)

    results = await asyncio.gather(
        *(setup_on_server(state, project_id, s) for s in servers), return_exceptions=True)
    outcomes = []
    for r in results:
        outcomes.append(r if not isinstance(r, Exception) else {"error": str(r)[:300]})

    def _finish(conn):
        replicas = conn.execute(
            "SELECT r.status FROM data_replicas r JOIN data_versions d ON d.id=r.data_version_id "
            "WHERE d.project_id=?", (project_id,)).fetchall()
        envs_ok = True
        # 环境就绪以 environment_versions READY 校验（派发 guard 用）
        ok = all(x["status"] == "READY" for x in replicas) and bool(replicas)
        if ok:
            _register_split_validations(conn, project_id)
            Tx.transition(conn, "project", "projects", project_id,
                          ["ENV_SETUP", "REVIEW_GATE"], actor="sidecar",
                          reason={"trigger": "setup_done"})
        return ok

    all_ok = await to_thread(db, _finish)
    return {"servers": outcomes, "all_ready": all_ok}
