/**
 * sidecar 事件 WS（Phase 2）：失效通知客户端。
 * v0.3 §6——WebSocket 只发失效通知，REST/SQLite 保持权威；本客户端收到任何
 * 事件帧（attempt 前缀、project.state_changed、env_setup.done、review 前缀等）
 * 后回调，由调用方去抖失效 React Query（不做数据搬运）。
 * 重连：指数退避 1s→30s 封顶；心跳 ping 25s。
 */
import { getSidecarConfig } from "./config";

const CONTROL_FRAMES = new Set(["connected", "subscribed", "pong", "error", "snapshot_required"]);

export function connectSidecarEvents(onEvent: (type: string) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let attempt = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  let pingTimer: ReturnType<typeof setInterval> | undefined;

  const cleanupSocket = () => {
    if (pingTimer) clearInterval(pingTimer);
    pingTimer = undefined;
    if (ws) {
      ws.onclose = null;
      ws.onmessage = null;
      ws.onerror = null;
      try { ws.close(); } catch { /* 已关闭 */ }
    }
    ws = null;
  };

  const dispose = () => {
    closed = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    cleanupSocket();
  };

  const connect = async () => {
    if (closed) return;
    let url: string;
    try {
      const cfg = await getSidecarConfig();
      url = cfg.baseUrl.replace(/^http/, "ws") + "/ws?token=" + encodeURIComponent(cfg.token);
    } catch {
      scheduleReconnect();
      return;
    }
    try {
      ws = new WebSocket(url);
    } catch {
      scheduleReconnect();
      return;
    }
    ws.onopen = () => {
      attempt = 0;
      ws?.send(JSON.stringify({ type: "subscribe", filters: {} }));
      pingTimer = setInterval(() => {
        if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
      }, 25_000);
    };
    ws.onmessage = (ev) => {
      try {
        const frame = JSON.parse(String(ev.data)) as { type?: string };
        if (frame.type && !CONTROL_FRAMES.has(frame.type)) onEvent(frame.type);
      } catch {
        // 非 JSON 帧忽略
      }
    };
    ws.onclose = () => {
      cleanupSocket();
      scheduleReconnect();
    };
    ws.onerror = () => {
      // onclose 随后触发，重连统一在 onclose 处理
    };
  };

  const scheduleReconnect = () => {
    if (closed || reconnectTimer) return;
    const delay = Math.min(30_000, 1000 * 2 ** Math.min(attempt, 5));
    attempt += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = undefined;
      void connect();
    }, delay);
  };

  void connect();
  return dispose;
}
