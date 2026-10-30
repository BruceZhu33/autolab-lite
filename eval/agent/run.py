"""agent eval harness runner(Phase B2.2a):同一套任务 + cassette + 断言,回归评测 agent。

不调真实模型、不碰网络/GPU:CassetteModel 回放预设回复,Job 由 harness 直接
落库置 SUCCEEDED 并写指标 artifact(照抄 control 测试的 finish_job 做法)。
复用 control 的原语(Repo / baseline.start / research.start / runtime.step),
任何 prompt/tool/runtime 改动后跑一遍即可回答「哪些任务退化了」。

用法:
    uv run python eval/agent/run.py --tasks all --out eval/agent/reports/latest.md
    uv run python eval/agent/run.py --tasks baseline_happy,research_multiseed
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:  # 支持 `python eval/agent/run.py` 直跑
    sys.path.insert(0, str(REPO_ROOT))

from autolab_control.agent import runtime, trace  # noqa: E402
from autolab_control.agent.model import model_settings  # noqa: E402
from autolab_control.agent.spec import apply_profile_overlay, overlay_hash  # noqa: E402
from autolab_control.core.credentials import CredentialStore  # noqa: E402
from autolab_control.core.db import Database, utc_now  # noqa: E402
from autolab_control.core.errors import UserError  # noqa: E402
from autolab_control.core.repo import Repo  # noqa: E402
from autolab_control.stages import agent_specs  # noqa: E402
from autolab_control.stages import baseline as baseline_stage  # noqa: E402
from autolab_control.stages import research as research_stage  # noqa: E402

from eval.agent.model import CassetteModel, LiveModel  # noqa: E402

TASKS_DIR = AGENT_DIR / "tasks"
LIVE_TASKS_DIR = AGENT_DIR / "live_tasks"
DEFAULT_REPORT = AGENT_DIR / "reports" / "latest.md"
DEFAULT_MAX_STEPS = 30
DEFAULT_METRIC_BASELINE = 0.81
STOP_STATUSES = ("done", "failed", "stopped", "waiting_human")
_MISSING = object()


class JobMetricsExhausted(Exception):
    """job_metrics 队列耗尽而仍有 QUEUED Job:任务直接判 FAIL。"""


# ------------------------------------------------------------------ 离线护栏


@contextmanager
def offline_tools():
    """harness 期间绝不探测网络:关掉 build_tools 的知识库/论文雷达可用性探测,
    以及状态转移的飞书 webhook(环境里配了也不能真发)。用完恢复原函数。"""
    from autolab_control.agent import knowledge, paper_radar
    from autolab_control.core import notify

    saved = (paper_radar.available, knowledge.configured, notify.webhook_url)
    paper_radar.available = lambda: False
    knowledge.configured = lambda: False
    notify.webhook_url = lambda: ""
    try:
        yield
    finally:
        paper_radar.available, knowledge.configured, notify.webhook_url = saved


def load_spec_file(path: Path) -> dict:
    """--spec-file:JSON 内容为 overlay config(不含 id)。校验复用注册表
    (stages/agent_specs);坏文件/坏 config 打到 stderr 并 sys.exit(2)。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"--spec-file 读取失败:{path}({exc})", file=sys.stderr)
        sys.exit(2)
    try:
        return agent_specs.clean_overlay_config(raw)
    except UserError as exc:
        print(f"--spec-file 非法:{exc}", file=sys.stderr)
        sys.exit(2)


# ------------------------------------------------------------------ Job 落库


def _run_job_ids(repo: Repo, run_id: str) -> list[str]:
    """本 run 提交的 Job:训练(agent-run:<id>)与平台评测(platform-eval:<id>,
    B2.3b),按批准顺序——训练 Job 先、其评测 Job 后。"""
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM plan_revisions WHERE approved_by IN (?, ?)"
            " ORDER BY approved_at, id",
            (f"agent-run:{run_id}", f"platform-eval:{run_id}")).fetchall()
    finally:
        conn.close()
    ids: list[str] = []
    for row in rows:
        ids.extend(job["id"] for job in repo.list_jobs(plan_id=row["id"]))
    return ids


def _finish_job(repo: Repo, job_id: str, metrics: dict) -> None:
    """直接 SQL 把 Job 置 SUCCEEDED 并插入 metrics artifact(control 测试同款)。"""
    conn = repo.db.connect()
    try:
        now = utc_now()
        with repo.db.tx(conn):
            conn.execute(
                "UPDATE jobs SET status = 'SUCCEEDED', started_at = ?, finished_at = ?"
                " WHERE id = ?", (now, now, job_id))
            conn.execute(
                "INSERT INTO artifacts (id, job_id, kind, location_kind, remote_path,"
                " sha256, size_bytes, metadata_json, created_at)"
                " VALUES (?, ?, 'metrics', 'remote', ?, ?, 10, ?, ?)",
                (f"art-{job_id}", job_id, f"/tmp/out/{job_id}/metrics.json", "0" * 64,
                 json.dumps({"metric_values": metrics}), now))
    finally:
        conn.close()


def _drain_queued_jobs(repo: Repo, run_id: str, metrics_queue: list[dict],
                       metric_fn=None) -> None:
    """把本 run 仍 QUEUED 的 Job 依次消费 metrics_queue 置为 SUCCEEDED。

    live 模式给 metric_fn:按 Job 参数(dataset/variant/seed)确定性生成指标,
    不再依赖预录 job_metrics。"""
    for job_id in _run_job_ids(repo, run_id):
        job = repo.get_job(job_id)
        if job is None or job["status"] != "QUEUED":
            continue
        if metric_fn is not None:
            metrics = metric_fn(job)
        else:
            if not metrics_queue:
                raise JobMetricsExhausted(
                    f"job_metrics 队列已耗尽,Job {job_id} 仍是 QUEUED")
            metrics = metrics_queue.pop(0)
        _finish_job(repo, job_id, metrics)


def _simulated_metrics(task: dict, job: dict) -> dict:
    """live 任务的确定性 Job 指标:baseline + variant_gain + seed_jitter。

    metric_profile 缺省时回落到 0.81(不静默写 0);seed 抖动按 seed % 7
    对称展开,同一 seed 每次结果一致,不做随机。"""
    try:
        params = json.loads(job.get("params_json") or "{}")
    except (TypeError, ValueError):
        params = {}
    dataset = str(params.get("dataset") or "")
    variant = str(params.get("variant") or "base")
    profile = task.get("metric_profile") or {}
    baselines = profile.get("baseline") or {}
    baseline = baselines.get(dataset)
    if baseline is None:
        baseline = baselines.get("") if "" in baselines else DEFAULT_METRIC_BASELINE
    gains = (profile.get("variant_gains") or {}).get(dataset) or {}
    gain = float(gains.get(variant) or 0.0)
    jitter = 0.0
    seed = params.get("seed")
    if seed is not None:
        try:
            seed_int = int(seed)
        except (TypeError, ValueError):
            seed_int = 0
        jitter = ((seed_int % 7) - 3) * float(profile.get("seed_spread") or 0.0)
    metric = str((task.get("contract") or {}).get("primary") or "val_dice")
    return {metric: float(baseline) + gain + jitter}


def _refresh_job_context(repo: Repo, run_id: str, context: dict) -> None:
    """占位符上下文:{{job}}/{{jobs}} 指向最近一次 run_experiment 的 Job。"""
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM plan_revisions WHERE approved_by = ?"
            " ORDER BY approved_at DESC LIMIT 1", (f"agent-run:{run_id}",)).fetchone()
    finally:
        conn.close()
    if row is None:
        return
    jobs = repo.list_jobs(plan_id=row["id"])
    context["jobs"] = [job["id"] for job in jobs]
    if jobs:
        context["job"] = jobs[0]["id"]


# ------------------------------------------------------------------ 单任务执行


def _pending_view(pending: dict | None) -> dict | None:
    """runtime 的 pending.kind 恒为 human;对外投影成载荷里的业务 kind
    (confirm_baseline / dataset / round_review ...),payload 原样保留。"""
    if not isinstance(pending, dict):
        return pending
    view = dict(pending)
    payload = pending.get("payload")
    if isinstance(payload, dict) and payload.get("kind"):
        view["kind"] = payload["kind"]
    return view


def run_task(task: dict, *, root: Path | None = None, overlay: dict | None = None,
             spec: dict | None = None, credentials=None, model=None) -> dict:
    """跑一个任务,返回结果对象(expect 断言在它之上求值,见 evaluate_expect)。

    overlay(C4a --spec-file):候选 AgentSpec 的 overlay config,驱动前先
    apply_profile_overlay;cassette/断言同一套。

    model 为 None = replay:用 CassetteModel 回放,Job 指标消费 job_metrics。
    model 给了 = live:用它调真实模型(通常 LiveModel),Job 指标由
    metric_profile 确定性模拟。credentials 仅 live 需要。
    spec 是报告元数据 {file,hash,keys},随结果落 JSONL/Markdown。
    """
    started = time.monotonic()
    live = model is not None
    base = (Path(root) / task["id"]) if root is not None \
        else Path(tempfile.mkdtemp(prefix="autolab-eval-"))
    work, out = base / "work", base / "out"
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    fixture = AGENT_DIR / task["fixture"]
    shutil.copytree(fixture, work, dirs_exist_ok=True)
    (work / ".autolab").mkdir(exist_ok=True)

    repo = Repo(Database(base / "control.db"))
    server = repo.add_local_server(
        name="eval-local", runner_port=0, runner_token_ref="eval-creds", gpu_slots=1,
        allowed_work_roots=[str(work)], allowed_output_roots=[str(out)])
    pid = repo.add_project(name=task["id"], server_id=server["id"],
                           workdir=str(work), output_root=str(out))["id"]
    if task.get("baseline"):
        snapshot: dict = {"datasets": task["baseline"]["datasets"]}
        if task.get("contract"):
            snapshot["metric_contract"] = task["contract"]
        repo.update_project(pid, baseline_passed_at=utc_now(),
                            baseline_json=json.dumps(snapshot, ensure_ascii=False))

    harness_error = ""
    statuses: list[str] = []
    with offline_tools():
        if task["kind"] == "baseline":
            profile = baseline_stage.PROFILE
            run = baseline_stage.start(repo, pid, note=task.get("note") or "")
        elif task["kind"] == "research":
            profile = research_stage.PROFILE
            run = research_stage.start(repo, pid, tier=task.get("tier") or "light",
                                       idea=task.get("idea") or "",
                                       datasets=task.get("datasets") or None, network=False)
        else:
            raise ValueError(f"未知任务 kind:{task['kind']}")
        if overlay:
            profile = apply_profile_overlay(profile, overlay)
        if isinstance(model, LiveModel):
            if model.model:  # --model / canary 显式模型优先,快照与实际调用一致
                profile = apply_profile_overlay(profile, {"model": model.model})
            else:
                model.model = str((overlay or {}).get("model") or "").strip()
        run_id = run["id"]

        context = {"run_id": run_id, "project_id": pid}
        if model is None:
            model = CassetteModel(task.get("cassette") or [], context)
        metrics_queue = list(task.get("job_metrics") or [])
        metric_fn = (lambda job: _simulated_metrics(task, job)) if live else None
        max_steps = int((task.get("budget") or {}).get("max_steps") or DEFAULT_MAX_STEPS)
        for _ in range(max_steps):
            if model.exhausted:
                if live:
                    harness_error = (
                        getattr(model, "limit_error", "")
                        or f"live 模型调用上限 {getattr(model, 'max_calls', '?')}"
                           f" 或 token 上限 {getattr(model, 'max_tokens', '?')} 已用完"
                           f"(calls={getattr(model, 'calls', 0)},"
                           f" tokens={getattr(model, 'tokens_total', 0)})")
                else:
                    harness_error = (f"cassette 耗尽"
                                     f"(状态 {statuses[-1] if statuses else '?'})")
                break
            status = runtime.step(repo, credentials, run_id, profile, model=model)
            statuses.append(status)
            limit_error = getattr(model, "limit_error", "")
            if limit_error:  # LiveModel 超限:任务直接判 FAIL,不再重试
                harness_error = str(limit_error)
                break
            _refresh_job_context(repo, run_id, context)
            try:
                _drain_queued_jobs(repo, run_id, metrics_queue, metric_fn=metric_fn)
            except JobMetricsExhausted as exc:
                harness_error = str(exc)
                break
            if status in STOP_STATUSES:
                break
        else:
            harness_error = (harness_error or f"步数上限 {max_steps} 内未到终态"
                             f"(最后 {statuses[-1] if statuses else '?'})")

        run_row = runtime.get_run(repo, run_id)
        spans = trace.list_spans(repo, run_id)
        events = runtime.list_events(repo, run_id)
        tool_calls = [e["payload"].get("name") for e in events if e["kind"] == "tool_call"]
        tool_errors = [f'{e["payload"].get("name")}: {e["payload"].get("error")}'
                       for e in events if e["kind"] == "tool_error"]

    if live:
        model_label = str(getattr(model, "model", "") or "")
        if not model_label:
            try:
                model_label = model_settings(credentials)["model"]
            except Exception:  # noqa: BLE001 - 仅展示用
                model_label = ""
        model_label = model_label or "injected"
    else:
        model_label = "cassette"
    tokens_total = int(run_row["tokensIn"]) + int(run_row["tokensOut"])
    result = {
        "task_id": task["id"],
        "kind": task.get("kind"),
        "tier": task.get("tier"),
        "mode": "live" if live else "replay",
        "model": model_label,
        "model_calls": int(getattr(model, "calls", 0) or 0),
        "tokens_total": tokens_total,
        "status": run_row["status"],
        "statuses": statuses,
        "run": run_row,
        "result": run_row.get("result"),
        "pending": _pending_view(run_row.get("pending")),
        "steps": run_row["steps"],
        "tokens_in": run_row["tokensIn"],
        "tokens_out": run_row["tokensOut"],
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "ask_humans": tool_calls.count("ask_human"),
        "span_kinds": dict(sorted(Counter(s["kind"] for s in spans).items())),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "workdir": str(base),
        "harness_error": harness_error,
        "spec": spec,
    }
    result["failures"] = evaluate_expect(result, task.get("expect") or {})
    if harness_error:
        result["failures"].append(
            {"path": "harness", "op": "ok", "expected": "无 harness 错误",
             "actual": harness_error})
    result["pass"] = not result["failures"]
    return result


# ------------------------------------------------------------------ 断言


def _get_path(obj: object, path: str) -> object:
    """点路径穿 dict 与 list(数字下标);取不到返回 _MISSING。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else _MISSING
        else:
            return _MISSING
    return cur


def _contains(actual: object, expected: str) -> bool:
    if isinstance(actual, str):
        return expected in actual
    if isinstance(actual, list):
        return any(expected in str(item) for item in actual)
    return False


def evaluate_expect(result: dict, expect: dict) -> list[dict]:
    """按点路径断言;后缀决定算子:_contains 包含、_gte/_lte 数值比较、其余 ==。"""
    failures = []
    for key, expected in expect.items():
        op, path = "==", key
        for suffix in ("contains", "gte", "lte"):
            if key.endswith("_" + suffix):
                op, path = suffix, key[: -(len(suffix) + 1)]
                break
        actual = _get_path(result, path)
        if op == "contains":
            ok = _contains(actual, str(expected))
        elif op in ("gte", "lte"):
            ok = (isinstance(actual, (int, float)) and not isinstance(actual, bool)
                  and (actual >= expected if op == "gte" else actual <= expected))
        else:
            ok = actual == expected
        if not ok:
            failures.append({"path": path, "op": op, "expected": expected,
                             "actual": "«missing»" if actual is _MISSING else actual})
    return failures


# ------------------------------------------------------------------ 报告与 CLI


def load_tasks(live: bool = False) -> dict[str, Path]:
    """{任务 id: 任务文件};文件名 stem 必须与 JSON 里的 id 一致。

    live=True 只读 eval/agent/live_tasks(真实模型任务);默认 replay 任务。"""
    directory = LIVE_TASKS_DIR if live else TASKS_DIR
    tasks: dict[str, Path] = {}
    for path in sorted(directory.glob("*.json")):
        task = json.loads(path.read_text(encoding="utf-8"))
        if task.get("id") != path.stem:
            raise ValueError(f"任务文件 {path.name} 的 id 字段({task.get('id')})与文件名不一致")
        tasks[path.stem] = path
    return tasks


def load_live_tasks() -> dict[str, Path]:
    return load_tasks(live=True)


def write_report(results: list[dict], out: Path) -> tuple[Path, Path]:
    """写 Markdown 表 + 同名 .jsonl(每任务一行);返回两个路径。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    jsonl = out.with_suffix(".jsonl")
    passed = sum(1 for r in results if r["pass"])
    mode = results[0].get("mode", "replay") if results else "replay"
    model = results[0].get("model", "") if results else ""
    total_calls = sum(int(r.get("model_calls") or 0) for r in results)
    total_tokens = sum(int(r.get("tokens_total") or 0) for r in results)
    lines = [
        f"# Agent Eval Report({mode})",
        "",
        f"- 生成时间:{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- mode:`{mode}`",
        f"- model:`{model or '-'}`",
        f"- 模型调用:{total_calls}",
        f"- tokens:{total_tokens}",
        f"- 结果:{passed}/{len(results)} PASS",
    ]
    spec = next((r["spec"] for r in results if r.get("spec")), None)
    if spec:  # C4a:--spec-file 时记录评测的候选 overlay(文件/指纹/键)
        lines.append(f"- spec:`{spec['file']}`(hash `{spec['hash']}`;"
                     f"keys {', '.join(spec['keys']) or '-'})")
    lines += [
        "",
        "| task | kind | 结果 | steps | tokens(in/out) | tool errors | 耗时(ms) | 失败断言 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        fails = "; ".join(f'{f["path"]} {f["op"]}={f["expected"]!r}'
                          for f in r["failures"]) or "-"
        lines.append(f'| {r["task_id"]} | {r["kind"] or "-"} '
                     f'| {"PASS" if r["pass"] else "FAIL"} | {r["steps"]} '
                     f'| {r["tokens_in"]}/{r["tokens_out"]} | {len(r["tool_errors"])} '
                     f'| {r["duration_ms"]} | {fails} |')
    failed = [r for r in results if not r["pass"]]
    if failed:
        lines += ["", "## 失败明细", ""]
        for r in failed:
            lines.append(f'### {r["task_id"]}(状态 {r["status"]})')
            if r.get("harness_error"):
                lines.append(f'- harness 错误:{r["harness_error"]}')
            for f in r["failures"]:
                lines.append(f'- `{f["path"]}` {f["op"]} 期望 {f["expected"]!r},'
                             f'实际 {f["actual"]!r}')
            lines.append(f'- workdir:`{r["workdir"]}`(临时目录,便于排查,不自动删)')
            lines.append("")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in results:
            row = {key: r.get(key) for key in (
                "task_id", "kind", "tier", "mode", "model", "model_calls",
                "tokens_total", "pass", "status", "steps", "tokens_in", "tokens_out",
                "span_kinds", "tool_calls", "tool_errors", "ask_humans", "duration_ms",
                "spec", "failures", "harness_error", "workdir")}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out, jsonl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval.agent.run",
        description="agent eval harness:B2.2a replay(默认,不调真实模型)"
                    "/ C4b live(真实 GLM,显式 --credentials)")
    parser.add_argument("--tasks", default="all",
                        help='"all" 或逗号分隔的任务 id(replay 查 tasks/,live 查 live_tasks/)')
    parser.add_argument("--out", default=str(DEFAULT_REPORT),
                        help="Markdown 报告路径(同时写同名 .jsonl)")
    parser.add_argument("--spec-file", default=None,
                        help="overlay config JSON 文件(C4a:评测候选 AgentSpec)")
    parser.add_argument("--mode", default="replay", choices=["replay", "live"],
                        help="replay(默认)/ live(真实模型)")
    parser.add_argument("--credentials", default=None,
                        help="凭据文件(live 必填;必须 0600)")
    parser.add_argument("--model", default=None,
                        help="临时覆盖模型 id(不写 AgentSpec)")
    parser.add_argument("--max-model-calls", type=int, default=12,
                        help="每个任务模型调用上限(live,默认 12)")
    parser.add_argument("--max-tokens", type=int, default=200000,
                        help="每个任务输入+输出 token 上限(live,默认 200000)")
    args = parser.parse_args(argv)

    live = args.mode == "live"
    tasks = load_tasks(live=live)
    if not tasks:
        print(f"没有任务:{LIVE_TASKS_DIR if live else TASKS_DIR}", file=sys.stderr)
        return 2
    if args.tasks.strip() == "all":
        selected = sorted(tasks)
    else:
        wanted = [name.strip() for name in args.tasks.split(",") if name.strip()]
        unknown = [name for name in wanted if name not in tasks]
        if unknown:
            print(f"未知任务:{', '.join(unknown)}(可选:{', '.join(sorted(tasks))})",
                  file=sys.stderr)
            return 2
        selected = wanted

    overlay, spec = None, None
    if args.spec_file:  # C4a:候选 overlay 校验失败直接退出 2,不跑任务
        overlay = load_spec_file(Path(args.spec_file))
        spec = {"file": str(args.spec_file), "hash": overlay_hash(overlay),
                "keys": sorted(overlay)}

    credentials = None
    model_override = None
    if live:
        if not args.credentials:
            print("--mode live 需要 --credentials PATH", file=sys.stderr)
            return 2
        if args.max_model_calls < 1 or args.max_tokens < 1:
            print("--max-model-calls / --max-tokens 必须为正整数", file=sys.stderr)
            return 2
        cred_path = Path(args.credentials).expanduser()
        if not cred_path.exists():
            print(f"credentials 文件不存在:{cred_path}", file=sys.stderr)
            return 2
        try:
            credentials = CredentialStore(cred_path)
        except Exception as exc:  # noqa: BLE001 - 坏凭据(权限/JSON)一律退出 2
            print(f"credentials 不可用:{exc}", file=sys.stderr)
            return 2
        model_override = ((args.model or "").strip()
                          or str((overlay or {}).get("model") or "").strip() or None)
        if not model_settings(credentials, model=model_override)["api_key"]:
            print("credentials 里没有模型 API Key(model:api-key);"
                  "先在 Settings 页配置", file=sys.stderr)
            return 2

    results = []
    for name in selected:
        task = json.loads(tasks[name].read_text(encoding="utf-8"))
        if live:
            live_model = LiveModel(credentials, model=model_override,
                                   max_calls=args.max_model_calls,
                                   max_tokens=args.max_tokens)
            result = run_task(task, overlay=overlay, spec=spec,
                              credentials=credentials, model=live_model)
        else:
            result = run_task(task, overlay=overlay, spec=spec)
        results.append(result)
    md, jsonl = write_report(results, Path(args.out))
    for r in results:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"[{mark}] {r['task_id']}: status={r['status']} steps={r['steps']} "
              f"tokens={r['tokens_in']}/{r['tokens_out']} "
              f"tool_errors={len(r['tool_errors'])} {r['duration_ms']}ms "
              f"workdir={r['workdir']}")
        for f in r["failures"]:
            print(f"    {f['path']} {f['op']} 期望 {f['expected']!r} 实际 {f['actual']!r}")
    print(f"报告:{md}(jsonl:{jsonl})")
    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
