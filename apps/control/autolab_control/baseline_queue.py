"""联合基线:多数据集顺序运行队列 + 人工确认门 + 确认快照。

产品语义(owner 定):
- Baseline 配置是一份数据集清单(config.datasets);每个数据集自己的
  命令/数据路径/独立 run 目录(.autolab/baseline/<名>/)。
- 点「跑 Baseline」= 按清单顺序把还没跑通的数据集各跑一遍;某个数据集
  失败则队列停下(修复后可续跑),全部 跑通+有指标 才出现确认按钮。
- 确认通过时把各数据集的指标/命令快照进 baseline_confirmed_json——
  这份快照就是自动迭代的数据集可选池("基线跑了哪些,迭代才能选哪些")。

运行状态存 baseline_run_json:
{"dataset": 活动数据集, "queue": 待跑名单, "status": ..., "pid": ...,
 "runs": {名: {status, exit_code, metrics}}, "started_at": ...}
旧单数据集记录无 dataset/runs 键 → 读取时按配置合成,历史基线原样可用。

写入方唯一:本模块的 advance/tick(serve 进程后台线程)。HTTP 轮询只读
(SSH 现场刷新展示,不落库),避免并发双写导致重复起跑。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from . import server_admin
from .credentials import CredentialStore
from .db import utc_now
from .metric_sanity import metrics_sanity_issues
from .repo import Repo


class BaselineQueueError(RuntimeError):
    """面向用户的联合基线操作失败(可操作)。"""


# ------------------------------------------------------------------ 读取投影

def normalize_run(project: dict) -> dict:
    """baseline_run_json → 统一形状(旧记录按配置合成,不改库)。"""
    try:
        stored = json.loads(project.get("baseline_run_json") or "{}")
    except Exception:
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    if "dataset" in stored or "runs" in stored:
        run = {
            "dataset": stored.get("dataset"),
            "queue": [str(n) for n in stored.get("queue") or []],
            "status": stored.get("status") or "idle",
            "pid": stored.get("pid"),
            "exit_code": stored.get("exit_code"),
            "log_tail": stored.get("log_tail") or "",
            "progress_tail": stored.get("progress_tail") or "",
            "metrics": stored.get("metrics"),
            "sanity_note": stored.get("sanity_note") or "",
            "started_at": stored.get("started_at"),
            "runs": dict(stored.get("runs") or {}),
            "diagnosis": stored.get("diagnosis"),
            "fix_attempts": stored.get("fix_attempts") or 0,
            "progress": stored.get("progress"),
            "metrics_source": stored.get("metrics_source"),
            "seed": stored.get("seed"),
        }
        return run
    # 旧单数据集记录:归到配置合成的数据集名下
    config = server_admin.baseline_config_defaults(project)
    datasets = server_admin.baseline_datasets(config)
    name = datasets[0]["name"] if datasets else "default"
    run = {
        "dataset": name if stored.get("status") else None,
        "queue": [],
        "status": stored.get("status") or "idle",
        "pid": stored.get("pid"),
        "exit_code": stored.get("exit_code"),
        "log_tail": stored.get("log_tail") or "",
        "progress_tail": "",
        "metrics": stored.get("metrics"),
        "sanity_note": stored.get("sanity_note") or "",
        "started_at": stored.get("started_at"),
        "runs": {},
        "diagnosis": stored.get("diagnosis"),
        "fix_attempts": stored.get("fix_attempts") or 0,
    }
    if stored.get("status") == "finished":
        run["runs"][name] = {"status": "finished",
                             "exit_code": stored.get("exit_code"),
                             "metrics": stored.get("metrics")}
    return run


# ------------------------------------------------- 失败诊断与升级
# 自动修复不是无限重试的黑洞:修复重跑仍失败时,把"如何手动解决"明确交还
# 给用户——分类失败原因,给出具体步骤,而不是静默阻塞在页面上。

_FAILURE_PATTERNS: list[tuple[tuple[str, ...], str, str, list[str]]] = [
    (("huggingface.co", "couldn't connect"), "hf_offline", "离线环境下载 HF 模型失败", [
        "服务器连不上 huggingface.co。两种解法(推荐①):",
        "① 本地化:找服务器上已有的同名模型目录(zwm 副本 / ~/.cache/huggingface/hub),"
        "在 Baseline 体检的供给卡把它链接进工作区(如 models/<模型名>),"
        "再把代码里 from_pretrained(\"组织名/模型名\") 改为该本地路径(保留联网回退更好)",
        "② 有网机器下载整个模型目录(config.json + 权重 + tokenizer),打包上传到工作区后同样改路径",
        "注意:代码里可能不止一处写死模型名(模型加载 + tokenizer 各一处),全仓库搜索逐处确认",
    ]),
    (("AttributeError", "DATASET"), "config_broken", "训练配置损坏(配置段缺失/被误删)", [
        "典型的补丁误伤:configs/*.yaml 被删掉了 DATASET/MODEL/TEST 等整段。",
        "① 到工作区 .autolab/patches/<最近补丁>/orig/ 找该配置的原始备份,恢复后把需要的参数加回去;",
        "② 或让实现 Agent 重打补丁(补丁系统现已带 yaml 门,会拦截丢段写入);",
        "③ 修好后到 Iteration 页失败卡点「重试」。",
    ]),
    (("yaml.", "error"), "config_broken", "训练配置解析失败(yaml 语法错误)", [
        "configs/*.yaml 语法非法(缩进/冒号/引号)。",
        "对照 .autolab/patches/<最近补丁>/orig/ 的原始配置修正语法;修好后点「重试」。",
    ]),
    (("ModuleNotFoundError",), "module_missing", "缺少 Python 依赖", [
        "训练环境缺包。在目标 conda 环境里 pip install <缺的包>;注意版本要匹配"
        "requirements.txt(离线时用有网机器 pip download 打包上传安装)",
    ]),
    (("FileNotFoundError", "No such file or directory"), "file_not_found", "文件/数据/权重缺失", [
        "按报错路径核对:数据集是否就位(体检第③步)、权重目录(checkpoints/)是否链接、"
        "相对路径是否以工作区为基准。缺什么就在供给卡补什么,再复跑",
    ]),
    (("CUDA out of memory", "OutOfMemoryError", "CUDA error: out of memory"), "cuda_oom",
     "GPU 显存不足", [
        "减小 batch size / 图像尺寸,或清理占用显存的进程(nvidia-smi 查占用);"
        "共享 GPU 时等其他任务结束",
    ]),
    (("Connection refused", "Address already in use"), "conn_refused", "端口占用或服务未启动", [
        "按报错端口核对:是否被旧进程占用(ss -tlnp | grep 端口),kill 后重试;或换端口",
    ]),
]


def diagnose_run_failure(log: str) -> dict:
    """从日志尾部识别失败类别,给出手动解决步骤(未识别时附查看日志的方法)。"""
    text = log or ""
    low = text.lower()
    for markers, kind, title, steps in _FAILURE_PATTERNS:
        if all(m.lower() in low for m in markers):
            return {"kind": kind, "title": title, "steps": steps}
    return {"kind": "unknown", "title": "未识别的失败类型", "steps": [
        "展开下方「运行输出」看完整日志尾部,定位最后一个 Traceback;",
        "可把日志贴给修复 Agent 再试一次;仍失败则按 Traceback 手动处理"
        "(缺文件→供给,缺包→装包,代码错→改代码)",
        "也可 SSH 到服务器直接看:tail -100 <工作区>/outputs_*/train.log",
    ]}


def datasets_progress(project: dict) -> list[dict]:
    """逐数据集进度清单(配置 × 已有运行记录);确认门据此判定。
    没有配置清单的旧流程:运行记录本身视为单个隐式数据集(default)。"""
    config = server_admin.baseline_config_defaults(project)
    datasets = server_admin.baseline_datasets(config)
    run = normalize_run(project)
    if not datasets:
        finished = run["status"] == "finished"
        return [{
            "name": run["dataset"] or "default",
            "command": config.get("command") or "",
            "datasetPath": config.get("dataset_path") or "",
            "status": run["status"] if run["status"] in ("running", "finished")
            else "pending",
            "exitCode": run["exit_code"] if finished else None,
            "metrics": run["metrics"] if finished else None,
            "ok": bool(finished and run["exit_code"] == 0 and run["metrics"]),
            "active": run["status"] == "running",
        }]
    out = []
    for ds in datasets:
        record = run["runs"].get(ds["name"]) or {}
        finished = record.get("status") == "finished"
        exit_code = record.get("exit_code")
        metrics = record.get("metrics")
        seed = record.get("seed")
        if seed is None:
            m = re.search(r"--seed\s+(\d+)", ds["command"] or "")
            seed = int(m.group(1)) if m else None
        out.append({
            "name": ds["name"], "command": ds["command"],
            "datasetPath": ds["dataset_path"],
            "metricsSource": record.get("metrics_source"),
            "seed": seed,
            "status": ("running" if run["dataset"] == ds["name"]
                       and run["status"] == "running"
                       else record.get("status") or "pending"),
            "exitCode": exit_code if finished else None,
            "metrics": metrics if finished else None,
            "ok": bool(finished and exit_code == 0 and metrics
                       and not metrics_sanity_issues(metrics)),
            "active": run["dataset"] == ds["name"] and run["status"] == "running",
        })
    return out


def all_datasets_ok(project: dict) -> bool:
    progress = datasets_progress(project)
    return bool(progress) and all(p["ok"] for p in progress)


def confirmed_datasets(project: dict) -> dict[str, dict]:
    """确认快照里的数据集池 {名: {metrics, command, dataset_path}};
    旧项目没有快照 → 从运行记录合成单数据集。"""
    try:
        snap = json.loads(project.get("baseline_confirmed_json") or "{}")
        datasets = snap.get("datasets")
        if isinstance(datasets, dict) and datasets:
            return {str(k): v for k, v in datasets.items()
                    if isinstance(v, dict)}
    except Exception:
        pass
    progress = [p for p in datasets_progress(project) if p["ok"]]
    if not progress:
        return {}
    return {p["name"]: {"metrics": p["metrics"], "command": p["command"],
                        "dataset_path": p["datasetPath"]}
            for p in progress}


def baseline_metrics_display(project: dict) -> dict | None:
    """迭代/方向页的基线指标展示口径:确认快照优先(确认过 = 权威),
    未确认回落最后一次 run 的指标。

    联合基线没有单一基线 → 快照键带数据集前缀(Kvasir·val_dice),
    展示胶囊不撒谎;只收 float——适配器 metrics.json 混着 config_file/
    seed/returncode 等回显字段,整数与字符串不是指标。run.metrics
    会被后续队列动作冲掉(确认后补跑档位失败 → null),已确认的基线
    不能跟着消失(真机事故:迭代页基线显示 null,像"从没跑过基线")。"""
    out: dict[str, float] = {}
    for name, info in confirmed_datasets(project).items():
        metrics = info.get("metrics")
        if not isinstance(metrics, dict):
            continue
        for key, value in metrics.items():
            if isinstance(value, float) and value == value:  # 排 NaN
                out.setdefault(f"{name}·{key}", value)
    if out:
        return out
    try:
        metrics = json.loads(project.get("baseline_run_json") or "{}").get("metrics")
    except Exception:
        return None
    return metrics if isinstance(metrics, dict) and metrics else None


# ------------------------------------------------------------------ 起跑/推进

def _persist(repo: Repo, project_id: str, run: dict) -> None:
    repo.update_project(project_id, baseline_run_json=json.dumps(
        {k: v for k, v in run.items() if k != "progress_tail"},
        ensure_ascii=False))


def _dataset_readiness(project: dict) -> dict[str, bool]:
    """最近一次体检的逐数据集就绪判定(dataset:<name> 为 ok)。
    无体检记录时返回空 dict → 调用方按"全就绪"处理(向后兼容)。"""
    try:
        checks = json.loads(project.get("baseline_checks_json") or "[]")
    except Exception:  # noqa: BLE001 - 损坏的检查 JSON 不应阻断起跑
        return {}
    out: dict[str, bool] = {}
    for c in checks:
        key = c.get("key") or ""
        if key.startswith("dataset:"):
            out[key[len("dataset:"):]] = c.get("status") == "ok"
    return out


def start_run(repo: Repo, credentials: CredentialStore, hostkeys_path: Path,
              project: dict, *, dataset: str | None = None,
              fix_attempt: bool = False) -> dict:
    """起跑:dataset 指定则只跑它;否则跑所有尚未跑通且体检就绪的数据集
    (都跑通过 → 全部重跑)。空壳/缺材料的数据集跳过——渐进式基线,
    未供给的不阻塞已就绪的。返回起始状态(含 skipped)。"""
    workdir = (project.get("remote_workdir") or "").strip()
    if not workdir:
        raise BaselineQueueError("项目还没有工作区")
    config = server_admin.baseline_config_defaults(project)
    datasets = server_admin.baseline_datasets(config)
    if not datasets:
        raise BaselineQueueError("先配置运行命令(command)再跑")
    by_name = {d["name"]: d for d in datasets}
    if dataset is not None and dataset not in by_name:
        raise BaselineQueueError(f"数据集 {dataset} 不在 Baseline 配置里")
    current = normalize_run(project)
    if current["status"] == "running":
        raise BaselineQueueError("Baseline 正在运行,等它结束再起新的")

    skipped: list[str] = []
    if dataset is not None:
        order = [dataset]
    else:
        def ok(name: str) -> bool:
            record = current["runs"].get(name) or {}
            return (record.get("status") == "finished"
                    and record.get("exit_code") == 0
                    and bool(record.get("metrics")))

        pending = [d["name"] for d in datasets if not ok(d["name"])]
        readiness = _dataset_readiness(project)
        if pending and readiness:
            ready = [n for n in pending if readiness.get(n, False)]
            skipped = [n for n in pending if n not in ready]
            if ready:
                pending = ready
            else:
                raise BaselineQueueError(
                    "没有体检就绪的数据集:先在 Baseline 第③步补齐材料"
                    "(供给数据集/权重)后重新检查,或用单数据集「跑」按钮指定")
        order = pending or [d["name"] for d in datasets]

    server = repo.get_server(project["server_id"])
    transport = None
    try:
        if server and server["transport_mode"] == "ssh":
            transport = server_admin.connect_ssh(server, credentials, hostkeys_path)
        first = by_name[order[0]]
        pid = server_admin.start_baseline_run(
            workdir, config, ssh_transport=transport,
            run_dir=first["run_dir"], command=first["command"])
    except server_admin.ServerAdminError as exc:
        raise BaselineQueueError(str(exc)) from exc
    finally:
        if transport is not None:
            transport.close()
    for name in order:
        current["runs"].pop(name, None)
    if fix_attempt:
        current["fix_attempts"] = (current.get("fix_attempts") or 0) + 1
    run = {**current, "dataset": order[0], "queue": order[1:],
           "status": "running", "pid": pid, "exit_code": None,
           "log_tail": "", "metrics": None, "started_at": utc_now()}
    _persist(repo, project["id"], run)
    return {"pid": pid, "dataset": order[0], "queue": order[1:],
            "skipped": skipped,
            "datasets": [d["name"] for d in datasets]}


def sync_run(repo: Repo, credentials: CredentialStore, hostkeys_path: Path,
             project: dict, *, advance_enabled: bool) -> dict:
    """刷新活动数据集的现场状态;advance_enabled 时在跑通后自动起下一个。

    本模块唯一的落库路径(tick 与测试调用);HTTP 轮询用 advance_enabled=False
    且不落库——直接返回现场状态用于展示。"""
    workdir = (project.get("remote_workdir") or "").strip()
    config = server_admin.baseline_config_defaults(project)
    datasets = server_admin.baseline_datasets(config)
    by_name = {d["name"]: d for d in datasets}
    run = normalize_run(project)
    if run["status"] != "running" or not workdir:
        return run
    active = run["dataset"]
    if active not in by_name:
        return run  # 配置改了名字:序列冻结,等用户重新起跑

    server = repo.get_server(project["server_id"])
    transport = None
    try:
        if server and server["transport_mode"] == "ssh":
            transport = server_admin.connect_ssh(server, credentials, hostkeys_path)
        state = server_admin.baseline_run_state(
            workdir, run_dir=by_name[active]["run_dir"], ssh_transport=transport)
        if state["status"] == "finished" and state["exit_code"] is None:
            # 竞态窗口:训练进程已退出、nohup 的 `echo $? > run.exit` 还没落盘
            # (毫秒级),此刻 sync 会误判「异常终止」并把 finished+无指标持久
            # 化,之后真正的 exit 0 再也没人看(实测事故)。等 2 秒复核一次。
            time.sleep(2)
            state = server_admin.baseline_run_state(
                workdir, run_dir=by_name[active]["run_dir"], ssh_transport=transport)
        run.update({"status": state["status"], "pid": state["pid"],
                    "exit_code": state["exit_code"],
                    "log_tail": state["log_tail"],
                    "progress_tail": state.get("progress_tail", ""),
                    "progress": state.get("progress")})
        if state["status"] == "finished":
            metrics = None
            metrics_source = None
            if state["exit_code"] == 0:
                metrics = server_admin.baseline_dataset_metrics(
                    workdir, by_name[active]["run_dir"],
                    (config.get("metric_file") or "").strip(),
                    ssh_transport=transport)
                if metrics:
                    metrics_source = "file"
            if state["exit_code"] == 0 and not metrics:
                # 指标回退:训练脚本没写指标文件时,取训练日志最后一轮解析出的
                # 指标作为基准(能跑通且日志里有数,比"没指标"诚实)
                parsed = state.get("progress") or {}
                latest = (parsed.get("latest") or {}).get("metrics") or {}
                if latest:
                    metrics = dict(latest)
                    metrics_source = "log"
            # 指标合理性门(D-023):超域指标按无效处理(能跑≠可信)
            sanity_issues = metrics_sanity_issues(metrics or {})
            run["sanity_note"] = (";".join(sanity_issues) if sanity_issues else "")
            run["metrics"] = metrics
            run["metrics_source"] = metrics_source
            run["seed"] = (state.get("progress") or {}).get("seed")
            run["runs"][active] = {"status": "finished",
                                   "exit_code": state["exit_code"],
                                   "metrics": metrics,
                                   "metrics_source": metrics_source,
                                   "seed": run["seed"]}
            if state["exit_code"] == 0:
                # 跑通:清掉失败痕迹
                run.pop("diagnosis", None)
                run["fix_attempts"] = 0
            else:
                # 失败:落诊断(分类 + 手动步骤),前端据此展示升级面板
                run["diagnosis"] = diagnose_run_failure(state["log_tail"] or "")
            # 队列只在「跑通」时前进;失败停下等修复,queue 保留待续跑
            if advance_enabled and state["exit_code"] == 0 and run["queue"]:
                nxt = run["queue"][0]
                if nxt in by_name:
                    pid = server_admin.start_baseline_run(
                        workdir, config, ssh_transport=transport,
                        run_dir=by_name[nxt]["run_dir"],
                        command=by_name[nxt]["command"])
                    run.update({"dataset": nxt, "queue": run["queue"][1:],
                                "status": "running", "pid": pid,
                                "exit_code": None, "log_tail": "",
                                "metrics": None})
            elif state["exit_code"] != 0:
                run["queue"] = run["queue"]  # 失败:保留剩余队列
        if advance_enabled:
            _persist(repo, project["id"], run)
        return run
    except server_admin.ServerAdminError:
        return run  # SSH 抖动:保留库里状态,下个 tick 再试
    finally:
        if transport is not None:
            transport.close()


# ------------------------------------------------------------------ 确认门

def _stop_active_run(repo: Repo, project: dict, *, credentials=None,
                     hostkeys_path=None) -> str | None:
    """确认即定口径:停掉仍在跑的基线队列。渐进式确认的语义是
    「这份快照就是基线」——确认后队列继续磨剩余档位(每个都可能
    是满 epoch 配置)是无主见的 GPU 浪费(真机事故:确认后
    BTMRI-100 磨了一天被手动杀掉)。想扩池:回来再起跑补跑,
    然后追加确认。返回停止说明(未在跑 → None)。"""
    run = normalize_run(project)
    if run.get("status") != "running":
        return None
    remaining = len(run.get("queue") or [])
    pid = run.get("pid")
    if pid:
        try:
            server = repo.get_server(project["server_id"])
            if server is not None and server["transport_mode"] == "ssh" \
                    and credentials is not None:
                with server_admin.connect_ssh(server, credentials, hostkeys_path) as t:
                    t.exec_command(f"kill {int(pid)} 2>/dev/null")
            elif server is not None and server["transport_mode"] != "ssh":
                import os
                import signal

                os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass  # 杀不掉也会被 sync 的存活检测收敛;队列冻结才是硬保证
    run.update({"status": "stopped", "queue": [], "exit_code": None})
    _persist(repo, project["id"], run)
    note = (f"基线已确认:停止基线队列(当前档 {run.get('dataset')},"
            f"剩余 {remaining} 个档位未跑;想扩池再起跑补跑,然后追加确认)")
    repo.add_review_message(project_id=project["id"], role="system", kind="text",
                            content=f"⏹ {note}")
    return note


def confirm_pass(repo: Repo, project: dict, *, credentials=None,
                 hostkeys_path=None) -> dict:
    """人工确认通过(渐进式基线):至少 1 个数据集 跑通+有指标 即可确认,
    快照只收录已跑通的数据集(自动迭代的数据集可选池)。

    已确认过的项目支持**追加确认**:后来加入清单并跑通的数据集
    (不在现有快照里)会被合并进快照,迭代可选池随之扩大;
    没有新跑通的数据集时幂等返回 replayed。典型节奏:先在 1 个数据集上
    跑通→确认→迭代;有进步后回来补跑其余数据集→追加确认→扩大迭代池。"""
    snapshot = confirmed_datasets(project)
    progress = datasets_progress(project)
    if not progress:
        raise BaselineQueueError("还没有数据集配置,先在 Baseline 页配置再跑")
    passed_at = project.get("baseline_passed_at")
    newly_ok = {p["name"]: p for p in progress
                if p["ok"] and p["name"] not in snapshot}
    if not passed_at:
        ok_rows = [p for p in progress if p["ok"]]
        if not ok_rows:
            raise BaselineQueueError(
                "还没有数据集满足确认条件(至少 1 个 跑通且有指标;"
                "未跑通的可后续补跑后追加纳入)")
        snapshot = {p["name"]: {"metrics": p["metrics"], "command": p["command"],
                                "dataset_path": p["datasetPath"]}
                    for p in ok_rows}
        now = utc_now()
        repo.update_project(project["id"], baseline_passed_at=now,
                            baseline_confirmed_json=json.dumps(
                                {"passed_at": now, "datasets": snapshot},
                                ensure_ascii=False))
        result = {"projectId": project["id"], "passed": True, "replayed": False,
                  "datasets": snapshot}
    elif not newly_ok:
        result = {"projectId": project["id"], "passed": True, "replayed": True,
                  "datasets": snapshot}
    else:
        # 追加确认:合并新跑通的数据集(未跑通的不拦——它们本来就不在池里)
        for name, p in newly_ok.items():
            snapshot[name] = {"metrics": p["metrics"], "command": p["command"],
                              "dataset_path": p["datasetPath"]}
        repo.update_project(project["id"], baseline_confirmed_json=json.dumps(
            {"passed_at": project.get("baseline_passed_at"), "datasets": snapshot},
            ensure_ascii=False))
        result = {"projectId": project["id"], "passed": True, "replayed": False,
                  "added": list(newly_ok.keys()), "datasets": snapshot}
    # 确认即定口径:停掉仍在跑的队列(防无主见的 GPU 消耗)
    stopped = _stop_active_run(repo, repo.get_project(project["id"]),
                               credentials=credentials,
                               hostkeys_path=hostkeys_path)
    if stopped:
        result["queueStopped"] = stopped
    return result


# ------------------------------------------------------------------ 后台推进

class BaselineQueueManager:
    """每 5s 扫一遍有进行中基线序列的项目并推进(serve 进程常驻线程)。"""

    def __init__(self, repo: Repo, credentials_path: str, *,
                 interval: float = 5.0):
        self.repo = repo
        self.credentials = CredentialStore(credentials_path)
        self.interval = interval

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            stop.wait(self.interval)

    def tick(self) -> None:
        conn = self.repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT id FROM projects WHERE baseline_run_json IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
        hostkeys = server_admin.hostkeys_path_for(self.credentials)
        for (project_id,) in rows:
            project = self.repo.get_project(project_id)
            if project is None:
                continue
            try:
                run = normalize_run(project)
                if run["status"] == "running":
                    sync_run(self.repo, self.credentials, hostkeys, project,
                             advance_enabled=True)
            except Exception:
                continue  # 单个项目失败不拖垮整个 tick
