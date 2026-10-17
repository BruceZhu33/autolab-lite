"""LABNOTE 知识库客户端(链路 C:研究循环的"读书"与"留痕")。

检索:论文/笔记/往轮实验结论 → 种子注入(build_task)+ search_knowledge 工具;
回流:每轮 end_round 的报告入库,下一轮与对话可检索"上次为什么失败"。
契约见 labnote docs/S8-AUTOLAB-PATCH.md;认证 PAT(服务账号 autolab-svc,
editor@autolab 专属空间)。原则:知识库不可达**不阻塞**研究循环——
检索/回流失败都降级,只在控制端日志留痕。
"""

from __future__ import annotations

import os

import httpx

LABNOTE_URL = os.getenv("LABNOTE_URL", "")
LABNOTE_PAT = os.getenv("LABNOTE_PAT", "")


def configured() -> bool:
    return bool(LABNOTE_URL and LABNOTE_PAT)


def _client() -> httpx.Client:
    # 内网自签 TLS(Caddy tls internal)→ verify=False;服务器间直连不吃代理
    return httpx.Client(verify=False, timeout=60, trust_env=False,
                        headers={"Authorization": f"Bearer {LABNOTE_PAT}"})


def retrieve(query: str, top_k: int = 5, space_id: str = "") -> str:
    """query → top_k 父块文本(带出处/层级/标题路径);失败抛给调用方降级。"""
    payload = {"query": query, "top_k": top_k}
    if space_id:
        payload["space_id"] = space_id
    with _client() as client:
        response = client.post(f"{LABNOTE_URL}/api/v1/retrieve", json=payload)
    response.raise_for_status()
    docs = response.json().get("docs") or []
    parts = [f"[{i}] {d.get('filename', '')}(L{d.get('chunk_level')}) "
             f"{d.get('heading_path', '')}:\n{d.get('text', '')}"
             for i, d in enumerate(docs, 1)]
    return "\n\n---\n\n".join(parts) or "知识库无相关内容"


def ingest(filename: str, text: str, kind: str = "experiment") -> None:
    """markdown 文本 → 三级树入库(autolab 空间),下一轮可检索。"""
    with _client() as client:
        response = client.post(f"{LABNOTE_URL}/api/v1/ingest",
                               json={"filename": filename, "text": text, "kind": kind})
    response.raise_for_status()


def retrieve_spaces(query: str, space_ids: list[str], per: int = 3) -> str:
    """多空间检索合并(autolab 回流空间由默认值兜底,这里只查额外授权的空间);
    单个空间 403/超时只跳过并留日志——授权是 owner 在 /kb 页给的。"""
    seen: set[str] = set()
    parts: list[str] = []
    for space_id in space_ids:
        if space_id in seen:
            continue
        seen.add(space_id)
        try:
            text = retrieve(query, per, space_id=space_id)
        except Exception as exc:  # noqa: BLE001 - 单空间失败不拖垮整体
            print(f"autolab-control: 知识库空间 {space_id} 检索失败(未授权?):{exc}",
                  flush=True)
            continue
        if text and text != "知识库无相关内容":
            parts.append(f"(空间 {space_id})\n{text}")
    return "\n---\n".join(parts)
