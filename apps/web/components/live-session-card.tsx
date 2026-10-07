"use client";

/** L1 Agent 会话实时面板:时间线观看 / 注入引导 / 停止 / 回放。
 * iteration 与 baseline 页共用(fix 会话同样可观看可介入)。 */

import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Bot, ChevronRight, Eye, Loader2, Send, Square, Terminal,
} from "lucide-react";
import {
  fetchAgentSessionDetail, fetchAgentSessions, injectAgentSessionMessage,
  requestAgentSessionStop, type AgentSessionEvent,
} from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";

const SESSION_ROLE_LABEL: Record<string, string> = {
  session: "实现 Agent", fix: "修复 Agent",
  analyzer: "分析 Agent", reviewer: "评分 Agent",
  onboard: "接入 Agent",
};

function SessionStatusPill({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    running: { label: "进行中", cls: "bg-brand/10 text-brand" },
    done: { label: "完成", cls: "bg-success/10 text-success" },
    exhausted: { label: "预算耗尽", cls: "bg-warning/10 text-warning" },
    stopped: { label: "已停止", cls: "bg-muted/60 text-muted-foreground" },
    interrupted: { label: "中断(可续跑)", cls: "bg-warning/10 text-warning" },
    error: { label: "失败", cls: "bg-destructive/10 text-destructive" },
  };
  const item = map[status]
    ?? { label: status, cls: "bg-muted/60 text-muted-foreground" };
  return (
    <span className={`rounded-full px-2 py-0.5 text-micro font-medium ${item.cls}`}>
      {item.label}
    </span>
  );
}

function EventRow({ event }: { event: AgentSessionEvent }) {
  const p = event.payload;
  if (event.kind === "status") {
    const text = p.phase === "started" ? "会话开始"
      : p.phase === "resumed" ? "从 checkpoint 断点续跑"
      : p.phase === "submitted" ? "submit_result 已受理,结构化结果在手,收尾"
      : p.phase === "stop_requested" ? "收到停止请求,当前工具步完成后收尾"
      : p.phase === "wall_clock_budget" ? "墙钟预算见底,best-effort 收尾"
      : p.phase === "finished"
        ? `会话结束(${p.status ?? "?"}${p.toolCalls != null ? `,工具 ${p.toolCalls},冒烟 ${p.smokeRuns ?? 0}` : ""})`
        : (p.phase ?? "状态");
    return (
      <p className="text-micro text-muted-foreground/70">— {text} —</p>
    );
  }
  if (event.kind === "inject") {
    return (
      <p className="rounded-lg border-[0.5px] border-brand/40 bg-brand/10 px-2 py-1 text-caption text-brand">
        <Eye className="mr-1 inline size-3 align-[-2px]" />你:{p.message}
      </p>
    );
  }
  if (event.kind === "ai") {
    return (
      <p className="whitespace-pre-wrap text-caption leading-relaxed">
        <Bot className="mr-1 inline size-3 align-[-2px] text-brand" />
        {p.text}
      </p>
    );
  }
  if (event.kind === "tool_call") {
    let args = "";
    try {
      const parsed = JSON.parse(p.args ?? "{}") as Record<string, unknown>;
      args = Object.entries(parsed).map(([k, v]) =>
        `${k}=${String(v).slice(0, 60)}`).join(" ");
    } catch { args = (p.args ?? "").slice(0, 80); }
    return (
      <p className="font-mono text-micro text-foreground/90">
        <Terminal className="mr-1 inline size-3 align-[-2px] text-muted-foreground" />
        <span className="font-semibold">{p.tool}</span>
        <span className="ml-1 text-muted-foreground">{args}</span>
      </p>
    );
  }
  // tool_result
  return (
    <pre className="ml-4 max-h-24 overflow-y-auto whitespace-pre-wrap rounded-lg border-[0.5px] border-surface-border bg-muted/40 px-2 py-1 font-mono text-micro leading-relaxed text-muted-foreground">
      {(p.content ?? "").trim() || "(空)"}
    </pre>
  );
}

export function LiveSessionCard({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState<boolean | null>(null);
  const [guide, setGuide] = useState("");
  const [sending, setSending] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  const { data: sessions = [] } = useQuery({
    queryKey: ["agent-sessions", projectId],
    queryFn: () => fetchAgentSessions(projectId, 6),
    refetchInterval: (query) =>
      query.state.data?.some((s) => s.status === "running") ? 2500 : 15000,
  });
  const running = sessions.find((s) => s.status === "running") ?? null;
  // 手选历史会话回放;无手选时自动跟随 running(或最新)
  const [pickedId, setPickedId] = useState<string | null>(null);
  const picked = sessions.find((s) => s.id === pickedId) ?? null;
  const selected = picked ?? running ?? sessions[0] ?? null;
  const { data: detail } = useQuery({
    queryKey: ["agent-session", selected?.id ?? ""],
    queryFn: () => fetchAgentSessionDetail(selected!.id),
    enabled: !!selected,
    refetchInterval: (query) => {
      const d = query.state.data;
      return d && d.status === "running" ? 2000 : false;
    },
  });
  // 会话刚结束:轮询停止后补拉一次最终事件(收尾/提交事件不再丢失)
  const [lastStatus, setLastStatus] = useState<string | null>(null);
  useEffect(() => {
    const st = detail?.status ?? null;
    if (st && lastStatus === "running" && st !== "running") {
      void queryClient.invalidateQueries({
        queryKey: ["agent-session", selected?.id] });
    }
    setLastStatus(st);
  }, [detail?.status, lastStatus, selected?.id, queryClient]);
  // 新会话开跑时清除手选,回到自动跟随
  useEffect(() => {
    if (running && pickedId && running.id !== pickedId) setPickedId(null);
  }, [running?.id, pickedId]);
  // 进行中的会话自动展开;结束后保持用户可折叠回放
  const expanded = open ?? !!running;
  const events = detail?.events ?? [];
  useEffect(() => {
    if (expanded && detail?.status === "running") {
      bottomRef.current?.scrollIntoView({ block: "end" });
    }
  }, [events.length, expanded, detail?.status]);

  if (!selected) return null;
  const isRunning = detail?.status === "running" || selected.status === "running";

  const send = async () => {
    const text = guide.trim();
    if (!text || !selected) return;
    setSending(true);
    try {
      await injectAgentSessionMessage(selected.id, text);
      setGuide("");
      toast.success("已注入引导消息:下一个步进点进入 Agent 上下文");
      void queryClient.invalidateQueries({
        queryKey: ["agent-session", selected.id] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSending(false);
    }
  };

  const stopSession = async () => {
    if (!selected) return;
    try {
      await requestAgentSessionStop(selected.id);
      toast.info("停止请求已排队:当前工具步完成后会话收尾");
      void queryClient.invalidateQueries({
        queryKey: ["agent-sessions", projectId] });
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  return (
    <div className="rounded-xl border-[0.5px] border-surface-border bg-background/40">
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left"
              onClick={() => setOpen(!expanded)}>
        <ChevronRight className={`size-3.5 shrink-0 text-muted-foreground transition-transform ${expanded ? "rotate-90" : ""}`} />
        <Bot className={`size-4 shrink-0 ${isRunning ? "animate-pulse text-brand" : "text-muted-foreground"}`} />
        <span className="min-w-0 flex-1 truncate text-caption font-semibold">
          {SESSION_ROLE_LABEL[selected.role] ?? "Agent"} 会话
          {isRunning ? "(工作中,可介入)" : "(回放)"}
        </span>
        <SessionStatusPill status={detail?.status ?? selected.status} />
        {selected.toolCalls > 0 ? (
          <span className="rounded-full bg-muted/60 px-2 py-0.5 font-mono text-micro text-muted-foreground">
            工具 {detail?.toolCalls ?? selected.toolCalls}
          </span>
        ) : null}
      </button>
      {expanded ? (
        <>
          {sessions.length > 1 ? (
            <div className="flex flex-wrap gap-1 border-b border-surface-border px-3 py-1.5">
              {sessions.slice(0, 4).map((s) => (
                <button key={s.id} type="button"
                        onClick={() => setPickedId(s.id)}
                        className={`rounded-full px-2 py-0.5 text-micro ${
                          s.id === selected?.id
                            ? "bg-brand/15 font-medium text-brand"
                            : "bg-muted/50 text-muted-foreground hover:bg-muted"}`}>
                  {(SESSION_ROLE_LABEL[s.role] ?? "Agent").slice(0, 2)}·
                  {s.status === "running" ? "进行中"
                   : s.status === "done" ? "完成" : s.status}
                  {s.toolCalls ? `·${s.toolCalls}` : ""}
                </button>
              ))}
            </div>
          ) : null}
          <div className="max-h-72 space-y-1.5 overflow-y-auto border-b border-surface-border px-3 py-2">
            {events.length === 0 ? (
              <p className="flex items-center gap-1.5 text-caption text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" />等待 Agent 第一步…
              </p>
            ) : events.map((e) => <EventRow key={e.seq} event={e} />)}
            <div ref={bottomRef} />
          </div>
          {isRunning ? (
            <div className="flex items-center gap-1.5 px-3 py-2">
              <input
                value={guide}
                onChange={(e) => setGuide(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing) void send(); }}
                placeholder="引导 Agent(如:优先查 dataloader 的设备问题)"
                className="min-w-0 flex-1 rounded-lg border border-input bg-background px-2.5 py-1.5 text-caption outline-none placeholder:text-muted-foreground/60 focus-visible:ring-1 focus-visible:ring-ring"
              />
              <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-micro"
                      disabled={sending || !guide.trim()} onClick={() => void send()}>
                {sending ? <Loader2 className="size-3 animate-spin" /> : <Send className="size-3" />}
                注入
              </Button>
              <Button variant="outline" size="sm"
                      className="h-7 gap-1 px-2 text-micro text-destructive hover:text-destructive"
                      onClick={() => void stopSession()}>
                <Square className="size-3" />停止
              </Button>
            </div>
          ) : (
            <p className="px-3 py-2 text-micro text-muted-foreground/70">
              会话已结束:全程时间线保留在 Control DB,可随时回放;进行中可注入引导 / 停止。
            </p>
          )}
        </>
      ) : null}
    </div>
  );
}
