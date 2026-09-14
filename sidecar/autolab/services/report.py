"""M4 产出物：实验报告（JSON+Markdown）+ 科研图表包（D28，matplotlib）+ 可复现代码包（A06）。

REPORTING 流：FINISHING +report_requested → REPORTING → 生成 → REPORTED（唯一终态）。
任何终态可产出阶段报告（Q05）；未执行的消融/多 seed 标记为未完成、不伪造图表。
"""
from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path

from .. import tx as Tx
from ..db import to_thread
from ..errors import TxError
from ..ids import nid


async def generate_report(state, pid: str) -> dict:
    """生成报告 + 图表 + 复现包；项目 REPORTING → REPORTED。"""
    def _load(conn):
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if proj is None:
            raise TxError("NOT_FOUND", pid)
        if proj["status"] not in ("FINISHING", "REPORTING"):
            raise TxError("GUARD_UNMET", f"项目 {proj['status']}（报告在 FINISHING/REPORTING）")
        proto = json.loads(conn.execute("SELECT frozen_yaml FROM plan_protocols WHERE id=?",
                                        (proj["current_protocol_id"],)).fetchone()["frozen_yaml"])
        metric = proto["goal"]["primary_metric"]["name"]
        # val 指标序列（round, value）
        series = [(r["round_no"], r["role"], r["value"]) for r in conn.execute(
            "SELECT r.round_no, r.role, er.value FROM evaluation_results er "
            "JOIN evaluation_requests rq ON rq.id=er.evaluation_request_id "
            "JOIN runs r ON r.id=rq.run_id WHERE r.project_id=? AND rq.scope='val' "
            "AND rq.status='COMPLETED' AND er.metric=? ORDER BY r.round_no",
            (pid, metric))]
        # 预算与消耗
        acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?",
                           (pid,)).fetchone()
        # test 释放（若已 final-read）
        rel = conn.execute(
            "SELECT release_payload_json FROM test_result_releases WHERE batch_id IN "
            "(SELECT id FROM batches WHERE candidate_id IN "
            " (SELECT id FROM candidates WHERE proto_id=?)) ORDER BY released_at DESC LIMIT 1",
            (proj["current_protocol_id"],)).fetchone()
        # 批次状态
        batches = [dict(r) for r in conn.execute(
            "SELECT b.id, b.status, COUNT(bi.id) n_items, "
            "SUM(bi.status='SUCCEEDED') n_ok FROM batches b "
            "LEFT JOIN batch_items bi ON bi.batch_id=b.id "
            "WHERE b.plan_protocol_id=? GROUP BY b.id", (proj["current_protocol_id"],))]
        # 快照链
        snaps = conn.execute("SELECT COUNT(*) c FROM code_snapshots WHERE project_id=?",
                             (pid,)).fetchone()["c"]
        # runs 统计
        runs_stat = dict(conn.execute(
            "SELECT COUNT(*) n, SUM(status='JUDGED') judged FROM runs WHERE project_id=?",
            (pid,)).fetchone())
        return dict(proj), metric, series, dict(acc) if acc else {}, \
            (json.loads(rel["release_payload_json"]) if rel else None), batches, snaps, runs_stat
    proj, metric, series, acc, release, batches, snaps, runs_stat = \
        await to_thread(state.db, _load)

    out_dir = state.cfg.projects_dir / pid / "report"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "charts").mkdir(parents=True)
    (out_dir / "data").mkdir(parents=True)

    # ---- 图表包（D28：曲线/基线对比/批次汇总；缺失项显式留空标注）----
    charts = _make_charts(out_dir, metric, series, release, batches)
    # ---- 数据源文件（图表可自行重绘）----
    (out_dir / "data" / "val_series.json").write_text(
        json.dumps([{"round": r, "role": ro, "value": v} for r, ro, v in series],
                   ensure_ascii=False, indent=2), encoding="utf-8")
    if release:
        (out_dir / "data" / "test_release.json").write_text(
            json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8")
    # ---- 报告主体（Markdown，Q05 结构）----
    md = _markdown_report(proj, metric, series, acc, release, batches, snaps, runs_stat, charts)
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    # ---- 可复现代码包（A06：代码 + 环境 + 命令 + seed + 数据版本）----
    repro = await _make_repro_package(state, pid, out_dir, proj)

    def _finish(conn):
        rev = conn.execute("SELECT state_revision FROM projects WHERE id=?",
                           (pid,)).fetchone()[0]
        cur = conn.execute("SELECT status FROM projects WHERE id=?", (pid,)).fetchone()
        if cur["status"] == "FINISHING":
            Tx.s_cas(conn, "projects", "project", pid, "FINISHING", "REPORTING", rev,
                     "report_requested", actor="user")
            rev += 1
        cur2 = conn.execute("SELECT status FROM projects WHERE id=?", (pid,)).fetchone()
        if cur2["status"] == "REPORTING":
            Tx.s_cas(conn, "projects", "project", pid, "REPORTING", "REPORTED", rev,
                     "report_done", actor="sidecar",
                     reason={"report_dir": str(out_dir)})
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
                (nid("aud"), "sidecar", "report.generated",
                 json.dumps({"dir": str(out_dir), "charts": list(charts)},
                            ensure_ascii=False)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _finish)
    return {"report_dir": str(out_dir), "charts": charts, "repro_package": str(repro),
            "final_outcome": release.get("outcome") if release else proj.get("outcome")}


def _make_charts(out_dir: Path, metric: str, series, release, batches) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    made = []
    rounds = [r for r, _, _ in series if r is not None]
    values = [v for _, _, v in series]
    if series:
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.plot(rounds, values, marker="o", label=metric)
        if values:
            ax.axhline(max(values), ls="--", c="gray", label=f"best={max(values):.4f}")
        base = [v for r, ro, v in series if ro == "baseline"]
        if base:
            ax.axhline(base[0], ls=":", c="red", label=f"baseline={base[0]:.4f}")
        ax.set_xlabel("round"); ax.set_ylabel(metric); ax.legend(); ax.grid(alpha=.3)
        fig.tight_layout(); fig.savefig(out_dir / "charts" / "metric_curve.png", dpi=150)
        fig.savefig(out_dir / "charts" / "metric_curve.svg")
        plt.close(fig); made.append("metric_curve")
    if release:
        fc = release["final_comparison"]
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar(["candidate", "BASE"], [fc["candidate_mean"], fc["base_mean"]],
               yerr=[_std(release["test_metrics"]["candidate"]),
                     _std(release["test_metrics"]["base"])],
               capsize=5, color=["#2563eb", "#94a3b8"])
        ax.set_ylabel(f"test {fc['main_metric']}")
        ax.set_title(f"final confirmation: Δ={fc['delta']:.4f} "
                     f"(min {fc['minimal_improvement']}) → {release['outcome']}")
        fig.tight_layout(); fig.savefig(out_dir / "charts" / "final_comparison.png", dpi=150)
        plt.close(fig); made.append("final_comparison")
    if not made:
        # 无数据：显式留空标注（D28：不伪造图表）
        (out_dir / "charts" / "NO_DATA.txt").write_text(
            "无可信指标序列——项目未产生已评测轮次（不伪造图表，D28）", encoding="utf-8")
    return made


def _std(vals: list) -> float:
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5


def _markdown_report(proj, metric, series, acc, release, batches, snaps, runs_stat,
                     charts) -> str:
    lines = [f"# AutoLab 实验报告 · {proj['name']}",
             "",
             f"- 项目 ID：`{proj['id']}`",
             f"- 终态：{proj.get('outcome') or '（进行中/未终局）'}",
             f"- 预算：C={acc.get('consumed_s', 0)}s / B={acc.get('limit_s', 0)}s"
             f"（消耗 {acc.get('consumed_s', 0) / max(1, acc.get('limit_s', 1)):.0%}）",
             f"- runs：{runs_stat.get('n', 0)}（JUDGED {runs_stat.get('judged') or 0}）"
             f"· 快照链 {snaps} 节点",
             ""]
    lines += [f"## 指标序列（{metric}，可信 val 来源）", "",
              "| round | role | value |", "|---|---|---|"]
    for r, ro, v in series:
        lines.append(f"| {r} | {ro} | {v:.4f} |")
    if not series:
        lines.append("_（无已评测轮次——如实留空）_")
    lines += ["", "## 最终确认（test 一次性释放）", ""]
    if release:
        fc = release["final_comparison"]
        lines += [f"- 结论：**{release['outcome']}**",
                  f"- candidate {fc['candidate_mean']:.4f}（n={fc['sample_counts']['candidate']}）"
                  f" vs BASE {fc['base_mean']:.4f}（n={fc['sample_counts']['base']}）",
                  f"- Δ={fc['delta']:.4f}，最小关注改进 {fc['minimal_improvement']}",
                  f"- spec_hash `{fc['spec_hash'][:16]}…`"]
    else:
        lines.append("_（未执行最终确认——测试结果未释放）_")
    lines += ["", "## 批次", ""]
    if batches:
        lines += ["| batch | 状态 | 条目 | 成功 |", "|---|---|---|---|"]
        for b in batches:
            lines.append(f"| {b['id'][:14]}… | {b['status']} | {b['n_items']} | {b['n_ok'] or 0} |")
    else:
        lines.append("_（未执行消融/多 seed 批次——标记为未完成）_")
    lines += ["", f"## 图表（{len(charts)} 张）", ""]
    for c in charts:
        lines.append(f"- `charts/{c}.png`（附 .svg 与 data/*.json 数据源，可自行重绘）")
    lines += ["", "## 复现", "",
              "- `repro_package.tar.gz`：代码快照 + 环境说明 + 命令 + seed + 数据版本清单（A06）"]
    return "\n".join(lines)


async def _make_repro_package(state, pid: str, out_dir: Path, proj) -> Path:
    repro_dir = out_dir / "repro"
    repro_dir.mkdir(exist_ok=True)
    # 代码：work 仓库当前态（git archive 若有 .git，否则目录拷贝）
    work = state.cfg.projects_dir / pid / "work"
    if (work / ".git").exists():
        import subprocess
        head = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        ar = subprocess.run(["git", "-C", str(work), "archive", head],
                            capture_output=True)
        rp = repro_dir / "code"
        rp.mkdir()
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".tar") as tf:
            tf.write(ar.stdout); tf.flush()
            with tarfile.open(tf.name) as t:
                t.extractall(rp)
    # 环境/命令/数据版本清单
    def _meta(conn):
        proto = json.loads(conn.execute("SELECT frozen_yaml FROM plan_protocols WHERE id=?",
                                        (proj["current_protocol_id"],)).fetchone()["frozen_yaml"])
        dv = conn.execute("SELECT id, manifest_sha FROM data_versions WHERE project_id=? "
                          "ORDER BY created_at LIMIT 1", (pid,)).fetchone()
        env = conn.execute("SELECT env_name, spec_json FROM environment_versions "
                           "WHERE server_id IN (SELECT DISTINCT server_id FROM project_gpus pg "
                           "JOIN gpus g ON g.id=pg.gpu_id WHERE pg.project_id=?) "
                           "ORDER BY created_at DESC LIMIT 1", (pid,)).fetchone()
        return proto, (dict(dv) if dv else {}), (dict(env) if env else {})
    from ..db import to_thread
    proto, dv, env = await to_thread(state.db, _meta)
    meta = {
        "commit": proj.get("name"),
        "plan": {"commands": proto.get("commands", {}),
                 "seeds": proto.get("randomness", {}).get("registered_seed_sets", [])},
        "data_version": dv, "environment": env,
        "reproduce": "解包 code/ → 建环境（environment.spec_json）→ 按 plan.commands 渲染命令",
    }
    (repro_dir / "REPRODUCE.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tar_path = out_dir / "repro_package.tar.gz"
    with tarfile.open(tar_path, "w:gz") as t:
        t.add(repro_dir, arcname="repro")
    shutil.rmtree(repro_dir)
    return tar_path
