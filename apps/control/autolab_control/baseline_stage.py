"""Baseline 阶段:一个 agent 会话把工程跑通,缺什么就问 owner。

取代旧的六步 workflow + Set 门 + analyzer/advisor/fix 三角色:
- agent 自己探索代码、选/装环境、写适配器、冒烟;
- 缺数据集/权重/信息 → ask_human(结构化,前端按 kind 渲染上传/选目录/回答);
- 正式基线 → run_experiment(每个数据集一个 Job,走 Runner);
- finish → Python 验收(每个数据集的 Job 成功且有合法指标)→ 暂停等 owner 确认;
  owner 确认 = 写确认快照(与旧流程同形,研究阶段零改动);owner 回话 = 修改意见回给 agent。
"""

from __future__ import annotations

import json

from . import agent_loop, server_admin
from .agent_loop import AgentLoopError, LoopEnv, LoopProfile, Pause, Tool, ToolError
from .db import utc_now
from .experiments import experiment_tools, job_metrics
from .repo import Repo

KIND = "baseline"
CONFIRM_KIND = "confirm_baseline"

SYSTEM_PROMPT = """你是 AutoLab 的 Baseline Agent。目标:让这个代码库在 GPU 服务器上跑通论文/代码默认的基线训练,并为每个数据集拿到可信的评测指标。你独立工作,owner 只在你需要材料或信息时介入。

## 工作方式
1. 先读懂工程:README、训练/评测入口、configs、数据加载代码,弄清需要哪些数据集、预训练权重、依赖和目录约定。
2. 环境:先用 shell 看服务器已有的 conda 环境(`conda env list`)和已装的包,优先复用能用的环境。
3. 缺材料就问:数据集、权重、私有文件、你无法确定的口径,用 ask_human 向 owner 要。
   - kind 选 dataset/weights/file 时必须给 target_path(工作区相对路径,依据代码里读取的位置)和 why(哪段代码/配置需要它)。
   - 同一类缺口一次问清;能自己从代码查到的不要问。
   - 问完之后你会暂停,owner 回复后继续。
4. 指标出口:正式运行的程序必须把评测指标写成 JSON 到 `$AUTOLAB_OUTPUT_DIR/metrics.json`(键名即指标名,值为数字)。工程本身做不到时,写一个适配器脚本(如 autolab_baseline.py)包住原入口,**尽量不改原工程源码**。
5. 先冒烟再正式跑:用 shell 跑一个极短的版本(很少的 step/epoch 或很小的数据比例)确认能走通、指标能写出,再用 run_experiment 正式跑。
6. 正式基线:用 run_experiment 每个数据集一个 Job,然后 wait_jobs。失败就读返回的日志尾部,修好再跑。
7. 训练口径:用代码/论文的默认配置。如果默认训练预计非常久(数小时以上),先用 ask_human(kind=confirm)和 owner 确认口径。
8. 全部数据集都成功、指标合理后,调用 finish 提交基线;Python 会核验,然后交给 owner 确认。owner 可能提出修改意见,照做后再 finish。

## 约束
- 服务器的网络情况见下方「环境事实」。没有外网时不要尝试 pip install / wget / git clone / 从 HuggingFace 下载,缺什么就问 owner。
- 指标必须是真实评测结果,不要伪造或硬编码。比率类指标(dice/iou/acc 等)应在 [0,1]。
- 回复用中文,简洁。"""


def _facts(env: LoopEnv) -> str:
    project = env.project
    online = bool(env.config.get("network"))
    lines = [
        "## 环境事实",
        f"- 项目:{project['name']}",
        f"- 工作区:{project['remote_workdir']}(所有路径相对它)",
        f"- 输出根目录:{project['output_root']}(每个 Job 有独立 $AUTOLAB_OUTPUT_DIR)",
        f"- 网络:{'可访问外网' if online else '无外网(缺材料只能问 owner)'}",
    ]
    return "\n".join(lines)


def _finish_tool() -> Tool:
    def handler(env: LoopEnv, args: dict):
        proposal = validate_proposal(env.repo, env.project, args)
        return Pause("human", {"kind": CONFIRM_KIND, "question":
                               "Baseline 已跑通,请确认是否采用这组基线。",
                               "proposal": proposal})

    return Tool(
        "finish",
        "提交基线给 owner 确认。每个数据集给出成功的正式 Job。Python 会核验 Job 成功且有合法指标。",
        {"type": "object", "properties": {
            "conda_env": {"type": "string", "description": "运行用的 conda 环境(没有则空)"},
            "datasets": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "job_id": {"type": "string", "description": "该数据集正式基线的 Job"},
                "dataset_path": {"type": "string"}},
                "required": ["name", "job_id"]}},
            "notes": {"type": "string",
                      "description": "给 owner 的说明:做了哪些适配/改动、口径、已知问题"}},
         "required": ["datasets", "notes"]},
        handler)


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
        job = repo.get_job(job_id)
        plan = repo.get_plan(job["plan_revision_id"]) if job else None
        if job is None or plan is None or plan["project_id"] != project["id"]:
            problems.append(f"{name}:Job {job_id} 不存在或不属于本项目")
            continue
        if job["status"] != "SUCCEEDED":
            problems.append(f"{name}:Job {job_id} 状态 {job['status']},不是 SUCCEEDED")
            continue
        metrics = job_metrics(repo, job_id)
        if not metrics:
            problems.append(f"{name}:Job {job_id} 没有提取到合法指标"
                            "(metrics.json 缺失、键名不符或数值越界)")
            continue
        params = json.loads(job["params_json"] or "{}")
        out.append({"name": name, "jobId": job_id, "metrics": metrics,
                    "command": params.get("command") or "",
                    "datasetPath": str((item or {}).get("dataset_path") or "")})
    if problems:
        raise ToolError("基线核验未通过:\n- " + "\n- ".join(problems))
    return {"condaEnv": str(args.get("conda_env") or "").strip(),
            "datasets": out, "notes": str(args.get("notes") or "").strip()}


def build_tools(env: LoopEnv) -> list[Tool]:
    return [*agent_loop.workspace_tools(), agent_loop.shell_tool(),
            *experiment_tools(baseline_ref="baseline"),
            agent_loop.ask_human_tool(), _finish_tool()]


PROFILE = LoopProfile(
    kind=KIND, system_prompt=SYSTEM_PROMPT, build_tools=build_tools,
    system_suffix=_facts, max_steps=300, max_tokens=6_000_000)


# ------------------------------------------------------------------ 对外动作

def start(repo: Repo, project_id: str, *, note: str = "", network: bool = False) -> dict:
    project = repo.get_project(project_id)
    if project is None:
        raise AgentLoopError(f"项目 {project_id} 不存在")
    if not (project.get("remote_workdir") or "").strip():
        raise AgentLoopError("项目还没有工作区,先完成立项")
    task = ("请把这个工程的 Baseline 跑通。论文(如有)在工作区 .autolab/papers/ 下。")
    if note.strip():
        task += f"\n\nowner 补充说明:\n{note.strip()}"
    run = agent_loop.start_run(repo, project_id, KIND, task,
                               {"network": bool(network)})
    repo.request_baseline(project_id, agent_id=None)  # 看板进 Baseline 列
    return run


def confirm(repo: Repo, run_id: str) -> dict:
    """owner 确认基线:重新核验后写配置与确认快照(与旧 confirm_pass 同形)。"""
    run = agent_loop.get_run(repo, run_id)
    pending = run.get("pending") or {}
    payload = pending.get("payload") or {}
    if run["kind"] != KIND or payload.get("kind") != CONFIRM_KIND:
        raise AgentLoopError("该会话没有待确认的基线")
    project = repo.get_project(run["projectId"])
    proposal = payload["proposal"]
    # 提案提交后到确认前,Job 记录不会变;仍复核一次,防止手工改库
    validate_proposal(repo, project, {
        "conda_env": proposal["condaEnv"], "notes": proposal["notes"],
        "datasets": [{"name": d["name"], "job_id": d["jobId"],
                      "dataset_path": d["datasetPath"]} for d in proposal["datasets"]]})
    config = server_admin.baseline_config_defaults(project)
    config.update({
        "conda_env": proposal["condaEnv"],
        "command": proposal["datasets"][0]["command"],
        "datasets": [{"name": d["name"], "command": d["command"],
                      "dataset_path": d["datasetPath"]} for d in proposal["datasets"]],
        "onboarded": True,
    })
    now = utc_now()
    snapshot = {d["name"]: {"metrics": d["metrics"], "command": d["command"],
                            "dataset_path": d["datasetPath"], "job_id": d["jobId"]}
                for d in proposal["datasets"]}
    repo.update_project(
        project["id"],
        baseline_config_json=json.dumps(config, ensure_ascii=False),
        baseline_passed_at=now,
        baseline_confirmed_json=json.dumps(
            {"passed_at": now, "datasets": snapshot, "run_id": run_id},
            ensure_ascii=False))
    return agent_loop.complete_run(repo, run_id, {"confirmed": True, **proposal})
