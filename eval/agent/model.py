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

# 整串占位(值可以是 list,整串替换保留原类型,如 job_ids: "{{jobs}}")
_FULL_PLACEHOLDER = re.compile(r"^\{\{(?P<name>[A-Za-z0-9_]+)\}\}$")


class CassetteError(Exception):
    """cassette 耗尽或条目非法:harness 以此把任务判 FAIL,绝不重试。"""


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
