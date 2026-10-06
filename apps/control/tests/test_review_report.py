"""评审报告(MD)生成:方案+基线+评分 → 模板组装 → 写入工作区 .autolab/reviews/。"""

from __future__ import annotations

import json
from pathlib import Path

from .test_http_api import OWNER, AGENT, api, repo  # noqa: F401
from .test_review import _pass_baseline


def test_proposal_report_flow(api, tmp_path):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    pid = _pass_baseline(api, tmp_path)
    proposal = client.post(f"/api/owner/projects/{pid}/proposals", json={
        "title": "换 Swin backbone",
        "content": "预期 +1~2 点",
        "refs": ["https://github.com/x/y"],
    }, headers=owner).json()["proposal"]

    # 无评分也能出报告(标注"尚未评分")
    report = client.post(f"/api/owner/proposals/{proposal['id']}/report",
                         headers=owner)
    assert report.status_code == 200
    body = report.json()
    md = body["reportMd"]
    assert "方案评审报告:换 Swin backbone" in md
    assert "Baseline 指标" in md and "val_dice" in md and "0.8" in md
    assert "尚未评分" in md
    # 报告落在工作区 .autolab/reviews/
    path = Path(body["reportPath"])
    assert path.is_file() and path.parent.name == "reviews"
    assert path.read_text(encoding="utf-8") == md

    # 补评分后重新生成 → 结论段有 verdict 汇总
    created = client.post("/api/owner/agents", json={
        "name": "scorer", "system_prompt": "s",
    }, headers=owner).json()["agent"]
    api["repo"].upsert_proposal_review(proposal_id=proposal["id"], agent_id=created["id"],
                                       agent_name=created["name"], verdict="feasible",
                                       reason="工程量可控")
    again = client.post(f"/api/owner/proposals/{proposal['id']}/report", headers=owner)
    md2 = again.json()["reportMd"]
    assert "✅ 可行" in md2 and "scorer" in md2 and "1 位评分 Agent" in md2

    # 状态查询带上报告
    state = client.get(f"/api/projects/{pid}/review",
                       headers={"Authorization": f"Bearer {AGENT}"}).json()
    stored = next(p for p in state["proposals"] if p["id"] == proposal["id"])
    assert stored["reportMd"] == md2 and stored["reportPath"].endswith(".md")
