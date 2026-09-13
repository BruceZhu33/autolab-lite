"""执行器：进程组运行 + usage 采样 + execution_cap 硬截止 + 停止链。

一卡一任务（D15）：同 gpu_index 串行（前序终态后自动取下一个）。
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

from .store import Store

USAGE_SAMPLE_S = 5.0


class Runner:
    def __init__(self, store: Store, base_dir: Path):
        self.store = store
        self.base_dir = base_dir
        self._gpu_locks: dict[int, threading.Lock] = {}
        self._global = threading.Lock()
        self._stopping: set[str] = set()  # 停止链致死的 attempt（竞态裁决②：不算 failed）

    # ------------------------------------------------------------------
    def _lock_for(self, gpu_index: int) -> threading.Lock:
        with self._global:
            return self._gpu_locks.setdefault(gpu_index, threading.Lock())

    def submit(self, dispatch_id: str, manifest: dict):
        """受理后异步启动（同卡排队）。"""
        gpu_index = (manifest.get("gpu") or {}).get("index", 0)
        threading.Thread(target=self._run_guarded, args=(dispatch_id, manifest, gpu_index),
                         daemon=True, name=f"run-{dispatch_id[:12]}").start()

    def _run_guarded(self, dispatch_id: str, manifest: dict, gpu_index: int):
        lock = self._lock_for(gpu_index)
        with lock:
            at = self.store.get_attempt(dispatch_id)
            if at is None or at["status"] not in ("ACCEPTED", "REMOTE_QUEUED"):
                return  # 已终态/已被并发处理
            try:
                self._run(dispatch_id, manifest, gpu_index)
            except Exception as e:  # noqa: BLE001 —— 进程级异常转为 failed 事件
                self.store.update_attempt(dispatch_id, status="FAILED", rc=-1, ended_at=_now())
                self.store.emit(dispatch_id, "failed",
                                {"dispatch_id": dispatch_id, "rc": -1, "fail_class": "infra",
                                 "stderr_tail": f"agent runner error: {e}"[:500]})
            finally:
                self._drain_queue(gpu_index)

    def _drain_queue(self, gpu_index: int):
        """前序终态后，取同卡下一个 ACCEPTED 任务启动。"""
        for at in self.store.active_on_gpu(gpu_index):
            if at["status"] == "ACCEPTED":
                manifest = __import__("json").loads(at["manifest_json"])
                self.store.update_attempt(at["dispatch_id"], status="REMOTE_QUEUED")
                self.store.emit(at["dispatch_id"], "remote_start",
                                {"dispatch_id": at["dispatch_id"]})
                self.submit(at["dispatch_id"], manifest)
                break

    def _run(self, dispatch_id: str, manifest: dict, gpu_index: int):
        env = dict(os.environ)
        env_dir = (manifest.get("env") or {}).get("bin_dir")
        if env_dir:
            env["PATH"] = f"{env_dir}:{env.get('PATH', '')}"
            env["CONDA_DEFAULT_ENV"] = (manifest.get("env") or {}).get("env_name", "")
        code_dir = _expand((manifest.get("code") or {}).get("remote_path", ""))
        data_dir = _expand((manifest.get("data") or {}).get("remote_path", ""))
        out_dir = _expand((manifest.get("output_dir") or f"~/autolab/runs/{dispatch_id}"))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        env["AUTOLAB_CODE_DIR"] = code_dir
        env["AUTOLAB_DATA_DIR"] = data_dir
        env["AUTOLAB_OUT_DIR"] = out_dir
        env["AUTOLAB_ENV_NAME"] = env.get("CONDA_DEFAULT_ENV", "")
        env["AUTOLAB_DISPATCH_ID"] = dispatch_id
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
        log_path = Path(out_dir) / "log.txt"
        # 允许以代码目录为 cwd 的工作习惯：manifest.command 里的 $AUTOLAB_* 均可用
        command = manifest.get("command") or "python train.py"

        self.store.update_attempt(dispatch_id, status="REMOTE_QUEUED", out_dir=str(out_dir),
                                  log_path=str(log_path))

        with open(log_path, "ab", buffering=0) as logf:
            header = (f"=== AutoLab attempt {dispatch_id} ===\n"
                      f"gpu_index={gpu_index} cwd={out_dir}\n"
                      f"AUTOLAB_CODE_DIR={code_dir} AUTOLAB_DATA_DIR={data_dir}\n"
                      f"command: {command}\n")
            logf.write(header.encode())
            proc = subprocess.Popen(
                ["/bin/bash", "-lc", command],
                cwd=out_dir, env=env, stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True)
            self.store.update_attempt(dispatch_id, status="RUNNING", pid=proc.pid,
                                      started_at=_now())
            self.store.emit(dispatch_id, "running",
                            {"dispatch_id": dispatch_id, "pid": proc.pid})
            self._supervise(dispatch_id, proc, logf)

    def _supervise(self, dispatch_id: str, proc: subprocess.Popen, logf):
        cap = (self.store.get_attempt(dispatch_id) or {}).get("execution_cap_s")
        started = time.monotonic()
        usage_seq = 0
        stop_requested = False
        deadline_hit = False
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            elapsed = time.monotonic() - started
            self.store.update_attempt(dispatch_id, cumulative_s=round(elapsed, 1))
            usage_seq += 1
            self.store.emit(dispatch_id, "usage",
                            {"dispatch_id": dispatch_id, "usage_seq": usage_seq,
                             "cumulative_s": round(elapsed, 1),
                             "metering": "wall_clock_m1"})
            if cap is not None and elapsed >= cap:
                # ADD-5：远端硬截止（控制端失联不豁免）
                _kill_group(proc)
                deadline_hit = True
                proc.wait(timeout=30)
                break
            try:
                proc.wait(timeout=USAGE_SAMPLE_S)
            except subprocess.TimeoutExpired:
                continue
        final_elapsed = round(time.monotonic() - started, 1)
        rc = proc.returncode
        self.store.update_attempt(dispatch_id, cumulative_s=final_elapsed, rc=rc,
                                  ended_at=_now())
        if dispatch_id in self._stopping:
            # 停止信号致死（竞态裁决②）：终态事件由 stop() 统一发 stop_ack
            return
        tail = _tail(self.store.get_attempt(dispatch_id)["log_path"], 50)
        if deadline_hit:
            self.store.update_attempt(dispatch_id, status="STOPPED")
            self.store.emit(dispatch_id, "deadline_exceeded_ack",
                            {"dispatch_id": dispatch_id, "final_cumulative_s": final_elapsed,
                             "usage_seq": usage_seq + 1, "rc": rc, "reason": "execution_cap",
                             "tail": tail})
        elif rc == 0:
            self.store.update_attempt(dispatch_id, status="FINISHED")
            self.store.emit(dispatch_id, "finished",
                            {"dispatch_id": dispatch_id, "rc": 0,
                             "final_cumulative_s": final_elapsed, "usage_seq": usage_seq + 1,
                             "tail": tail})
        else:
            self.store.update_attempt(dispatch_id, status="FAILED")
            self.store.emit(dispatch_id, "failed",
                            {"dispatch_id": dispatch_id, "rc": rc,
                             "fail_class": _classify(rc),
                             "stderr_tail": tail,
                             "final_cumulative_s": final_elapsed, "usage_seq": usage_seq + 1})

    # ------------------------------------------------------------------
    def stop(self, dispatch_id: str) -> dict:
        at = self.store.get_attempt(dispatch_id)
        if at is None:
            return {"error": {"code": "NOT_FOUND", "message": dispatch_id}}
        if at["status"] in ("FINISHED", "FAILED", "STOPPED"):
            return {"dispatch_id": dispatch_id, "status": at["status"], "already_terminal": True}
        self._stopping.add(dispatch_id)
        if at["pid"]:
            try:
                os.kill(at["pid"], 0)
                _kill_group_by_pid(at["pid"])
                _wait_pid_gone(at["pid"], timeout=15)
            except (ProcessLookupError, PermissionError):
                pass
        # 等监督线程把最终累计量写回（≤采样间隔 + 宽限）
        deadline = time.monotonic() + 20
        final = at["cumulative_s"]
        while time.monotonic() < deadline:
            fresh = self.store.get_attempt(dispatch_id)
            if fresh["status"] in ("FINISHED", "FAILED", "STOPPED") or fresh["ended_at"]:
                final = fresh["cumulative_s"]
                break
            time.sleep(0.3)
            final = self.store.get_attempt(dispatch_id)["cumulative_s"]
        self.store.update_attempt(dispatch_id, status="STOPPED", ended_at=_now())
        self.store.emit(dispatch_id, "stop_ack",
                        {"dispatch_id": dispatch_id, "final_cumulative_s": final,
                         "usage_seq": 999999, "rc": None})
        return {"dispatch_id": dispatch_id, "status": "STOPPED"}


def _kill_group(proc: subprocess.Popen):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _kill_group_by_pid(pid: int):
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_pid_gone(pid: int, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        try:
            os.waitpid(pid, os.WNOHANG)  # noqa: SIM115 —— 非子进程时直接抛
        except (ChildProcessError, OSError):
            pass
        time.sleep(0.2)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _classify(rc: int | None) -> str:
    """D41 粗分类（M1：非零退出按载荷/退出码猜 infra；细化留给控制端人工/适配配置）。"""
    if rc is not None and rc < 0:
        return "infra"  # 信号致死（停止链/失联类）
    return "model"


def _tail(path: str, n: int) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def _expand(p: str) -> str:
    return os.path.expanduser(p)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
