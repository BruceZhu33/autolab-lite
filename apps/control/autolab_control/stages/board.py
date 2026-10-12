"""看板:每个项目一张卡,三格阶段(立项 → Baseline → 研究),全部由事实派生。

事实来源只有三处:项目工作区、基线快照、各阶段最近一次 agent 会话。
"""

from __future__ import annotations

from ..agent import runtime
from ..core.repo import Repo
from . import baseline, research

STAGES = ("setup", "baseline", "research")

_RUN_STATE = {
    "running": ("active", "Agent 工作中"),
    "waiting_human": ("attention", "等你回复"),
    "waiting_jobs": ("active", "GPU 实验运行中"),
}


def _cell(state: str, summary: str, link: str) -> dict:
    return {"state": state, "summary": summary, "link": link}


def project_card(repo: Repo, project: dict) -> dict:
    pid = project["id"]
    has_workdir = bool((project.get("remote_workdir") or "").strip())
    setup = _cell("done" if has_workdir else "attention",
                  "工作区已就位" if has_workdir else "待补工作区", "/projects")

    pool = baseline.baseline_pool(project)
    base_run = runtime.latest_run(repo, pid, baseline.KIND)
    link = f"/baseline?project={pid}"
    if base_run and base_run["status"] in _RUN_STATE:
        base = _cell(*_RUN_STATE[base_run["status"]], link)
    elif pool:
        base = _cell("done", f"{len(pool)} 个数据集已确认", link)
    elif base_run and base_run["status"] in ("failed", "stopped"):
        base = _cell("attention", base_run.get("stopReason") or "会话已停止", link)
    else:
        base = _cell("pending", "未开始", link)

    link = f"/research?project={pid}"
    res_run = runtime.latest_run(repo, pid, research.KIND)
    rounds = len(research.list_rounds(repo, pid, res_run["id"])) if res_run else 0
    if not pool:
        res = _cell("pending", "等基线确认", link)
    elif res_run is None:
        res = _cell("pending", "可以开始", link)
    elif res_run["status"] in _RUN_STATE:
        state, text = _RUN_STATE[res_run["status"]]
        res = _cell(state, f"第 {rounds + 1} 轮 · {text}", link)
    elif res_run["status"] == "done":
        result = res_run.get("result") or {}
        best = result.get("bestGain")
        res = (_cell("done", f"{rounds} 轮,超越基线 {best:+.4g}", link)
               if result.get("success") and isinstance(best, (int, float))
               else _cell("attention", f"{rounds} 轮未超越基线", link))
    else:
        res = _cell("pending", f"已停止({rounds} 轮)", link)

    cells = {"setup": setup, "baseline": base, "research": res}
    current = "research" if pool else ("baseline" if has_workdir else "setup")
    runs = [r for r in (base_run, res_run) if r]
    return {
        "projectId": pid, "title": project["name"], "stage": current, "stages": cells,
        "needsYou": any(c["state"] == "attention" for c in cells.values()),
        "updatedAt": max([project["updated_at"], *(r["updatedAt"] for r in runs)]),
    }
