"""needsYou 飞书通知:会话等待 owner / 完成 / 停止 / 失败时推一条 webhook 消息。

实验一跑几小时,owner 不该守着 3 秒轮询看板。本模块在 runtime 的状态转移点
被调用,把「该你了」推到飞书群机器人。

环境变量(server drop-in 里配):
- AUTOLAB_FEISHU_WEBHOOK  飞书自定义机器人 webhook 地址;未配置 = 功能整体关闭
- AUTOLAB_WEB_URL         控制台对外地址(如 http://192.0.2.46:3430),
                          用于消息里的直达链接;未配置则不带链接

发送在后台线程,任何失败只打日志,绝不影响会话推进。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

import httpx

# 会话 kind → 展示名(round_review 不是独立 kind,是研究轮评审的暂停载荷)
KIND_LABELS = {"baseline": "Baseline", "research": "研究"}

STATUS_TITLES = {
    "waiting_human": "等你介入",
    "done": "会话完成",
    "stopped": "会话停止",
    "failed": "会话失败",
}

_KIND_PAGE = {"research": "/research"}  # 其余(baseline)默认 /baseline


def webhook_url() -> str:
    return os.getenv("AUTOLAB_FEISHU_WEBHOOK", "").strip()


def message(run: dict, project: dict | None, status: str, detail: str = "") -> str:
    """组装通知文本(纯函数,便于测试)。"""
    kind = str(run.get("kind") or "")
    name = str((project or {}).get("name") or run.get("project_id") or "?")
    title = f"【AutoLab】{KIND_LABELS.get(kind, kind or '会话')} · {name} {STATUS_TITLES.get(status, status)}"
    text = title + (f"\n{detail.strip()}" if detail.strip() else "")
    base = os.getenv("AUTOLAB_WEB_URL", "").strip().rstrip("/")
    if base and run.get("project_id"):
        page = _KIND_PAGE.get(kind, "/baseline")
        text += f"\n打开:{base}{page}?project={run['project_id']}"
    return text[:2000]


def finish_detail(result: Any) -> str:
    """Finish 载荷 → 一行摘要(研究会话:轮数/判定/原因)。"""
    if not isinstance(result, dict):
        return str(result or "")[:300]
    parts: list[str] = []
    if result.get("reason"):
        parts.append(str(result["reason"]))
    if isinstance(result.get("rounds"), int):
        parts.append(f"共 {result['rounds']} 轮")
    if isinstance(result.get("bestGain"), (int, float)):
        parts.append(f"最佳改善 {result['bestGain']:+.4g}")
    return ";".join(parts) or json.dumps(result, ensure_ascii=False)[:300]


def notify_run(repo: Any, run: dict, status: str, detail: str = "") -> None:
    """状态转移通知入口:webhook 未配置直接返回;全链路防御,绝不抛错。"""
    url = webhook_url()
    if not url:
        return
    try:
        project = repo.get_project(run.get("project_id") or "") if repo else None
        text = message(run, project, status, detail)
        threading.Thread(target=_post, args=(url, text), daemon=True,
                         name="autolab-notify").start()
    except Exception as exc:  # noqa: BLE001 - 通知绝不能拖垮会话推进
        print(f"autolab-control: 通知未发出({status}):{exc}", flush=True)


def _post(url: str, text: str) -> None:
    try:
        resp = httpx.post(url, json={"msg_type": "text", "content": {"text": text}},
                          timeout=10)
        if resp.status_code != 200:
            print(f"autolab-control: 飞书通知 HTTP {resp.status_code}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"autolab-control: 飞书通知发送失败:{exc}", flush=True)
