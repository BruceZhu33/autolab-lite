"""论文雷达(PaperPulse)检索:研究循环查最新论文找方向。

与 LABNOTE 知识库互补——知识库存"我们已经知道的结论"(论文/笔记/往轮实验),
论文雷达存"世界刚发生了什么"(arXiv/期刊每日新论文,模型筛选+中文导读)。
定研究方向、解读失败、找对标方法时先查这里。

论文站部署在 gpu-server,API 仅宿主机回环发布(PAPERPULSE_API,默认 127.0.0.1:3311),
公开 v1 接口无需鉴权。
"""

from __future__ import annotations

import os

import httpx

PAPERPULSE_URL = os.environ.get("PAPERPULSE_API", "http://127.0.0.1:3311").rstrip("/")

_WINDOWS = ("24h", "7d")


def available() -> bool:
    """论文站是否可达(build_tools 时探测,不可达则不注册工具)。"""
    try:
        with httpx.Client(verify=False, timeout=3, trust_env=False) as client:
            return client.get(f"{PAPERPULSE_URL}/api/health").status_code == 200
    except httpx.HTTPError:
        return False


def search(query: str = "", limit: int = 6, window: str = "7d") -> str:
    """按关键词搜最近的论文(空 query = 时间线最新);返回给人/模型读的文本块。"""
    if window not in _WINDOWS:
        window = "7d"
    params: dict[str, str | int] = {
        "mode": "all", "by": "timeline",
        "limit": max(1, min(int(limit), 20)), "window": window,
    }
    if query.strip():
        params["q"] = query.strip()
    with httpx.Client(verify=False, timeout=20, trust_env=False) as client:
        response = client.get(f"{PAPERPULSE_URL}/api/v1/items", params=params)
        response.raise_for_status()
        data = response.json()
    items = data.get("items") or []
    if not items:
        return "论文雷达:窗口内没有匹配的论文。"
    lines = []
    for it in items:
        source = it.get("source")
        source_name = source.get("name") if isinstance(source, dict) else (source or "")
        title = it.get("title") or "(无标题)"
        score = it.get("score")
        mark = "★" if it.get("selected") else f"{score:g}分" if score is not None else ""
        tags = ",".join((it.get("tags") or [])[:4])
        date = (it.get("publishedAt") or "")[:10] or "近7天"
        summary = (it.get("summary") or "").replace("\n", " ")[:160]
        head = f"- {title}({source_name},{date}){mark}"
        if tags:
            head += f"[{tags}]"
        lines.append(head + (f"\n  {summary}" if summary else ""))
    return "论文雷达最近的论文:\n" + "\n".join(lines)
