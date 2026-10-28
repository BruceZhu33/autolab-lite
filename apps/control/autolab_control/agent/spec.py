"""AgentSpec:把「被试」变成可快照的数据(A1)。

一个会话的 prompt / 工具 / 模型 / 预算 / 验收器在第一次成功 step 前固化成
一份不可变的 spec,连同 spec_hash 存进 agent_runs。两次 run 的差异从此可以
归因到配置差异,而不是靠读代码反推。绝不记录 api_key。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .model import model_settings

if TYPE_CHECKING:  # 仅类型标注,避免 runtime ↔ spec 循环导入
    from .runtime import LoopProfile, Tool


@dataclass(frozen=True)
class AgentSpec:
    spec_id: str                 # 例如 "research:glm-5.3:v1"
    kind: str                    # baseline / research
    prompt_hash: str             # sha256(system_prompt)
    tool_names: tuple[str, ...]  # 本会话实际注册的工具名(排序后)
    model_id: str
    model_base_url: str          # 不记录 api_key
    max_output_tokens: int
    budgets: dict                # steps / tokens / wall_clock_seconds
    context_policy: dict         # compact / reset / keep_recent
    verifier: dict               # 例如 {"id": "baseline-finish", "version": 1}
    code_rev: str                # control 代码版本;取不到时用包版本

    def to_dict(self) -> dict:
        return {
            "specId": self.spec_id,
            "kind": self.kind,
            "promptHash": self.prompt_hash,
            "toolNames": list(self.tool_names),
            "modelId": self.model_id,
            "modelBaseUrl": self.model_base_url,
            "maxOutputTokens": self.max_output_tokens,
            "budgets": self.budgets,
            "contextPolicy": self.context_policy,
            "verifier": self.verifier,
            "codeRev": self.code_rev,
        }

    def spec_hash(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _code_rev() -> str:
    """优先 git short HEAD;失败退包版本;再失败 "unknown"。绝不让会话因此失败。"""
    try:
        import autolab_control

        pkg_dir = autolab_control.__file__.rsplit("/", 1)[0]
        out = subprocess.run(  # noqa: S603 - 固定参数调用 git
            ["git", "-C", pkg_dir, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001 - 版本探测失败不致命
        pass
    try:
        import autolab_control

        return str(autolab_control.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def build_agent_spec(profile: LoopProfile, credentials: Any,
                     tools: list[Tool], *, keep_recent: int = 12) -> AgentSpec:
    """从 profile + 当步实际构建的工具 + 模型设置生成 spec(确定性)。"""
    settings = model_settings(credentials)  # 只取 model / base_url;key 绝不落库
    prompt_hash = hashlib.sha256(
        profile.system_prompt.encode("utf-8")).hexdigest()
    return AgentSpec(
        spec_id=f"{profile.kind}:{settings['model']}:{prompt_hash[:8]}",
        kind=profile.kind,
        prompt_hash=prompt_hash,
        tool_names=tuple(sorted(t.name for t in tools)),
        model_id=settings["model"],
        model_base_url=settings["base_url"],
        max_output_tokens=profile.max_output_tokens,
        budgets={
            "max_steps": profile.max_steps,
            "max_tokens": profile.max_tokens,
            "max_output_tokens": profile.max_output_tokens,
            "wall_clock_seconds": profile.wall_clock_seconds,
        },
        context_policy={"compact": True, "reset": True, "keep_recent": keep_recent},
        verifier=dict(profile.verifier),
        code_rev=_code_rev(),
    )


def spec_from_json(raw: str | None) -> dict | None:
    """agent_runs.spec_json → spec dict;空/坏数据返回 None(API 用)。"""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) and value else None
