"""Baseline 阶段:一个 agent 会话把工程跑通,缺什么就问 owner。

agent 自己探索代码、选/装环境、写适配器、冒烟;缺数据集/权重/信息 → ask_human;
正式基线 → run_experiment(每个数据集一个 Job);finish → Python 核验每个数据集的
Job 成功且有合法指标 → 暂停等 owner 确认。owner 确认 = 写基线快照
(projects.baseline_json),项目进入研究阶段;owner 回话 = 修改意见回给 agent。
"""

from __future__ import annotations

import json

from ..agent import knowledge, runtime, tools
from ..agent.runtime import AgentLoopError, LoopEnv, LoopProfile, Pause, ToolError
from ..core.db import utc_now
from ..core.repo import Repo
from ..execution.evidence import hash_paths
from ..execution.experiments import job_metrics, project_job
from ..workspace.fs import open_workspace

KIND = "baseline"
CONFIRM_KIND = "confirm_baseline"

SYSTEM_PROMPT = """你是 AutoLab 的 Baseline Agent。目标:让这个代码库在 GPU 服务器上跑通论文/代码默认的基线训练,并为每个数据集拿到可信的评测指标。你独立工作,owner 只在你需要材料或信息时介入。

## 工作方式
1. 先读懂工程:README、训练/评测入口、configs、数据加载代码,弄清需要哪些数据集、预训练权重、依赖和目录约定。
2. 环境:先用 shell 看服务器已有的 conda 环境(`conda env list`)和已装的包,优先复用能用的环境。
3. 缺材料就问:数据集、权重、私有文件、你无法确定的口径,用 ask_human 向 owner 要。
   - kind 选 dataset/weights/file 时必须给 target_path(工作区相对路径,依据代码里读取的位置)和 why(哪段代码/配置需要它)。
   - **数据集供给纪律**:先查服务器上已有的数据目录(工程 data/、~/datasets 等)能不能直接用;
     允许联网时公开直链能下则下;**Kaggle/需登录/私有源,或下载失败过一次**的,立即
     ask_human(kind=dataset,name=数据集名,target_path,why) 让 owner 上传或指路——
     不要反复尝试下载,也不要悄悄跳过该数据集不报。
   - 同一类缺口一次问清;能自己从代码查到的不要问。
   - 问完之后你会暂停,owner 回复后继续。
4. 指标出口:正式运行的程序必须把评测指标写成 JSON 到 `$AUTOLAB_OUTPUT_DIR/metrics.json`(键名即指标名,值为数字)。工程本身做不到时,写一个适配器脚本(如 autolab_baseline.py)包住原入口,**尽量不改原工程源码**。
5. 先冒烟再正式跑:用 shell 跑一个极短的版本(很少的 step/epoch 或很小的数据比例)确认能走通、指标能写出,再用 run_experiment 正式跑。
6. 数据集范围必须 owner 拍板:工程的 configs 支持多个数据集时,先用 ask_human(kind=confirm)列出候选(说明服务器上已有哪些可用)让 owner 勾选,再正式跑;单数据集工程不用问。owner 在任务备注里已写明范围的不用再问。
7. 正式基线:用 run_experiment 每个数据集一个 Job(填 dataset),然后 wait_jobs。失败就读返回的日志尾部,修好再跑。
8. 训练口径:用代码/论文的默认配置。如果默认训练预计非常久(数小时以上),先用 ask_human(kind=confirm)和 owner 确认口径。
9. 全部数据集都成功、指标合理后,调用 finish 提交基线;Python 会核验,然后交给 owner 确认。owner 可能提出修改意见,照做后再 finish。

## 约束
- 服务器的网络情况见下方「环境事实」。没有外网时不要尝试 pip install / wget / git clone / 从 HuggingFace 下载,缺什么就问 owner。
- **离线前缀是常量**:涉及 HuggingFace/transformers 的命令(冒烟、run_experiment)一律加前缀
  `export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`——这些库即使文件已缓存也默认联网校验版本,
  网络不通会挂在重试里假死(真机教训)。命令输出累计 3 次出现网络不可达错误时会被强制暂停问 owner。
- **磁盘纪律(共享服务器,真机事故:checkpoint 按 epoch 堆了 285G 把盘写满,控制端崩溃)**:
  正式跑之前弄清工程怎么存 checkpoint,默认适配成**只存 best(最多再留 last),旧 checkpoint 即时删**;
  工程 config 支持就配置,不支持就在适配器(autolab_baseline.py)里加清理钩子。
  预训练权重目录(如 checkpoints/)不是训练产物,不要动。正式 Job 结束后 `df -h` 看一眼磁盘。
- 指标必须是真实评测结果,不要伪造或硬编码。比率类指标(dice/iou/acc 等)应在 [0,1]。
- 回复用中文,简洁。"""


def baseline_snapshot(project: dict) -> dict:
    """确认过的基线快照 {passed_at, conda_env, datasets: {名: {metrics, command, ...}}}。"""
    try:
        snapshot = json.loads(project.get("baseline_json") or "{}")
    except ValueError:
        return {}
    return snapshot if isinstance(snapshot, dict) else {}


def baseline_contract(project: dict) -> dict | None:
    """owner 固化的指标契约(基线快照顶层 metric_contract);未配置返回 None,
    判定链保持旧行为(agent 声明的第一个命中基线的指标为主指标)。"""
    contract = baseline_snapshot(project).get("metric_contract")
    return contract if isinstance(contract, dict) and contract else None


def default_metric_contract(pool: dict[str, dict]) -> dict:
    """未配置契约时的默认形态(第一个数据集的第一个指标)。B1 不强制默认契约:
    只作 GET 端点的预填建议,判定链不使用它。"""
    for info in pool.values():
        metrics = list(info.get("metrics") or {})
        if metrics:
            return {"primary": metrics[0], "direction": "maximize",
                    "guardrails": [], "eval": {}}
    return {"primary": "", "direction": "maximize", "guardrails": [], "eval": {}}


def _clean_eval_paths(paths, field: str) -> list[str]:
    out = []
    for raw in paths if isinstance(paths, list) else []:
        rel = str(raw or "").strip().strip("/")
        if not rel:
            continue
        if rel.startswith("/") or ".." in rel.split("/"):
            raise ToolError(f"eval.{field} 必须是工作区相对路径:{rel}")
        out.append(rel)
    return out
def _clean_contract(payload: dict, pool: dict[str, dict]) -> dict:
    """核验 owner 提交的契约;非法即 ToolError(映射 409)。"""
    if not isinstance(payload, dict):
        raise ToolError("契约必须是对象:{primary, direction, guardrails, eval}")
    known = {name for info in pool.values() for name in (info.get("metrics") or {})}
    primary = str(payload.get("primary") or "").strip()
    if not primary:
        raise ToolError("primary 不能为空")
    if primary not in known:
        raise ToolError(f"primary 指标 {primary} 不在任何数据集的基线指标里")
    direction = str(payload.get("direction") or "maximize")
    if direction not in ("maximize", "minimize"):
        raise ToolError(f"direction 只能是 maximize|minimize:{direction}")
    guardrails = []
    for raw in payload.get("guardrails") or []:
        rule = raw if isinstance(raw, dict) else {}
        metric = str(rule.get("metric") or "").strip()
        if metric not in known:
            raise ToolError(f"guardrail 指标 {metric or '(空)'} 不在基线指标里")
        rule_direction = str(rule.get("direction") or "maximize")
        if rule_direction not in ("maximize", "minimize"):
            raise ToolError(f"guardrail direction 只能是 maximize|minimize:{rule_direction}")
        regress = rule.get("max_regress", 0.0)
        if isinstance(regress, bool) or not isinstance(regress, (int, float)) or regress < 0:
            raise ToolError(f"guardrail {metric} 的 max_regress 必须 ≥ 0")
        guardrails.append({"metric": metric, "direction": rule_direction,
                           "max_regress": float(regress)})
    raw_eval = payload.get("eval") or {}
    if not isinstance(raw_eval, dict):
        raise ToolError("eval 必须是对象:{metric_file, code_paths, data_paths}")
    return {"primary": primary, "direction": direction,
            "guardrails": guardrails,
            "eval": {
                "metric_file": str(raw_eval.get("metric_file") or "metrics.json").strip(),
                "code_paths": _clean_eval_paths(raw_eval.get("code_paths"), "code_paths"),
                "data_paths": _clean_eval_paths(raw_eval.get("data_paths"), "data_paths"),
                "code_hash": "", "data_hash": ""}}


def refresh_contract_hashes(workspace, contract: dict) -> dict:
    """eval 保护路径有配置时重算指纹(owner PATCH / confirm / adopt 共用)。"""
    eval_cfg = contract.get("eval") or {}
    if eval_cfg.get("code_paths"):
        eval_cfg["code_hash"] = hash_paths(workspace, eval_cfg["code_paths"])["hash"]
    if eval_cfg.get("data_paths"):
        eval_cfg["data_hash"] = hash_paths(workspace, eval_cfg["data_paths"])["hash"]
    return contract


def get_metric_contract(project: dict) -> dict:
    """GET 端点载荷:当前契约(未配置 null)+ 逐数据集指标清单 + 预填默认。"""
    pool = baseline_pool(project)
    return {"contract": baseline_contract(project),
            "metrics": {name: list(info.get("metrics") or {})
                        for name, info in pool.items()},
            "default": default_metric_contract(pool)}


def set_metric_contract(repo: Repo, credentials, project_id: str, payload: dict) -> dict:
    """owner 固化指标契约:写入基线快照顶层 metric_contract(其余快照字段原样)。"""
    project = repo.get_project(project_id)
    if project is None:
        raise AgentLoopError(f"项目 {project_id} 不存在")
    pool = baseline_pool(project)
    if not pool:
        raise AgentLoopError("还没有确认过的基线,无从绑定指标契约")
    contract = _clean_contract(payload, pool)
    if contract["eval"]["code_paths"] or contract["eval"]["data_paths"]:
        with open_workspace(repo, credentials, project) as ws:
            refresh_contract_hashes(ws, contract)
    snapshot = baseline_snapshot(project)
    snapshot["metric_contract"] = contract
    repo.update_project(project_id, baseline_json=json.dumps(snapshot, ensure_ascii=False))
    return get_metric_contract(repo.get_project(project_id))

def baseline_pool(project: dict) -> dict[str, dict]:
    """研究阶段可用的数据集池(只有确认过的才算)。"""
    if not project.get("baseline_passed_at"):
        return {}
    datasets = baseline_snapshot(project).get("datasets")
    return {str(k): v for k, v in datasets.items() if isinstance(v, dict)} \
        if isinstance(datasets, dict) else {}


def baseline_label(project: dict) -> dict | None:
    """基线版本徽标(UI 投影):v 几、是什么(原始/采纳/分叉)、何时、分叉自谁。
    version_label / fork 结构化字段缺失时按形态推断,老数据也有合理显示——
    用户需要一眼分清「这个项目现在拿什么当对比对象」。"""
    snapshot = baseline_snapshot(project)
    if not snapshot.get("datasets"):
        return None
    history = snapshot.get("history") or []
    fork = snapshot.get("fork") or {}
    if fork.get("source_name"):
        kind, kind_label = "forked", "分叉"
    elif any("promoted_from" in (d or {}) for d in snapshot["datasets"].values()) \
            or str(snapshot.get("notes") or "").startswith("采纳"):
        kind, kind_label = "adopted", "采纳"
    else:
        kind, kind_label = "confirmed", "原始"
    label = snapshot.get("version_label") or ("原始基线" if not history else "")
    if not label:
        label = f"第 {len(history) + 1} 版基线"
    return {"version": f"v{len(history) + 1}", "label": str(label)[:40],
            "kind": kind, "kindLabel": kind_label,
            "date": str(snapshot.get("passed_at") or "")[:10],
            "forkedFrom": fork.get("source_name")}


def dataset_states(repo: Repo, project_id: str) -> list[dict]:
    """按数据集聚合状态(UI 投影,全部从事实派生,不依赖 agent 自报):
    confirmed = owner 已确认的基线快照;ran/running/failed = Job 实况;
    pending_supply = 当前 baseline 会话等待中的 dataset 求助(等 owner 供给)。"""
    project = repo.get_project(project_id)
    if project is None:
        return []
    out: dict[str, dict] = {}
    for name, info in baseline_pool(project).items():
        out[name] = {"name": name, "state": "confirmed",
                     "metrics": info.get("metrics") or {},
                     "command": info.get("command") or "",
                     "jobId": info.get("job_id") or ""}
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT j.id, j.status FROM jobs j"
            " JOIN plan_revisions pr ON pr.id = j.plan_revision_id"
            " WHERE pr.project_id = ? ORDER BY j.rowid", (project_id,)).fetchall()
    finally:
        conn.close()
    for r in rows:
        job = repo.get_job(r["id"])
        params = json.loads(job["params_json"] or "{}")
        name = str(params.get("dataset") or "").strip()
        if not name:
            continue
        if out.get(name, {}).get("state") == "confirmed":
            continue
        state = ("running" if r["status"] in ("QUEUED", "DISPATCHING", "RUNNING")
                 else "ran" if r["status"] == "SUCCEEDED" else "failed")
        out[name] = {"name": name, "state": state, "jobId": r["id"],
                     "jobStatus": r["status"], "metrics": job_metrics(repo, r["id"]),
                     "command": str(params.get("command") or "")}
    run = runtime.latest_run(repo, project_id, KIND)
    pending = ((run or {}).get("pending") or {}).get("payload") or {}
    if (run or {}).get("status") == "waiting_human" and pending.get("kind") == "dataset":
        name = str(pending.get("name") or "").strip() or "(未命名)"
        out[name] = {"name": name, "state": "pending_supply",
                     "targetPath": pending.get("target_path") or "",
                     "why": pending.get("why") or "",
                     "question": pending.get("question") or "", "runId": run["id"]}
    return sorted(out.values(), key=lambda d: d["name"])


def _facts(env: LoopEnv) -> str:
    project = env.project
    return "\n".join([
        "## 环境事实",
        f"- 项目:{project['name']}",
        f"- 工作区:{project['remote_workdir']}(所有路径相对它)",
        f"- 输出根目录:{project['output_root']}(每个 Job 有独立 $AUTOLAB_OUTPUT_DIR)",
        f"- 网络:{'可访问外网' if env.config.get('network') else '无外网(缺材料只能问 owner)'}",
    ])


def validate_proposal(repo: Repo, project: dict, args: dict) -> dict:
    """核验 finish 载荷;命令取 Job 实际执行的命令(不信 agent 自述)。"""
    datasets = args.get("datasets") or []
    if not isinstance(datasets, list) or not datasets:
        raise ToolError("datasets 不能为空")
    seen: set[str] = set()
    problems: list[str] = []
    out: list[dict] = []
    for item in datasets:
        name = str((item or {}).get("name") or "").strip()
        job_id = str((item or {}).get("job_id") or "").strip()
        if not name or not job_id:
            problems.append("每个数据集需要 name 和 job_id")
            continue
        if name in seen:
            problems.append(f"数据集重复:{name}")
            continue
        seen.add(name)
        try:
            job = project_job(repo, project, job_id)
        except Exception as exc:  # noqa: BLE001 - 汇总成一条核验意见
            problems.append(f"{name}:{exc}")
            continue
        if job["status"] != "SUCCEEDED":
            problems.append(f"{name}:Job {job_id} 状态 {job['status']},不是 SUCCEEDED")
            continue
        metrics = job_metrics(repo, job_id)
        if not metrics:
            problems.append(f"{name}:Job {job_id} 没有提取到合法指标"
                            "(metrics.json 缺失、键名不符或数值越界)")
            continue
        out.append({"name": name, "jobId": job_id, "metrics": metrics,
                    "command": json.loads(job["params_json"] or "{}").get("command") or "",
                    "datasetPath": str((item or {}).get("dataset_path") or "")})
    if problems:
        raise ToolError("基线核验未通过:\n- " + "\n- ".join(problems))
    return {"condaEnv": str(args.get("conda_env") or "").strip(),
            "datasets": out, "notes": str(args.get("notes") or "").strip()}


def _submit_for_confirmation(env: LoopEnv, args: dict) -> Pause:
    proposal = validate_proposal(env.repo, env.project, args)
    return Pause("human", {"kind": CONFIRM_KIND, "proposal": proposal,
                           "question": "Baseline 已跑通,请确认是否采用这组基线。"})


def build_tools(env: LoopEnv) -> list:
    knowledge_tools = [tools.knowledge_tool()] if knowledge.configured() else []
    from ..agent import paper_radar
    paper_tools = [tools.paper_radar_tool()] if paper_radar.available() else []
    return [
        *tools.workspace_tools(), tools.shell_tool(), *knowledge_tools, *paper_tools,
        *tools.experiment_tools(),
        tools.read_paper_tool(), tools.ask_human_tool(),
        tools.finish_tool(
            "提交基线给 owner 确认。每个数据集给出成功的正式 Job;Python 会核验 Job 成功且有合法指标。",
            {"conda_env": {"type": "string", "description": "运行用的 conda 环境(没有则空)"},
             "datasets": {"type": "array", "items": {"type": "object", "properties": {
                 "name": {"type": "string"},
                 "job_id": {"type": "string", "description": "该数据集正式基线的 Job"},
                 "dataset_path": {"type": "string"}}, "required": ["name", "job_id"]}},
             "notes": {"type": "string",
                       "description": "给 owner 的说明:做了哪些适配/改动、口径、已知问题"}},
            ["datasets", "notes"], handler=_submit_for_confirmation),
    ]


PROFILE = LoopProfile(kind=KIND, system_prompt=SYSTEM_PROMPT, build_tools=build_tools,
                      system_suffix=_facts, max_steps=300, max_tokens=6_000_000,
                      wall_clock_seconds=6 * 3600,
                      verifier={"id": "baseline-finish", "version": 1})


# ------------------------------------------------------------------ 对外动作

def start(repo: Repo, project_id: str, *, note: str = "", network: bool = False) -> dict:
    project = repo.get_project(project_id)
    if project is None:
        raise AgentLoopError(f"项目 {project_id} 不存在")
    if not (project.get("remote_workdir") or "").strip():
        raise AgentLoopError("项目还没有工作区,先完成立项")
    task = "请把这个工程的 Baseline 跑通。论文(如有)可用 read_paper 阅读。"
    pool = baseline_pool(project)
    if pool:
        listing = "\n".join(
            f"- {name}: 指标 {json.dumps(info.get('metrics') or {}, ensure_ascii=False)}"
            for name, info in pool.items())
        task += ("\n\n## 已确认的基线(不要重跑,除非 owner 明确要求)\n" + listing
                 + "\n本次只需为**新增**的数据集补跑基线;finish 时把新增的列出来即可。")
    if note.strip():
        task += f"\n\nowner 补充说明:\n{note.strip()}"
    return runtime.start_run(repo, project_id, KIND, task, {"network": bool(network)})


def confirm(repo: Repo, credentials, run_id: str) -> dict:
    """owner 确认基线:复核后写基线快照,项目进入研究阶段。

    B1.3:已配置的 metric_contract 原样带进新快照(没有则不写,保持旧行为);
    eval 保护路径有配置时重算指纹(确认时的代码状态即被保护状态)。"""
    run = runtime.get_run(repo, run_id)
    payload = (run.get("pending") or {}).get("payload") or {}
    if run["kind"] != KIND or payload.get("kind") != CONFIRM_KIND:
        raise AgentLoopError("该会话没有待确认的基线")
    project = repo.get_project(run["projectId"])
    proposal = payload["proposal"]
    validate_proposal(repo, project, {
        "conda_env": proposal["condaEnv"], "notes": proposal["notes"],
        "datasets": [{"name": d["name"], "job_id": d["jobId"], "dataset_path": d["datasetPath"]}
                     for d in proposal["datasets"]]})
    now = utc_now()
    # 追加确认:新提案覆盖同名数据集,已确认的其他数据集原样保留(池只增不减)
    old_snapshot = baseline_snapshot(project)
    datasets = {name: info for name, info in old_snapshot.get("datasets", {}).items()}
    datasets.update({d["name"]: {"metrics": d["metrics"], "command": d["command"],
                                 "dataset_path": d["datasetPath"], "job_id": d["jobId"]}
                     for d in proposal["datasets"]})
    snapshot = {
        "passed_at": now, "run_id": run_id, "conda_env": proposal["condaEnv"],
        "notes": proposal["notes"], "datasets": datasets,
    }
    contract = old_snapshot.get("metric_contract")
    if isinstance(contract, dict) and contract:
        if (contract.get("eval") or {}).get("code_paths") \
                or (contract.get("eval") or {}).get("data_paths"):
            with open_workspace(repo, credentials, project) as ws:
                refresh_contract_hashes(ws, contract)
        snapshot["metric_contract"] = contract
    repo.update_project(project["id"], baseline_passed_at=now,
                        baseline_json=json.dumps(snapshot, ensure_ascii=False))
    return runtime.complete_run(repo, run_id, {"confirmed": True, **proposal})
