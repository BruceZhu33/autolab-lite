"""实验:把一组命令包成最小 PlanSpec,经 Plan/Job/Runner 管线执行。

agent 不起草 PlanSpec、不走审批门:submit 以「agent-run:<会话>」名义自动批准
(owner 启动阶段会话即授权会话内的实验),派发 / 排队 / GPU 租约 / 重启恢复 /
指标提取与合理性门全部沿用 scheduler + Runner。每次提交在工作区留一份
.autolab/experiments/EXP-NNNN.md 记录。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field

from autolab_protocol import (
    CodeAnchor,
    MetricDefinition,
    MetricSource,
    PlanOutputs,
    PlanResources,
    PlanSpecV1,
)

from ..core.errors import UserError
from ..core.repo import TERMINAL_JOB_STATUSES, Repo
from ..workspace.fs import Workspace
from ..workspace.host import open_host, q

EXPERIMENTS_REL = ".autolab/experiments"
MAX_JOBS_PER_SUBMIT = 8
_METRIC_NAME = re.compile(r"^[A-Za-z0-9_.\-/]+$")


@dataclass
class ExperimentRequest:
    jobs: list[dict]                 # [{name, command, dataset?}]
    metrics: list                    # ["val_dice"] 或 [{name, direction}]
    purpose: str
    conda_env: str = ""
    metric_file: str = "metrics.json"
    timeout_minutes: int = 120
    gpus_per_job: int = 1
    allowed_datasets: set[str] | None = field(default=None)
    dataset_commands: dict[str, str] | None = field(default=None)  # {数据集: 基线命令}


def list_experiments(ws: Workspace) -> list[str]:
    names = [n[:-3] for n in ws.listdir(EXPERIMENTS_REL) if re.fullmatch(r"EXP-\d+\.md", n)]
    return sorted(names, key=lambda x: int(x.split("-")[1]))


def code_anchor(ws: Workspace) -> CodeAnchor:
    """git 仓库取 commit+branch;否则用文件清单指纹。"""
    head = ws.sh("git rev-parse HEAD 2>/dev/null")
    if head.ok and re.fullmatch(r"[0-9a-f]{7,40}", head.out.strip()):
        branch = ws.sh("git rev-parse --abbrev-ref HEAD 2>/dev/null").out.strip()
        return CodeAnchor(git_commit=head.out.strip(), branch=branch or None)
    listing = ws.sh("find . -maxdepth 2 -type f -not -path './.git/*' -not -path './.autolab/*'"
                    " | sort | head -200 | xargs cksum 2>/dev/null | cksum")
    seed = listing.out.strip() or f"workspace:{ws.workdir}"
    return CodeAnchor(non_git_fingerprint=hashlib.sha256(seed.encode()).hexdigest())


def _clean_jobs(request: ExperimentRequest) -> list[dict]:
    if not request.jobs:
        raise UserError("jobs 不能为空:[{name, command}]")
    if len(request.jobs) > MAX_JOBS_PER_SUBMIT:
        raise UserError(f"一次最多 {MAX_JOBS_PER_SUBMIT} 个 Job,分批提交")
    out, names = [], set()
    for item in request.jobs:
        item = item or {}
        name, command = str(item.get("name") or "").strip(), str(item.get("command") or "").strip()
        if not name or not command:
            raise UserError("每个 Job 需要 name 和 command")
        if name in names:
            raise UserError(f"Job 名重复:{name}")
        names.add(name)
        job = {"name": name, "command": command}
        dataset = str(item.get("dataset") or "").strip()
        if dataset:
            allowed = request.allowed_datasets
            if allowed is not None and dataset not in allowed:
                raise UserError(f"数据集 {dataset} 不在基线确认池里(可选:"
                                f"{', '.join(sorted(allowed)) or '无'})")
            if request.dataset_commands:
                check_dataset_command_consistency(dataset, command, request.dataset_commands)
            job["dataset"] = dataset  # 对照时按它找该数据集的基线
        out.append(job)
    return out


# dataset 标注 vs 命令的「身分参数」比对。真机事故:agent 把 BUSI@10% 的实验
# 标成 BUSI,对照挂到 100% 基线(0.85 vs 0.68),判定链失明三轮才触发停止。
# 身分参数 = 值/存在性在确认池各数据集命令间有差异的 flag=值对
# (如 --config-file configs/BUSI.yaml、--data-percentage 10);seed 类 flag
# (研究本来就要换 seed)和无值 flag(如 python -u)不参与。
_SEED_FLAG_NAMES = frozenset({"seed", "random-seed", "random_seed"})


def _flag_pairs(command: str) -> dict[str, str]:
    """只解析 `--长旗标 值` / `--长旗标=值` 对。单短旗标(-u/-s)与 getopt 的取值
    歧义无法判定,一律不参与——身分比对宁可漏报不可误拦。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return {}
    pairs: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("--"):
            i += 1
            continue
        if "=" in tok:
            name, _, value = tok.partition("=")
        else:
            if i + 1 >= len(tokens) or tokens[i + 1].startswith("-"):
                i += 1
                continue  # 无值旗标不构成数据集身分
            name, value = tok, tokens[i + 1]
            i += 1
        if name.lstrip("-") not in _SEED_FLAG_NAMES:
            pairs[name] = value
        i += 1
    return pairs


def check_dataset_command_consistency(dataset: str, command: str,
                                      commands: dict[str, str]) -> None:
    """标注的 dataset 与命令实际跑的数据口径不一致时 UserError 拦下并点名。"""
    tagged = _flag_pairs(commands.get(dataset) or "")
    others = {name: _flag_pairs(cmd or "") for name, cmd in commands.items()
              if name != dataset}
    all_flags = set(tagged) | {f for pairs in others.values() for f in pairs}
    for flag in sorted(all_flags):
        # 身分参数:存在性或取值与其他数据集不同 → 提交的命令必须与标注数据集一致
        differs = any(flag not in pairs or pairs[flag] != tagged.get(flag)
                      for pairs in others.values()) if others else False
        if not differs:
            continue
        submitted = _flag_pairs(command)
        expected = tagged.get(flag)
        if flag in submitted and (expected is None or submitted[flag] != expected):
            owners = "/".join(sorted(n for n, p in others.items() if p.get(flag) == submitted[flag])
                              ) or "其他数据集"
            raise UserError(
                f"数据集标注不一致:命令带 `{flag} {submitted[flag]}`,但 {dataset} 的基线命令"
                f"{'没有这个参数' if expected is None else f'里它是 `{flag} {expected}`'}"
                f"——该参数属于 {owners}。请把 dataset 改成实际跑的数据集,或修正命令。")
        if flag not in submitted and expected is not None:
            raise UserError(
                f"数据集标注不一致:{dataset} 的基线命令带 `{flag} {expected}`,你的命令没有"
                "——数据口径对不上会让对照失真。请补上该参数,或改 dataset 标注。")


def _clean_metrics(raw: list) -> list[MetricDefinition]:
    out = []
    for item in raw or []:
        item = {"name": item} if isinstance(item, str) else (item or {})
        name = str(item.get("name") or "").strip()
        if not _METRIC_NAME.match(name):
            raise UserError(f"非法指标名:{name!r}(metrics.json 的键名)")
        direction = item.get("direction") or "maximize"
        if direction not in ("maximize", "minimize"):
            raise UserError(f"指标 {name} 的 direction 只能是 maximize/minimize")
        out.append(MetricDefinition(name=name, direction=direction))
    if not out:
        raise UserError("至少声明一个指标(metrics.json 里的键名)")
    return out


def submit(repo: Repo, ws: Workspace, project: dict, request: ExperimentRequest, *,
           approved_by: str, baseline_ref: str = "baseline") -> dict:
    """提交并自动批准;返回 {planId, experimentId, jobs:[{id, name}]}。"""
    jobs = _clean_jobs(request)
    metrics = _clean_metrics(request.metrics)
    metric_file = (request.metric_file or "metrics.json").strip().lstrip("/")
    if ".." in metric_file.split("/"):
        raise UserError("metric_file 必须是输出目录内的相对路径")
    timeout_minutes = max(1, min(int(request.timeout_minutes or 120), 24 * 60))
    gpus = max(1, min(int(request.gpus_per_job or 1), 8))

    prefix = ""
    if request.conda_env.strip():
        home = ws.host.home()
        prefix = (f"source {home}/anaconda3/etc/profile.d/conda.sh 2>/dev/null || "
                  f"source {home}/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
                  f"conda activate {q(request.conda_env.strip())} && ")

    existing = list_experiments(ws)
    exp_id = f"EXP-{(int(existing[-1].split('-')[1]) + 1 if existing else 1):04d}"
    record = "\n".join([
        f"# {exp_id}", "", f"- 来源:{approved_by}", f"- 目的:{request.purpose}",
        f"- 指标:{', '.join(m.name for m in metrics)}", "", "## Jobs", "",
        *[f"- `{j['name']}`{'[' + j['dataset'] + ']' if j.get('dataset') else ''}:"
          f" `{j['command']}`" for j in jobs]]) + "\n"
    body = record.encode("utf-8")
    ws.write_bytes(f"{EXPERIMENTS_REL}/{exp_id}.md", body)

    spec = PlanSpecV1(
        schema_version=1, project_id=project["id"],
        experiment_ref=f"{EXPERIMENTS_REL}/{exp_id}.md",
        experiment_hash=hashlib.sha256(body).hexdigest(),
        hypothesis=request.purpose, change_from_previous=request.purpose,
        baseline=baseline_ref, metrics=metrics,
        success_criteria=[f"{metrics[0].name} 有合法数值"],
        risk_and_fallback="失败日志回给 agent 自行修复",
        code_anchor=code_anchor(ws),
        # 不用 exec:命令可以是 `cd x && python ...` 这类复合形式
        command_template=["bash", "-c", prefix + "{{command}}"],
        job_definition={"kind": "explicit", "jobs": jobs},
        resources=PlanResources(gpu_count=gpus, max_parallel=min(len(jobs), 4)),
        timeout_seconds=timeout_minutes * 60,
        outputs=PlanOutputs(subdir_template="{{job_id}}", metrics=[
            MetricSource(kind="json", path=metric_file, selector=m.name) for m in metrics]),
    )
    plan = repo.create_draft_plan(project_id=project["id"], spec=spec,
                                  summary=request.purpose[:200])
    repo.approve_plan_from_local_user(
        plan["id"], expect_spec_hash=plan["spec_hash"], approved_by=approved_by,
        approval_key=f"{approved_by}:{plan['id']}")
    created = sorted(repo.list_jobs(plan_id=plan["id"]), key=lambda j: j["ordinal"])
    return {"planId": plan["id"], "experimentId": exp_id,
            "jobs": [{"id": j["id"], "name": json.loads(j["params_json"]).get("name")}
                     for j in created]}


def project_job(repo: Repo, project: dict, job_id: str) -> dict:
    job = repo.get_job(job_id)
    plan = repo.get_plan(job["plan_revision_id"]) if job else None
    if plan is None or plan["project_id"] != project["id"]:
        raise UserError(f"Job {job_id} 不存在或不属于本项目")
    return job


def all_terminal(repo: Repo, job_ids: list[str]) -> bool:
    return all((j := repo.get_job(i)) is None or j["status"] in TERMINAL_JOB_STATUSES
               for i in job_ids)


def job_metrics(repo: Repo, job_id: str) -> dict:
    """已通过合理性门的指标(scheduler 提取;越域值不会出现在这里)。"""
    metrics: dict = {}
    for artifact in repo.job_artifacts(job_id):
        metrics.update(artifact["metadata"].get("metric_values") or {})
    return metrics


def job_log_tail(repo: Repo, credentials, project: dict, job: dict, limit: int = 1500) -> str:
    """失败 Job 的日志尾部:优先终态事件自带的 log_tail(免连服务器),
    再读输出目录的 train_stdout.log / runner.log。"""
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT payload_json FROM job_events WHERE job_id = ?"
            " AND event_type IN ('job.failed', 'job.timed_out')"
            " ORDER BY event_seq DESC LIMIT 1", (job["id"],)).fetchone()
    finally:
        conn.close()
    if row is not None:
        tail = str(json.loads(row["payload_json"] or "{}").get("log_tail") or "").strip()
        if tail:
            return tail[-limit:]
    base = (project.get("output_root") or "").rstrip("/")
    if not base:
        return ""
    try:
        with open_host(repo, credentials, project["server_id"]) as host:
            for name in ("train_stdout.log", "runner.log"):
                path = f"{base}/{job['id']}/{name}"
                result = host.run(f"test -s {q(path)} && tail -c {limit} {q(path)}", timeout=20)
                if result.ok and result.out.strip():
                    return result.out.strip()
    except Exception:  # noqa: BLE001 - 取不到日志不影响结果汇总
        return ""
    return ""


def describe_jobs(repo: Repo, credentials, project: dict, job_ids: list[str]) -> str:
    """Job 终态汇总(给 agent 读):状态 + 指标 + 失败日志尾部。"""
    lines = []
    for job_id in job_ids:
        job = repo.get_job(job_id)
        if job is None:
            lines.append(f"- {job_id}: 不存在")
            continue
        line = f"- {job_id} [{job['status']}] params={job['params_json']}"
        metrics = job_metrics(repo, job_id)
        if metrics:
            line += f" metrics={json.dumps(metrics, ensure_ascii=False)}"
        lines.append(line)
        if job["status"] in ("FAILED", "TIMED_OUT"):
            tail = job_log_tail(repo, credentials, project, job)
            if tail:
                lines.append(f"  日志尾部:\n```\n{tail}\n```")
    return "实验已全部结束:\n" + "\n".join(lines)
