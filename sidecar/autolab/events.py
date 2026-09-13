"""WS 事件枢纽（ws-events.md 的 M1 实现：投影，不做写决策）。

实现范围：subscription_id / sub_seq 严格 +1 / global_seq / epoch、心跳带
last_emitted_sub_seq watermark、每订阅已发缓冲（resume 重放）、过滤（project_ids /
kinds 前缀 / include.log_tail）。快照路径 M1 简化为 snapshot_required → 客户端走
REST 全量刷新（权威状态在 DB + HTTP）。
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

REPLAY_BUFFER = 10_000


@dataclass
class Subscription:
    sub_id: str
    ws: object
    filters: dict = field(default_factory=dict)
    next_sub_seq: int = 1
    sent: deque = field(default_factory=lambda: deque(maxlen=REPLAY_BUFFER))  # (sub_seq, frame_str)
    log_tail_rate: dict = field(default_factory=dict)  # attempt_id → last_ts（节流）


class Hub:
    def __init__(self):
        self.epoch = uuid.uuid4().hex[:8]
        self.subs: dict[str, Subscription] = {}
        self._global_seq = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    def _match(self, sub: Subscription, type_: str, project_id: str | None,
               include_log_tail: bool) -> bool:
        f = sub.filters or {}
        kinds = f.get("kinds")
        if kinds:
            ok = any(fnmatch.fnmatch(type_, k) for k in kinds)
            if not ok:
                return False
        pids = f.get("project_ids")
        if pids and project_id not in pids:
            return False
        if type_ == "attempt.log_tail" and not include_log_tail:
            return False
        return True

    async def publish(self, type_: str, payload: dict, aggregate: dict | None = None,
                      project_id: str | None = None):
        """向全部匹配订阅发送一帧（过滤 → 分配 sub_seq → 发送，同一原子段）。"""
        async with self._lock:
            self._global_seq += 1
            gseq = self._global_seq
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for sub in list(self.subs.values()):
                include_log_tail = bool((sub.filters or {}).get("include", {}).get("log_tail"))
                if not self._match(sub, type_, project_id, include_log_tail):
                    continue
                frame = {
                    "subscription_id": sub.sub_id,
                    "sub_seq": sub.next_sub_seq,
                    "global_seq": gseq,
                    "epoch": self.epoch,
                    "type": type_,
                    "occurred_at": now,
                    "aggregate": aggregate or {},
                    "payload": payload,
                }
                sub.next_sub_seq += 1
                raw = json.dumps(frame, ensure_ascii=False)
                sub.sent.append((frame["sub_seq"], raw))
                try:
                    await asyncio.wait_for(sub.ws.send_text(raw), timeout=5)
                except Exception:
                    self.subs.pop(sub.sub_id, None)

    def publish_threadsafe(self, loop: asyncio.AbstractEventLoop, type_: str, payload: dict,
                           aggregate: dict | None = None, project_id: str | None = None):
        """从工作线程调度 publish（事务回调侧）。"""
        asyncio.run_coroutine_threadsafe(
            self.publish(type_, payload, aggregate, project_id), loop)

    # ------------------------------------------------------------------
    async def attach(self, ws) -> Subscription:
        sub = Subscription(sub_id=f"sub-{uuid.uuid4().hex[:6]}", ws=ws)
        self.subs[sub.sub_id] = sub
        return sub

    def detach(self, sub: Subscription):
        self.subs.pop(sub.sub_id, None)

    async def try_resume(self, sub: Subscription, resume: dict | None) -> str:
        """返回要发送给客户端的动作：'subscribed' | 'replayed' | 'snapshot_required'。"""
        if resume is None or resume == {}:
            return "subscribed"  # 全新订阅
        if resume.get("epoch") != self.epoch:
            return "snapshot_required"
        last = int(resume.get("last_sub_seq", 0))
        buffered = [seq for seq, _ in sub.sent]
        if buffered and last + 1 < buffered[0]:
            return "snapshot_required"
        return "replayed" if last else "subscribed"

    def replay_frames(self, sub: Subscription, last_sub_seq: int) -> list[str]:
        return [raw for seq, raw in sub.sent if seq > last_sub_seq]

    async def heartbeat(self):
        while True:
            await asyncio.sleep(20)
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for sub in list(self.subs.values()):
                watermark = sub.next_sub_seq - 1
                frame = json.dumps({
                    "type": "heartbeat", "subscription_id": sub.sub_id,
                    "last_emitted_sub_seq": watermark, "occurred_at": now})
                try:
                    await sub.ws.send_text(frame)
                except Exception:
                    self.detach(sub)
