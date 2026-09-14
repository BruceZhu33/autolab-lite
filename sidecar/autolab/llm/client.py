"""OpenAI 兼容薄客户端（TD7）。"""
from __future__ import annotations

import httpx


class LLMError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class LLMSettings:
    def __init__(self, base_url: str, model: str, api_key: str, params: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.params = params or {}


def load_settings(state, use_point: str = "round_analysis") -> LLMSettings | None:
    """从 DB + 凭据存储装配；未配置返回 None。"""
    row = state.db.connect().execute(
        "SELECT * FROM llm_settings WHERE use_point=? AND enabled=1", (use_point,)).fetchone()
    if row is None:
        return None
    import json
    key = ""
    try:
        cred = state.creds.get(row["api_key_ref"])
        key = cred.get("keys", {}).get(use_point, "")
    except KeyError:
        key = ""
    return LLMSettings(row["base_url"], row["model"], key,
                       json.loads(row["params_json"]))


async def chat(settings: LLMSettings, messages: list[dict], timeout: float = 120.0,
               max_tokens: int | None = None) -> str:
    """chat/completions；返回首个 choice 文本。网络/鉴权/限流统一 LLMError。"""
    body = {"model": settings.model, "messages": messages,
            **({"max_tokens": max_tokens} if max_tokens else {}), **settings.params}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                f"{settings.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                json=body)
    except httpx.HTTPError as e:
        raise LLMError("LLM_NETWORK", str(e)[:200]) from e
    if r.status_code in (401, 403):
        raise LLMError("LLM_AUTH", f"HTTP {r.status_code}（检查 API Key）")
    if r.status_code == 429:
        raise LLMError("LLM_RATE_LIMIT", "429 rate limited")
    if r.status_code >= 400:
        raise LLMError("LLM_API", f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        raise LLMError("LLM_RESPONSE", f"响应结构异常: {e}") from e


async def ping(settings: LLMSettings) -> dict:
    try:
        text = await chat(settings, [{"role": "user", "content": "回复：ok"}],
                          timeout=30, max_tokens=8)
        return {"ok": True, "reply": text.strip()[:80], "model": settings.model}
    except LLMError as e:
        return {"ok": False, "error": f"{e.code}: {e.message}"}


def load_engine(state, use_point: str = "round_analysis") -> str:
    """提案引擎选择（migration 0007；缺省 builtin）。"""
    row = state.db.connect().execute(
        "SELECT engine FROM llm_settings WHERE use_point=? AND enabled=1",
        (use_point,)).fetchone()
    return (row["engine"] if row and row["engine"] else "builtin")
