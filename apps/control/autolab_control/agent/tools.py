"""agent 可用的全部工具。各阶段按需组合(stages/baseline.py、stages/research.py)。

工具只做薄包装:业务逻辑在 workspace/ 与 execution/;工具里抛出的 UserError
会作为错误结果回给 agent,不中断会话。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from ..execution import experiments
from ..workspace import papers, refs
from ..workspace.host import q
from .runtime import Finish, LoopEnv, Pause, Tool, ToolError

_OBJ = "object"

# 网络不可达升级(真机教训:HF 组件即使有缓存也默认联网校验,网络部分不通时
# 训练进程挂在重试里干等,agent 跟着空转烧步数)。同会话累计撞到阈值就强制
# 暂停问 owner,而不是让 agent 无限排障。
_NETWORK_FAIL_RE = re.compile(
    r"network is unreachable|connection refused|could not resolve host"
    r"|max retries exceeded|errno 1[01][17]|failed to establish a new connection"
    r"|connection timed out|connect\(\) timed out", re.IGNORECASE)
_NETWORK_FAIL_LIMIT = 3
_net_fail_counts: dict[str, int] = {}


def reset_network_failures(run_id: str) -> None:
    _net_fail_counts.pop(run_id, None)


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": _OBJ, "properties": properties, "required": required or []}


# ------------------------------------------------------------------ 工作区

def workspace_tools(*, writable: bool = True) -> list[Tool]:
    """writable=False 供验证型会话:只读诊断,不能改代码(验证的就是当前工作区)。"""
    tools_list = [
        Tool("read_file", "读工作区文件(带行号,分片)",
             _schema({"path": {"type": "string"}, "offset": {"type": "integer"},
                      "limit": {"type": "integer"}}, ["path"]),
             lambda env, a: env.workspace.read_file(a["path"], a.get("offset") or 1,
                                                    a.get("limit") or 400)),
        Tool("list_files", "列工作区目录(两层)",
             _schema({"path": {"type": "string"}}),
             lambda env, a: env.workspace.list_files(a.get("path") or ".")),
        Tool("grep", "在工作区搜索(py/yaml/json/md/sh/cfg)",
             _schema({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
             lambda env, a: env.workspace.grep(a["pattern"], a.get("path") or "")),
    ]
    if writable:
        tools_list.append(
            Tool("write_file", "写整个文件(首次改动前备份原件;.py 语法检查不过自动回滚)",
                 _schema({"path": {"type": "string"}, "content": {"type": "string"},
                          "reason": {"type": "string"}}, ["path", "content", "reason"]),
                 lambda env, a: env.workspace.write_file(a["path"], a["content"], a["reason"])))
    return tools_list


# 治理(真机前科:agent 用 shell 直改控制端 DB 的 projects 表自救、给平台
# 进程埋雷)。控制端数据库/凭据是产品边界——状态改动只能走工具/API;
# 平台自身进程(control/runner/web)的生死是 owner 的事。
_KILL_RE = re.compile(r"\b(?:pkill|killall|kill)\b")
_PLATFORM_TOKENS = ("autolab_control", "autolab_runner", "autolab-runner",
                    "next-server", "next start", "npm", "sshd", "systemctl",
                    "docker", "systemd")


def _governance_paths(env: LoopEnv) -> list[str]:
    paths: list[str] = []
    for holder in (getattr(env, "repo", None), getattr(env, "credentials", None)):
        db = getattr(holder, "db", None) or holder
        path = getattr(db, "path", None)
        if path is not None:
            paths.append(str(path))
    return paths


def _governance_check(env: LoopEnv, command: str) -> None:
    for path in _governance_paths(env):
        if path and path in command:
            raise ToolError(
                f"命令碰到了控制端数据/凭据({path})——这是产品边界,agent 不能直读直改:"
                "改状态用工具(它们走产品 API);需要 owner 做的事用 ask_human。")
    if _KILL_RE.search(command) and any(t in command.lower() for t in _PLATFORM_TOKENS):
        raise ToolError(
            "不允许杀平台自身进程(control/runner/web)或系统服务——那是 owner 的领地;"
            "自己的训练/冒烟进程可以 kill(按 train.py、autolab_baseline.py 等匹配)。")


_DENY = ("sudo ", "rm -rf /", "rm -rf ~", "mkfs", "shutdown", "reboot",
         "git push", ":(){", "> /dev/sd", "chmod -R 777 /")


def shell_tool(*, max_timeout: int = 900) -> Tool:
    """工作区内执行 shell(探索 / 装环境 / 解压 / 冒烟)。只拦破坏性命令;
    长训练用 run_experiment(排队、可恢复、有 GPU 租约)。"""

    def handler(env: LoopEnv, args: dict) -> str:
        command = (args.get("command") or "").strip()
        if not command:
            raise ToolError("command 不能为空")
        hit = next((d for d in _DENY if d in command.lower()), None)
        if hit:
            raise ToolError(f"命令被拒绝(含 {hit.strip()})")
        _governance_check(env, command)
        timeout = max(5, min(int(args.get("timeout") or 120), max_timeout))
        conda_env = (args.get("conda_env") or "").strip()
        if conda_env:
            command = ("source $HOME/anaconda3/etc/profile.d/conda.sh 2>/dev/null || "
                       "source $HOME/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
                       f"conda activate {q(conda_env)} && {command}")
        try:
            result = env.workspace.sh(command, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 超时/断连都是观察
            raise ToolError(f"执行超时/中断({timeout}s):{exc}") from exc
        out = result.text
        if len(out) > 12000:
            out = out[:3000] + "\n…(中间省略)…\n" + out[-8000:]
        failure = _NETWORK_FAIL_RE.search(out)
        if failure is not None:
            run_id = str(env.run.get("id") or "")
            count = _net_fail_counts.get(run_id, 0) + 1
            _net_fail_counts[run_id] = count
            if count >= _NETWORK_FAIL_LIMIT:
                _net_fail_counts.pop(run_id, None)
                return Pause("human", {
                    "kind": "info",
                    "question": (
                        f"本会话已累计 {count} 次命令输出出现网络不可达错误"
                        f"(最近一处:{failure.group(0)}),自动暂停请 owner 定夺。\n"
                        "常见处置:①离线模式——后续命令加前缀 "
                        "export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 用本地缓存"
                        "(HuggingFace 组件即使已缓存也默认联网校验版本);"
                        "②换镜像源;③确实要联网下载的材料,请 owner 配代理或直接提供。"
                        "owner 回复后继续。")})
        return f"exit={result.code}\n{out}"

    return Tool(
        "shell",
        "在工作区目录执行 bash 命令,返回 exit code 与输出;可选 conda_env 激活环境。"
        f"超时上限 {max_timeout}s。用于探索、装依赖、解压、短冒烟;正式训练用 run_experiment。",
        _schema({"command": {"type": "string"},
                 "timeout": {"type": "integer", "description": "秒,默认 120"},
                 "conda_env": {"type": "string"}}, ["command"]),
        handler)


# ------------------------------------------------------------------ 与 owner 交互

def knowledge_tool() -> Tool:
    """LABNOTE 知识库检索(链路 C):论文/笔记/往轮实验结论。只在
    LABNOTE_URL/LABNOTE_PAT 配好时由阶段 build_tools 注册。"""

    def handler(env: LoopEnv, args: dict) -> str:
        from . import knowledge
        query = (args.get("query") or "").strip()
        if not query:
            raise ToolError("query 不能为空")
        top_k = max(1, min(int(args.get("top_k") or 5), 10))
        spaces: list[str] = []
        try:
            config = json.loads(str(env.run.get("config_json") or "{}"))
            if config.get("kb_assist"):
                spaces = [s for s in (config.get("kb_spaces") or []) if s]
        except (ValueError, AttributeError):
            pass
        try:
            if spaces:
                return knowledge.retrieve_spaces(query, spaces, per=top_k)
            return knowledge.retrieve(query, top_k)
        except Exception as exc:  # noqa: BLE001 - 知识库故障是观察,不是会话失败
            raise ToolError(f"知识库检索失败:{exc}") from exc

    return Tool(
        "search_knowledge",
        "检索 LABNOTE 知识库:论文、笔记、往轮实验结论(含失败原因),返回相关片段与出处。"
        "定研究方向前先查:同类方法试过什么、为什么失败;遇到怪现象也可以查。",
        _schema({"query": {"type": "string"}, "top_k": {"type": "integer"}}, ["query"]),
        handler)


def paper_radar_tool() -> Tool:
    """论文雷达检索(PaperPulse):世界刚发表的论文——arXiv 学科/核心期刊每日新文,
    模型双重评分+中文导读。论文站不可达时不注册(阶段 build_tools 探测)。"""

    def handler(env: LoopEnv, args: dict) -> str:
        from . import paper_radar
        query = (args.get("query") or "").strip()
        top_k = max(1, min(int(args.get("top_k") or 6), 20))
        window = args.get("window") or "7d"
        try:
            return paper_radar.search(query, top_k, window)
        except Exception as exc:  # noqa: BLE001 - 论文站故障是观察,不是会话失败
            raise ToolError(f"论文雷达检索失败:{exc}") from exc

    return Tool(
        "search_papers",
        "检索论文雷达(PaperPulse):世界最近发表的 AI 论文,arXiv 学科+核心期刊每日新文,"
        "已按学术重要性筛选并写中文摘要。定研究方向/选基线方法/实验连续失败找原因时查:"
        "同行刚用什么方法、什么指标、是否已有人做过同样的事。"
        "query 用中文或英文关键词(如\"医学图像分割 半监督\"),留空返回最近时间线。",
        _schema({
            "query": {"type": "string", "description": "关键词,留空=最新"},
            "top_k": {"type": "integer", "description": "返回条数,默认 6"},
            "window": {"type": "string", "enum": ["24h", "7d"], "description": "时间窗,默认 7d"},
        }, []),
        handler)


def ask_human_tool() -> Tool:
    """结构化求助:kind 决定前端渲染(上传 / 选目录 / 文字回答)。"""

    def handler(env: LoopEnv, args: dict) -> Pause:
        question = (args.get("question") or "").strip()
        if not question:
            raise ToolError("question 不能为空")
        payload = {"question": question, "kind": args.get("kind") or "info"}
        for key in ("name", "target_path", "why"):
            if args.get(key):
                payload[key] = str(args[key])
        return Pause("human", payload)

    return Tool(
        "ask_human",
        "向 owner 提问或索要材料(数据集/权重/文件/信息),会话暂停直到 owner 回复。"
        "一次把同类缺口问清楚;能自己查到的不要问。",
        _schema({
            "question": {"type": "string", "description": "要 owner 做什么/回答什么"},
            "kind": {"type": "string", "enum": ["dataset", "weights", "file", "info", "confirm"],
                     "description": "dataset/weights/file=需要上传或指定目录;info=文字回答;"
                                    "confirm=请 owner 确认"},
            "name": {"type": "string", "description": "材料名,如 Kvasir-SEG"},
            "target_path": {"type": "string", "description": "材料应放到工作区的相对路径"},
            "why": {"type": "string", "description": "依据(哪段代码/配置需要它)"},
        }, ["question", "kind"]),
        handler)


def finish_tool(description: str, properties: dict, required: list[str],
                handler: Callable[[LoopEnv, dict], object] | None = None) -> Tool:
    return Tool("finish", description, _schema(properties, required),
                handler or (lambda env, args: Finish(dict(args))))


# ------------------------------------------------------------------ 实验

def experiment_tools(*, baseline_ref: Callable[[LoopEnv], str] | None = None,
                     allowed_datasets: Callable[[LoopEnv], set[str] | None] | None = None,
                     dataset_commands: Callable[[LoopEnv], dict[str, str] | None] | None = None,
                     before_submit: Callable[[LoopEnv], None] | None = None) -> list[Tool]:
    """baseline_ref / allowed_datasets / dataset_commands 按会话惰性求值;
    before_submit 抛 ToolError 可拦截提交(如 GPU 预算用完)。"""

    def run_experiment(env: LoopEnv, args: dict) -> str:
        if before_submit is not None:
            before_submit(env)
        request = experiments.ExperimentRequest(
            jobs=args.get("jobs") or [], metrics=args.get("metrics") or [],
            purpose=(args.get("purpose") or "").strip() or "实验",
            conda_env=args.get("conda_env") or "",
            metric_file=args.get("metric_file") or "metrics.json",
            timeout_minutes=args.get("timeout_minutes") or 120,
            gpus_per_job=args.get("gpus_per_job") or 1,
            allowed_datasets=allowed_datasets(env) if allowed_datasets else None,
            dataset_commands=dataset_commands(env) if dataset_commands else None)
        result = experiments.submit(
            env.repo, env.workspace, env.project, request,
            approved_by=f"agent-run:{env.run['id']}",
            baseline_ref=baseline_ref(env) if baseline_ref else "baseline")
        listing = "\n".join(f"- {j['name']}: {j['id']}" for j in result["jobs"])
        return (f"已提交 {result['experimentId']}(plan {result['planId']}),Job 已排队:\n"
                f"{listing}\n用 wait_jobs 等结果。")

    def wait_jobs(env: LoopEnv, args: dict):
        job_ids = [str(j) for j in args.get("job_ids") or []]
        if not job_ids:
            raise ToolError("job_ids 不能为空")
        for job_id in job_ids:
            experiments.project_job(env.repo, env.project, job_id)
        if experiments.all_terminal(env.repo, job_ids):
            return experiments.describe_jobs(env.repo, env.credentials, env.project, job_ids)
        return Pause("jobs", {"job_ids": job_ids})

    def cancel_jobs(env: LoopEnv, args: dict) -> str:
        cancelled = []
        for job_id in [str(j) for j in args.get("job_ids") or []]:
            job = experiments.project_job(env.repo, env.project, job_id)
            if not experiments.all_terminal(env.repo, [job["id"]]):
                env.repo.request_cancel(job_id)
                cancelled.append(job_id)
        return f"已请求取消:{', '.join(cancelled) or '(无未结束的 Job)'}"

    job_ids = {"job_ids": {"type": "array", "items": {"type": "string"}}}
    return [
        Tool("run_experiment",
             "在 GPU 上正式运行一组命令(每条一个 Job,经 Runner 排队执行,可过夜,重启不丢)。"
             "命令在工作区目录执行;程序必须把指标写成 JSON 到 $AUTOLAB_OUTPUT_DIR/<metric_file>"
             "(默认 metrics.json),键名与 metrics 一致。提交后立即返回 Job id,用 wait_jobs 等结果。",
             _schema({
                 "jobs": {"type": "array", "items": _schema({
                     "name": {"type": "string", "description": "如 kvasir 或 lr1e-4"},
                     "command": {"type": "string"},
                     "dataset": {"type": "string",
                                 "description": "该 Job 跑的数据集(研究阶段必填,用于对照该数据集基线)"},
                 }, ["name", "command"])},
                 "metrics": {"type": "array", "items": _schema({
                     "name": {"type": "string"},
                     "direction": {"type": "string", "enum": ["maximize", "minimize"]}}, ["name"])},
                 "purpose": {"type": "string", "description": "这批实验要验证什么"},
                 "conda_env": {"type": "string"}, "metric_file": {"type": "string"},
                 "timeout_minutes": {"type": "integer"}, "gpus_per_job": {"type": "integer"},
             }, ["jobs", "metrics", "purpose"]),
             run_experiment),
        Tool("wait_jobs", "等待 Job 结束(会话暂停,不占 LLM);结束后返回状态、指标、失败日志尾部。",
             _schema(job_ids, ["job_ids"]), wait_jobs),
        Tool("cancel_jobs", "取消仍在排队/运行的 Job(方向错了及时止损)。",
             _schema(job_ids, ["job_ids"]), cancel_jobs),
    ]


# ------------------------------------------------------------------ 资料

def read_paper_tool() -> Tool:
    def handler(env: LoopEnv, args: dict) -> str:
        name = str(args.get("path") or "").strip()
        if not name:
            return "\n".join(papers.list_papers(env.workspace)) or "(没有上传论文)"
        first = int(args.get("from_page") or 1)
        return papers.read_pages(env.workspace, name.rsplit("/", 1)[-1], first,
                                 int(args.get("to_page") or first + 7))[:20000]

    return Tool("read_paper", "不带 path:列出论文;带 path:按页读取文本(默认 8 页一段)。",
                _schema({"path": {"type": "string"}, "from_page": {"type": "integer"},
                         "to_page": {"type": "integer"}}),
                handler)


def read_reference_tool() -> Tool:
    def handler(env: LoopEnv, args: dict) -> str:
        registered = json.loads(env.project.get("refs_json") or "[]")
        return refs.read(registered, str(args.get("ref") or ""), str(args.get("path") or "."),
                         int(args.get("offset") or 1), int(args.get("limit") or 400))

    return Tool("read_reference",
                "读参考代码库:ref=库名(不填列出全部);path 为目录则列出,为文件则带行号读取。",
                _schema({"ref": {"type": "string"}, "path": {"type": "string"},
                         "offset": {"type": "integer"}, "limit": {"type": "integer"}}),
                handler)
