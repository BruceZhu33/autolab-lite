"""Iteration 模块:已采纳方案的评审报告 → LLM 实验计划草案 → Plan 体系。

边界(用户规则,不可违反):
- Agent 只产 DRAFT,绝不批准 Plan(批准唯一入口是 owner 的 spec_hash 确认);
- 失败也是资产:结论写回实验文件,失败经验进 knowledge/failures.md,历史不删。

工作区文件(.autolab/experiments/、knowledge/)在远端(SSH SFTP)或本地,
两栖的读写助手统一走 transport 参数(与 server_admin 的既有模式一致)。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any

from autolab_client import research
from autolab_client.templates import experiment_markdown
from autolab_protocol import (
    CodeAnchor,
    ExplicitJobs,
    ExpansionError,
    MatrixJobs,
    MetricDefinition,
    MetricSource,
    PlanOutputs,
    PlanResources,
    PlanSpecV1,
    expand_jobs,
    render_template,
)

from . import server_admin
from .credentials import CredentialStore
from .repo import TERMINAL_JOB_STATUSES, Repo, experiment_name

EXPERIMENTS_REL = ".autolab/experiments"
KNOWLEDGE_FAILURES_REL = ".autolab/knowledge/failures.md"
PATCHES_REL = ".autolab/patches"
_CONTEXT_REPORT_LIMIT = 12000
_CONTEXT_EXPERIMENT_LIMIT = 2500
_LLM_MAX_TOKENS = 3500
_MAX_EXTRACT_ATTEMPTS = 3
# Agent 实现方案改动的边界:小而可审
_IMPL_MAX_FILES = 6
_IMPL_FILE_LIMIT = 24000
_IMPL_CONTEXT_FILES = 10
_IMPL_CONTEXT_LIMIT = 12000


class IterationError(RuntimeError):
    """面向用户的失败信息(可操作)。"""


# --------------------------------------------------------------- LLM 草案 schema

class _DraftMetric:
    __slots__ = ("name", "direction", "target")

    def __init__(self, name: str, direction: str, target: float | None):
        self.name = name
        self.direction = direction
        self.target = target


def parse_plan_draft(text: str) -> dict[str, Any]:
    """从模型输出提取 ```json 代码块并做结构性校验;失败抛 IterationError。

    校验口径:研究性必填字段缺失/为空、metrics 为空、command 为空或含换行、
    job_definition 不合法 → 报错(触发一次重试);数值类字段缺省给保守默认。
    """
    data = _extract_json_block(text)
    if not isinstance(data, dict):
        raise IterationError("模型输出不是 JSON 对象")

    def need(key: str) -> str:
        value = str(data.get(key) or "").strip()
        if not value:
            raise IterationError(f"缺少必填字段 {key} 或为空")
        return value

    draft: dict[str, Any] = {
        "summary": str(data.get("summary") or "").strip()[:120],
        "experiment_title": need("experiment_title")[:120],
        "hypothesis": need("hypothesis"),
        "change_from_previous": need("change_from_previous"),
        "datasets_and_metrics": str(data.get("datasets_and_metrics") or "").strip(),
        "risk_and_fallback": need("risk_and_fallback"),
        "command": need("command"),
    }
    criteria = data.get("success_criteria")
    if isinstance(criteria, str):
        criteria = [criteria]
    if not isinstance(criteria, list) or not [
        c for c in criteria if str(c).strip()
    ]:
        raise IterationError("success_criteria 需为非空字符串数组")
    draft["success_criteria"] = [str(c).strip() for c in criteria if str(c).strip()]

    raw_metrics = data.get("metrics")
    if not isinstance(raw_metrics, list) or not raw_metrics:
        raise IterationError("metrics 需为非空数组")
    metrics: list[_DraftMetric] = []
    for i, item in enumerate(raw_metrics):
        if not isinstance(item, dict):
            raise IterationError(f"metrics[{i}] 需为对象")
        name = str(item.get("name") or "").strip()
        if not name:
            raise IterationError(f"metrics[{i}].name 为空")
        direction = str(item.get("direction") or "maximize").strip()
        if direction not in ("maximize", "minimize", "target"):
            raise IterationError(f"metrics[{i}].direction 非法:{direction}")
        target = item.get("target")
        if direction == "target" and not isinstance(target, (int, float)):
            raise IterationError(f"metrics[{i}] direction=target 需要 target 数值")
        metrics.append(_DraftMetric(name, direction, target))
    draft["metrics"] = metrics

    source = data.get("metric_source")
    if not isinstance(source, dict):
        source = {}
    kind = str(source.get("kind") or "json").strip()
    path = str(source.get("path") or "").strip().lstrip("./")
    selector = str(source.get("selector") or "").strip()
    if kind not in ("json", "csv", "regex"):
        raise IterationError(f"metric_source.kind 非法:{kind}")
    if not path or path.startswith("/"):
        raise IterationError("metric_source.path 需为相对路径")
    if kind in ("json", "regex") and not selector:
        raise IterationError(f"metric_source.kind={kind} 需要 selector")
    draft["metric_source"] = {"kind": kind, "path": path, "selector": selector}

    command = draft["command"]
    if "\n" in command:
        raise IterationError("command 不能包含换行")
    if re.search(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-f[a-zA-Z]*r)[a-zA-Z]*\s+/(?:\s|$)",
                 command):
        raise IterationError("command 含破坏性命令(rm -rf /),已拒绝")
    draft["command"] = " ".join(command.split())

    # 联合迭代:按数据集给命令。形状不对时宽容忽略——单数据集流程
    # 不需要它;多数据集的覆盖要求由 _llm_plan_draft 按 required_datasets
    # 校验并带着明确错误重试(避免模型随手填个字符串卡死整轮)
    commands = data.get("commands")
    if isinstance(commands, dict) and commands:
        cleaned_commands = {}
        for key, value in commands.items():
            name = str(key).strip()
            raw_text = str(value or "")
            if not name or not raw_text.strip() or "\n" in raw_text:
                continue  # 坏条目丢弃,缺失的由覆盖校验兜底
            cleaned_commands[name] = " ".join(raw_text.split())
        draft["commands"] = cleaned_commands
    else:
        draft["commands"] = {}

    job_definition = data.get("job_definition")
    try:
        if isinstance(job_definition, dict) and job_definition.get("kind") == "matrix":
            MatrixJobs.model_validate(job_definition)
        elif isinstance(job_definition, dict) and job_definition.get("kind") == "explicit":
            ExplicitJobs.model_validate(job_definition)
        else:
            raise IterationError("job_definition 需为 matrix 或 explicit")
    except IterationError:
        raise
    except Exception as exc:
        raise IterationError(f"job_definition 不合法:{exc}") from exc
    draft["job_definition"] = job_definition

    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        artifacts = [path]
    draft["artifacts"] = [str(a).strip().lstrip("./") for a in artifacts if str(a).strip()]

    def clamp_int(key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(data.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    draft["timeout_seconds"] = clamp_int("timeout_seconds", 3600, 60, 30 * 24 * 3600)
    draft["gpu_count"] = clamp_int("gpu_count", 1, 1, 8)
    draft["max_parallel"] = clamp_int("max_parallel", 1, 1, 8)
    conda_env = str(data.get("conda_env") or "").strip()
    if conda_env and not re.fullmatch(r"[A-Za-z0-9._-]+", conda_env):
        raise IterationError(f"conda_env 非法:{conda_env}")
    draft["conda_env"] = conda_env
    return draft


def _extract_json_block(text: str) -> Any:
    blocks = re.findall(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    for block in blocks:
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            continue
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise IterationError(f"输出中找不到合法 JSON:{exc}") from exc


# ------------------------------------------------------------- LLM 编排

_PLAN_SYSTEM_PROMPT = """你是 AutoLab 的自动迭代 Agent。任务:读入执行依据(项目方向,或老项目已采纳方案的评审报告)与项目事实,为下一轮迭代实验生成一份可直接执行的实验计划,输出为一个 JSON 对象。

必须遵守:
1. 只输出一个 ```json``` 代码块,不要输出任何其他文字。
2. 改动以现有工作区代码为载体(执行依据即改进计划);依据/材料里没有的事实不要编造,不确定的字段按 Baseline 事实保守取值。
3. command 是一条 shell 命令,工作目录为项目工作区;矩阵参数用 {{参数名}} 引用,且每个参数名必须出现在 job_definition.parameters 的键里;command 引用的每个参数都必须在 job_definition 中定义。
4. 训练/评估脚本必须把指标 JSON 写到 $AUTOLAB_OUTPUT_DIR/<metric_source.path>(系统已注入该环境变量,参考 Baseline 的运行方式)。
5. 不要在 command 里激活 conda / 改环境变量(系统会按 Baseline 配置自动包装)。
6. metric_source.selector 必须是指标 JSON 里真实存在的键,且与 metrics[0].name 一致,便于与基线逐项对比。
7. **数据集范围锁定**:实验必须且只能在上下文"范围约束"列出的选定数据集上进行(它们都来自 Baseline 已跑通的数据集);不得切换到其他数据集来"更容易超越基线",也不得缩小范围。
8. **多数据集联合迭代**(范围约束列出多个数据集时):commands 字段必填——为每个选定数据集分别给出运行命令,以该数据集自己的 Baseline 命令为基础改写(如换 --config-file),参数占位符与 job_definition 一致;单数据集时 commands 留空,用 command 字段。
9. **seed 对齐(硬性)**:基线跑了哪些 seed,本轮矩阵就只能用那些 seed(上下文"范围约束"给出)。基线是单 seed 时矩阵就是那一个 seed——多 seed 对单 seed 基线是多次抽样对单次抽样,指标波动就能刷出假"改进",属于不公平比较。要多种子稳健性,正确做法是先给基线补 seed,而不是只给迭代加。

输出 JSON 字段(全部必填,conda_env/commands 按条件可选):
{
  "summary": "≤60字计划摘要",
  "experiment_title": "实验标题",
  "hypothesis": "可证伪的假设,2-4 句",
  "change_from_previous": "相对基线只改什么、保持什么不变",
  "datasets_and_metrics": "数据集、指标与方向说明",
  "success_criteria": ["事前可判定的成功条件"],
  "risk_and_fallback": "主要风险与回退方式",
  "metrics": [{"name": "主指标名", "direction": "maximize|minimize|target", "target": null}],
  "metric_source": {"kind": "json", "path": "metrics.json", "selector": "主指标名"},
  "command": "python3 train.py --seed {{seed}}",
  "commands": {"数据集名": "python3 train.py --config {{该数据集配置}} --seed {{seed}}"},
  "job_definition": {"kind": "matrix", "parameters": {"seed": [1, 2, 3]}},
  "artifacts": ["metrics.json"],
  "timeout_seconds": 3600,
  "gpu_count": 1,
  "max_parallel": 1,
  "conda_env": "(可选)上下文『服务器可用训练环境』列表中的环境名;训练依赖该环境时必须填,留空则用 Baseline 配置"
}

job_definition 也可为 {"kind": "explicit", "jobs": [{"seed": 1}, ...]}。矩阵参数默认与基线完全同 seed/同配置,只改方案涉及的变量。"""


def build_plan_user_prompt(context: str) -> str:
    return context + "\n\n请给出下一轮迭代实验计划(只输出 ```json``` 代码块)。"


def baseline_seeds(config: dict, pool: dict[str, dict] | None = None) -> list[int]:
    """基线实际跑过的 seed:从各数据集命令的 --seed N 与确认快照指标的
    seed 字段提取(用于迭代的 seed 对齐校验)。空 = 未知,不设限。"""
    seeds: set[int] = set()
    sources: list[str] = []
    if pool:
        sources.extend(str((v or {}).get("command") or "") for v in pool.values())
        for v in (pool or {}).values():
            metrics = (v or {}).get("metrics") or {}
            raw = metrics.get("seed")
            if isinstance(raw, int):
                seeds.add(raw)
    else:
        sources.append(str((config or {}).get("command") or ""))
    for command in sources:
        for match in re.finditer(r"(?:^|\s)--seed[= ](\d+)", command):
            seeds.add(int(match.group(1)))
    return sorted(seeds)


def _draft_seeds(draft: dict[str, Any]) -> list[int] | None:
    """草案里实际用到的 seed(job_definition 的 seed 参数值);无 seed 维 → None。"""
    jd = draft.get("job_definition") or {}
    values: set[int] = set()
    if isinstance(jd, dict) and jd.get("kind") == "matrix":
        for value in (jd.get("parameters") or {}).get("seed") or []:
            if isinstance(value, int):
                values.add(value)
    elif isinstance(jd, dict) and jd.get("kind") == "explicit":
        for job in jd.get("jobs") or []:
            if isinstance(job, dict) and isinstance(job.get("seed"), int):
                values.add(job["seed"])
    return sorted(values) if values else None


def _llm_plan_draft(credentials: CredentialStore, context: str,
                    agent_key_ref: str | None,
                    required_datasets: list[str] | None = None,
                    allowed_seeds: list[int] | None = None) -> dict[str, Any]:
    """LLM → 解析校验(失败带错误重试一次)。required_datasets 非空时
    还要求 commands 覆盖每个选定数据集(联合迭代);allowed_seeds 非空时
    矩阵的 seed 只能取基线跑过的 seed(公平比较)。"""
    from . import llm as _llm

    def validate(draft: dict[str, Any]) -> None:
        if required_datasets:
            missing = [n for n in required_datasets
                       if not draft.get("commands", {}).get(n)]
            if missing:
                raise IterationError(
                    "多数据集联合迭代要求 commands 覆盖每个选定数据集,缺少:"
                    + "、".join(missing))
        if allowed_seeds is not None:
            used = _draft_seeds(draft)
            if used is not None:
                beyond = [s for s in used if s not in allowed_seeds]
                if beyond:
                    raise IterationError(
                        "seed 对齐失败:基线只跑过 seed "
                        + "、".join(str(s) for s in allowed_seeds)
                        + ",草案却用了 " + "、".join(str(s) for s in beyond)
                        + "。多 seed 对单 seed 基线是不公平比较,"
                        "矩阵的 seed 必须与基线完全一致(要多种子先给基线补)。")
        # 空参数值会在 manifest 展开成空 argv(对照档陷阱):起草时就拦,
        # "无开关"对照用省略该 flag 表达,不要给空字符串档位
        empty_vals = [
            f"{name}={v!r}" for name, values in
            (draft.get("job_definition", {}).get("parameters") or {}).items()
            if isinstance(values, list)
            for v in values if isinstance(v, str) and not v.strip()
        ]
        if empty_vals:
            raise IterationError(
                "矩阵参数值不得为空字符串(展开会生成空命令行参数):"
                + "、".join(empty_vals[:5])
                + "。要表达\"无该开关\"的对照档,请写不含该 flag 的取值"
                  "(如 \"--aug\" 与无 aug 字样各一档,分两个参数组合)。")

    messages = [{"role": "user", "content": build_plan_user_prompt(context)}]
    last_error = ""
    for _attempt in range(2):
        if last_error:
            messages.append({"role": "user",
                             "content": f"你上次的输出无法使用:{last_error}\n"
                                        "请严格按 system 中的 schema 只输出一个 ```json``` 代码块。"})
        try:
            reply = _llm.chat_multi(credentials, system=_PLAN_SYSTEM_PROMPT,
                                    messages=messages, agent_key_ref=agent_key_ref,
                                    max_tokens=_LLM_MAX_TOKENS, timeout=180.0)
        except _llm.LlmError:
            raise
        try:
            draft = parse_plan_draft(reply)
            validate(draft)
            return draft
        except IterationError as exc:
            last_error = str(exc)
            continue
    raise IterationError(
        f"模型两次输出均无法解析为实验计划:{last_error[:200]}。"
        "可重试,或先在方向页完善方向陈述。"
    )


def _verify_cli_contract(command_text: str, parameters: dict,
                         workdir: str, exec_sh) -> list[str]:
    """计划契约核验(D-024 P1,--aug 事故对策):
    a) 模板占位符 {{p}} 与矩阵 parameters 一一对应(双向);
    b) 命令里的每个 --flag 必须在入口脚本文本中出现(argparse 定义或
       字面引用)——编造参数导致整批 Job exit 2 的事后拦截前移到起草时。
    返回问题列表(空=通过)。入口读不到时仅做 a)。"""
    import re as _re

    issues: list[str] = []
    params = set(parameters or {})
    placeholders = set(_re.findall(r"\{\{(\w+)\}\}", command_text or ""))
    only_template = placeholders - params
    only_matrix = params - placeholders
    if only_template:
        issues.append("模板占位符未在矩阵 parameters 中定义:"
                      + "、".join(sorted(only_template)))
    if only_matrix:
        issues.append("矩阵参数未出现在命令模板中:" + "、".join(sorted(only_matrix)))
    # CLI flags:全部核验(占位符的值不是 flag;--mode {{mode}} 里的
    # --mode 本身就是需要核验的参数名)
    flags = set(_re.findall(r"(?<!\w)(--[A-Za-z][\w-]*)", command_text or ""))
    if not flags:
        return issues
    # 入口脚本 = 第一个 .py token(conda 前缀/包装之后)
    entry = next((tok for tok in (command_text or "").split()
                  if tok.endswith(".py")), "")
    entry_text = ""
    if entry:
        import shlex as _shlex

        try:
            code, out = exec_sh(
                f"cd {_shlex.quote(workdir)} && head -c 20000 {_shlex.quote(entry)}"
                " 2>/dev/null")
            if code == 0:
                entry_text = out or ""
        except Exception:
            entry_text = ""
    if not entry_text:
        return issues  # 读不到入口:只做占位符核验(保守放行 CLI 检查)
    for flag in sorted(flags):
        if flag not in entry_text:
            issues.append(f"命令参数 {flag} 在入口脚本 {entry} 中不存在"
                          "(argparse 未定义且无引用)——疑似编造参数")
    # REMAINDER 顺序陷阱静态检查(D-026):yacs opts 型位置参数(如
    # TRAIN.NUM_EPOCHS)之后的具名参数会被 REMAINDER 入口吞掉——两次
    # 真机事故同根因(--aug / --hflip)。入口含 REMAINDER 定义时,
    # 模板里出现在位置参数之后的 --flag 一律拒收。
    if "REMAINDER" in entry_text:
        tokens = (command_text or "").split()
        first_opts = next(
            (i for i, t in enumerate(tokens)
             if not t.startswith("-")
             and _re.match(r"[A-Z][A-Z0-9_]*\.[A-Z0-9_]+$", t)), None)
        if first_opts is not None:
            trailing = sorted({t for t in tokens[first_opts:]
                               if t.startswith("--")})
            if trailing:
                issues.append(
                    f"具名参数 {' '.join(trailing)} 出现在位置参数 "
                    f"{tokens[first_opts]} 之后:该入口用 argparse.REMAINDER,"
                    "之后的具名参数会被静默吞掉;必须移到位置参数之前"
                    "(或入口改为转换而非丢弃)")
    return issues


def _draft_with_verification(repo, credentials, hostkeys_path, project,
                             context, agent_key_ref, workdir, ssh_transport,
                             *, required_datasets, allowed_seeds) -> dict:
    """会话优先起草 + 契约硬核验;核验失败带错误重试一次;
    会话层失败回落单发(纪律:新层挂死不影响旧路径)。"""
    exec_sh = _exec_sh(ssh_transport)
    last_issues: list[str] = []
    for attempt in range(2):
        feedback = ""
        if last_issues:
            feedback = ("\n\n## 上次草案核验未过,必须修正\n- "
                        + "\n- ".join(last_issues)
                        + "\n请修正后重新提交。")
        draft = None
        try:
            from . import agent_session

            draft = agent_session.run_planner_session(
                repo, credentials, hostkeys_path, project,
                context=context + feedback)
        except Exception as exc:
            repo.add_review_message(
                project_id=project["id"], role="system", kind="text",
                content=f"(计划会话模式不可用,回落单发路径:{str(exc)[:120]})")
        if draft is None:
            draft = _llm_plan_draft(
                credentials, context + feedback, agent_key_ref,
                required_datasets=required_datasets,
                allowed_seeds=allowed_seeds)
        # 契约核验:联合迭代(commands)逐条核;单数据集核模板+矩阵
        if required_datasets:
            last_issues = []
            for name, cmd in (draft.get("commands") or {}).items():
                last_issues += _verify_cli_contract(
                    cmd, (draft.get("job_definition") or {}).get("parameters")
                    or {}, workdir, exec_sh)
        else:
            last_issues = _verify_cli_contract(
                draft.get("command") or "",
                (draft.get("job_definition") or {}).get("parameters") or {},
                workdir, exec_sh)
        if not last_issues:
            return draft
    raise IterationError(
        "计划草案两次未过契约核验(模板↔矩阵↔CLI):"
        + ";".join(last_issues[:4])[:300])


# --------------------------------------------------------- 工作区两栖读写助手

def _exec_sh(ssh_transport) -> Any:
    return (server_admin._ssh_exec_sh(ssh_transport) if ssh_transport is not None
            else server_admin._local_exec_sh())


def _read_bytes(workdir: str, rel: str, ssh_transport) -> bytes:
    path = f"{workdir.rstrip('/')}/{rel}"
    if ssh_transport is not None:
        return ssh_transport.sftp_read(path)
    return Path(path).read_bytes()


def _write_bytes(workdir: str, rel: str, content: bytes, ssh_transport) -> None:
    path = f"{workdir.rstrip('/')}/{rel}"
    if ssh_transport is not None:
        ssh_transport.sftp_write(path, content)
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def list_experiments(workdir: str, *, ssh_transport=None) -> list[str]:
    """工作区已有的实验 id(EXP-0001 形式),按编号排序。"""
    root = f"{workdir.rstrip('/')}/{EXPERIMENTS_REL}"
    try:
        if ssh_transport is not None:
            names = ssh_transport.sftp_listdir(root)
        else:
            names = [p.name for p in Path(root).iterdir()]
    except Exception:
        return []
    ids = []
    for name in names:
        match = re.fullmatch(r"(EXP-\d+)\.md", name)
        if match:
            ids.append(match.group(1))
    return sorted(ids, key=lambda x: int(x.split("-")[1]))


def read_experiment_conclusion(workdir: str, experiment_id: str, *,
                               ssh_transport=None) -> dict[str, Any] | None:
    """读实验文件 front matter 的 conclusion/next_decision;文件缺失返回 None。"""
    try:
        raw = _read_bytes(workdir, f"{EXPERIMENTS_REL}/{experiment_id}.md", ssh_transport)
        front, _body = research.parse_front_matter(
            raw.decode("utf-8", "replace"), Path(f"{experiment_id}.md"))
    except Exception:
        return None
    return {"conclusion": front.get("conclusion"),
            "next_decision": front.get("next_decision")}


def workspace_code_anchor(workdir: str, exec_sh) -> CodeAnchor:
    """git 仓库取 commit+branch;否则用文件清单+cksum 指纹(与 CLI 的
    non_git_fingerprint 同语义,可跨 SSH/本地执行)。"""
    code, out = exec_sh(f"git -C {shlex.quote(workdir)} rev-parse HEAD 2>/dev/null")
    if code == 0 and re.fullmatch(r"[0-9a-f]{7,40}", out.strip()):
        _, branch = exec_sh(
            f"git -C {shlex.quote(workdir)} rev-parse --abbrev-ref HEAD 2>/dev/null")
        return CodeAnchor(git_commit=out.strip(),
                          branch=branch.strip() or None)
    code, out = exec_sh(
        f"cd {shlex.quote(workdir)} && find . -maxdepth 2 -type f "
        "-not -path './.git/*' -not -path './.autolab/*' | sort | head -200 "
        "| xargs cksum 2>/dev/null | cksum")
    digest_input = out.strip() if code == 0 and out.strip() else f"workspace:{workdir}"
    return CodeAnchor(non_git_fingerprint=hashlib.sha256(
        digest_input.encode("utf-8")).hexdigest())


_SHELL_METACHARS = re.compile(r"[&|;<>()$`]")


def build_command_template(command: str, conda_env: str, home: str) -> list[str]:
    """LLM 的单条 shell 命令 → PlanSpecV1.command_template。

    conda 环境按 Baseline 的激活链包进 bash -c(conda.sh 是 bash 语法,
    Ubuntu 的 /bin/sh=dash 下 source 不可用;模板参数照常逐元素渲染);
    含 shell 元字符的命令同样走 bash -c,纯 argv 则 shlex.split。
    """
    if conda_env:
        activate = (
            f"source {home}/anaconda3/etc/profile.d/conda.sh 2>/dev/null || "
            f"source {home}/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
            f"conda activate {shlex.quote(conda_env)}"
        )
        return ["bash", "-c", f"{activate} && exec {command}"]
    if _SHELL_METACHARS.search(command):
        return ["bash", "-c", command]
    return shlex.split(command)


def build_parametrized_command_template(conda_env: str, home: str) -> list[str]:
    """联合迭代模板:命令体由每个 job 的 {{command}} 参数提供
    (各数据集命令结构可能完全不同,不能共用一条字面命令)。"""
    prefix = ""
    if conda_env:
        prefix = (
            f"source {home}/anaconda3/etc/profile.d/conda.sh 2>/dev/null || "
            f"source {home}/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
            f"conda activate {shlex.quote(conda_env)}; "
        )
    return ["bash", "-c", f"{prefix}exec {{{{command}}}}"]


def _explicit_jobs_for_datasets(draft: dict[str, Any], datasets: list[str],
                                pool: dict[str, dict]) -> dict:
    """联合迭代 job 展开:数据集 × LLM 参数矩阵,每个 job 携带
    {dataset, command(渲染完参数的完整命令), ...矩阵参数}。

    两个硬校验(踩过的坑):
    - LLM 的 explicit job 行里若自带 dataset/command 键,会被丢弃——
      数据集归属只能由选定的数据集决定,命令只能来自 commands[数据集];
    - 多数据集时,各数据集渲染出的命令必须互不相同——LLM 曾把数据集
      差异参数(如 --data_percentage)挪进矩阵却不放进命令,导致 9 个
      job 跑同一份全量训练;发现即报错重试。
    """
    commands = draft.get("commands") or {}
    jd = draft["job_definition"]
    if isinstance(jd, dict) and jd.get("kind") == "matrix":
        matrix = MatrixJobs.model_validate(jd)
        rows: list[dict] = [{}]
        for name in sorted(matrix.parameters.keys()):  # 与协议展开同序
            rows = [{**row, name: value} for row in rows
                    for value in matrix.parameters[name]]
    else:  # explicit:LLM 已给出逐 job 参数(仅取参数,不取其 dataset/command)
        rows = [{k: v for k, v in (job or {}).items()
                 if k not in ("dataset", "command")}
                for job in (jd or {}).get("jobs") or []]
    jobs: list[dict] = []
    rendered_by_dataset: dict[str, set[str]] = {}
    for name in datasets:
        template = str(commands.get(name) or "").strip()
        if not template:
            raise IterationError(f"草案缺少数据集 {name} 的运行命令(commands.{name})")
        rendered_by_dataset.setdefault(name, set())
        for row in rows:
            try:
                rendered = render_template(template, row)
            except ExpansionError as exc:
                raise IterationError(f"数据集 {name} 命令渲染失败:{exc}") from exc
            if rendered in rendered_by_dataset[name]:
                continue  # 行参数没进命令(如百分比轴残留):同命令不重复排队
            rendered_by_dataset[name].add(rendered)
            jobs.append({"dataset": name, "command": rendered, **row})
    if len(datasets) > 1:
        owner_of: dict[str, str] = {}  # rendered 命令 -> 数据集名
        clash = None
        for name, rendered_set in rendered_by_dataset.items():
            for rendered in rendered_set:
                if rendered in owner_of and owner_of[rendered] != name:
                    clash = rendered
                    break
                owner_of[rendered] = name
            if clash:
                break
        if clash:
            raise IterationError(
                "联合迭代校验失败:不同数据集渲染出了相同命令("
                f"{clash[:200]})。每个数据集的命令必须基于它自己的 Baseline "
                "命令改写(保留区分数据集的参数,如 --config-file/"
                "--data_percentage),不得把数据集差异挪进矩阵参数却在命令里丢失。")
    return {"kind": "explicit", "jobs": jobs}


# ------------------------------------------------------------- 实验文件

def baseline_experiment_markdown(experiment_id: str, metrics: dict, config: dict,
                                 proposal_title: str) -> str:
    """基线实验记录:项目还没有实验文件时补建,作为迭代的对照锚点。"""
    metric_lines = "\n".join(f"- {k} = {v}" for k, v in (metrics or {}).items()) or "- (无记录)"
    sections = {
        "hypothesis": "在当前代码与默认配置下,基线可在既有数据集上跑通并获得基准指标,"
                      "作为后续迭代轮的对照。",
        "change_from_previous": "首轮记录,无上一轮;本实验即基线。",
        "baseline": "本实验即基线(由 Baseline 模块真实运行获得)。",
        "datasets_and_metrics": (
            f"运行命令:`{config.get('command') or '(见 Baseline 配置)'}`;"
            f"指标文件:`{config.get('metric_file') or 'metrics.json'}`。\n{metric_lines}"
        ),
        "success_criteria": "- 至少在 1 个数据集上跑通并获得基准模型的对应指标。",
        "risk_and_fallback": "跑不通则回到 Baseline 页检查环境/数据集后重跑。",
        "evidence": f"- Baseline 模块运行产出指标:\n{metric_lines}",
        "analysis": f"基线指标来自 Baseline 模块的一次真实运行;"
                    f"对应已采纳方案《{proposal_title}》,其评审报告是下一轮迭代的执行依据。",
    }
    return experiment_markdown(
        experiment_id=experiment_id, title="Baseline 基线记录",
        created_at=research.utc_now_stamp(), parent_experiment_id=None,
        sections=sections)


def iteration_experiment_markdown(experiment_id: str, draft: dict[str, Any],
                                  baseline_exp: str, baseline_metrics: dict) -> str:
    metric_summary = "、".join(f"{k}={v}" for k, v in (baseline_metrics or {}).items()) or "无"
    sections = {
        "hypothesis": draft["hypothesis"],
        "change_from_previous": draft["change_from_previous"],
        "baseline": f"{baseline_exp} 基线实验:{metric_summary}(以该轮为对照)",
        "datasets_and_metrics": draft["datasets_and_metrics"] or (
            "指标:" + "、".join(m.name for m in draft["metrics"])),
        "success_criteria": "\n".join(f"- {c}" for c in draft["success_criteria"]),
        "risk_and_fallback": draft["risk_and_fallback"],
        "analysis": f"本计划由自动迭代 Agent 依据已采纳方案的评审报告生成,"
                    f"摘要:{draft['summary'] or draft['experiment_title']}。",
    }
    return experiment_markdown(
        experiment_id=experiment_id, title=draft["experiment_title"],
        created_at=research.utc_now_stamp(), parent_experiment_id=baseline_exp,
        sections=sections)


# ------------------------------------------------- Agent 实现方案改动(改代码)

_IMPLEMENT_SYSTEM_PROMPT = """你是 AutoLab 的实现 Agent。任务:依据已采纳方案的评审报告,在项目工作区里真实修改/新建代码文件,把方案实现出来(报告即执行计划)。

必须遵守:
1. 只输出一个 ```json 代码块,不要任何其他文字。
2. changes 里每个文件给出**完整新内容**(不要 diff、不要省略号、不要"其余不变");新建文件用 action=create,修改用 action=modify。
3. 只改实现方案必需的文件(通常 1-4 个:入口脚本/配置/新模块);数据、模型权重、.autolab/ 下的研究文件、README 一律不碰。
4. 训练入口必须保留把指标 JSON 写到 $AUTOLAB_OUTPUT_DIR/<metric_file> 的行为(环境变量已注入)。
5. 代码要能在该工作区现有运行方式下直接跑(python3 + 现有依赖;不要引入工作区没有的第三方库,除非 requirements 里已有)。
6. path 一律是工作区相对路径(不带 ./、不绝对、无 ..)。

输出 JSON 字段(全部必填):
{
  "summary": "≤80字:实现了什么、怎么实现",
  "changes": [
    {"path": "configs/config.yaml", "action": "create", "reason": "收敛超参", "content": "文件完整内容"},
    {"path": "train.py", "action": "modify", "reason": "从配置读取超参", "content": "新完整内容"}
  ]
}"""


def parse_changes_payload(text: str) -> dict[str, Any]:
    """校验实现 Agent 的输出;失败抛 IterationError(触发修复回路)。"""
    data = _extract_json_block(text)
    if not isinstance(data, dict):
        raise IterationError("实现输出不是 JSON 对象")
    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise IterationError("缺少 summary")
    raw_changes = data.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise IterationError("changes 需为非空数组")
    if len(raw_changes) > _IMPL_MAX_FILES:
        raise IterationError(f"一次最多改 {_IMPL_MAX_FILES} 个文件(当前 {len(raw_changes)})")
    changes: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_changes):
        if not isinstance(item, dict):
            raise IterationError(f"changes[{i}] 需为对象")
        path = str(item.get("path") or "").strip().lstrip("./")
        action = str(item.get("action") or "").strip()
        content = item.get("content")
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise IterationError(f"changes[{i}].path 非法:{path}")
        if path in seen:
            raise IterationError(f"changes[{i}].path 重复:{path}")
        if action not in ("create", "modify"):
            raise IterationError(f"changes[{i}].action 需为 create/modify")
        if not isinstance(content, str) or not content.strip():
            raise IterationError(f"changes[{i}].content 为空(必须给完整文件内容)")
        if len(content) > _IMPL_FILE_LIMIT:
            raise IterationError(f"changes[{i}].content 超过 {_IMPL_FILE_LIMIT // 1000}KB 上限")
        seen.add(path)
        changes.append({"path": path, "action": action,
                        "reason": str(item.get("reason") or "").strip()[:200],
                        "content": content})
    return {"summary": summary[:200], "changes": changes}


def _collect_workspace_sources(workdir: str, config: dict, exec_sh) -> dict[str, str]:
    """给实现 Agent 的工作区源码上下文:入口脚本 + py/yaml 源文件(截断)。"""
    workdir = workdir.rstrip("/")
    targets: list[str] = []
    for token in (config.get("command") or "").split():
        if token.endswith((".py", ".sh")) and "/" not in token:
            targets.append(token)
    listing = exec_sh(
        f"cd {shlex.quote(workdir)} && find . -maxdepth 2 -type f "
        "\\( -name '*.py' -o -name '*.yaml' -o -name '*.yml' -o -name '*.sh' "
        "-o -name '*.json' -o -name '*.toml' \\) "
        "-not -path './.git/*' -not -path './.autolab/*' -not -path './output/*' "
        "| sort | head -30")[1]
    for line in listing.splitlines():
        name = line.strip().lstrip("./")
        if name and name not in targets:
            targets.append(name)
    files: dict[str, str] = {}
    total = 0
    for name in targets:
        if len(files) >= _IMPL_CONTEXT_FILES or total >= _IMPL_CONTEXT_LIMIT:
            break
        code, out = exec_sh(
            f"head -c {_IMPL_FILE_LIMIT} {shlex.quote(workdir + '/' + name)} 2>/dev/null")
        if code == 0 and out.strip():
            files[name] = out
            total += len(out)
    return files


def _impl_python_bin(config: dict, home: str) -> str:
    env = (config.get("conda_env") or "").strip()
    if env and home:
        return f"{home}/anaconda3/envs/{env}/bin/python"
    return "python3"


def _write_patch(workdir: str, change: dict[str, str], patch_abs: str,
                 ssh_transport) -> None:
    """写入改动,原文件(若存在)备份到 patch 目录保持原相对路径。"""
    rel = change["path"]
    try:
        original = _read_bytes(workdir, rel, ssh_transport)
    except Exception:
        original = None  # create
    if original is not None:
        _write_abs(f"{patch_abs}/orig/{rel}", original, ssh_transport)
    _write_abs(f"{workdir.rstrip('/')}/{rel}",
               change["content"].encode("utf-8"), ssh_transport)


def _rollback_patch(workdir: str, changes: list[dict[str, str]], patch_abs: str,
                    ssh_transport) -> None:
    """恢复本次写入的文件:修改的还原备份,新建的删除。"""
    for change in reversed(changes):
        rel = change["path"]
        try:
            original = _read_abs(f"{patch_abs}/orig/{rel}", ssh_transport)
        except Exception:
            original = None
        if original is not None:
            _write_abs(f"{workdir.rstrip('/')}/{rel}", original, ssh_transport)
        else:
            _delete_abs(f"{workdir.rstrip('/')}/{rel}", ssh_transport)


def _read_abs(path: str, ssh_transport) -> bytes:
    if ssh_transport is not None:
        return ssh_transport.sftp_read(path)
    return Path(path).read_bytes()


def _write_abs(path: str, content: bytes, ssh_transport) -> None:
    if ssh_transport is not None:
        ssh_transport.sftp_write(path, content)
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _delete_abs(path: str, ssh_transport) -> None:
    if ssh_transport is not None:
        ssh_transport.exec_argv(["rm", "-f", path], timeout=15)
    else:
        Path(path).unlink(missing_ok=True)


def _yaml_gate(old_content: bytes | None, new_content: bytes) -> str | None:
    """yaml 配置门:① 必须可解析;② 顶层段只增不减(补丁误删大半配置的
    事故防线——正常调参不会删掉 DATASET/MODEL 这类整段)。返回错误或 None。"""
    import yaml

    try:
        new_doc = yaml.safe_load(new_content) or {}
    except yaml.YAMLError as exc:
        return f"yaml 解析失败,已回滚:{str(exc)[:200]}"
    if old_content is None:
        return None  # 新建文件:能解析即可
    try:
        old_doc = yaml.safe_load(old_content) or {}
    except yaml.YAMLError:
        return None  # 原文件本来就不合法:不因历史问题阻塞修改
    if isinstance(old_doc, dict) and isinstance(new_doc, dict):
        lost = [k for k in old_doc if k not in new_doc]
        if lost:
            return ("本次写入会删除配置顶层段 "
                    f"{', '.join(map(str, lost))} —— 已回滚;"
                    "调参请保留全部段,如确要删段需 owner 手动处理")
    return None


def _compile_check(workdir: str, changes: list[dict[str, str]], python_bin: str,
                   exec_sh) -> list[str]:
    """对改动的 .py 做语法编译检查;返回错误列表(空 = 通过)。"""
    errors: list[str] = []
    for change in changes:
        if not change["path"].endswith(".py"):
            continue
        path = f"{workdir.rstrip('/')}/{change['path']}"
        code, out = exec_sh(
            f"{python_bin} -m py_compile {shlex.quote(path)} 2>&1", )
        if code != 0:
            errors.append(f"{change['path']}:\n{(out or '').strip()[:500]}")
    return errors


def direction_basis(repo: Repo, project: dict) -> dict | None:
    """迭代执行依据(D-028 双门):已采纳方案的报告(旧流程)或已确认的
    项目方向(新流程)。返回 proposal 形状的字典 {title, content, report_md,
    basis},两个门都不满足时返回 None。

    两个门是 OR 关系——老项目零影响,新项目走方向页,不必再过方案评审;
    返回值与旧 proposal 行同构,implement/draft_plan 的下游零改动。
    """
    proposal_id = project.get("accepted_proposal_id")
    if proposal_id:
        conn = repo.db.connect()
        try:
            row = conn.execute(
                "SELECT title, content, report_md FROM proposals WHERE id = ?",
                (proposal_id,)).fetchone()
        finally:
            conn.close()
        if row is not None and (row["report_md"] or "").strip():
            return {"title": row["title"], "content": row["content"],
                    "report_md": row["report_md"], "basis": "report"}
    text = (project.get("direction_text") or "").strip()
    if text and project.get("direction_confirmed_at"):
        return {"title": "项目方向", "content": text, "report_md": text,
                "basis": "direction"}
    return None


_DRAFT_DIRECTION_SYSTEM = (
    "你是研究方向提炼者。输入是一篇论文(或其缺失)、项目代码库概览与"
    " Baseline 已确认的数据集指标。产出一份数学/工程上可执行的研究方向"
    "陈述,给后续自动迭代循环当大方向边界。用中文 markdown,四个小节,"
    "总共不超过 800 字:\n"
    "## 研究目标(要超越的基线与目标指标)\n"
    "## 可试的改进假设(按性价比排序 3-5 条,每条一句为什么)\n"
    "## 约束(数据集、算力、不许动的东西——训练代码口径/评测协议必须保持可比)\n"
    "## 明确不做什么(防止循环跑偏的负面清单)\n"
    "不要编造论文里没有的内容;论文缺失时基于代码与 baseline 指标推断,"
    "并在开头一句说明。"
)


def draft_direction(
    repo: Repo,
    credentials: CredentialStore,
    hostkeys_path: Path,
    project_id: str,
    *,
    agent_key_ref: str | None = None,
) -> dict[str, Any]:
    """D-028 方向提炼:论文 PDF + 代码概览 + baseline 指标 → 方向陈述草稿。

    LLM 产出只是草稿(写 direction_text、清 direction_confirmed_at),
    owner 编辑确认后才成为执行依据——与 spec-hash 批准门同款纪律。
    """
    from . import llm as _llm
    from .baseline_queue import confirmed_datasets

    project = repo.get_project(project_id)
    if project is None:
        raise IterationError("项目不存在")
    workdir = (project.get("remote_workdir") or "").strip()
    if not workdir:
        raise IterationError("项目还没有工作区,先在项目页补全")
    if not project.get("baseline_passed_at"):
        raise IterationError("还没跑通 Baseline:方向要立在基线指标上,先去 Baseline 页")

    server = repo.get_server(project["server_id"])
    ssh_transport = None
    try:
        if server is not None and server["transport_mode"] == "ssh":
            ssh_transport = server_admin.connect_ssh(server, credentials, hostkeys_path)

        parts: list[str] = [f"# 项目:{project['name']}"]
        papers = server_admin.read_project_papers(workdir, ssh_transport=ssh_transport)
        if papers:
            body = ["# 论文内容"]
            for name, blob in papers[:2]:
                text = server_admin.pdf_text_from_bytes(blob)
                if text.strip():
                    body.append(f"## {name}\n{text[:6000]}")
            if len(body) > 1:
                parts.append("\n".join(body))
        core = server_admin.read_workspace_core_files(
            workdir, [], ssh_transport=ssh_transport)
        if core:
            body = ["# 代码库概览(核心文件)"]
            for path, content in list(core.items())[:5]:
                body.append(f"## {path}\n```\n{content[:2500]}\n```")
            parts.append("\n".join(body))
        datasets = confirmed_datasets(project)
        if datasets:
            body = ["# Baseline 已确认的数据集与指标"]
            for name, info in datasets.items():
                body.append(f"- {name}:指标 {json.dumps(info.get('metrics') or {}, ensure_ascii=False)};"
                            f"命令 `{info.get('command')}`")
            parts.append("\n".join(body))
        memory = read_research_memory(workdir, ssh_transport=ssh_transport)
        if memory.strip():
            parts.append(f"# 研究记忆(context/failures 节选)\n\n{memory[:3000]}")

        text = _llm.chat(
            credentials,
            system=_DRAFT_DIRECTION_SYSTEM,
            user="\n\n".join(parts),
            max_tokens=2000,
            agent_key_ref=agent_key_ref,
        ).strip()
        if not text:
            raise _llm.LlmError("方向提炼返回空")
        updated = repo.set_project_direction(project_id, text[:4000], None)
        return {"directionText": updated.get("direction_text"),
                "confirmedAt": None,
                "papers": [name for name, _ in papers]}
    except server_admin.ServerAdminError as exc:
        raise IterationError(str(exc)) from exc
    finally:
        if ssh_transport is not None:
            ssh_transport.close()


def implement_proposal_changes(
    repo: Repo,
    credentials: CredentialStore,
    hostkeys_path: Path,
    project_id: str,
    *,
    agent_key_ref: str | None = None,
    extra_context: str = "",
) -> dict[str, Any]:
    """实现 Agent:读评审报告与工作区代码 → LLM 产出文件改动 → 备份后写入 →
    语法检查,失败带错误修复一轮,仍失败则整体回滚(不留坏代码)。
    extra_context:自动迭代循环注入的上一轮结果(失败经验反馈给改码)。
    """
    from . import llm as _llm

    project = repo.get_project(project_id)
    if project is None:
        raise IterationError("项目不存在")
    proposal = direction_basis(repo, project)
    if proposal is None:
        raise IterationError(
            "项目还没有执行依据:在方向页提炼或手写方向并确认"
            "(老项目也可走方案评审页采纳方案)")
    proposal_id = project.get("accepted_proposal_id")
    report = (proposal.get("report_md") or "").strip()
    if not project.get("remote_workdir"):
        raise IterationError("项目还没有工作区,先在项目页补全")
    server = repo.get_server(project["server_id"])
    if server is None:
        raise IterationError("项目所在服务器不存在")

    workdir = project["remote_workdir"]
    ssh_transport = None
    try:
        if server["transport_mode"] == "ssh":
            ssh_transport = server_admin.connect_ssh(server, credentials, hostkeys_path)
        exec_sh = _exec_sh(ssh_transport)
        config = server_admin.baseline_config_defaults(project)
        presence = server_admin.code_presence(workdir, ssh_transport=ssh_transport)
        if not presence["present"]:
            raise IterationError(
                f"服务器工作区还没有代码({presence['reason']}),先在 Baseline 页同步代码")
        sources = _collect_workspace_sources(workdir, config, exec_sh)
        if not sources:
            raise IterationError("工作区里没有可读的源码文件(python/yaml/sh)")

        files_block = "\n\n".join(
            f"### 当前文件 {name}\n```\n{content}\n```" for name, content in sources.items())
        user_prompt = (
            f"## 已采纳方案\n《{proposal['title']}》\n{proposal.get('content') or ''}\n\n"
            f"## 评审报告(执行依据)\n{report[:_CONTEXT_REPORT_LIMIT]}\n\n"
        )
        if extra_context.strip():
            user_prompt += extra_context.strip() + "\n\n"
        memory = read_research_memory(workdir, ssh_transport=ssh_transport)
        if memory:
            user_prompt += "## 研究记忆(项目 context 与失败经验,避免重复踩坑)\n" \
                           + memory + "\n\n"
        _, home = exec_sh("echo $HOME")
        envs = server_admin._list_envs_fast(exec_sh, (home or "").strip())
        envs_line = ("、".join(
            f"{e['name']}(py{e.get('python') or '?'})" for e in envs) or "未发现 conda 环境")
        user_prompt += (
            f"## 工作区当前文件\n{files_block}\n\n"
            f"## 运行事实\n- Baseline 命令:`{config.get('command')}`;"
            f"指标文件:`{config.get('metric_file')}`;"
            f"conda 环境:`{config.get('conda_env') or '无(系统 python3)'}`\n"
            f"- 服务器可用训练环境:{envs_line}"
            "(写代码只能用这些环境里已有的库,不要假设可以安装新依赖)\n"
        )
        dataset = (config.get("dataset_path") or "").strip()
        if dataset:
            user_prompt += (
                f"- **范围约束(硬性)**:Baseline 使用的数据集是 {dataset};"
                "你实现的改动与后续实验只能在该数据集上运行,不得引入/切换其他数据集\n")
        user_prompt += "\n请实现该方案(只输出 ```json``` 代码块)。"

        messages = [{"role": "user", "content": user_prompt}]
        last_error = ""
        result = None
        for _attempt in range(2):
            if last_error:
                messages.append({"role": "user",
                                 "content": f"上一次输出不可用:{last_error}\n"
                                            "请修正后重新给出完整的 ```json``` 代码块"
                                            "(changes 里仍是完整文件内容)。"})
            try:
                reply = _llm.chat_multi(
                    credentials, system=_IMPLEMENT_SYSTEM_PROMPT, messages=messages,
                    agent_key_ref=agent_key_ref, max_tokens=8000, timeout=240.0)
            except _llm.LlmError:
                raise
            try:
                result = parse_changes_payload(reply)
                changes = result["changes"]
            except IterationError as exc:
                last_error = str(exc)
                continue

            # 写入(带备份)→ 语法检查 → 失败则回滚并进入修复轮
            import datetime as _dt
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            patch_abs = f"{workdir.rstrip('/')}/{PATCHES_REL}/{stamp}"
            for change in changes:
                _write_patch(workdir, change, patch_abs, ssh_transport)
            _, home = exec_sh("echo $HOME")
            errors = _compile_check(workdir, changes,
                                    _impl_python_bin(config, (home or "").strip()),
                                    exec_sh)
            if errors:
                _rollback_patch(workdir, changes, patch_abs, ssh_transport)
                last_error = "语法检查失败:\n" + "\n".join(errors)
                continue
            manifest = {
                "created_at": research.utc_now_stamp(),
                "proposal_id": proposal_id,
                "proposal_title": proposal["title"],
                "basis": proposal["basis"],
                "summary": result["summary"],
                "files": [{"path": c["path"], "action": c["action"],
                           "reason": c["reason"]} for c in changes],
            }
            _write_abs(f"{patch_abs}/manifest.json",
                       json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                       ssh_transport)
            repo.add_review_message(
                project_id=project_id, role="system", kind="text",
                content=f"实现 Agent 已按评审报告修改代码:{len(changes)} 个文件"
                        f"({', '.join(c['path'] for c in changes)}),"
                        f"原文件备份在 {PATCHES_REL}/{stamp}/orig/,语法检查通过")
            return {"summary": result["summary"],
                    "patchDir": f"{PATCHES_REL}/{stamp}",
                    "files": manifest["files"], "compileOk": True}
        raise IterationError(
            f"实现 Agent 两轮输出均不可用,已回滚全部改动:{last_error[:300]}。"
            "可重试,或先完善评审报告中的实现指引。"
        )
    except server_admin.ServerAdminError as exc:
        raise IterationError(str(exc)) from exc
    finally:
        if ssh_transport is not None:
            ssh_transport.close()


def read_latest_implementation(workdir: str, *, ssh_transport=None) -> dict | None:
    """最近一次实现记录(patch manifest);没有则 None。"""
    root = f"{workdir.rstrip('/')}/{PATCHES_REL}"
    try:
        if ssh_transport is not None:
            names = ssh_transport.sftp_listdir(root)
        else:
            names = [p.name for p in Path(root).iterdir() if p.is_dir()]
    except Exception:
        return None
    stamps = sorted(n for n in names if n[:1].isdigit())
    if not stamps:
        return None
    try:
        raw = _read_bytes(f"{root}/{stamps[-1]}", "manifest.json", ssh_transport)
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def restore_baseline_code(repo: Repo, credentials: CredentialStore,
                          hostkeys_path: Path, project_id: str) -> dict:
    """把工作区代码回滚到迭代开始前:遍历全部补丁目录,每个被改过的文件
    取**最早**一份 orig 备份恢复;迭代新建的文件(无 orig)删除。
    用于废弃方案后重跑干净基线——迭代补丁会污染基线数据。"""
    project = repo.get_project(project_id)
    if project is None:
        raise IterationError("项目不存在")
    workdir = (project.get("remote_workdir") or "").strip()
    if not workdir:
        raise IterationError("项目还没有工作区")
    server = repo.get_server(project["server_id"])
    ssh_transport = None
    try:
        if server is not None and server["transport_mode"] == "ssh":
            ssh_transport = server_admin.connect_ssh(server, credentials, hostkeys_path)
        root = f"{workdir.rstrip('/')}/{PATCHES_REL}"
        if ssh_transport is not None:
            names = ssh_transport.sftp_listdir(root)
        else:
            names = [p.name for p in Path(root).iterdir() if p.is_dir()]
        stamps = sorted(n for n in names if n[:1].isdigit())
        if not stamps:
            return {"restored": [], "deleted": [], "patches": 0,
                    "note": "没有任何迭代补丁,工作区已是基线代码"}

        # 每个文件:最早出现 orig 的那份 = 迭代前原貌;但只要历史上
        # 有一次 create(无 orig),该文件基线就不存在 → 一律删除,
        # 后续修改产生的 orig 只是中间版本,不能当基线恢复
        orig_by_file: dict[str, str] = {}   # rel -> patch_stamp(最早 orig)
        created: set[str] = set()
        for stamp in stamps:  # 从早到晚
            try:
                manifest = json.loads(_read_bytes(
                    f"{root}/{stamp}", "manifest.json", ssh_transport).decode(
                    "utf-8", "replace"))
            except Exception:
                continue
            for item in manifest.get("files") or []:
                rel = str(item.get("path") or "").strip()
                if not rel or rel.startswith("/") or ".." in rel.split("/"):
                    continue
                has_orig = True
                try:
                    _read_bytes(f"{root}/{stamp}", f"orig/{rel}", ssh_transport)
                except Exception:
                    has_orig = False
                if not has_orig:
                    created.add(rel)
                else:
                    orig_by_file.setdefault(rel, stamp)
        for rel in created:  # 创建过的文件绝不恢复
            orig_by_file.pop(rel, None)
        restored, deleted = [], []
        for rel, stamp in sorted(orig_by_file.items()):
            original = _read_bytes(f"{root}/{stamp}", f"orig/{rel}", ssh_transport)
            _write_abs(f"{workdir.rstrip('/')}/{rel}", original, ssh_transport)
            restored.append(rel)
        for rel in sorted(created):
            _delete_abs(f"{workdir.rstrip('/')}/{rel}", ssh_transport)
            deleted.append(rel)
        # 恢复后语法门:只检查最终仍存在的恢复文件(被删除的不查)
        exec_sh = _exec_sh(ssh_transport)
        config = server_admin.baseline_config_defaults(project)
        python_bin = _impl_python_bin(config, "$HOME")
        check_files = [p for p in restored if p not in created]
        errors = _compile_check(
            workdir, [{"path": p} for p in check_files], python_bin, exec_sh) \
            if check_files else []
        repo.add_review_message(
            project_id=project_id, role="system", kind="text",
            content=f"已回滚全部迭代补丁(恢复 {len(restored)} 个文件,"
                    f"删除 {len(deleted)} 个新建文件),工作区回到基线代码")
        return {"restored": restored, "deleted": deleted,
                "patches": len(stamps), "compileOk": not errors,
                "compileErrors": errors[:3]}
    except server_admin.ServerAdminError as exc:
        raise IterationError(str(exc)) from exc
    finally:
        if ssh_transport is not None:
            ssh_transport.close()


def draft_iteration_plan(
    repo: Repo,
    credentials: CredentialStore,
    hostkeys_path: Path,
    project_id: str,
    *,
    agent_key_ref: str | None = None,
    extra_context: str = "",
    datasets: list[str] | None = None,
) -> dict[str, Any]:
    """读报告 → LLM 草案 → 写实验文件 → DRAFT Plan(不批准)。
    extra_context:自动迭代循环注入的上一轮结果反馈。
    datasets:本轮迭代的数据集选择(必须 ⊆ 基线确认过的数据集池;
    缺省 = 池中全部)。"""
    project = repo.get_project(project_id)
    if project is None:
        raise IterationError("项目不存在")
    if not project.get("baseline_passed_at"):
        raise IterationError("还没跑通 Baseline:迭代计划要立在基线指标上,"
                             "先在 Baseline 页跑通并确认")
    proposal = direction_basis(repo, project)
    if proposal is None:
        raise IterationError(
            "项目还没有执行依据:在方向页提炼或手写方向并确认"
            "(老项目也可走方案评审页采纳方案并生成报告)")
    if not project.get("remote_workdir"):
        raise IterationError("项目还没有工作区,先在项目页补全")

    plans = repo.list_plans(project_id)
    if plans and max(plans, key=lambda p: p["revision_no"])["status"] == "DRAFT":
        latest = max(plans, key=lambda p: p["revision_no"])
        raise IterationError(
            f"已有待批准的实验计划草案({latest['id']},revision {latest['revision_no']}):"
            "先在 Plan 页批准或放弃,再生成新的迭代计划"
        )

    server = repo.get_server(project["server_id"])
    if server is None:
        raise IterationError("项目所在服务器不存在")

    workdir = project["remote_workdir"]
    ssh_transport = None
    try:
        if server["transport_mode"] == "ssh":
            ssh_transport = server_admin.connect_ssh(server, credentials, hostkeys_path)
        exec_sh = _exec_sh(ssh_transport)
        config = server_admin.baseline_config_defaults(project)
        # 联合迭代入口 = 确认快照(确认通过时对各数据集指标的快照);
        # 旧项目没有快照 → 走旧的单数据集分支
        try:
            snapshot = json.loads(
                project.get("baseline_confirmed_json") or "{}")
            pool = {str(k): v for k, v in (snapshot.get("datasets") or {}).items()
                    if isinstance(v, dict)}
        except Exception:
            pool = {}
        selected = [n for n in (datasets or []) if n in pool]
        if datasets and not selected:
            raise IterationError(
                "所选数据集都不在 Baseline 确认过的范围内:" + "、".join(datasets)
                + "(确认通过后数据集池才生效)")
        if pool and not selected:
            selected = list(pool.keys())
        if not pool:
            pool = {}
            selected = []
            # 旧项目:回落运行记录里的单数据集指标
            try:
                legacy = json.loads(
                    project.get("baseline_run_json") or "{}").get("metrics") or {}
            except Exception:
                legacy = {}
            baseline_metrics = legacy
        else:
            baseline_metrics = pool[selected[0]].get("metrics") or {}
        _, home = exec_sh("echo $HOME")
        envs = server_admin._list_envs_fast(exec_sh, (home or "").strip())

        # 工作区就位检查(与 Baseline 模块同口径)
        presence = server_admin.code_presence(workdir, ssh_transport=ssh_transport)
        if not presence["present"]:
            raise IterationError(
                f"服务器工作区还没有代码({presence['reason']}),先在 Baseline 页同步代码"
            )

        context = _build_context(repo, project, proposal, baseline_metrics, config,
                                 workdir, ssh_transport, envs=envs,
                                 dataset_pool=pool, selected_datasets=selected)
        if extra_context.strip():
            context += "\n\n" + extra_context.strip()
        draft = _draft_with_verification(
            repo, credentials, hostkeys_path, project, context, agent_key_ref,
            workdir, ssh_transport,
            required_datasets=(selected if len(selected) > 1 else None),
            allowed_seeds=baseline_seeds(config, pool if len(selected) > 1 else None)
            or None)

        # 实验文件:没有实验记录时先补建基线记录;再写本轮迭代实验。
        existing = list_experiments(workdir, ssh_transport=ssh_transport)
        if existing:
            baseline_exp = existing[-1]
        else:
            baseline_exp = "EXP-0001"
            _write_bytes(workdir, f"{EXPERIMENTS_REL}/{baseline_exp}.md",
                         baseline_experiment_markdown(
                             baseline_exp, baseline_metrics, config,
                             proposal["title"]).encode("utf-8"),
                         ssh_transport)
        new_exp = f"EXP-{int(baseline_exp.split('-')[1]) + 1:04d}"
        new_bytes = iteration_experiment_markdown(
            new_exp, draft, baseline_exp, baseline_metrics).encode("utf-8")
        _write_bytes(workdir, f"{EXPERIMENTS_REL}/{new_exp}.md", new_bytes, ssh_transport)

        # 环境选择:计划草案可指定服务器已有环境(校验存在,幻觉名回落 Baseline 配置)
        selected_env = (config.get("conda_env") or "").strip()
        if draft.get("conda_env"):
            known = {env["name"] for env in envs}
            selected_env = (draft["conda_env"] if draft["conda_env"] in known
                            else selected_env)
        if len(selected) > 1:
            # 联合迭代:每个 job 自带该数据集的完整命令(ExplicitJobs),
            # command_template 只负责 conda 激活 + {{command}} 占位
            command_template = build_parametrized_command_template(
                selected_env, (home or "").strip())
            job_definition = _explicit_jobs_for_datasets(
                draft, selected, pool)
        else:
            command_template = build_command_template(
                draft["command"], selected_env, (home or "").strip())
            job_definition = draft["job_definition"]
        anchor = workspace_code_anchor(workdir, exec_sh)
        spec = _build_spec(project, draft, new_exp, new_bytes, baseline_exp,
                           command_template, anchor,
                           parent_plan_revision_id=(plans[-1]["id"] if plans else None),
                           job_definition=job_definition)
        plan = repo.create_draft_plan(
            project_id=project_id, spec=spec,
            summary=draft["summary"] or draft["experiment_title"],
            parent_plan_revision_id=(plans[-1]["id"] if plans else None))
        repo.add_review_message(
            project_id=project_id, role="system", kind="text",
            content=f"自动迭代 Agent 已依据评审报告创建实验计划:{new_exp} → "
                    f"{plan['id']}(revision {plan['revision_no']}),待 owner 批准后执行")
        return {
            "plan": {k: plan[k] for k in ("id", "revision_no", "status", "spec_hash",
                                          "experiment_ref", "summary")},
            "experimentId": new_exp,
            "baselineExperimentId": baseline_exp,
            "jobsPreview": len(expand_jobs(spec)),
            "commandTemplate": command_template,
            "datasets": selected,
        }
    except server_admin.ServerAdminError as exc:
        raise IterationError(str(exc)) from exc
    finally:
        if ssh_transport is not None:
            ssh_transport.close()


def read_research_memory(workdir: str, *, ssh_transport=None) -> str:
    """研究记忆(FR-10):context.md 节选 + failures.md 最近记录,喂给
    实现/计划 Agent,避免只依赖当前报告与对话。"""
    sections: list[str] = []
    try:
        text = _read_bytes(workdir, f"{research.AUTOLAB_DIR}/{research.CONTEXT_FILE}",
                           ssh_transport).decode("utf-8", "replace")
        if text.strip():
            sections.append("### 研究上下文(context.md 节选)\n" + text[:3000])
    except Exception:
        pass
    try:
        failures = _read_bytes(workdir, KNOWLEDGE_FAILURES_REL,
                               ssh_transport).decode("utf-8", "replace")
        # 模板约半 KB;只有存在真实条目时才带,避免灌模板噪音
        if len(failures) > 800:
            sections.append("### 失败经验(failures.md 最近记录,避免重复踩坑)\n"
                            + failures[-2500:])
    except Exception:
        pass
    return "\n\n".join(sections)


def _build_context(repo: Repo, project: dict, proposal: dict, baseline_metrics: dict,
                   config: dict, workdir: str, ssh_transport,
                   envs: list[dict] | None = None,
                   dataset_pool: dict[str, dict] | None = None,
                   selected_datasets: list[str] | None = None) -> str:
    report = (proposal.get("report_md") or "")[:_CONTEXT_REPORT_LIMIT]
    heading = ("## 项目方向(执行依据,owner 已确认)" if proposal.get("basis") == "direction"
               else "## 已采纳方案的评审报告(执行依据)")
    sections = [f"{heading}\n\n{report}"]
    if envs:
        body = ["## 服务器可用训练环境(conda)"]
        for env in envs:
            body.append(f"- {env['name']}(Python {env.get('python') or '?'});"
                        "优先使用已有环境,不要假设可以随意安装依赖")
        sections.append("\n".join(body))
    implementation = read_latest_implementation(workdir, ssh_transport=ssh_transport)
    if implementation:
        body = [f"## 实现 Agent 已完成的代码改动:{implementation.get('summary', '')}"]
        for item in implementation.get("files") or []:
            body.append(f"- {item.get('action')} `{item.get('path')}`:{item.get('reason', '')}")
        body.append("实验命令应基于上述改动后的代码(而不是旧命令)。")
        sections.append("\n".join(body))
    if dataset_pool and selected_datasets:
        body = ["## Baseline 指标与运行配置(分数据集,对照口径)"]
        for name in selected_datasets:
            info = dataset_pool.get(name) or {}
            body.append(f"- {name}:指标 {json.dumps(info.get('metrics') or {}, ensure_ascii=False)};"
                        f"命令 `{info.get('command')}`;"
                        f"数据路径 `{info.get('dataset_path')}`")
        body.append(f"- 共享 conda_env:`{config.get('conda_env')}`;"
                    f"指标文件:`{config.get('metric_file')}`")
        sections.append("\n".join(body))
    else:
        sections.append("## Baseline 指标(对照口径)\n"
                        + json.dumps(baseline_metrics, ensure_ascii=False))
        sections.append(
            "## Baseline 运行配置\n"
            f"- command:`{config.get('command')}`\n"
            f"- conda_env:`{config.get('conda_env')}`\n"
            f"- metric_file:`{config.get('metric_file')}`"
        )
    entry = ""
    for token in (config.get("command") or "").split():
        if token.endswith((".py", ".sh")) and "/" not in token:
            entry = token
            break
    try:
        files = server_admin.read_workspace_core_files(
            workdir, [entry], ssh_transport=ssh_transport)
    except Exception:
        files = {}
    if files:
        body = ["## 工作区核心文件"]
        for name, content in files.items():
            body.append(f"### {name}\n```\n{content[:3000]}\n```")
        sections.append("\n".join(body))
    experiments = list_experiments(workdir, ssh_transport=ssh_transport)
    if experiments:
        body = ["## 已有实验记录(最近一轮)"]
        try:
            raw = _read_bytes(workdir, f"{EXPERIMENTS_REL}/{experiments[-1]}.md",
                              ssh_transport)
            body.append(f"### {experiments[-1]}.md\n"
                        + raw.decode("utf-8", "replace")[:_CONTEXT_EXPERIMENT_LIMIT])
        except Exception:
            body.append(f"(最近一轮 {experiments[-1]} 读取失败)")
        sections.append("\n".join(body))
    else:
        sections.append("## 已有实验记录\n无(本轮会先补建基线实验记录 EXP-0001)")
    memory = read_research_memory(workdir, ssh_transport=ssh_transport)
    if memory:
        sections.append("## 研究记忆(项目 context 与失败经验)\n" + memory)
    sections.append(
        "## 项目事实\n"
        f"- 项目:{project['name']}(工作区即 Baseline 运行的同一目录)\n"
        f"- 服务器 GPU 槽位:{repo.get_server(project['server_id'])['gpu_slots']}"
    )
    if dataset_pool and selected_datasets:
        lines = [
            "## 范围约束(硬性)",
            f"Baseline 跑通并确认的数据集:{'、'.join(dataset_pool.keys())}。",
            f"本轮迭代只在以下选定数据集上进行:{'、'.join(selected_datasets)};",
            "不得引入名单之外的数据集,也不得为\"更容易超越基线\"而缩小范围;"
            "每个数据集与其基线指标各自对比。",
        ]
        if len(selected_datasets) > 1:
            lines.append(
                "本轮为多数据集联合迭代:commands 字段必须为每个选定数据集分别"
                "给出运行命令(以该数据集的 Baseline 命令为基础改写,参数占位符"
                "{{{参数名}}} 与 job_definition 保持一致)。")
        seeds = baseline_seeds({}, dataset_pool)
        if seeds:
            lines.append(
                f"seed 对齐:基线实际跑过的 seed 为 {'、'.join(str(s) for s in seeds)},"
                "本轮矩阵的 seed 参数只能取这些值(同 seed 配对比较才公平);"
                "基线没跑过的 seed 不得出现。")
        sections.append("\n".join(lines))
    else:
        dataset = (config.get("dataset_path") or "").strip()
        lines = ["## 范围约束(硬性)"]
        if dataset:
            lines.append(
                f"Baseline 使用的数据集:{dataset}。本轮迭代实验必须且只能在该数据集上"
                "进行(命令引用的数据集配置/路径不得更换);与基线的对比只对该数据集有效。")
        seeds = baseline_seeds(config, None)
        if seeds:
            lines.append(
                f"seed 对齐:基线实际跑过的 seed 为 {'、'.join(str(s) for s in seeds)},"
                "本轮矩阵的 seed 参数必须与之一致(基线单 seed → 本轮也只跑该 seed,"
                "同 seed 配对比较;要多种子稳健性需先给基线补 seed)。")
        if len(lines) > 1:
            sections.append("\n".join(lines))
    return "\n\n".join(sections)[:60000]


def _build_spec(project: dict, draft: dict[str, Any], new_exp: str, new_bytes: bytes,
                baseline_exp: str, command_template: list[str], anchor: CodeAnchor,
                parent_plan_revision_id: str | None,
                job_definition: dict | None = None) -> PlanSpecV1:
    source = draft["metric_source"]
    return PlanSpecV1(
        schema_version=1,
        project_id=project["id"],
        experiment_ref=f"{EXPERIMENTS_REL}/{new_exp}.md",
        experiment_hash=hashlib.sha256(new_bytes).hexdigest(),
        parent_plan_revision_id=parent_plan_revision_id,
        hypothesis=draft["hypothesis"],
        change_from_previous=draft["change_from_previous"],
        baseline=baseline_exp,  # 必须精确为 EXP id:看板据此判迭代列
        metrics=[MetricDefinition(name=m.name, direction=m.direction, target=m.target)
                 for m in draft["metrics"]],
        success_criteria=draft["success_criteria"],
        risk_and_fallback=draft["risk_and_fallback"],
        code_anchor=anchor,
        command_template=command_template,
        environment=None,
        job_definition=job_definition if job_definition is not None
        else draft["job_definition"],
        resources=PlanResources(gpu_count=draft["gpu_count"],
                                max_parallel=draft["max_parallel"]),
        timeout_seconds=draft["timeout_seconds"],
        outputs=PlanOutputs(
            subdir_template="{{job_id}}",
            artifacts=draft["artifacts"],
            metrics=[MetricSource(kind=source["kind"], path=source["path"],
                                  selector=source["selector"] or None)],
        ),
    )


# ------------------------------------------------------------- 基线对比

def compute_comparison(repo: Repo, project: dict, plan: dict) -> dict[str, Any] | None:
    """迭代 Plan 的指标 vs 基线:主指标逐 Job 对比;≥1 个 Job 超越基线 = improved。

    联合迭代:job 参数带 dataset 时,该 job 与其所属数据集的基线各自对比
    (基线值来自确认快照);无 dataset 的 job 沿用项目级基线指标。"""
    try:
        spec = PlanSpecV1.model_validate(json.loads(plan["spec_json"]))
    except Exception:
        return None
    from .baseline_queue import confirmed_datasets as _confirmed

    pool = _confirmed(project)
    try:
        legacy_metrics = json.loads(
            project.get("baseline_run_json") or "{}").get("metrics") or {}
    except Exception:
        legacy_metrics = {}
    spec_metrics = [(m.name, m.direction) for m in spec.metrics]
    primary = next((name for name, _ in spec_metrics if any(
        name in (pool.get(_n) or {}).get("metrics", {}) for _n in pool)
        or name in legacy_metrics), spec_metrics[0][0] if spec_metrics else None)
    direction = dict(spec_metrics).get(primary, "maximize")

    def baseline_for(dataset: str | None):
        if dataset and dataset in pool:
            return (pool[dataset].get("metrics") or {}).get(primary)
        return legacy_metrics.get(primary)

    jobs = repo.list_jobs(plan_id=plan["id"])
    rows = []
    for job in jobs:
        params = json.loads(job["params_json"] or "{}")
        dataset = params.get("dataset") if isinstance(params.get("dataset"), str) else None
        baseline_value = baseline_for(dataset)
        value = None
        for artifact in repo.job_artifacts(job["id"]):
            values = artifact["metadata"].get("metric_values") or {}
            if primary in values:
                value = values[primary]
                break
            if values:
                value = next(iter(values.values()))
                break
        improved = None
        delta = None
        if isinstance(value, (int, float)) and isinstance(baseline_value, (int, float)):
            delta = value - baseline_value
            if direction == "maximize":
                improved = value > baseline_value
            elif direction == "minimize":
                improved = value < baseline_value
            else:
                improved = abs(delta) < 1e-9
        rows.append({
            "jobId": job["id"], "ordinal": job["ordinal"],
            "params": params, "dataset": dataset,
            "status": job["status"], "metricValue": value,
            "baseline": baseline_value, "delta": delta, "improved": improved,
        })
    all_terminal = bool(jobs) and all(j["status"] in TERMINAL_JOB_STATUSES for j in jobs)
    succeeded = sum(1 for j in jobs if j["status"] == "SUCCEEDED")
    failed = sum(1 for j in jobs if j["status"] in ("FAILED", "TIMED_OUT"))
    any_improved = any(r["improved"] for r in rows if r["improved"] is not None)
    if not all_terminal:
        verdict = "running"
    elif any_improved:
        verdict = "improved"
    elif succeeded and all(r["metricValue"] is not None for r in rows if r["status"] == "SUCCEEDED"):
        verdict = "not_improved"
    else:
        verdict = "inconclusive"
    # 分数据集小结(联合迭代 UI 用;单数据集也给出统一一组)
    by_dataset: dict[str, dict] = {}
    for row in rows:
        name = row["dataset"] or "default"
        group = by_dataset.setdefault(name, {"dataset": name, "jobs": 0,
                                             "improved": 0, "baseline": row["baseline"]})
        group["jobs"] += 1
        if row["improved"]:
            group["improved"] += 1
    return {
        "metricName": primary, "direction": direction,
        "baselineValue": legacy_metrics.get(primary),
        "rows": rows, "datasets": list(by_dataset.values()),
        "allTerminal": all_terminal, "succeeded": succeeded, "failed": failed,
        "anyImproved": any_improved, "verdict": verdict,
        "paramInsensitivity": _param_insensitivity(rows),
    }


def _param_insensitivity(rows: list[dict]) -> dict | None:
    """参数敏感性检查(D-023 P0-③):同 seed 下仅一个参数取值不同、
    且该组 Job 的指标逐位相同 → 参数大概率没生效,对照结论不可信。
    (真机教训:hflip 0/0.5 两组同分,是 REMAINDER 吞参的接线断路,
    而非增强无益。)返回警示信息或 None。"""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if row["status"] != "SUCCEEDED" or not isinstance(row["metricValue"], (int, float)):
            continue
        params = dict(row["params"] or {})
        dataset = params.pop("dataset", None)
        groups.setdefault((row["dataset"] or dataset or "default",), []).append(
            {**row, "_cmp": params})
    for members in groups.values():
        if len(members) < 2:
            continue
        keys = set(members[0]["_cmp"])
        if not keys or any(set(m["_cmp"]) != keys for m in members):
            continue
        # 找恰好只有一个参数在成员间取值不同、其余全同的对照结构
        varying = [k for k in keys
                   if len({json.dumps(m["_cmp"][k], sort_keys=True) for m in members}) > 1]
        fixed_same = all(
            len({json.dumps(m["_cmp"][k], sort_keys=True) for m in members}) == 1
            for k in keys if k not in varying)
        if len(varying) != 1 or not fixed_same:
            continue
        values = {m["metricValue"] for m in members}
        if len(values) == 1:
            return {
                "param": varying[0],
                "values": [m["_cmp"][varying[0]] for m in members],
                "metric": members[0]["metricValue"],
                "note": (f"对照组仅 {varying[0]} 取值不同但指标逐位相同"
                         f"({members[0]['metricValue']}):参数疑似未生效"
                         "(接线断路/分支未触发),本轮对照结论不可信"),
            }
    return None


def draft_analysis_text(comparison: dict | None, spec_title: str) -> str:
    """结论记录的默认分析文本(用户可在 UI 里改写)。"""
    if not comparison or not comparison.get("rows"):
        return f"《{spec_title}》本轮无运行记录。"
    lines = []
    insensitive = comparison.get("paramInsensitivity")
    if insensitive:
        lines.append(f"⚠ {insensitive['note']};结论按 inconclusive 处理,"
                     "下一轮先修参数接线再对照。")
    for row in comparison["rows"]:
        params = "、".join(f"{k}={v}" for k, v in row["params"].items()) or "-"
        if isinstance(row["metricValue"], (int, float)) and isinstance(row["baseline"], (int, float)):
            sign = "+" if (row["delta"] or 0) >= 0 else ""
            verdict = "超越基线" if row["improved"] else "未超越基线"
            lines.append(
                f"- {params}({row['status']}):{comparison['metricName']}="
                f"{row['metricValue']} vs 基线 {row['baseline']}"
                f"({sign}{row['delta']:.4g}),{verdict}")
        else:
            lines.append(f"- {params}({row['status']}):未读到指标")
    verdict_label = {"improved": "至少 1 个配置超越基线,假设获得支持",
                     "not_improved": "全部配置未超越基线",
                     "inconclusive": "指标不完整,证据不足",
                     "running": "仍在运行"}
    summary = verdict_label.get(comparison["verdict"], comparison["verdict"])
    return f"《{spec_title}》本轮结果:{summary}\n" + "\n".join(lines)


def draft_analysis_llm(credentials: CredentialStore, *, project_name: str,
                       experiment_title: str, implementation_summary: str,
                       comparison: dict | None,
                       agent_key_ref: str | None = None) -> str:
    """Agent 总结:实验事实 → 2-4 句结论分析;失败回落模板(不阻塞结论)。"""
    from . import llm as _llm

    rows = []
    for row in (comparison or {}).get("rows") or []:
        params = "、".join(f"{k}={v}" for k, v in (row.get("params") or {}).items())
        value = row.get("metricValue")
        baseline = row.get("baseline")
        rows.append(f"- {params or '-'}({row['status']}):"
                    f"{(comparison or {}).get('metricName')}={value} vs 基线 {baseline}"
                    f"(Δ={row.get('delta')})")
    system = ("你是科研实验总结 Agent。根据给定实验事实写 2-4 句结论分析:"
              "这轮改了什么、指标与基线对比的结果、结论的意义与下一步建议。"
              "只输出分析正文,不要标题和列表符号,不得编造事实之外的内容。")
    user = (f"项目:{project_name}\n实验:{experiment_title}\n"
            f"实现改动:{implementation_summary or '(无实现记录,参数矩阵重跑)'}\n"
            f"判定:{(comparison or {}).get('verdict')}"
            f"(方向 {(comparison or {}).get('direction')},"
            f"{'有配置超越基线' if (comparison or {}).get('anyImproved') else '没有配置超越基线'})\n"
            + "\n".join(rows[:12]))
    for _attempt in range(2):  # GLM 端点偶发 5xx/超时:重试一次再回落模板
        try:
            text = _llm.chat_multi(credentials, system=system,
                                   messages=[{"role": "user", "content": user}],
                                   agent_key_ref=agent_key_ref, max_tokens=500)
            if text.strip():
                return text.strip()[:1500]
        except _llm.LlmError:
            pass
        import time

        time.sleep(1.5)
    return ""


# ------------------------------------------------------------- 记录结论

def record_iteration_conclusion(
    repo: Repo,
    credentials: CredentialStore,
    hostkeys_path: Path,
    plan_id: str,
    *,
    conclusion: str = "",
    analysis: str = "",
    next_decision: str | None = None,
    reconsideration: str = "",
    auto: bool = False,
    agent_key_ref: str | None = None,
) -> dict[str, Any]:
    """把结论写回 Plan 引用的实验文件(失败也追加 knowledge/failures.md)。

    auto=True(一键结论):conclusion/next_decision 按对比 verdict 推导
    (improved→supported+promote),分析由 Agent 总结生成(失败回落模板),
    且 improved 时自动归档(confirm→done),用户不再手动走那两步。
    研究文件的写入复用 autolab_client.research.record_conclusion 的全部校验
    (证据引用、rejected/stop 需 reconsideration),SSH 工作区走临时目录中转。
    """
    from .repo import RepoError

    plan = repo.get_plan(plan_id)
    if plan is None:
        raise IterationError("Plan 不存在")
    project = repo.get_project(plan["project_id"])
    if project is None:
        raise IterationError("项目不存在")

    comparison = compute_comparison(repo, project, plan)
    if auto:
        verdict = (comparison or {}).get("verdict")
        if verdict == "improved":
            conclusion = conclusion or "supported"
            next_decision = next_decision or "promote"
        elif verdict == "not_improved":
            conclusion = conclusion or "rejected"
        else:
            conclusion = conclusion or "inconclusive"
    if conclusion not in ("supported", "rejected", "inconclusive"):
        raise IterationError("conclusion 需为 supported / rejected / inconclusive")
    if next_decision is not None and next_decision not in ("iterate", "promote", "stop"):
        raise IterationError("next_decision 需为 iterate / promote / stop")

    experiment_id = experiment_name(plan["experiment_ref"])
    workdir = project["remote_workdir"]
    # research 协议:rejected 或 stop 必须带重审条件;没给就给默认文案
    if (conclusion == "rejected" or next_decision == "stop") and not reconsideration.strip():
        reconsideration = _default_reconsideration(conclusion)

    evidence: list[str] = []
    for job in repo.list_jobs(plan_id=plan_id):
        if job["status"] == "SUCCEEDED":
            evidence.append(f"job:{job['id']}")
            for artifact in repo.job_artifacts(job["id"]):
                values = artifact["metadata"].get("metric_values") or {}
                for name in values:
                    evidence.append(f"metric:{artifact['id']}#{name}")
        else:
            evidence.append(f"job:{job['id']}")
    if not evidence:
        raise IterationError("该 Plan 还没有展开的 Job,无从记录结论")
    evidence = evidence[:20]

    server = repo.get_server(project["server_id"])
    ssh_transport = None
    try:
        if server is not None and server["transport_mode"] == "ssh":
            ssh_transport = server_admin.connect_ssh(server, credentials, hostkeys_path)
        # 一键结论的 Agent 总结:带上最近一次实现改动(SSH 下经 transport 读)
        if not analysis.strip() and auto:
            implementation = read_latest_implementation(
                workdir, ssh_transport=ssh_transport) if workdir else None
            analysis = draft_analysis_llm(
                credentials, project_name=project["name"],
                experiment_title=plan.get("summary") or experiment_id,
                implementation_summary=(implementation or {}).get("summary", ""),
                comparison=comparison, agent_key_ref=agent_key_ref)
        rel = f"{EXPERIMENTS_REL}/{experiment_id}.md"
        try:
            raw = _read_bytes(workdir, rel, ssh_transport)
        except Exception as exc:
            raise IterationError(
                f"工作区里找不到实验文件 {rel}:先重新生成迭代计划") from exc
        updated = _apply_conclusion_bytes(
            raw, experiment_id, conclusion, evidence,
            analysis or draft_analysis_text(
                comparison, plan.get("summary") or experiment_id),
            next_decision, reconsideration)
        _write_bytes(workdir, rel, updated, ssh_transport)

        failed_jobs = [j for j in repo.list_jobs(plan_id=plan_id)
                       if j["status"] in ("FAILED", "TIMED_OUT")]
        failure_entry = None
        if conclusion == "rejected" or failed_jobs:
            # 失败日志尾部一并入档:下个循环/下轮 Agent 读 failures.md
            # 就能看到真正的死因(OOM/traceback),不用瞎猜
            tails = {job["id"]: failure_log_tail(
                repo, credentials, project, job, ssh_transport=ssh_transport)
                for job in failed_jobs[:2]}
            failure_entry = _failure_entry_lines(
                experiment_id, plan, comparison, failed_jobs,
                reconsideration=reconsideration or _default_reconsideration(conclusion),
                log_tails=tails)
            _append_failure_entry(workdir, project["id"], failure_entry, ssh_transport)

        repo.add_review_message(
            project_id=project["id"], role="system", kind="text",
            content=f"实验 {experiment_id} 已记录结论:{conclusion}"
                    + (f"(next_decision={next_decision})" if next_decision else "")
                    + (";失败经验已入 knowledge/failures.md" if failure_entry else ""))
        # 一键结论:改进且全终态 → 直接归档,用户不用手动走 confirm→done
        archived = False
        if auto and conclusion == "supported" and comparison and comparison.get("allTerminal"):
            try:
                repo.advance_project_stage(project["id"], to_stage="done",
                                           expect_stage="confirm")
                archived = True
            except RepoError:
                archived = False  # 阶段已被并发流转/已归档:不阻塞结论本身
        return {
            "experimentId": experiment_id,
            "conclusion": conclusion,
            "nextDecision": next_decision,
            "evidence": evidence,
            "analysis": analysis or draft_analysis_text(
                comparison, plan.get("summary") or experiment_id),
            "failureKnowledgeWritten": bool(failure_entry),
            "archived": archived,
            "comparison": comparison,
        }
    except server_admin.ServerAdminError as exc:
        raise IterationError(str(exc)) from exc
    finally:
        if ssh_transport is not None:
            ssh_transport.close()


def _apply_conclusion_bytes(raw: bytes, experiment_id: str, conclusion: str,
                            evidence: list[str], analysis: str,
                            next_decision: str | None,
                            reconsideration: str) -> bytes:
    expected = hashlib.sha256(raw).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / EXPERIMENTS_REL).mkdir(parents=True, exist_ok=True)
        (root / EXPERIMENTS_REL / f"{experiment_id}.md").write_bytes(raw)
        try:
            research.record_conclusion(
                root, experiment_id, conclusion=conclusion, evidence=evidence,
                analysis=analysis, next_decision=next_decision,
                reconsideration=reconsideration or None,
                expected_hash=expected)
        except research.ResearchError as exc:
            raise IterationError(str(exc)) from exc
        return (root / EXPERIMENTS_REL / f"{experiment_id}.md").read_bytes()


def _default_reconsideration(conclusion: str) -> str:
    if conclusion == "rejected":
        return ("当出现新的数据、方法思路或算力(如评审报告之外的新方案)时,"
                "回方案评审页讨论下一轮迭代。")
    return "当失败原因被排除(环境/数据修复)且证据仍不足时,重新评估本轮假设。"


def failure_log_tail(repo: Repo, credentials: CredentialStore, project: dict,
                     job: dict, *, ssh_transport=None, limit: int = 900) -> str:
    """失败 Job 日志尾部,按可靠性顺序取:
    1. 终态事件自带的 log_tail(runner 落在 job.failed 事件里,免 SSH);
    2. 输出目录文件:train_stdout.log(baseline 适配约定)→
       runner.log(runner 归档,stdout+stderr 全量)。
    真正的死因(OOM/traceback)在日志里,摘要往往只有 exit code;
    轮内反馈与 failures.md 记录共用。"""
    import json as _json
    import shlex as _shlex

    # 1) 事件里的 log_tail(D-023:策略层不再被 exit code 瞎眼)
    try:
        conn = repo.db.connect()
        try:
            row = conn.execute(
                "SELECT payload_json FROM job_events WHERE job_id = ?"
                " AND event_type IN ('job.failed', 'job.timed_out')"
                " ORDER BY event_seq DESC LIMIT 1", (job["id"],)).fetchone()
        finally:
            conn.close()
        if row is not None:
            try:
                payload = _json.loads(row["payload_json"] or "{}")
            except ValueError:
                payload = {}
            tail = str(payload.get("log_tail") or "").strip()
            if tail:
                return tail[-limit:]
    except Exception:
        pass

    output_root = (project.get("output_root") or "").strip()
    if not output_root:
        return ""
    base = output_root.rstrip("/")
    candidates = [f"{base}/{job['id']}/train_stdout.log",
                  f"{base}/{job['id']}/runner.log"]
    transport = ssh_transport
    owns = False
    try:
        if transport is None:
            server = repo.get_server(project["server_id"])
            if server is not None and server["transport_mode"] == "ssh":
                transport = server_admin.connect_ssh(
                    server, credentials,
                    server_admin.hostkeys_path_for(credentials))
                owns = True
        for log_path in candidates:
            if transport is not None:
                result = transport.exec_argv(
                    ["sh", "-c",
                     f"test -s {_shlex.quote(log_path)} && tail -c {limit} {_shlex.quote(log_path)}"],
                    timeout=20)
                out = (result.stdout or "").strip()
                if out and result.exit_code == 0:
                    return out[-limit:]
            else:
                from pathlib import Path as _Path

                target = _Path(log_path)
                if target.is_file() and target.stat().st_size > 0:
                    return target.read_text(encoding="utf-8",
                                            errors="replace").strip()[-limit:]
    except Exception:
        return ""
    finally:
        if owns and transport is not None:
            transport.close()
    return ""


def _failure_entry_lines(experiment_id: str, plan: dict, comparison: dict | None,
                         failed_jobs: list[dict], *, reconsideration: str,
                         log_tails: dict[str, str] | None = None) -> list[str]:
    lines = [f"## {experiment_id} · {plan.get('summary') or '迭代实验失败记录'}", "",
             f"- Recorded at: {research.utc_now_stamp()}",
             f"- Related experiments: {experiment_id}"]
    if failed_jobs:
        summary = "; ".join(
            f"{j['id']}({j['status']}:{(j.get('failure_summary') or '退出异常')[:80]})"
            for j in failed_jobs[:5])
        lines.append(f"- Evidence: job:{failed_jobs[0]['id']}")
        lines.append(f"- What failed: {summary}")
        tails = log_tails or {}
        for job in failed_jobs[:2]:
            tail = tails.get(job["id"])
            if tail:
                # 日志尾部经常直接点名死因(OOM/ImportError/断言)
                cause = next((ln for ln in tail.splitlines()
                              if ("Error" in ln or "error" in ln)
                              and len(ln) < 200), "")
                if cause:
                    lines.append(f"- Likely cause ({job['id']}): {cause.strip()}")
                lines.append(f"- Log tail ({job['id']}):\n```\n{tail[-600:]}\n```")
    else:
        lines.append("- Evidence: 见实验文件 Evidence 节")
        lines.append(f"- What failed: 结论 {comparison and comparison.get('verdict')}"
                     "(指标未超越基线)")
        lines.append("- Likely cause: (待补充:对照日志与指标输出分析)")
    if not any(ln.startswith("- Likely cause") for ln in lines):
        lines.append("- Likely cause: (待补充:对照日志与指标输出分析)")
    lines.append("- What was ruled out: 本轮已排除该假设在当前配置下的直接有效性")
    lines.append(f"- Reconsider when: {reconsideration}")
    lines.append("- Avoid repeating: 同一配置直接重跑前先核对失败原因与实验文件结论")
    return lines


def _append_failure_entry(workdir: str, project_id: str, entry_lines: list[str],
                          ssh_transport) -> None:
    rel_path = f"{workdir.rstrip('/')}/{KNOWLEDGE_FAILURES_REL}"
    try:
        current = _read_bytes(workdir, KNOWLEDGE_FAILURES_REL, ssh_transport).decode(
            "utf-8", "replace")
    except Exception:
        current = research.FAILURES_TEMPLATE.replace(
            "PROJECT_ID", project_id).replace(
            "UPDATED_AT", research.utc_now_stamp())
    updated = current.rstrip() + "\n\n" + "\n".join(entry_lines) + "\n"
    if ssh_transport is not None:
        ssh_transport.sftp_write(rel_path, updated.encode("utf-8"))
    else:
        target = Path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(updated, encoding="utf-8")


def append_failure_note(repo: Repo, credentials, hostkeys_path, project_id: str,
                        *, title: str, detail: str) -> None:
    """循环终态留痕到 knowledge/failures.md(公开包装)。

    供 iteration_loop 在异常终止/失败链耗尽时调用:终态原因只在 DB 里,
    下一个会话读不到;写进 failures.md 才符合「失败经验进证据层」的纪律。
    尽力而为:任何异常(SSH 不通/文件缺失)都吞掉,绝不反噬状态机落库。
    """
    try:
        project = repo.get_project(project_id)
        if project is None or not project.get("remote_workdir"):
            return
        server = repo.get_server(project["server_id"])
        ssh_transport = None
        if server is not None and server["transport_mode"] == "ssh":
            ssh_transport = server_admin.connect_ssh(server, credentials, hostkeys_path)
        entry = [
            f"## {research.utc_now_stamp()} · {title}",
            f"- 项目: {project_id}",
            f"- What happened: {detail}",
            "- Likely cause: 循环在此终止,完整原因见循环 stop_reason 与控制面日志",
            "- What was ruled out: —",
            "- Reconsider when: 处理上述原因后再重启循环",
            "- Avoid repeating: 重启前先读本条与上一轮结论,不要原样重跑",
        ]
        _append_failure_entry(project["remote_workdir"], project_id, entry, ssh_transport)
    except Exception:
        pass
