"""Baseline 修复 Agent:训练失败后,Agent 自己 读日志→定位文件→打补丁→重跑。

定位(D-020):接入期的"诊断-修复"循环此前靠人工(读 traceback、写 shim、
本地化 HF 权重路径、写指标适配)。本模块把该循环产品化:
失败日志 + 接入体检事实 + traceback 涉及的源文件 → LLM 最小补丁 →
备份到 .autolab/patches/ → py_compile 语法门(失败回滚)→ 自动重跑 Baseline。
用户只在 Baseline 页点一次「Agent 修复并重跑」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import iteration, server_admin
from .credentials import CredentialStore
from .db import utc_now
from .repo import Repo


class BaselineFixError(RuntimeError):
    """面向用户的修复失败信息(可操作)。"""


_FIX_SYSTEM_PROMPT = """你是 AutoLab 的接入修复 Agent。Baseline 命令在真实 GPU 服务器上运行失败。任务:读失败日志、接入体检结果和相关源代码,给出**最小修复补丁**,让 baseline 命令能跑通并产出指标 JSON。

必须遵守:
1. 只输出一个 ```json``` 代码块,不要任何其他文字:
{"summary": "≤80字:问题诊断与修法", "root_cause": "根因一句话",
 "changes": [{"path": "相对路径", "action": "modify|create|delete", "reason": "为什么", "content": "完整新文件内容(delete 时省略)"}]}
2. changes 最多 4 个文件;只改与故障直接相关的代码;不重构、不改动无关逻辑。
3. 已知问题模式(按需采用):
   - 离线服务器:`from_pretrained("hf-id")`/任何 HF 下载都会失败。若上下文列出本地已有模型目录(models/...)或权重(checkpoints/...),把加载改为该本地路径;没有本地副本时如实说缺什么,不要编造。
   - 依赖版本过老缺 API:用 try/except 导入 + 数值等价 shim(如 LayerNorm2d → GroupNorm(1, C))。
   - 训练脚本不写指标文件:可创建一个适配脚本(或修改入口),训练/评估后把指标 JSON 写到 $AUTOLAB_OUTPUT_DIR/metrics.json(环境变量由系统注入;注意训练日志可能打到 stderr)。
   - Mac 打包残留的 ._* 二进制文件干扰 glob:可 action=delete 逐个列出。
   - shell/conda 兼容:激活链必须 bash;命令中的 python 应能解析到所选环境。
4. content 必须是该文件的**完整**新内容(不要 diff、不要省略号)。"""


def _tail(text: str, limit: int = 4500) -> str:
    return text[-limit:] if text and len(text) > limit else (text or "")


def _traceback_files(log: str, workdir: str) -> list[str]:
    """从 traceback 提取工作区内的 py 文件(相对路径,最多 3 个)。"""
    files: list[str] = []
    for match in re.finditer(r'File "([^"]+\.py)", line \d+', log or ""):
        path = match.group(1)
        if path.startswith(workdir.rstrip("/") + "/"):
            rel = path[len(workdir.rstrip("/")) + 1:]
            if rel not in files:
                files.append(rel)
    return files[:3]


def agent_fix_baseline(
    repo: Repo,
    credentials: CredentialStore,
    hostkeys_path: Path,
    project_id: str,
    *,
    agent_key_ref: str | None = None,
) -> dict[str, Any]:
    """读最近一次 Baseline 失败 → LLM 诊断补丁 → 备份+语法门+回滚 → 自动重跑。"""
    from . import llm as _llm

    project = repo.get_project(project_id)
    if project is None:
        raise BaselineFixError("项目不存在")
    workdir = (project.get("remote_workdir") or "").strip()
    if not workdir:
        raise BaselineFixError("项目还没有工作区")
    server = repo.get_server(project["server_id"])
    if server is None:
        raise BaselineFixError("项目所在服务器不存在")
    config = server_admin.baseline_config_defaults(project)
    datasets = server_admin.baseline_datasets(config)
    by_name = {d["name"]: d for d in datasets}
    # 联合基线:修复针对最近活动的数据集(它自己的 run 目录与命令)
    try:
        stored_run = json.loads(project.get("baseline_run_json") or "{}")
    except Exception:
        stored_run = {}
    active_name = stored_run.get("dataset") \
        or (datasets[0]["name"] if datasets else None)
    active = by_name.get(active_name) if active_name else None
    run_dir_rel = (active or {}).get("run_dir") or server_admin.BASELINE_RUN_DIR
    active_command = (active or {}).get("command") or config.get("command")

    ssh_transport = None
    try:
        if server["transport_mode"] == "ssh":
            ssh_transport = server_admin.connect_ssh(server, credentials, hostkeys_path)
        exec_sh = iteration._exec_sh(ssh_transport)

        run = server_admin.baseline_run_state(workdir, ssh_transport=ssh_transport,
                                               run_dir=run_dir_rel)
        if run.get("exit_code") is None and run.get("status") != "finished":
            # 工作区无运行痕迹(如 control 重启后)→ 回落 DB 里的最近一次记录
            try:
                stored = json.loads(project.get("baseline_run_json") or "{}")
            except Exception:
                stored = {}
            source = (stored.get("runs") or {}).get(active_name) \
                if isinstance(stored.get("runs"), dict) else None
            source = source or stored
            if source.get("status") == "finished":
                run = {"status": "finished", "pid": source.get("pid"),
                       "exit_code": source.get("exit_code"),
                       "log_tail": source.get("log_tail") or ""}
        if run["status"] == "running":
            raise BaselineFixError("Baseline 正在运行,等它结束后再修复")
        failure_log = run.get("log_tail") or ""
        diagnosable = (run.get("exit_code") not in (None, 0)
                       or "Traceback" in failure_log
                       or "未从训练日志解析" in failure_log)
        if not diagnosable:
            raise BaselineFixError(
                "没有可诊断的失败记录:先跑一次 Baseline,失败后再让 Agent 修")

        # 上下文:适配脚本日志(如有)+ 体检事实 + 本地资源 + traceback 涉及文件
        _, adapter_log = exec_sh(
            f"tail -c 4000 {workdir.rstrip('/')}/{run_dir_rel}"
            "/output/train_stdout.log 2>/dev/null")
        readiness = server_admin._collect_readiness_facts(exec_sh, workdir, config)
        readiness_lines = [f"- {c['label']}[{c['status']}]: {c['detail'][:100]}"
                           for c in server_admin.build_readiness_checks(readiness, config)]
        _, models_ls = exec_sh(f"ls {workdir}/models {workdir}/checkpoints 2>/dev/null | head -12")
        _, envs_line = exec_sh(
            "~/anaconda3/envs/*/bin/python --version 2>/dev/null | head -1; "
            f"test -x ~/anaconda3/envs/{config.get('conda_env')}/bin/python && echo env-ok")

        tb_files = _traceback_files(failure_log, workdir)
        file_blocks = []
        read = iteration._read_bytes
        for rel in tb_files:
            try:
                content = read(workdir, rel, ssh_transport).decode("utf-8", "replace")
                file_blocks.append(f"### 文件 {rel}\n```\n{content[:16000]}\n```")
            except Exception:
                continue
        entry = next((t for t in (active_command or "").split()
                      if t.endswith(".py") and "/" not in t), "")
        if entry and entry not in tb_files:
            try:
                content = read(workdir, entry, ssh_transport).decode("utf-8", "replace")
                file_blocks.append(f"### 入口 {entry}\n```\n{content[:12000]}\n```")
            except Exception:
                pass

        user_prompt = (
            f"## Baseline 命令(数据集:{active_name or 'default'})\n"
            f"`{active_command}`"
            f"(conda 环境:{config.get('conda_env') or '系统 python'};"
            f"指标文件:{config.get('metric_file') or 'metrics.json'})\n\n"
            f"## 运行状态\nexit={run.get('exit_code')}\n\n"
            f"## 失败日志(尾部)\n```\n{_tail(failure_log)}\n```\n\n"
            + (f"## 适配脚本日志(尾部)\n```\n{_tail(adapter_log)}\n```\n\n"
               if adapter_log.strip() else "")
            + "## 接入体检\n" + "\n".join(readiness_lines) + "\n\n"
            + (f"## 工作区本地资源(models/ checkpoints/)\n{models_ls}\n\n"
               if models_ls.strip() else "")
            + "\n\n".join(file_blocks)
            + "\n\n请给出最小修复补丁(只输出 ```json``` 代码块)。"
        )

        messages = [{"role": "user", "content": user_prompt}]
        last_error = ""
        for _attempt in range(2):
            if last_error:
                messages.append({"role": "user",
                                 "content": f"上次补丁不可用:{last_error}"
                                            "请修正后重新输出完整 ```json```。"})
            try:
                reply = _llm.chat_multi(credentials, system=_FIX_SYSTEM_PROMPT,
                                        messages=messages, agent_key_ref=agent_key_ref,
                                        max_tokens=8000, timeout=240.0)
            except _llm.LlmError:
                raise
            try:
                result = iteration.parse_changes_payload(reply)
            except iteration.IterationError as exc:
                last_error = str(exc)
                continue

            import datetime as _dt
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            patch_abs = f"{workdir.rstrip('/')}/{iteration.PATCHES_REL}/{stamp}-fix"
            applied, deleted = [], []
            for change in result["changes"]:
                rel = change["path"]
                if change["action"] == "delete":
                    try:
                        original = iteration._read_bytes(workdir, rel, ssh_transport)
                        iteration._write_abs(f"{patch_abs}/orig/{rel}", original,
                                             ssh_transport)
                    except Exception:
                        pass
                    iteration._delete_abs(f"{workdir.rstrip('/')}/{rel}", ssh_transport)
                    deleted.append(rel)
                    continue
                iteration._write_patch(workdir, change, patch_abs, ssh_transport)
                applied.append(rel)
            errors = iteration._compile_check(
                workdir, [{"path": p} for p in applied],
                f"$HOME/anaconda3/envs/{config.get('conda_env')}/bin/python"
                if config.get("conda_env") else "python3",
                exec_sh)
            if errors:
                # 回滚本次全部改动,带错误进入修复轮
                for rel in reversed(applied):
                    try:
                        original = iteration._read_abs(f"{patch_abs}/orig/{rel}",
                                                       ssh_transport)
                        iteration._write_abs(f"{workdir.rstrip('/')}/{rel}",
                                             original, ssh_transport)
                    except Exception:
                        iteration._delete_abs(f"{workdir.rstrip('/')}/{rel}",
                                              ssh_transport)
                for rel in deleted:
                    try:
                        original = iteration._read_abs(f"{patch_abs}/orig/{rel}",
                                                       ssh_transport)
                        iteration._write_abs(f"{workdir.rstrip('/')}/{rel}",
                                             original, ssh_transport)
                    except Exception:
                        pass
                last_error = "语法检查失败:" + ";".join(errors)[:300]
                continue

            manifest = {"created_at": utc_now(), "kind": "baseline-fix",
                        "summary": result["summary"],
                        "root_cause": result.get("root_cause", ""),
                        "files": [{"path": c["path"], "action": c["action"]}
                                  for c in result["changes"]]}
            iteration._write_abs(
                f"{patch_abs}/manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=1).encode(),
                ssh_transport)
            repo.add_review_message(
                project_id=project_id, role="system", kind="text",
                content=f"Baseline 修复 Agent:{result['summary']}"
                        f"(改动 {len(result['changes'])} 处,备份 {iteration.PATCHES_REL}"
                        f"/{stamp}-fix)")
            # 自动重跑修复的数据集(修复的价值要用下一次运行验证),并落
            # running 状态供 UI 轮询;队列里剩余数据集随后自动续跑
            pid = server_admin.start_baseline_run(
                workdir, config, ssh_transport=ssh_transport,
                run_dir=run_dir_rel, command=active_command)
            queue = [n for n in stored_run.get("queue") or []
                     if n in by_name and n != active_name]
            repo.update_project(project_id, baseline_run_json=json.dumps({
                "dataset": active_name, "queue": queue,
                "status": "running", "pid": pid, "exit_code": None,
                "log_tail": "", "metrics": None,
                "runs": {k: v for k, v in (stored_run.get("runs") or {}).items()
                         if k != active_name},
                "started_at": utc_now(),
            }, ensure_ascii=False))
            return {"summary": result["summary"],
                    "rootCause": result.get("root_cause", ""),
                    "changes": manifest["files"],
                    "patchDir": f"{iteration.PATCHES_REL}/{stamp}-fix",
                    "compileOk": True, "rerunPid": pid}
        raise BaselineFixError(
            f"修复 Agent 两轮均未给出可用补丁:{last_error[:250]}。"
            "可重试,或把失败日志贴给维护者。")
    except server_admin.ServerAdminError as exc:
        raise BaselineFixError(str(exc)) from exc
    finally:
        if ssh_transport is not None:
            ssh_transport.close()
