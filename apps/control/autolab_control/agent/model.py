"""模型调用:Anthropic 兼容的 tool_use 接口(默认 GLM 端点)。

key 存在凭据文件的 model:api-key 条目;旧版在 Chat 设置里存的
chatagent:api-key 仍可读(迁移期兼容)。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from ..core.credentials import CredentialStore
from ..core.errors import UserError

_DEFAULT_HTTPX_POST = httpx.post

MODEL_KEY_REF = "model:api-key"
_LEGACY_KEY_REF = "chatagent:api-key"
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/anthropic"
DEFAULT_MODEL = "glm-5.3"

ModelFn = Callable[..., dict]


class ModelError(UserError):
    """模型不可用(网络抖动、限流、5xx 端点故障)。"""

    retryable: bool = True


class FatalModelError(ModelError):
    """不可重试的模型错误(API Key 缺失/失效、400/401/403/404 请求或配置错误)。"""

    retryable: bool = False


def _existing_entry_fields(credentials: CredentialStore | None) -> tuple[str, str]:
    existing_base_url = ""
    existing_model = ""
    if credentials is None:
        return existing_base_url, existing_model
    for ref in (MODEL_KEY_REF, _LEGACY_KEY_REF):
        try:
            entry = credentials.get_entry(ref)
        except Exception:  # noqa: BLE001 - 条目不存在
            continue
        if isinstance(entry, dict):
            if not existing_base_url and str(entry.get("base_url") or "").strip():
                existing_base_url = str(entry["base_url"]).strip()
            if not existing_model and str(entry.get("model") or "").strip():
                existing_model = str(entry["model"]).strip()
    return existing_base_url, existing_model


def model_settings(credentials: CredentialStore | None, *,
                   model: str | None = None) -> dict:
    """{api_key, base_url, model};找不到 key 时 api_key 为空。

    model 给了就覆盖凭据里的模型(AgentSpec overlay / --model);key 与
    base_url 仍来自凭据,不因覆盖改变。"""
    override = model.strip() if isinstance(model, str) and model.strip() else None
    for ref in (MODEL_KEY_REF, _LEGACY_KEY_REF):
        try:
            entry = credentials.get_entry(ref) if credentials else {}
        except Exception:  # noqa: BLE001 - 条目不存在
            continue
        if isinstance(entry, dict) and entry.get("api_key"):
            return {"api_key": entry["api_key"],
                    "base_url": str(entry.get("base_url") or "").strip() or DEFAULT_BASE_URL,
                    "model": override or str(entry.get("model") or "").strip() or DEFAULT_MODEL}
    existing_base_url, existing_model = _existing_entry_fields(credentials)
    return {"api_key": "",
            "base_url": existing_base_url or DEFAULT_BASE_URL,
            "model": override or existing_model or DEFAULT_MODEL}


def save_model_settings(credentials: CredentialStore, *, api_key: str,
                        base_url: str = "", model: str = "") -> dict:
    if not api_key.strip():
        raise UserError("API Key 不能为空")
    existing_base_url, existing_model = _existing_entry_fields(credentials)
    resolved_base_url = (base_url or "").strip() or existing_base_url or DEFAULT_BASE_URL
    resolved_model = (model or "").strip() or existing_model or DEFAULT_MODEL
    credentials.put_entry(MODEL_KEY_REF, {"api_key": api_key.strip(),
                                          "base_url": resolved_base_url,
                                          "model": resolved_model})
    return public_settings(credentials)


def public_settings(credentials: CredentialStore | None) -> dict:
    settings = model_settings(credentials)
    return {"configured": bool(settings["api_key"]), "baseUrl": settings["base_url"],
            "model": settings["model"]}


def _should_disable_thinking(base_url: str, model_name: str) -> bool:
    return ("bigmodel.cn" in (base_url or "").lower()
            or (model_name or "").strip().lower().startswith("glm"))


def call_model(credentials: CredentialStore | None, *, system: str, messages: list[dict],
               tools: list[dict], max_tokens: int, model: str | None = None) -> dict:
    """一次 tool_use 调用;空回复 / 网络抖动 / 5xx / 408 / 429 退避重试 4 次。

    model 覆盖 payload 里的模型(api_key / base_url 不变);
    缺失 API Key 或 HTTP 400/401/403/404 抛 FatalModelError 立即中止。"""
    settings = model_settings(credentials, model=model)
    if not settings["api_key"]:
        raise FatalModelError("还没有配置模型 API Key:在 Settings 页填写")
    payload: dict[str, Any] = {
        "model": settings["model"],
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "tools": tools,
    }
    if _should_disable_thinking(settings["base_url"], settings["model"]):
        # GLM 是思考模型:不禁用时 thinking 块会吃光 max_tokens
        payload["thinking"] = {"type": "disabled"}

    url = f"{settings['base_url'].rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": settings["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    use_patched_post = httpx.post is not _DEFAULT_HTTPX_POST
    client: httpx.Client | None = None
    if not use_patched_post:
        transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=2)
        client = httpx.Client(transport=transport, timeout=300)

    last: str = ""
    try:
        for attempt in range(4):
            try:
                if use_patched_post:
                    response = httpx.post(url, headers=headers, json=payload, timeout=300)
                else:
                    assert client is not None
                    response = client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last = str(exc)
            else:
                code = response.status_code
                if code in (401, 403):
                    raise FatalModelError("模型 API Key 无效或过期:在 Settings 页更新")
                if code == 200:
                    try:
                        body = response.json()
                    except ValueError:
                        body = {}
                    if isinstance(body, dict) and body.get("content"):
                        return body
                    last = "空回复"
                elif code in (400, 404) or (400 <= code < 500 and code not in (408, 429)):
                    raise FatalModelError(f"模型返回 {code}:{response.text[:200]}")
                else:
                    last = f"HTTP {code}"
            if attempt < 3:
                time.sleep(min(2.0 * (attempt + 1), 8.0))
    finally:
        if client is not None:
            client.close()
    raise ModelError(f"模型请求失败(重试 4 次):{last}")
