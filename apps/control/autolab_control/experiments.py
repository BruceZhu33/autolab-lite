"""阶段 agent 的实验工具:自由提交命令 → 现有 Plan/Job/Runner 管线执行。

agent 不再起草 PlanSpec、不走审批门:run_experiment 把一组命令包成最小
PlanSpec(每条命令一个显式 Job),以「agent-run:<会话>」名义自动批准
(owner 启动阶段会话即授权),派发 / 排队 / GPU 租约 / 重启恢复 / 指标
提取与合理性门全部沿用 scheduler + Runner。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex

from autolab_protocol import (
    MetricDefinition,
    MetricSource,
    PlanOutputs,
    PlanResources,
    PlanSpecV1,
)

from . import iteration
from .agent_loop import LoopEnv, Pause, Tool, ToolError
from .repo import TERMINAL_JOB_STATUSES

MAX_JOBS_PER_SUBMIT = 8
_METRIC_NAME = re.compile(r"^[A-Za-z0-9_.\-/]+$")


def _experiment_markdown(exp_id: str, run: dict, purpose: str,
                         jobs: list[dict], metrics: list[str]) -> str:
    lines = [f"# {exp_id}", "", f"- 来源:{run['kind']} 会话 {run['id']}",
             f"- 目的:{purpose}", f"- 指标:{', '.join(metrics)}", "", "## Jobs", ""]
    lines += [f"- `{j['name']}`: `{j['command']}`" for j in jobs]
    return "\n".join(lines) + "\n"


def _normalize_metrics(raw) -> list[MetricDefinition]:
    out: list[MetricDefinition] = []
    for item in raw or []:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or not _METRIC_NAME.match(name):
            raise ToolError(f"非法指标名:{name!r}(metrics.json 的键名)")
        direction = item.get("direction") or "maximize"
        if direction not in ("maximize", "minimize"):
            raise ToolError(f"指标 {name} 的 direction 只能是 maximize/minimize")
        out.append(MetricDefinition(name=name, direction=direction))
    if not out:
        raise ToolError("至少声明一个指标(metrics.json 里的键名)")
    return out


def submit_experiment(env: LoopEnv, *, jobs: list[dict], metrics: list,
                      purpose: str, conda_env: str = "", metric_file: str = "metrics.json",
                      timeout_minutes: int = 120, gpus_per_job: int = 1,
                      baseline_ref: str = "baseline",
                      allowed_datasets: set[str] | None = None) -> dict:
    """把命令包成 Plan 并自动批准;返回 {planId, experimentId, jobs:[{id,name}]}。"""
    if not isinstance(jobs, list) or not jobs:
        raise ToolError("jobs 不能为空:[{name, command}]")
    if len(jobs) > MAX_JOBS_PER_SUBMIT:
        raise ToolError(f"一次最多 {MAX_JOBS_PER_SUBMIT} 个 Job,分批提交")
    clean: list[dict] = []
    names: set[str] = set()
    for item in jobs:
        name = str((item or {}).get("name") or "").strip()
        command = str((item or {}).get("command") or "").strip()
        if not name or not command:
            raise ToolError("每个 Job 需要 name 和 command")
        if name in names:
            raise ToolError(f"Job 名重复:{name}")
        names.add(name)
        job = {"name": name, "command": command}
        dataset = str((item or {}).get("dataset") or "").strip()
        if dataset:
            if allowed_datasets is not None and dataset not in allowed_datasets:
                raise ToolError(f"数据集 {dataset} 不在基线确认池里(可选:"
                                f"{', '.join(sorted(allowed_datasets)) or '无'})")
            job["dataset"] = dataset  # compute_comparison 按它找该数据集的基线
        clean.append(job)
    metric_defs = _normalize_metrics(metrics)
    metric_file = (metric_file or "metrics.json").strip().lstrip("/")
    if ".." in metric_file.split("/"):
        raise ToolError("metric_file 必须是输出目录内的相对路径")
    timeout_minutes = max(1, min(int(timeout_minutes or 120), 24 * 60))
    gpus_per_job = max(1, min(int(gpus_per_job or 1), 8))

    ws = env.workspace
    _, home = ws._sh("echo $HOME")
    home = (home or "").strip() or "~"
    prefix = ""
    if conda_env.strip():
        prefix = (f"source {home}/anaconda3/etc/profile.d/conda.sh 2>/dev/null || "
                  f"source {home}/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
                  f"conda activate {shlex.quote(conda_env.strip())} && ")
    # 不用 exec:命令可以是 `cd x && python ...` 这类复合形式
    command_template = ["bash", "-c", prefix + "{{command}}"]

    existing = iteration.list_experiments(ws.workdir, ssh_transport=ws.transport)
    next_no = int(existing[-1].split("-")[1]) + 1 if existing else 1
    exp_id = f"EXP-{next_no:04d}"
    body = _experiment_markdown(exp_id, env.run, purpose, clean,
                                [m.name for m in metric_defs]).encode("utf-8")
    iteration._write_bytes(ws.workdir, f"{iteration.EXPERIMENTS_REL}/{exp_id}.md",
                           body, ws.transport)
    anchor = iteration.workspace_code_anchor(ws.workdir, iteration._exec_sh(ws.transport))
    spec = PlanSpecV1(
        schema_version=1,
        project_id=env.project["id"],
        experiment_ref=f"{iteration.EXPERIMENTS_REL}/{exp_id}.md",
        experiment_hash=hashlib.sha256(body).hexdigest(),
        hypothesis=purpose,
        change_from_previous=purpose,
        baseline=baseline_ref,
        metrics=metric_defs,
        success_criteria=[f"{metric_defs[0].name} 有合法数值"],
        risk_and_fallback="失败日志回给 agent 自行修复",
        code_anchor=anchor,
        command_template=command_template,
        job_definition={"kind": "explicit", "jobs": clean},
        resources=PlanResources(gpu_count=gpus_per_job,
                                max_parallel=min(len(clean), 4)),
        timeout_seconds=timeout_minutes * 60,
        outputs=PlanOutputs(
            subdir_template="{{job_id}}",
            metrics=[MetricSource(kind="json", path=metric_file, selector=m.name)
                     for m in metric_defs]),
    )
    repo = env.repo
    plan = repo.create_draft_plan(project_id=env.project["id"], spec=spec,
                                  summary=purpose[:200])
    # owner 启动阶段会话 = 授权会话内的实验;批准事实与来源落 plan 行
    repo.approve_plan_from_local_user(
        plan["id"], expect_spec_hash=plan["spec_hash"],
        approved_by=f"agent-run:{env.run['id']}",
        approval_key=f"agent-run:{env.run['id']}:{plan['id']}")
    created = repo.list_jobs(plan_id=plan["id"])
    out_jobs = []
    for job in sorted(created, key=lambda j: j["ordinal"]):
        params = json.loads(job["params_json"] or "{}")
        out_jobs.append({"id": job["id"], "name": params.get("name")})
    return {"planId": plan["id"], "experimentId": exp_id, "jobs": out_jobs}


def _project_job(env: LoopEnv, job_id: str) -> dict:
    job = env.repo.get_job(job_id)
    if job is None:
        raise ToolError(f"Job {job_id} 不存在")
    plan = env.repo.get_plan(job["plan_revision_id"])
    if plan is None or plan["project_id"] != env.project["id"]:
        raise ToolError(f"Job {job_id} 不属于本项目")
    return job


def experiment_tools(*, baseline_ref="baseline", allowed_datasets=None,
                     before_submit=None) -> list[Tool]:
    """baseline_ref / allowed_datasets 可以是 fn(env) 惰性求值;
    before_submit(env, jobs) 抛 ToolError 可拦截提交(如 GPU 预算用完)。"""

    def _resolve(value, env):
        return value(env) if callable(value) else value

    def run_experiment(env: LoopEnv, args: dict) -> str:
        if before_submit is not None:
            before_submit(env, args.get("jobs") or [])
        result = submit_experiment(
            env, jobs=args.get("jobs") or [], metrics=args.get("metrics") or [],
            purpose=(args.get("purpose") or "").strip() or "实验",
            conda_env=args.get("conda_env") or "",
            metric_file=args.get("metric_file") or "metrics.json",
            timeout_minutes=args.get("timeout_minutes") or 120,
            gpus_per_job=args.get("gpus_per_job") or 1,
            baseline_ref=_resolve(baseline_ref, env),
            allowed_datasets=_resolve(allowed_datasets, env))
        listing = "\n".join(f"- {j['name']}: {j['id']}" for j in result["jobs"])
        return (f"已提交 {result['experimentId']}(plan {result['planId']}),"
                f"Job 已排队:\n{listing}\n用 wait_jobs 等结果。")

    def wait_jobs(env: LoopEnv, args: dict):
        job_ids = [str(j) for j in args.get("job_ids") or []]
        if not job_ids:
            raise ToolError("job_ids 不能为空")
        jobs = [_project_job(env, j) for j in job_ids]
        if all(j["status"] in TERMINAL_JOB_STATUSES for j in jobs):
            from .agent_loop import default_job_results

            return default_job_results(env, job_ids)
        return Pause("jobs", {"job_ids": job_ids})

    def cancel_jobs(env: LoopEnv, args: dict) -> str:
        done = []
        for job_id in [str(j) for j in args.get("job_ids") or []]:
            job = _project_job(env, job_id)
            if job["status"] not in TERMINAL_JOB_STATUSES:
                env.repo.request_cancel(job_id)
                done.append(job_id)
        return f"已请求取消:{', '.join(done) or '(无未结束的 Job)'}"

    return [
        Tool("run_experiment",
             "在 GPU 上正式运行一组命令(每条一个 Job,经 Runner 排队执行,可过夜,"
             "重启不丢)。命令在工作区目录执行;程序必须把指标写成 JSON 到 "
             "$AUTOLAB_OUTPUT_DIR/<metric_file>(默认 metrics.json),键名与 metrics 一致。"
             "提交后立即返回 Job id,用 wait_jobs 等结果。",
             {"type": "object", "properties": {
                 "jobs": {"type": "array", "items": {"type": "object", "properties": {
                     "name": {"type": "string", "description": "如 kvasir 或 lr1e-4"},
                     "command": {"type": "string"},
                     "dataset": {"type": "string",
                                 "description": "该 Job 跑的数据集(研究阶段必填,用于和该数据集基线对比)"}},
                     "required": ["name", "command"]}},
                 "metrics": {"type": "array", "items": {"type": "object", "properties": {
                     "name": {"type": "string"},
                     "direction": {"type": "string", "enum": ["maximize", "minimize"]}},
                     "required": ["name"]}},
                 "purpose": {"type": "string", "description": "这批实验要验证什么"},
                 "conda_env": {"type": "string"},
                 "metric_file": {"type": "string"},
                 "timeout_minutes": {"type": "integer"},
                 "gpus_per_job": {"type": "integer"}},
              "required": ["jobs", "metrics", "purpose"]},
             run_experiment),
        Tool("wait_jobs", "等待 Job 结束(会话暂停,不占 LLM);结束后返回状态、指标、失败日志尾部。",
             {"type": "object", "properties": {
                 "job_ids": {"type": "array", "items": {"type": "string"}}},
              "required": ["job_ids"]},
             wait_jobs),
        Tool("cancel_jobs", "取消仍在排队/运行的 Job(方向错了及时止损)。",
             {"type": "object", "properties": {
                 "job_ids": {"type": "array", "items": {"type": "string"}}},
              "required": ["job_ids"]},
             cancel_jobs),
    ]


def job_metrics(repo, job_id: str) -> dict:
    """Job 已通过合理性门的指标(scheduler 提取;越域值不会出现在这里)。"""
    metrics: dict = {}
    for artifact in repo.job_artifacts(job_id):
        metrics.update(artifact["metadata"].get("metric_values") or {})
    return metrics
