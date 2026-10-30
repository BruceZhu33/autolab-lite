"""AgentSpec:把「被试」变成可快照的数据(A1)。

一个会话的 prompt / 工具 / 模型 / 预算 / 验收器在第一次成功 step 前固化成
一份不可变的 spec,连同 spec_hash 存进 agent_runs。两次 run 的差异从此可以
归因到配置差异,而不是靠读代码反推。绝不记录 api_key。
"""

from __future__ import annotations

import dataclasses
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
    agent_spec_id: str = ""       # 变体注册表 id(C4a);无 overlay 为空
    agent_spec_hash: str = ""     # overlay_hash(config);无 overlay 为空

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
            "agentSpecId": self.agent_spec_id,
            "agentSpecHash": self.agent_spec_hash,
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

def overlay_hash(config: dict) -> str:
    """overlay config 的内容指纹:sha256(sort_keys JSON)。注册表(C4a)、
    spec 快照与 eval 报告用同一公式,owner 可直接对账。"""
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def apply_profile_overlay(profile: LoopProfile, overlay: dict) -> LoopProfile:
    """把 AgentSpec overlay 应用到 profile,返回新实例(不改原件)。

    prompt_append 追加到 system prompt 末尾;tool_exclude 包装 build_tools
    按 name 过滤(保留顺序);model → model_override;max_* /
    wall_clock_seconds / keep_recent 直接覆盖。
    verifier 不动:验收口径不属于变体自由度。契约校验在 stages 层(owner API),
    这里只做应用,容忍任何键。
    """
    updates: dict = {}
    append = str(overlay.get("prompt_append") or "").strip()
    if append:
        updates["system_prompt"] = profile.system_prompt + "\n\n## 变体指令\n" + append
    model = str(overlay.get("model") or "").strip()
    if model:
        updates["model_override"] = model
    excluded = [str(t) for t in overlay.get("tool_exclude") or []]
    if excluded:
        drop = frozenset(excluded)
        inner = profile.build_tools

        def build_tools(env):
            return [t for t in inner(env) if t.name not in drop]

        updates["build_tools"] = build_tools
    for key in ("max_steps", "max_tokens", "max_output_tokens",
                "wall_clock_seconds", "keep_recent"):
        if key in overlay:
            updates[key] = overlay[key]
    return dataclasses.replace(profile, **updates)


def build_agent_spec(profile: LoopProfile, credentials: Any,
                     tools: list[Tool], *, overlay: dict | None = None) -> AgentSpec:
    """从 profile + 当步实际构建的工具 + 模型设置生成 spec(确定性)。

    overlay = {"id","label","config"}(stages 注册表的运行时形态)或 None:
    只记录 id 与 config 的内容 hash,不记录完整 prompt_append 文本。
    """
    settings = model_settings(
        credentials, model=profile.model_override or None)  # 只取 model / base_url;key 绝不落库
    prompt_hash = hashlib.sha256(
        profile.system_prompt.encode("utf-8")).hexdigest()
    overlay_cfg = (overlay or {}).get("config") or {}
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
        context_policy={"compact": True, "reset": True,
                        "keep_recent": profile.keep_recent},
        verifier=dict(profile.verifier),
        code_rev=_code_rev(),
        agent_spec_id=str((overlay or {}).get("id") or ""),
        agent_spec_hash=overlay_hash(overlay_cfg) if overlay_cfg else "",
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
