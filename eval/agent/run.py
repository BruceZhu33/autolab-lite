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
from autolab_control.core.db import Database, utc_now  # noqa: E402
from autolab_control.core.repo import Repo  # noqa: E402
from autolab_control.stages import baseline as baseline_stage  # noqa: E402
from autolab_control.stages import research as research_stage  # noqa: E402

from eval.agent.model import CassetteModel  # noqa: E402

TASKS_DIR = AGENT_DIR / "tasks"
DEFAULT_REPORT = AGENT_DIR / "reports" / "latest.md"
DEFAULT_MAX_STEPS = 30
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


# ------------------------------------------------------------------ Job 落库


def _run_job_ids(repo: Repo, run_id: str) -> list[str]:
    """本 run 通过 run_experiment 提交的 Job(approved_by=agent-run:<id>),按序。"""
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM plan_revisions WHERE approved_by = ? ORDER BY approved_at",
            (f"agent-run:{run_id}",)).fetchall()
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


def _drain_queued_jobs(repo: Repo, run_id: str, metrics_queue: list[dict]) -> None:
    """把本 run 仍 QUEUED 的 Job 依次消费 metrics_queue 置为 SUCCEEDED。"""
    for job_id in _run_job_ids(repo, run_id):
        job = repo.get_job(job_id)
        if job is None or job["status"] != "QUEUED":
            continue
        if not metrics_queue:
            raise JobMetricsExhausted(
                f"job_metrics 队列已耗尽,Job {job_id} 仍是 QUEUED")
        _finish_job(repo, job_id, metrics_queue.pop(0))


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


def run_task(task: dict, *, root: Path | None = None) -> dict:
    """回放一个任务,返回结果对象(expect 断言在它之上求值,见 evaluate_expect)。"""
    started = time.monotonic()
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
        run_id = run["id"]

        context = {"run_id": run_id, "project_id": pid}
        model = CassetteModel(task.get("cassette") or [], context)
        metrics_queue = list(task.get("job_metrics") or [])
        max_steps = int((task.get("budget") or {}).get("max_steps") or DEFAULT_MAX_STEPS)
        for _ in range(max_steps):
            if model.exhausted:
                harness_error = f"cassette 耗尽(状态 {statuses[-1] if statuses else '?'})"
                break
            status = runtime.step(repo, None, run_id, profile, model=model)
            statuses.append(status)
            _refresh_job_context(repo, run_id, context)
            try:
                _drain_queued_jobs(repo, run_id, metrics_queue)
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

    result = {
        "task_id": task["id"],
        "kind": task.get("kind"),
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
    """点路径只穿 dict;取不到返回 _MISSING(list 用 *_contains 断言)。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
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


def load_tasks() -> dict[str, Path]:
    """{任务 id: 任务文件};文件名 stem 必须与 JSON 里的 id 一致。"""
    tasks: dict[str, Path] = {}
    for path in sorted(TASKS_DIR.glob("*.json")):
        task = json.loads(path.read_text(encoding="utf-8"))
        if task.get("id") != path.stem:
            raise ValueError(f"任务文件 {path.name} 的 id 字段({task.get('id')})与文件名不一致")
        tasks[path.stem] = path
    return tasks


def write_report(results: list[dict], out: Path) -> tuple[Path, Path]:
    """写 Markdown 表 + 同名 .jsonl(每任务一行);返回两个路径。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    jsonl = out.with_suffix(".jsonl")
    passed = sum(1 for r in results if r["pass"])
    lines = [
        "# Agent Eval Report(B2.2a replay)",
        "",
        f"- 生成时间:{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 结果:{passed}/{len(results)} PASS",
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
                "task_id", "kind", "pass", "status", "steps", "tokens_in", "tokens_out",
                "span_kinds", "tool_calls", "tool_errors", "ask_humans", "duration_ms",
                "failures", "harness_error", "workdir")}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out, jsonl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval.agent.run",
        description="离线 agent eval harness(B2.2a;唯一模式 replay,不调真实模型)")
    parser.add_argument("--tasks", default="all", help='"all" 或逗号分隔的任务 id')
    parser.add_argument("--out", default=str(DEFAULT_REPORT),
                        help="Markdown 报告路径(同时写同名 .jsonl)")
    parser.add_argument("--mode", default="replay", choices=["replay"],
                        help="本轮只有 replay")
    args = parser.parse_args(argv)

    tasks = load_tasks()
    if not tasks:
        print(f"没有任务:{TASKS_DIR}", file=sys.stderr)
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

    results = [run_task(json.loads(tasks[name].read_text(encoding="utf-8")))
               for name in selected]
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
