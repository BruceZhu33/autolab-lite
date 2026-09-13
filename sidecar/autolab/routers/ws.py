"""WS 路由（ws-events.md M1 子集）：connected/subscribe/事件帧/心跳。"""
from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # token 鉴权由中间件经 query 参数完成（浏览器 WS 无法自定义头）
    await ws.accept()
    state = ws.app.state.autolab
    hub = state.hub
    sub = await hub.attach(ws)
    try:
        await ws.send_text(json.dumps({"type": "connected", "epoch": hub.epoch,
                                       "subscription_id": sub.sub_id}))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "code": "SUBSCRIBE_INVALID",
                                               "message": "not JSON"}))
                continue
            mtype = msg.get("type")
            if mtype == "subscribe":
                sub.filters = msg.get("filters") or {}
                resume = msg.get("resume")
                action = await hub.try_resume(sub, resume)
                if action == "snapshot_required":
                    await ws.send_text(json.dumps({
                        "type": "snapshot_required", "subscription_id": sub.sub_id,
                        "message": "客户端走 REST 全量刷新（M1 快照路径）"}))
                    sub.next_sub_seq = 1
                    sub.sent.clear()
                elif action == "replayed":
                    for frame in hub.replay_frames(sub, int(resume.get("last_sub_seq", 0))):
                        await ws.send_text(frame)
                await ws.send_text(json.dumps({
                    "type": "subscribed", "subscription_id": sub.sub_id,
                    "sub_seq_reset": action != "replayed"}))
            elif mtype == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            else:
                await ws.send_text(json.dumps({"type": "error", "code": "SUBSCRIBE_INVALID",
                                               "message": f"unknown type {mtype}"}))
    except WebSocketDisconnect:
        pass
    finally:
        hub.detach(sub)
