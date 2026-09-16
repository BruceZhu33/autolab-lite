"""GPU 服务页内嵌平台助手（对话式，ZCode 式通用助手口径）。

- POST /api/v1/assistant/chat：携带最近对话 + 服务端实时快照（服务器/GPU/
  在途 attempt/run/项目/近期状态事件）组装 system prompt，经 llm.client
  调 OpenAI 兼容端点。LLM 配置复用 round_analysis 使用点（D11 用户必配项，
  助手与迭代分析共用一份 key，不新加 use_point——llm_settings 的 CHECK
  约束冻结在 0005 四个使用点，扩列走攒批修订）。
- GET /api/v1/assistant/status：轻量上下文摘要 + LLM 配置态（前端徽标用）。
- 只读诊断工具（用户口径 2026-09-15："只看快照不如自己去看"）：模型可请求
  白名单探测（nvidia-smi / df -h），后端用注册凭据经 SSH 执行、结果回传
  给模型出最终回答。命令由后端拼装——模型只能选「工具+服务器名」，
  不存在命令注入面；每轮最多 2 个探测。

快照是回答「现在什么状态」的唯一事实来源：助手端不猜、不编指标；
正式指标以落盘产物为准（deviation #20）。
"""
from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..db import to_thread
from ..llm import client as llm_client
from ..llm.client import LLMError
from ..ssh.transport import make_transport

router = APIRouter(prefix="/api/v1")

USE_POINT = "round_analysis"  # 助手的 LLM 来源（见模块 docstring）

_ACTIVE_ATTEMPT_STATUS = (
    "SUBMITTED", "ACKED", "REMOTE_QUEUED", "RUNNING",
    "STOP_REQUESTED", "STOP_SENT", "ATTEMPT_STOP_UNKNOWN",
)

MAX_HISTORY = 20      # 送入 LLM 的最近对话条数
MAX_SNAPSHOT_EVENTS = 12
MAX_PROBES = 2        # 每轮对话允许的探测次数

# 只读诊断工具白名单：模型只给 (tool, server)，命令串由后端拼——不收模型命令
_PROBE_TOOLS: dict[str, str] = {
    "gpu-telemetry": "nvidia-smi",
    "disk-free": "df -h",
}
_PROBE_RE = re.compile(r"\[\[probe:([a-z0-9-]+)\|([^\]|]+)\]\]")


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=8000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


def _snapshot(conn) -> dict:
    """只读快照：服务器/GPU/项目/run/在途 attempt/近期状态事件。"""
    servers = [dict(r) for r in conn.execute(
        "SELECT id,name,host,port,username,transport_mode,status,agent_port,"
        "agent_schema_ver,last_seen_at FROM servers ORDER BY name").fetchall()]
    gpus = [dict(r) for r in conn.execute(
        "SELECT server_id,gpu_index,model_name,vram_mb,status FROM gpus "
        "ORDER BY server_id,gpu_index").fetchall()]
    by_server: dict[str, list[dict]] = {}
    for g in gpus:
        by_server.setdefault(g.pop("server_id"), []).append(g)

    projects = [dict(r) for r in conn.execute(
        "SELECT name,status,updated_at FROM projects ORDER BY updated_at DESC "
        "LIMIT 6").fetchall()]
    runs = [dict(r) for r in conn.execute(
        "SELECT r.id,r.project_id,p.name AS project_name,r.round_no,r.role,"
        "r.status,r.updated_at FROM runs r JOIN projects p ON p.id=r.project_id "
        "ORDER BY r.updated_at DESC LIMIT 8").fetchall()]
    active_attempts = [dict(r) for r in conn.execute(
        "SELECT a.id,a.run_id,p.name AS project_name,r.role,r.round_no,a.profile,"
        "a.status,s.name AS server_name,g.gpu_index,g.model_name,a.updated_at "
        "FROM attempts a JOIN runs r ON r.id=a.run_id JOIN projects p ON p.id=r.project_id "
        "JOIN servers s ON s.id=a.server_id LEFT JOIN gpus g ON g.id=a.gpu_id "
        f"WHERE a.status IN ({','.join('?' * len(_ACTIVE_ATTEMPT_STATUS))}) "
        "ORDER BY a.updated_at DESC LIMIT 10", _ACTIVE_ATTEMPT_STATUS).fetchall()]
    events = [dict(r) for r in conn.execute(
        "SELECT aggregate_type,aggregate_id,event,from_state,to_state,actor,occurred_at "
        "FROM state_events ORDER BY occurred_at DESC, rowid DESC "
        f"LIMIT {MAX_SNAPSHOT_EVENTS}").fetchall()]

    return {
        "sidecar_version": __version__,
        "servers": [
            {**{k: s[k] for k in (
                "name", "host", "port", "username", "transport_mode", "status",
                "agent_port", "agent_schema_ver", "last_seen_at")},
             "gpus": by_server.get(s["id"], [])}
            for s in servers
        ],
        "projects": projects,
        "recent_runs": runs,
        "active_attempts": active_attempts,
        "recent_state_events": events,
    }


def _summary(snap: dict) -> dict:
    servers = snap["servers"]
    gpu_all = [g for s in servers for g in s["gpus"]]
    return {
        "snapshot_at": snap["snapshot_at"],
        "servers": len(servers),
        "reachable": sum(1 for s in servers if s["status"] == "REACHABLE"),
        "unreachable": sum(1 for s in servers if s["status"] == "UNREACHABLE"),
        "gpus_total": len(gpu_all),
        "gpus_idle": sum(1 for g in gpu_all if g["status"] == "IDLE"),
        "gpus_busy": sum(1 for g in gpu_all if str(g["status"]).startswith("BUSY")),
        "active_attempts": len(snap["active_attempts"]),
        "projects": len(snap["projects"]),
    }


def _system_prompt(snap: dict) -> str:
    return (
        "你是 AutoLab 平台的全局助手（/chat 对话页），角色对标 ZCode 这类编码助手的产品侧助手。\n"
        "\n"
        "职责：\n"
        "- 回答平台当前状态相关问题：GPU 服务器注册/连通/agent 部署状态、GPU 占用、"
        "在途训练 run、项目阶段、近期状态事件。\n"
        "- 也承担常规 AI 助手问答（概念解释、命令/代码建议、排障思路等）。\n"
        "\n"
        "只读诊断工具（快照没有的实时遥测用这个，别让用户自己上服务器看）：\n"
        "- 需要某台服务器的事实测数据时，回复中单独成行输出标记（这一轮只输出标记行，不写别的）：\n"
        "  [[probe:gpu-telemetry|服务器名]]  → nvidia-smi 全量输出（显存/利用率/进程）\n"
        "  [[probe:disk-free|服务器名]]      → df -h（磁盘占用）\n"
        "- 平台会用注册凭据 SSH 执行并把结果回传给你，你再基于结果给最终回答（引用具体数字）。\n"
        "- 每轮最多 2 个标记（多余的会被忽略）；服务器名必须用快照里的注册名。\n"
        "\n"
        "规则：\n"
        "- 用户问「现在什么状态」「有什么在跑」时，先依据下方实时快照回答，给出具体数字和名称。\n"
        "- 快照里没有的信息不要编造；训练指标以落盘产物为准（metrics.json / final.ckpt），"
        "内存数字与猜测不算数。\n"
        "- 变更类操作（部署/升级/删除/派发训练）你做不了，指引用户点页面按钮。\n"
        "- 用简体中文回答，简洁分点。\n"
        "\n"
        f"实时快照（生成时间 {snap['snapshot_at']}）：\n"
        + json.dumps({k: v for k, v in snap.items() if k != "snapshot_at"},
                     ensure_ascii=False, default=str)
    )


async def _run_probe(state, tool: str, server_name: str) -> dict:
    """执行白名单探测：命令由后端拼装，凭据来自注册钥匙串。失败不炸对话，
    作为工具结果回传给模型（它会在最终回答里向用户解释）。"""
    cmd = _PROBE_TOOLS.get(tool)
    if cmd is None:
        return {"tool": tool, "server": server_name, "ok": False,
                "error": f"未知工具（可用：{'/'.join(_PROBE_TOOLS)}）"}

    def _load(conn):
        row = conn.execute("SELECT * FROM servers WHERE name=?",
                           (server_name.strip(),)).fetchone()
        return dict(row) if row else None
    row = await to_thread(state.db, _load)
    if row is None:
        return {"tool": tool, "server": server_name, "ok": False,
                "error": "服务器未注册（用快照里的注册名）"}

    transport = make_transport(row, state.creds.get(row["credential_ref"]))
    try:
        r = await transport.exec(cmd, timeout=20)
        out = (r.stdout or "").strip()
        if r.rc != 0 and not out:
            return {"tool": tool, "server": server_name, "ok": False,
                    "error": (r.stderr or f"rc={r.rc}").strip()[:200]}
        return {"tool": tool, "server": server_name, "ok": True, "output": out[:4000]}
    except Exception as e:  # noqa: BLE001 —— SSH 不通等：作为工具结果反馈
        return {"tool": tool, "server": server_name, "ok": False, "error": str(e)[:200]}
    finally:
        with contextlib.suppress(Exception):
            await transport.close()


def _parse_probes(reply: str) -> list[tuple[str, str]]:
    """提取去重后的 (tool, server) 标记，保序、封顶 MAX_PROBES。"""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for tool, server in _PROBE_RE.findall(reply):
        pair = (tool, server.strip())
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out[:MAX_PROBES]


def _llm_view(state) -> dict:
    settings = llm_client.load_settings(state, USE_POINT)
    engine = llm_client.load_engine(state, USE_POINT)
    return {
        "configured": settings is not None,
        "has_key": bool(settings and settings.api_key),
        "use_point": USE_POINT,
        "model": settings.model if settings else None,
        "base_url": settings.base_url if settings else None,
        "engine": engine,
    }


@router.get("/assistant/status")
async def assistant_status(request: Request):
    state = request.app.state.autolab
    snap = await to_thread(state.db, _snapshot_with_ts)
    return {"summary": _summary(snap), "llm": _llm_view(state)}


@router.post("/assistant/chat")
async def assistant_chat(req: ChatIn, request: Request):
    state = request.app.state.autolab
    settings = llm_client.load_settings(state, USE_POINT)
    if settings is None or not settings.api_key:
        return JSONResponse(status_code=503, content={"error": {
            "code": "LLM_NOT_CONFIGURED",
            "message": "LLM 未配置或缺少 API Key（使用点 round_analysis，"
                       "迭代分析与平台助手共用）。请在设置里配置后重试。"}})

    snap = await to_thread(state.db, _snapshot_with_ts)
    messages = [{"role": "system", "content": _system_prompt(snap)}] + [
        {"role": m.role, "content": m.content} for m in req.messages[-MAX_HISTORY:]
    ]
    try:
        # 推理型模型（glm-4.7 等）的思考计入 max_tokens 预算，给足余量防空回复
        reply = await llm_client.chat(settings, messages, timeout=90.0, max_tokens=4096)
    except LLMError as e:
        return JSONResponse(status_code=502, content={"error": {
            "code": e.code, "message": e.message[:300]}})

    # 工具回合：模型请求探测 → SSH 执行 → 带结果重问一轮出最终回答
    probes: list[dict] = []
    wanted = _parse_probes(reply)
    if wanted:
        for tool, server in wanted:
            probes.append(await _run_probe(state, tool, server))
        messages = messages + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content":
                "平台工具执行结果（JSON；ok=false 时向用户说明失败原因，别编数字）：\n"
                + json.dumps(probes, ensure_ascii=False)},
        ]
        try:
            reply = await llm_client.chat(settings, messages, timeout=90.0, max_tokens=4096)
        except LLMError as e:
            return JSONResponse(status_code=502, content={"error": {
                "code": e.code, "message": e.message[:300]}})

    view = _summary(snap)
    view["model"] = settings.model
    return {"reply": reply, "context": view,
            "probes": [{"tool": p["tool"], "server": p["server"], "ok": p["ok"]}
                       for p in probes]}


def _snapshot_with_ts(conn) -> dict:
    snap = _snapshot(conn)
    snap["snapshot_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return snap
