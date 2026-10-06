"""Minimal LLM client for the 方案评审 module.

Talks to the Anthropic-compatible GLM endpoint (open.bigmodel.cn/api/anthropic)
— the same provider/key slot the Chat module uses (`chatagent:api-key`) — so
the user configures exactly one GLM key for all AI features. Falls back to an
agent's own api_key_ref when that agent carries one.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import net_preferences
from .credentials import CredentialStore

net_preferences.force_ipv4_for_httpx()

GLM_ANTHROPIC_BASE = "https://open.bigmodel.cn/api/anthropic"
CHAT_KEY_REF = "chatagent:api-key"
DEFAULT_MODEL = "glm-5.3"


class LlmError(RuntimeError):
    """Key 未配置 / 上游失败,消息直接面向用户。"""


def resolve_api_key(credentials: CredentialStore, *, agent_key_ref: str | None = None) -> str:
    """优先 agent 自己的 key,回落共享 chat key。都没有 → LlmError(带指引)。"""
    if agent_key_ref:
        try:
            key = credentials.get_entry(agent_key_ref).get("api_key", "")
            if key:
                return key
        except Exception:
            pass
    try:
        key = credentials.get_entry(CHAT_KEY_REF).get("api_key", "")
        if key:
            return key
    except Exception:
        pass
    raise LlmError(
        "还没有配置 GLM API Key:在 Chat 设置或创建 Agent 时填入,"
        "或把 key 写入凭据文件 chatagent:api-key 后重试"
    )


def stream_multi(
    credentials: CredentialStore,
    *,
    system: str,
    messages: list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2000,
    agent_key_ref: str | None = None,
    timeout: float = 180.0,
):
    """流式对话:逐段 yield 正文文本增量(Anthropic SSE 的 text delta)。

    与 chat_multi 同参同语义(thinking 已禁用);异常在迭代时抛 LlmError。
    上游断流无增量时正常结束——由调用方按累计长度判断空回复。"""
    key = resolve_api_key(credentials, agent_key_ref=agent_key_ref)
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "thinking": {"type": "disabled"},
        "stream": True,
    }
    try:
        with httpx.stream(
            "POST", f"{GLM_ANTHROPIC_BASE}/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=payload, timeout=timeout,
        ) as response:
            if response.status_code in (401, 403):
                raise LlmError("GLM Key 无效或过期:请更新 Key 后重试")
            if response.status_code != 200:
                raise LlmError(
                    f"GLM 返回 {response.status_code}:{response.text[:200]}")
            event_type = ""
            for line in response.iter_lines():
                line = line.strip()
                if not line:
                    event_type = ""
                    continue
                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                import json as _json

                try:
                    data = _json.loads(line.split(":", 1)[1].strip())
                except ValueError:
                    continue
                if data.get("type") == "content_block_delta":
                    delta = data.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield delta["text"]
                elif data.get("type") == "error":
                    err = data.get("error") or {}
                    raise LlmError(f"GLM 流式错误:{err.get('message') or data}")
    except httpx.HTTPError as exc:
        raise LlmError(f"GLM 请求失败:{exc}") from exc


def chat(
    credentials: CredentialStore,
    *,
    system: str,
    user: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2000,
    agent_key_ref: str | None = None,
    timeout: float = 120.0,
) -> str:
    return chat_multi(
        credentials, system=system,
        messages=[{"role": "user", "content": user}],
        model=model, max_tokens=max_tokens,
        agent_key_ref=agent_key_ref, timeout=timeout,
    )


def chat_multi(
    credentials: CredentialStore,
    *,
    system: str,
    messages: list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2000,
    agent_key_ref: str | None = None,
    timeout: float = 120.0,
) -> str:
    key = resolve_api_key(credentials, agent_key_ref=agent_key_ref)
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        # glm-5.3 是思考模型:不禁用时 thinking 块会吃光 max_tokens,
        # 正文一个字不出(表现为"端点空回复")。产品各环节只需要正文。
        "thinking": {"type": "disabled"},
    }
    # GLM 端点高发空回复(2xx 但无内容块)与瞬时网络错误(IPv6 半通/
    # TLS 抖动):同一指数退避循环内重试,最多 4 次;调用方拿到的要么
    # 非空要么异常,不再需要各自防御空串
    import time as _time

    last_empty = False
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            response = httpx.post(
                f"{GLM_ANTHROPIC_BASE}/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=payload, timeout=timeout,
            )
        except httpx.HTTPError as exc:
            last_exc = exc
            last_empty = False
            _time.sleep(min(1.5 * (attempt + 1), 5.0))
            continue
        if response.status_code in (401, 403):
            raise LlmError("GLM Key 无效或过期:请更新 Key 后重试")
        if response.status_code != 200:
            raise LlmError(f"GLM 返回 {response.status_code}:{response.text[:200]}")
        data = response.json()
        blocks = data.get("content") or []
        texts = [b.get("text", "") for b in blocks
                 if isinstance(b, dict) and b.get("type") == "text"]
        reply = "\n".join(t for t in texts if t).strip()
        if reply:
            return reply
        last_empty = True
        last_exc = None
        _time.sleep(min(1.5 * (attempt + 1), 5.0))
    if last_exc is not None:
        raise LlmError(f"GLM 请求失败(重试 4 次):{last_exc}") from last_exc
    raise LlmError(
        "GLM 连续 4 次返回空回复(端点波动):稍后重试;empty=" + str(last_empty))
