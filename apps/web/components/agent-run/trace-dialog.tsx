"use client";

/** 结构化 Trace 检视器 (Phase A2):
 * - 展示会话的 LLM / tool / verify 三类 span (含耗时、输入输出 token、状态与异常分类)
 * - 展开查看 agent_blobs 中的未截断原始 input / output
 * - 一键导出 trace.jsonl 供离线评测与回归分析
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity, ChevronDown, ChevronRight, Download, Loader2,
} from "lucide-react";
import { fetchRunEvents, fetchRunTrace, type RunEvent, type TraceSpan } from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "~/vendor/ui/components/ui/dialog";

const KIND_BADGE: Record<string, { label: string; cls: string }> = {
  llm: { label: "LLM", cls: "bg-brand/10 text-brand" },
  tool: { label: "Tool", cls: "bg-info/10 text-info" },
  verify: { label: "Verify", cls: "bg-warning/15 text-warning" },
};

const STATUS_BADGE: Record<string, { label: string; cls: string }> = {
  ok: { label: "ok", cls: "bg-success/10 text-success" },
  error: { label: "error", cls: "bg-destructive/10 text-destructive" },
  rejected: { label: "rejected", cls: "bg-warning/15 text-warning" },
};

function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "-";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function formatJson(value: unknown): string {
  if (value === null || value === undefined) return "(null)";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function deriveSpansFromEvents(events: RunEvent[]): TraceSpan[] {
  const out: TraceSpan[] = [];
  let stepNo = 0;
  let pendingTool: {
    seq: number;
    stepNo: number;
    name: string;
    input: unknown;
    at: string;
  } | null = null;

  for (const ev of events) {
    const p = ev.payload ?? {};
    if (ev.kind === "assistant") {
      stepNo += 1;
      out.push({
        id: ev.seq,
        stepNo,
        kind: "llm",
        name: "assistant_reply",
        attempt: 1,
        status: "ok",
        tokensIn: 0,
        tokensOut: 0,
        durationMs: null,
        errorClass: "",
        input: { eventSeq: ev.seq },
        output: { text: p.text ?? "" },
        startedAt: ev.at,
        endedAt: ev.at,
        createdAt: ev.at,
      });
    } else if (ev.kind === "tool_call") {
      stepNo += 1;
      pendingTool = {
        seq: ev.seq,
        stepNo,
        name: String(p.name ?? "tool"),
        input: p.input ?? {},
        at: ev.at,
      };
    } else if ((ev.kind === "tool_result" || ev.kind === "tool_error") && pendingTool) {
      const startMs = Date.parse(pendingTool.at);
      const endMs = Date.parse(ev.at);
      const durationMs = !Number.isNaN(startMs) && !Number.isNaN(endMs)
        ? Math.max(0, endMs - startMs)
        : null;
      out.push({
        id: pendingTool.seq,
        stepNo: pendingTool.stepNo,
        kind: "tool",
        name: pendingTool.name,
        attempt: 1,
        status: ev.kind === "tool_error" ? "error" : "ok",
        tokensIn: 0,
        tokensOut: 0,
        durationMs,
        errorClass: ev.kind === "tool_error" ? "ToolError" : "",
        input: pendingTool.input,
        output: p.output ?? p.error ?? p,
        startedAt: pendingTool.at,
        endedAt: ev.at,
        createdAt: ev.at,
      });
      pendingTool = null;
    } else if (ev.kind === "finish_rejected") {
      out.push({
        id: ev.seq,
        stepNo: Math.max(1, stepNo),
        kind: "verify",
        name: "verifier",
        attempt: 1,
        status: "rejected",
        tokensIn: 0,
        tokensOut: 0,
        durationMs: 0,
        errorClass: "",
        input: null,
        output: { reason: p.reason ?? "" },
        startedAt: ev.at,
        endedAt: ev.at,
        createdAt: ev.at,
      });
    }
  }
  if (pendingTool) {
    out.push({
      id: pendingTool.seq,
      stepNo: pendingTool.stepNo,
      kind: "tool",
      name: pendingTool.name,
      attempt: 1,
      status: "ok",
      tokensIn: 0,
      tokensOut: 0,
      durationMs: null,
      errorClass: "",
      input: pendingTool.input,
      output: "(等待返回或已暂停)",
      startedAt: pendingTool.at,
      endedAt: pendingTool.at,
      createdAt: pendingTool.at,
    });
  }
  return out;
}

export function RunTraceDialog({ runId }: { runId: string }) {
  const [open, setOpen] = useState(false);
  const [kindFilter, setKindFilter] = useState<string>("all");
  const [expandedId, setExpandedId] = useState<number | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["run-trace", runId],
    queryFn: () => fetchRunTrace(runId),
    enabled: open,
  });

  const needEventFallback = open && !isLoading && (data?.spans?.length ?? 0) === 0;
  const { data: eventsData } = useQuery({
    queryKey: ["run-trace-events-fallback", runId],
    queryFn: () => fetchRunEvents(runId, 0),
    enabled: needEventFallback,
  });

  const derivedMode = (data?.spans?.length ?? 0) === 0 && (eventsData?.events?.length ?? 0) > 0;
  const spans = useMemo(() => {
    if (data?.spans?.length) return data.spans;
    if (eventsData?.events?.length) return deriveSpansFromEvents(eventsData.events);
    return [];
  }, [data?.spans, eventsData?.events]);
  const filtered = useMemo(() => {
    if (kindFilter === "all") return spans;
    if (kindFilter === "abnormal") return spans.filter((s) => s.status !== "ok");
    return spans.filter((s) => s.kind === kindFilter);
  }, [spans, kindFilter]);

  const counts = useMemo(() => {
    const c = { llm: 0, tool: 0, verify: 0, abnormal: 0 };
    for (const s of spans) {
      if (s.kind === "llm") c.llm += 1;
      else if (s.kind === "tool") c.tool += 1;
      else if (s.kind === "verify") c.verify += 1;
      if (s.status !== "ok") c.abnormal += 1;
    }
    return c;
  }, [spans]);

  const downloadJsonl = () => {
    if (!spans.length) return;
    const content = spans.map((s) => JSON.stringify(s)).join("\n") + "\n";
    const blob = new Blob([content], { type: "application/x-ndjson;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${runId}-trace.jsonl`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <Button
        size="sm"
        variant="outline"
        className="h-7 gap-1 px-2 text-micro"
        onClick={() => setOpen(true)}
        title="查看 LLM / Tool / Verify 结构化调用轨迹 (Phase A2)"
      >
        <Activity className="size-3" />
        Trace
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[85vh] overflow-hidden sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex flex-wrap items-center gap-2 text-caption font-semibold">
              <Activity className="size-4 text-brand" />
              <span>结构化执行轨迹 (Structured Trace)</span>
              <code className="rounded bg-muted/60 px-1.5 py-0.5 font-mono text-micro text-muted-foreground">
                {runId}
              </code>
              {data?.specHash ? (
                <code className="rounded bg-muted/40 px-1.5 py-0.5 font-mono text-micro text-muted-foreground">
                  spec #{data.specHash.slice(0, 8)}
                </code>
              ) : null}
              {derivedMode ? (
                <span className="rounded-full bg-muted/60 px-2 py-0.5 text-micro font-normal text-muted-foreground">
                  由历史事件流派生 (A2 前会话)
                </span>
              ) : null}
            </DialogTitle>
          </DialogHeader>

          <div className="flex flex-wrap items-center gap-2 border-b-[0.5px] border-surface-border pb-2 text-micro">
            <div className="inline-flex rounded-md border-[0.5px] border-surface-border bg-muted/40 p-0.5">
              {[
                { id: "all", label: `全部 (${spans.length})` },
                { id: "llm", label: `LLM (${counts.llm})` },
                { id: "tool", label: `Tool (${counts.tool})` },
                { id: "verify", label: `Verify (${counts.verify})` },
                { id: "abnormal", label: `异常/拒绝 (${counts.abnormal})` },
              ].map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => setKindFilter(t.id)}
                  className={`rounded px-2 py-0.5 transition-colors ${
                    kindFilter === t.id
                      ? "bg-background font-medium text-foreground shadow-xs"
                      : "text-muted-foreground"
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>
            <Button
              size="xs"
              variant="outline"
              className="ml-auto gap-1"
              disabled={!spans.length}
              onClick={downloadJsonl}
            >
              <Download className="size-3" />
              导出 trace.jsonl
            </Button>
          </div>

          <div className="max-h-[62vh] space-y-1.5 overflow-y-auto pr-1">
            {isLoading ? (
              <div className="flex items-center justify-center py-8 text-micro text-muted-foreground">
                <Loader2 className="mr-1.5 size-4 animate-spin" />
                正在加载结构化 Trace…
              </div>
            ) : filtered.length === 0 ? (
              <p className="py-8 text-center text-micro text-muted-foreground">
                暂无匹配的 Span 记录
              </p>
            ) : (
              filtered.map((s: TraceSpan) => {
                const kb = KIND_BADGE[s.kind] ?? { label: s.kind, cls: "bg-muted/60 text-muted-foreground" };
                const sb = STATUS_BADGE[s.status] ?? { label: s.status, cls: "bg-muted/60 text-muted-foreground" };
                const isExpanded = expandedId === s.id;
                return (
                  <div
                    key={s.id}
                    className="rounded-lg border-[0.5px] border-surface-border bg-background/60 text-micro"
                  >
                    <button
                      type="button"
                      onClick={() => setExpandedId(isExpanded ? null : s.id)}
                      className="flex w-full flex-wrap items-center gap-1.5 px-2.5 py-1.5 text-left hover:bg-muted/30"
                    >
                      {isExpanded ? (
                        <ChevronDown className="size-3.5 text-muted-foreground" />
                      ) : (
                        <ChevronRight className="size-3.5 text-muted-foreground" />
                      )}
                      <span className="font-mono text-muted-foreground">#{s.stepNo}</span>
                      <span className={`rounded px-1.5 py-0.5 font-mono font-medium ${kb.cls}`}>
                        {kb.label}
                      </span>
                      <code className="font-mono font-semibold text-foreground">{s.name}</code>
                      <span className={`rounded-full px-1.5 py-0.5 font-mono ${sb.cls}`}>
                        {sb.label}
                      </span>
                      {s.attempt > 1 ? (
                        <span className="rounded bg-warning/10 px-1.5 py-0.5 font-mono text-warning">
                          重试 ×{s.attempt}
                        </span>
                      ) : null}
                      {s.errorClass ? (
                        <span className="rounded bg-destructive/10 px-1.5 py-0.5 font-mono text-destructive">
                          {s.errorClass}
                        </span>
                      ) : null}
                      <span className="ml-auto font-mono tabular-nums text-muted-foreground">
                        {s.tokensIn || s.tokensOut ? `${s.tokensIn}→${s.tokensOut} tok · ` : ""}
                        {formatDuration(s.durationMs)}
                      </span>
                    </button>
                    {isExpanded ? (
                      <div className="grid gap-2 border-t-[0.5px] border-surface-border bg-muted/20 p-2.5 md:grid-cols-2">
                        <div className="min-w-0 space-y-1">
                          <p className="font-semibold text-muted-foreground">Input (Blob)</p>
                          <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded border-[0.5px] border-surface-border bg-background p-2 font-mono text-[11px] leading-relaxed">
                            {formatJson(s.input)}
                          </pre>
                        </div>
                        <div className="min-w-0 space-y-1">
                          <p className="font-semibold text-muted-foreground">Output (Blob)</p>
                          <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded border-[0.5px] border-surface-border bg-background p-2 font-mono text-[11px] leading-relaxed">
                            {formatJson(s.output)}
                          </pre>
                        </div>
                      </div>
                    ) : null}
                  </div>
                );
              })
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
