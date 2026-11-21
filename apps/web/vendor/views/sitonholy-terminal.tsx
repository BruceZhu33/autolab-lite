"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CornerDownLeft,
  Eraser,
  ExternalLink,
  Maximize2,
  Minimize2,
  PlugZap,
  RotateCw,
  Send,
  Square,
  Terminal as TerminalIcon,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "~/vendor/ui/components/ui/badge";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import {
  sitonholyAuthToken,
  sitonholyCloseTerminal,
  sitonholyOpenTerminal,
  sitonholyTerminalInput,
  sitonholyTerminalStreamUrl,
  type SitonholyTerminalSessionInfo,
} from "~/vendor/views/gpu-sitonholy";
import {
  closeServerTerminal,
  openServerTerminal,
  serverTerminalInput,
  serverTerminalStreamUrl,
} from "~/vendor/views/gpu-sidecar";

interface TermCell {
  ch: string;
  fg: string | null;
  bold: boolean;
}

interface TermSpan {
  text: string;
  fg: string | null;
  bold: boolean;
}

interface TermBufferState {
  lines: TermCell[][];
  row: number;
  col: number;
  fg: string | null;
  bold: boolean;
  pendingEsc: string;
}

const ANSI_COLORS: Record<number, string> = {
  30: "#64748b",
  31: "#f87171",
  32: "#4ade80",
  33: "#facc15",
  34: "#60a5fa",
  35: "#e879f9",
  36: "#22d3ee",
  37: "#e2e8f0",
  90: "#94a3b8",
  91: "#fca5a5",
  92: "#86efac",
  93: "#fde047",
  94: "#93c5fd",
  95: "#f0abfc",
  96: "#67e8f3",
  97: "#f8fafc",
};

const MAX_SCROLLBACK_LINES = 1200;

function createEmptyBuffer(): TermBufferState {
  return {
    lines: [[]],
    row: 0,
    col: 0,
    fg: null,
    bold: false,
    pendingEsc: "",
  };
}

function applySgr(state: TermBufferState, rawParams: string): void {
  const parts = rawParams === "" ? [0] : rawParams.split(";").map((p) => Number(p) || 0);
  let i = 0;
  while (i < parts.length) {
    const code = parts[i];
    if (code === 0) {
      state.fg = null;
      state.bold = false;
    } else if (code === 1) {
      state.bold = true;
    } else if (code === 22) {
      state.bold = false;
    } else if (code === 39) {
      state.fg = null;
    } else if (ANSI_COLORS[code]) {
      state.fg = ANSI_COLORS[code];
    } else if (code === 38 && parts[i + 1] === 5 && parts[i + 2] != null) {
      const idx = parts[i + 2];
      if (idx < 8) state.fg = ANSI_COLORS[30 + idx] ?? null;
      else if (idx < 16) state.fg = ANSI_COLORS[90 + (idx - 8)] ?? null;
      i += 2;
    } else if (code === 38 && parts[i + 1] === 2 && parts[i + 4] != null) {
      state.fg = `rgb(${parts[i + 2]}, ${parts[i + 3]}, ${parts[i + 4]})`;
      i += 4;
    }
    i += 1;
  }
}

function ensureLine(state: TermBufferState, targetRow: number): TermCell[] {
  while (state.lines.length <= targetRow) {
    state.lines.push([]);
  }
  return state.lines[targetRow];
}

function putChar(state: TermBufferState, ch: string): void {
  const line = ensureLine(state, state.row);
  while (line.length < state.col) {
    line.push({ ch: " ", fg: null, bold: false });
  }
  const cell: TermCell = { ch, fg: state.fg, bold: state.bold };
  if (state.col === line.length) {
    line.push(cell);
  } else {
    line[state.col] = cell;
  }
  state.col += 1;
}

function feedTerminalChunk(prev: TermBufferState, rawChunk: string): TermBufferState {
  if (!rawChunk) return prev;
  const state: TermBufferState = {
    lines: prev.lines.slice(),
    row: prev.row,
    col: prev.col,
    fg: prev.fg,
    bold: prev.bold,
    pendingEsc: "",
  };
  const input = prev.pendingEsc + rawChunk;
  const touched = new Set<number>();
  const cloneRow = (r: number) => {
    const line = ensureLine(state, r);
    if (!touched.has(r)) {
      state.lines[r] = line.slice();
      touched.add(r);
    }
    return state.lines[r];
  };

  let i = 0;
  while (i < input.length) {
    const ch = input[i];
    if (ch === "\x1b") {
      if (i + 1 >= input.length) {
        state.pendingEsc = input.slice(i);
        break;
      }
      const next = input[i + 1];
      if (next === "[") {
        let j = i + 2;
        while (j < input.length && !(input.charCodeAt(j) >= 0x40 && input.charCodeAt(j) <= 0x7e)) {
          j += 1;
        }
        if (j >= input.length) {
          state.pendingEsc = input.slice(i);
          break;
        }
        const params = input.slice(i + 2, j);
        const cmd = input[j];
        if (cmd === "m") {
          applySgr(state, params);
        } else if (cmd === "K") {
          const mode = params === "" ? 0 : Number(params);
          const line = cloneRow(state.row);
          if (mode === 0) {
            line.length = Math.min(line.length, state.col);
          } else if (mode === 1) {
            for (let k = 0; k < Math.min(state.col, line.length); k += 1) {
              line[k] = { ch: " ", fg: null, bold: false };
            }
          } else if (mode === 2) {
            line.length = 0;
          }
        } else if (cmd === "J") {
          const mode = params === "" ? 0 : Number(params);
          if (mode === 2 || mode === 3) {
            state.lines = [[]];
            state.row = 0;
            state.col = 0;
            touched.clear();
          } else if (mode === 0) {
            const line = cloneRow(state.row);
            line.length = Math.min(line.length, state.col);
            state.lines.length = state.row + 1;
          }
        } else if (cmd === "H" || cmd === "f") {
          if (params === "" || params === ";" || params === "1;1") {
            state.col = 0;
            if (state.lines.length <= 1) state.row = 0;
          }
        } else if (cmd === "G") {
          const targetCol = Math.max(1, Number(params) || 1) - 1;
          state.col = targetCol;
        } else if (cmd === "C") {
          state.col += Math.max(1, Number(params) || 1);
        } else if (cmd === "D") {
          state.col = Math.max(0, state.col - Math.max(1, Number(params) || 1));
        }
        i = j + 1;
        continue;
      }
      if (next === "]") {
        let j = i + 2;
        let terminated = false;
        while (j < input.length) {
          if (input[j] === "\x07") {
            j += 1;
            terminated = true;
            break;
          }
          if (input[j] === "\x1b" && input[j + 1] === "\\") {
            j += 2;
            terminated = true;
            break;
          }
          j += 1;
        }
        if (!terminated) {
          state.pendingEsc = input.slice(i);
          break;
        }
        i = j;
        continue;
      }
      // Two-char ESC sequence (e.g. ESC = / ESC > / ESC M)
      i += 2;
      continue;
    }

    if (ch === "\r") {
      state.col = 0;
    } else if (ch === "\n") {
      state.row += 1;
      state.col = 0;
      ensureLine(state, state.row);
      if (state.lines.length > MAX_SCROLLBACK_LINES) {
        const overflow = state.lines.length - MAX_SCROLLBACK_LINES;
        state.lines.splice(0, overflow);
        state.row = Math.max(0, state.row - overflow);
        touched.clear();
      }
    } else if (ch === "\b") {
      state.col = Math.max(0, state.col - 1);
    } else if (ch === "\t") {
      const target = state.col + (8 - (state.col % 8));
      cloneRow(state.row);
      while (state.col < target) {
        putChar(state, " ");
      }
    } else if (ch.charCodeAt(0) >= 0x20 || ch === "\u00a0") {
      cloneRow(state.row);
      putChar(state, ch);
    }
    i += 1;
  }
  return state;
}

function lineToSpans(line: TermCell[]): TermSpan[] {
  if (line.length === 0) return [];
  const spans: TermSpan[] = [];
  let cur: TermSpan = { text: line[0].ch, fg: line[0].fg, bold: line[0].bold };
  for (let i = 1; i < line.length; i += 1) {
    const cell = line[i];
    if (cell.fg === cur.fg && cell.bold === cur.bold) {
      cur.text += cell.ch;
    } else {
      spans.push(cur);
      cur = { text: cell.ch, fg: cell.fg, bold: cell.bold };
    }
  }
  spans.push(cur);
  return spans;
}

function mapKeyToPty(e: React.KeyboardEvent<HTMLElement>): string | null {
  if (e.ctrlKey && !e.metaKey && !e.altKey) {
    const lower = e.key.toLowerCase();
    if (lower.length === 1 && lower >= "a" && lower <= "z") {
      // Allow Ctrl+V paste & Ctrl+Shift+C copy to propagate normally
      if (lower === "v" || (e.shiftKey && lower === "c")) return null;
      return String.fromCharCode(lower.charCodeAt(0) - 96);
    }
    if (e.key === "[") return "\x1b";
    if (e.key === "\\") return "\x1c";
    if (e.key === "]") return "\x1d";
  }
  if (e.metaKey) return null;

  switch (e.key) {
    case "Enter":
      return "\r";
    case "Backspace":
      return "\x7f";
    case "Tab":
      return "\t";
    case "Escape":
      return "\x1b";
    case "ArrowUp":
      return "\x1b[A";
    case "ArrowDown":
      return "\x1b[B";
    case "ArrowRight":
      return "\x1b[C";
    case "ArrowLeft":
      return "\x1b[D";
    case "Home":
      return "\x1b[H";
    case "End":
      return "\x1b[F";
    case "Delete":
      return "\x1b[3~";
    case "PageUp":
      return "\x1b[5~";
    case "PageDown":
      return "\x1b[6~";
    default:
      if (e.key.length === 1 && !e.altKey) {
        return e.key;
      }
      return null;
  }
}

export function SitonholyTerminalPane({
  taskId,
  taskName,
  nativeTerminalUrl,
  scope = "sitonholy",
  onClose,
}: {
  taskId: string;
  taskName: string;
  nativeTerminalUrl?: string;
  scope?: "sitonholy" | "server";
  onClose: () => void;
}) {
  const [session, setSession] = useState<SitonholyTerminalSessionInfo | null>(null);
  const [connecting, setConnecting] = useState(true);
  const [closedMsg, setClosedMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [focused, setFocused] = useState(false);
  const [cmdDraft, setCmdDraft] = useState("");
  const [reconnectSeq, setReconnectSeq] = useState(0);
  const [termBuf, setTermBuf] = useState<TermBufferState>(() => createEmptyBuffer());

  const viewportRef = useRef<HTMLDivElement | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const composingRef = useRef(false);

  const sendInput = useCallback(async (data: string) => {
    const sid = sessionIdRef.current;
    if (!sid || !data) return;
    try {
      if (scope === "server") {
        await serverTerminalInput(sid, data);
      } else {
        await sitonholyTerminalInput(sid, data);
      }
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : String(e));
      setClosedMsg("输入发送失败，终端可能已断开");
    }
  }, [scope]);

  useEffect(() => {
    let cancelled = false;
    const abort = new AbortController();
    setConnecting(true);
    setClosedMsg(null);
    setErrorMsg(null);
    setSession(null);
    setTermBuf(createEmptyBuffer());

    const closeSess = (sid: string) =>
      scope === "server"
        ? closeServerTerminal(sid, true)
        : sitonholyCloseTerminal(sid, true);

    const run = async () => {
      let sid = "";
      try {
        const opened =
          scope === "server"
            ? await openServerTerminal(taskId, {
                cols: 140,
                rows: 36,
                command: "/bin/bash",
              })
            : await sitonholyOpenTerminal(taskId, {
                cols: 140,
                rows: 36,
                command: "/bin/bash",
              });
        if (cancelled) {
          void closeSess(opened.session.session_id);
          return;
        }
        sid = opened.session.session_id;
        sessionIdRef.current = sid;
        setSession(opened.session);
        setConnecting(false);
        setTimeout(() => viewportRef.current?.focus(), 40);

        const streamUrl =
          scope === "server"
            ? serverTerminalStreamUrl(sid, 0)
            : sitonholyTerminalStreamUrl(sid, 0);
        const resp = await fetch(streamUrl, {
          headers: { Authorization: `Bearer ${sitonholyAuthToken()}` },
          signal: abort.signal,
          cache: "no-store",
        });
        if (!resp.ok || !resp.body) {
          throw new Error(`终端流连接失败 (${resp.status})`);
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let pendingSse = "";
        while (!cancelled) {
          const { value, done } = await reader.read();
          if (done) break;
          pendingSse += decoder.decode(value, { stream: true });
          const frames = pendingSse.split("\n\n");
          pendingSse = frames.pop() ?? "";
          for (const frame of frames) {
            const lines = frame.split("\n");
            let evType = "message";
            const dataLines: string[] = [];
            for (const line of lines) {
              if (line.startsWith("event:")) evType = line.slice(6).trim();
              else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
            }
            if (dataLines.length === 0) continue;
            try {
              const payload = JSON.parse(dataLines.join("\n")) as {
                data?: string;
                closed?: boolean;
                error?: string | null;
              };
              if (evType === "output" && payload.data) {
                setTermBuf((prev) => feedTerminalChunk(prev, payload.data!));
              } else if (evType === "exit") {
                setClosedMsg(payload.error || "远程 Shell 会话已结束");
              }
            } catch {
              /* ignore malformed event */
            }
          }
        }
      } catch (e) {
        if (!cancelled && (e as { name?: string })?.name !== "AbortError") {
          setErrorMsg(e instanceof Error ? e.message : String(e));
        }
      } finally {
        setConnecting(false);
      }
    };

    void run();

    const handleBeforeUnload = () => {
      if (sessionIdRef.current) {
        void closeSess(sessionIdRef.current);
      }
    };
    window.addEventListener("beforeunload", handleBeforeUnload);

    return () => {
      cancelled = true;
      abort.abort();
      window.removeEventListener("beforeunload", handleBeforeUnload);
      const activeSid = sessionIdRef.current;
      sessionIdRef.current = null;
      if (activeSid) {
        void closeSess(activeSid);
      }
    };
  }, [taskId, reconnectSeq, scope]);

  useEffect(() => {
    const el = viewportRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [termBuf]);

  const renderedLines = useMemo(
    () => termBuf.lines.map((line) => lineToSpans(line)),
    [termBuf.lines],
  );

  const handleViewportKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (composingRef.current) return;
    if (
      e.ctrlKey &&
      !e.metaKey &&
      !e.altKey &&
      !e.shiftKey &&
      e.key.toLowerCase() === "c" &&
      window.getSelection()?.toString()
    ) {
      return;
    }
    if (
      (e.ctrlKey && e.key.toLowerCase() === "c" && !e.shiftKey) ||
      e.key === "Tab" ||
      e.key === "Backspace" ||
      e.key.startsWith("Arrow") ||
      e.key === "Enter"
    ) {
      e.preventDefault();
    }
    const seq = mapKeyToPty(e);
    if (seq !== null) {
      e.preventDefault();
      void sendInput(seq);
    }
  };

  const handleViewportPaste = (e: React.ClipboardEvent<HTMLDivElement>) => {
    const text = e.clipboardData?.getData("text");
    if (text) {
      e.preventDefault();
      void sendInput(text);
    }
  };

  const sendDraftLine = () => {
    const text = cmdDraft;
    setCmdDraft("");
    void sendInput(`${text}\r`);
    viewportRef.current?.focus();
  };

  const handleClearLocal = () => {
    setTermBuf((prev) => ({ ...createEmptyBuffer(), fg: prev.fg, bold: prev.bold }));
    void sendInput("\x0c");
    viewportRef.current?.focus();
  };

  const containerCls = expanded
    ? "fixed inset-4 z-50 flex flex-col overflow-hidden rounded-xl border border-slate-700 bg-[#0b1220] text-slate-100 shadow-2xl"
    : "flex flex-col overflow-hidden rounded-xl border border-slate-700 bg-[#0b1220] text-slate-100 shadow-sm";

  return (
    <div className={containerCls}>
      {/* 顶部终端工具栏 */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 bg-slate-900/95 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <TerminalIcon className="size-4 shrink-0 text-emerald-400" />
          <span className="truncate text-sm font-medium text-slate-100">
            Web 终端 · {taskName || taskId}
          </span>
          {connecting ? (
            <Badge variant="secondary" className="bg-amber-500/20 text-amber-200">
              正在连接 WebSSH…
            </Badge>
          ) : closedMsg || errorMsg ? (
            <Badge variant="destructive">已断开</Badge>
          ) : (
            <Badge variant="secondary" className="bg-emerald-500/20 text-emerald-300">
              已连接 {session?.ip_address} ({session?.container_id?.slice(0, 12)})
            </Badge>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-1">
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-xs text-slate-200 hover:bg-slate-800 hover:text-white"
            onClick={() => void sendInput("\x03")}
            title="发送 Ctrl+C 中断当前命令"
          >
            <Square className="mr-1 size-3 text-rose-400" />
            Ctrl+C
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-xs text-slate-200 hover:bg-slate-800 hover:text-white"
            onClick={() => void sendInput("nvidia-smi\r")}
          >
            nvidia-smi
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-xs text-slate-200 hover:bg-slate-800 hover:text-white"
            onClick={() => void sendInput("conda env list\r")}
          >
            conda env
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-xs text-slate-200 hover:bg-slate-800 hover:text-white"
            onClick={handleClearLocal}
          >
            <Eraser className="mr-1 size-3" />
            清屏
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-xs text-slate-200 hover:bg-slate-800 hover:text-white"
            onClick={() => setReconnectSeq((n) => n + 1)}
          >
            <RotateCw className="mr-1 size-3" />
            重连
          </Button>
          {nativeTerminalUrl && (
            <a href={nativeTerminalUrl} target="_blank" rel="noreferrer">
              <Button
                size="sm"
                variant="ghost"
                className="h-7 px-2 text-xs text-slate-300 hover:bg-slate-800 hover:text-white"
              >
                原生终端
                <ExternalLink className="ml-1 size-3" />
              </Button>
            </a>
          )}
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-slate-300 hover:bg-slate-800 hover:text-white"
            onClick={() => setExpanded((v) => !v)}
            title={expanded ? "还原嵌入式视图" : "全屏放大"}
          >
            {expanded ? <Minimize2 className="size-3.5" /> : <Maximize2 className="size-3.5" />}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-slate-300 hover:bg-rose-900/50 hover:text-rose-200"
            onClick={() => {
              if (sessionIdRef.current) {
                const sid = sessionIdRef.current;
                sessionIdRef.current = null;
                void sitonholyCloseTerminal(sid);
              }
              onClose();
            }}
            title="关闭终端会话"
          >
            <X className="size-4" />
          </Button>
        </div>
      </div>

      {/* 异常/断开提示条 */}
      {(errorMsg || closedMsg) && (
        <div className="flex items-center justify-between border-b border-rose-900/60 bg-rose-950/60 px-3 py-1.5 text-xs text-rose-200">
          <span className="flex items-center gap-1.5">
            <PlugZap className="size-3.5" />
            {errorMsg || closedMsg}
          </span>
          <Button
            size="sm"
            variant="outline"
            className="h-6 border-rose-700 bg-transparent px-2 text-xs text-rose-100 hover:bg-rose-900"
            onClick={() => setReconnectSeq((n) => n + 1)}
          >
            重新连接
          </Button>
        </div>
      )}

      {/* 交互式终端屏幕区域（点击即可直接键入、粘贴、Ctrl+C、Tab 补全、方向键历史） */}
      <div
        ref={viewportRef}
        tabIndex={0}
        role="textbox"
        aria-label="Sitonholy 交互式终端"
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onKeyDown={handleViewportKeyDown}
        onPaste={handleViewportPaste}
        onClick={() => viewportRef.current?.focus()}
        onCompositionStart={() => {
          composingRef.current = true;
        }}
        onCompositionEnd={(e) => {
          composingRef.current = false;
          if (e.data) void sendInput(e.data);
        }}
        className={`overflow-auto px-3.5 py-3 font-mono text-[13px] leading-[1.45] outline-none select-text ${
          expanded ? "flex-1" : "h-[380px]"
        } ${focused ? "ring-1 ring-inset ring-emerald-500/40" : ""}`}
      >
        {renderedLines.map((spans, idx) => {
          const isLast = idx === renderedLines.length - 1;
          return (
            <div key={idx} className="min-h-[1.2em] whitespace-pre-wrap break-all">
              {spans.length === 0 ? (
                isLast ? null : " "
              ) : (
                spans.map((sp, sIdx) => (
                  <span
                    key={sIdx}
                    style={{
                      color: sp.fg ?? undefined,
                      fontWeight: sp.bold ? 600 : undefined,
                    }}
                  >
                    {sp.text}
                  </span>
                ))
              )}
              {isLast && !closedMsg && (
                <span
                  className={`inline-block h-[1.05em] w-[0.55em] translate-y-[2px] align-baseline ${
                    focused ? "animate-pulse bg-emerald-400" : "border border-slate-500"
                  }`}
                />
              )}
            </div>
          );
        })}
      </div>

      {/* 辅助输入框：适合中文输入法、粘贴长命令与一键回车 */}
      <div className="flex flex-wrap items-center gap-2 border-t border-slate-800 bg-slate-900/90 px-3 py-2">
        <span className="text-[11px] text-slate-400">
          提示：点击上方黑框可直接键盘交互（支持 Tab / ↑↓ / Ctrl+C / 粘贴）；也可用下方命令栏发送：
        </span>
        <div className="flex flex-1 items-center gap-1.5 min-w-[260px]">
          <Input
            className="h-7 border-slate-700 bg-slate-950 font-mono text-xs text-slate-100 placeholder:text-slate-500"
            placeholder="输入整行命令或中文路径后按 Enter 发送…"
            value={cmdDraft}
            onChange={(e) => setCmdDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                e.preventDefault();
                sendDraftLine();
              }
            }}
          />
          <Button
            size="sm"
            variant="secondary"
            className="h-7 shrink-0 px-2.5 text-xs"
            onClick={sendDraftLine}
            disabled={Boolean(closedMsg) || connecting}
          >
            <Send className="mr-1 size-3" />
            发送
            <CornerDownLeft className="ml-1 size-3 opacity-70" />
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 shrink-0 border-slate-700 bg-transparent px-2 text-xs text-slate-200 hover:bg-slate-800"
            onClick={() => {
              void navigator.clipboard.readText().then(
                (t) => {
                  if (t) void sendInput(t);
                },
                () => toast.info("请直接在黑框中 Ctrl+V / Cmd+V 粘贴"),
              );
            }}
          >
            粘贴剪贴板
          </Button>
        </div>
      </div>
    </div>
  );
}
