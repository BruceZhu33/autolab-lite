"""模型调用:Anthropic 兼容的 tool_use 接口(默认 GLM 端点)。

key 存在凭据文件的 model:api-key 条目;旧版在 Chat 设置里存的
chatagent:api-key 仍可读(迁移期兼容)。
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from ..core.credentials import CredentialStore
from ..core.errors import UserError
from ..infra import net

net.force_ipv4_for_httpx()  # 本机 IPv6 到 GLM 端点间歇半通

MODEL_KEY_REF = "model:api-key"
_LEGACY_KEY_REF = "chatagent:api-key"
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/anthropic"
DEFAULT_MODEL = "glm-5.3"

ModelFn = Callable[..., dict]


class ModelError(UserError):
    """模型不可用(key 缺失/失效、端点故障)。"""


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
        if entry.get("api_key"):
            return {"api_key": entry["api_key"],
                    "base_url": entry.get("base_url") or DEFAULT_BASE_URL,
                    "model": override or entry.get("model") or DEFAULT_MODEL}
    return {"api_key": "", "base_url": DEFAULT_BASE_URL,
            "model": override or DEFAULT_MODEL}


def save_model_settings(credentials: CredentialStore, *, api_key: str,
                        base_url: str = "", model: str = "") -> dict:
    if not api_key.strip():
        raise UserError("API Key 不能为空")
    credentials.put_entry(MODEL_KEY_REF, {"api_key": api_key.strip(),
                                          "base_url": base_url.strip() or DEFAULT_BASE_URL,
                                          "model": model.strip() or DEFAULT_MODEL})
    return public_settings(credentials)


def public_settings(credentials: CredentialStore | None) -> dict:
    settings = model_settings(credentials)
    return {"configured": bool(settings["api_key"]), "baseUrl": settings["base_url"],
            "model": settings["model"]}


def call_model(credentials: CredentialStore | None, *, system: str, messages: list[dict],
               tools: list[dict], max_tokens: int, model: str | None = None) -> dict:
    """一次 tool_use 调用;空回复 / 网络抖动 / 5xx / 429 退避重试 4 次。

    model 覆盖 payload 里的模型(api_key / base_url 不变)。"""
    settings = model_settings(credentials, model=model)
    if not settings["api_key"]:
        raise ModelError("还没有配置模型 API Key:在 Settings 页填写")
    payload = {"model": settings["model"], "max_tokens": max_tokens, "system": system,
               "messages": messages, "tools": tools,
               # GLM 是思考模型:不禁用时 thinking 块会吃光 max_tokens
               "thinking": {"type": "disabled"}}
    last: str = ""
    for attempt in range(4):
        try:
            response = httpx.post(
                f"{settings['base_url'].rstrip('/')}/v1/messages",
                headers={"x-api-key": settings["api_key"], "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=payload, timeout=300)
        except httpx.HTTPError as exc:
            last = str(exc)
        else:
            if response.status_code in (401, 403):
                raise ModelError("模型 API Key 无效或过期:在 Settings 页更新")
            if response.status_code == 200 and response.json().get("content"):
                return response.json()
            if response.status_code not in (200, 429) and response.status_code < 500:
                raise ModelError(f"模型返回 {response.status_code}:{response.text[:200]}")
            last = f"HTTP {response.status_code}" if response.status_code != 200 else "空回复"
        time.sleep(min(2.0 * (attempt + 1), 8.0))
    raise ModelError(f"模型请求失败(重试 4 次):{last}")
