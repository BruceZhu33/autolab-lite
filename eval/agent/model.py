"""CassetteModel:离线回放预设的模型回复(agent eval harness 专用,B2.2a)。

cassette 每条形如 {"content": [块, ...]}(与 tool_use 回复同构)。每次被
runtime.step 调用时弹出下一条,先把块里的 {{占位符}} 替换成当前上下文
(run_id / project_id / 最近一次 run_experiment 产生的 Job id),再返回;
cassette 本体不被修改。不访问网络、不依赖 GPU。
"""

from __future__ import annotations

import copy
import re
from typing import Any

from autolab_control.agent.model import call_model

# 整串占位(值可以是 list,整串替换保留原类型,如 job_ids: "{{jobs}}")
_FULL_PLACEHOLDER = re.compile(r"^\{\{(?P<name>[A-Za-z0-9_]+)\}\}$")


class CassetteError(Exception):
    """cassette 耗尽或条目非法:harness 以此把任务判 FAIL,绝不重试。"""


class LiveModelError(Exception):
    """live 模型调用/成本超过 harness 硬上限;任务判 FAIL,不再继续。"""


def substitute(value: Any, context: dict) -> Any:
    """递归替换字符串里的 {{name}}:整串占位保留原类型,其余按 str 拼接。

    context 里没有的占位符原样保留(任务本身写错时会以工具错误暴露)。
    """
    if isinstance(value, str):
        whole = _FULL_PLACEHOLDER.fullmatch(value.strip())
        if whole and whole.group("name") in context:
            return context[whole.group("name")]
        for name, replacement in context.items():
            token = "{{" + name + "}}"
            if token in value:
                value = value.replace(token, str(replacement))
        return value
    if isinstance(value, list):
        return [substitute(item, context) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, context) for key, item in value.items()}
    return value


class CassetteModel:
    """ModelFn 兼容(runtime.step 的 model= 参数):按序回放,usage 固定可断言。"""

    def __init__(self, cassette: list[dict], context: dict, *,
                 tokens_in: int = 100, tokens_out: int = 40):
        self._replies = list(cassette)
        self._context = context  # 与 runner 共享:每次回复取最新的 Job 上下文
        self._usage = {"input_tokens": tokens_in, "output_tokens": tokens_out}
        self.calls = 0

    @property
    def remaining(self) -> int:
        return len(self._replies)

    @property
    def exhausted(self) -> bool:
        return not self._replies

    def __call__(self, *, system: str, messages: list[dict],
                 tools: list[dict], max_tokens: int) -> dict:
        if not self._replies:
            raise CassetteError("cassette 已耗尽,没有更多预设回复")
        entry = self._replies.pop(0)
        content = entry.get("content") if isinstance(entry, dict) else None
        if not isinstance(content, list) or not content:
            raise CassetteError("cassette 条目缺少 content 块列表")
        self.calls += 1
        return {"content": substitute(copy.deepcopy(content), self._context),
                "usage": dict(self._usage)}


class LiveModel:
    """真实模型:包装 control 的 call_model,累计调用数与 token,超限即抛。

    runtime.step 以关键字参数 system / messages / tools / max_tokens 调用;
    model 非空时覆盖凭据里的模型(与 AgentSpec overlay 的 model 一致)。
    调用次数在发起前计数(含失败重试),token 按响应 usage 累计。
    """

    def __init__(self, credentials, *, model: str | None = None,
                 max_calls: int = 12, max_tokens: int = 200000):
        self.credentials = credentials
        self.model = (model or "").strip()
        self.max_calls = max(1, int(max_calls))
        self.max_tokens = max(1, int(max_tokens))
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.limit_error = ""

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def exhausted(self) -> bool:
        return self.calls >= self.max_calls or self.tokens_total >= self.max_tokens

    def __call__(self, *, system: str, messages: list[dict],
                 tools: list[dict], max_tokens: int) -> dict:
        if self.calls >= self.max_calls:
            self.limit_error = (f"模型调用上限 {self.max_calls} 次已用完"
                                f"(calls={self.calls})")
            raise LiveModelError(self.limit_error)
        if self.tokens_total >= self.max_tokens:
            self.limit_error = (f"token 上限 {self.max_tokens} 已用完"
                                f"(tokens={self.tokens_total})")
            raise LiveModelError(self.limit_error)
        self.calls += 1
        response = call_model(self.credentials, system=system, messages=messages,
                              tools=tools, max_tokens=max_tokens,
                              model=self.model or None)
        usage = response.get("usage") or {}
        self.tokens_in += int(usage.get("input_tokens") or 0)
        self.tokens_out += int(usage.get("output_tokens") or 0)
        if self.tokens_total > self.max_tokens:
            self.limit_error = (f"token 上限 {self.max_tokens} 超限"
                                f"(tokens={self.tokens_total})")
            raise LiveModelError(self.limit_error)
        return response
