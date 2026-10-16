"use client";

/** 阶段 agent 会话面板(agent_loop 运行时):时间线 + 当前等待项 + 发话/停止。
 * 等待项按类型渲染:要材料(选服务器目录链接 / 上传 zip)、要回答、等 GPU、
 * 基线提案确认。Baseline 页使用;研究阶段复用同一面板。 */

import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle, Bot, CheckCircle2, FileUp, FolderOpen, Hourglass, Loader2,
  MessageSquare, Send, Square, Terminal, User,
} from "lucide-react";
import {
  confirmBaseline, fetchRunEvents, isActive, linkAsset, sendRunMessage, stopRun, uploadAsset,
  type Project, type Run, type RunEvent, type RunPending,
} from "@/lib/api";
import { FolderBrowserDialog } from "@/components/folder-browser";
import { MiniMarkdown } from "@/components/mini-markdown";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";

const STATUS_LABEL: Record<string, { label: string; cls: string }> = {
  running: { label: "工作中", cls: "bg-brand/10 text-brand" },
  waiting_human: { label: "等你回复", cls: "bg-warning/15 text-warning" },
  waiting_jobs: { label: "等 GPU 实验", cls: "bg-info/10 text-info" },
  done: { label: "完成", cls: "bg-success/10 text-success" },
  failed: { label: "失败", cls: "bg-destructive/10 text-destructive" },
  stopped: { label: "已停止", cls: "bg-muted/60 text-muted-foreground" },
};

function brief(value: unknown, max = 120): string {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? "");
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function EventRow({ event }: { event: RunEvent }) {
  const p = event.payload as Record<string, unknown>;
  switch (event.kind) {
    case "assistant":
      return (
        <p className="whitespace-pre-wrap text-caption leading-relaxed">
          <Bot className="mr-1 inline size-3 align-[-2px] text-brand" />{String(p.text ?? "")}
        </p>
      );
    case "owner_message":
      return (
        <p className="rounded-lg border-[0.5px] border-brand/40 bg-brand/10 px-2 py-1 text-caption text-brand">
          <User className="mr-1 inline size-3 align-[-2px]" />你:{String(p.text ?? "")}
        </p>
      );
    case "tool_call": {
      const input = (p.input ?? {}) as Record<string, unknown>;
      const args = Object.entries(input).map(([k, v]) => `${k}=${brief(v, 60)}`).join(" ");
      return (
        <p className="font-mono text-micro text-foreground/90">
          <Terminal className="mr-1 inline size-3 align-[-2px] text-muted-foreground" />
          <span className="font-semibold">{String(p.name ?? "")}</span>
          <span className="ml-1 text-muted-foreground">{args}</span>
        </p>
      );
    }
    case "tool_result":
    case "tool_error":
    case "jobs_done":
      return (
        <pre className={`ml-4 max-h-28 overflow-y-auto whitespace-pre-wrap rounded-lg border-[0.5px] px-2 py-1 font-mono text-micro leading-relaxed ${
          event.kind === "tool_error"
            ? "border-destructive/30 bg-destructive/5 text-destructive"
            : "border-surface-border bg-muted/40 text-muted-foreground"}`}>
          {String(p.output ?? p.error ?? p.summary ?? "").trim() || "(空)"}
        </pre>
      );
    case "finish_rejected":
      return (
        <p className="text-caption text-warning">
          <AlertTriangle className="mr-1 inline size-3 align-[-2px]" />核验未通过:{String(p.reason ?? "")}
        </p>
      );
    case "model_error":
      return <p className="text-micro text-destructive">— 模型调用失败,稍后自动重试:{brief(p.error)} —</p>;
    case "new_context":
      return <p className="text-micro text-muted-foreground/70">— 本轮结束,带着研究日志翻篇进入下一轮 —</p>;
    case "waiting_human":
    case "waiting_jobs":
      return null; // 当前等待项单独展示
    default: {
      const label: Record<string, string> = {
        started: "会话开始", done: "会话完成", stopped: "会话停止", failed: "会话失败",
      };
      return <p className="text-micro text-muted-foreground/70">— {label[event.kind] ?? event.kind} —</p>;
    }
  }
}

/** 要材料:目标路径由 agent 给出,owner 只需给来源(服务器目录 或 zip)。 */
function MaterialSupply({ project, run, pending }: {
  project: Project; run: Run; pending: RunPending;
}) {
  const target = pending.payload.target_path ?? "";
  const [serverPath, setServerPath] = useState("");
  const [zip, setZip] = useState<File | null>(null);
  const [browsing, setBrowsing] = useState(false);
  const [busy, setBusy] = useState<"link" | "upload" | null>(null);
  const queryClient = useQueryClient();

  const supply = async (mode: "link" | "upload") => {
    setBusy(mode);
    try {
      if (mode === "link") await linkAsset(project.id, target, serverPath.trim());
      else if (zip) await uploadAsset(project.id, target, zip);
      const how = mode === "link" ? `链接自服务器目录 ${serverPath.trim()}` : `上传 zip ${zip?.name} 解包`;
      await sendRunMessage(run.id, `已把材料放到 ${target}(${how}),请检查后继续。`);
      toast.success("材料已就位,已通知 Agent");
      void queryClient.invalidateQueries({ queryKey: ["agent-run", run.id] });
    } catch (err) {
      toast.error(`供给失败:${(err as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  if (!target) return null;
  return (
    <div className="mt-2 grid gap-2 md:grid-cols-2">
      <div className="rounded-md border-[0.5px] border-surface-border p-2">
        <p className="text-micro font-medium text-muted-foreground">服务器上已有 → 链接到 {target}</p>
        <div className="mt-1.5 flex gap-1.5">
          <Input value={serverPath} onChange={(e) => setServerPath(e.target.value)}
                 placeholder="/data/..." className="h-7 font-mono text-micro" />
          <Button size="sm" variant="outline" className="h-7 px-2" onClick={() => setBrowsing(true)}>
            <FolderOpen className="size-3" />
          </Button>
          <Button size="sm" className="h-7 gap-1 px-2 text-micro"
                  disabled={busy !== null || !serverPath.trim()} onClick={() => void supply("link")}>
            {busy === "link" ? <Loader2 className="size-3 animate-spin" /> : null}链接
          </Button>
        </div>
        <FolderBrowserDialog open={browsing} onOpenChange={setBrowsing}
          source={{ kind: "server", serverId: project.server.id }}
          title={`选择 ${pending.payload.name ?? "材料"} 所在目录`}
          onPick={(p) => { setServerPath(p); setBrowsing(false); }} />
      </div>
      <div className="rounded-md border-[0.5px] border-surface-border p-2">
        <p className="text-micro font-medium text-muted-foreground">上传 zip 解包到 {target}(≤2 GiB)</p>
        <div className="mt-1.5 flex gap-1.5">
          <Input type="file" accept=".zip" className="h-7 text-micro"
                 onChange={(e) => setZip(e.target.files?.[0] ?? null)} />
          <Button size="sm" className="h-7 gap-1 px-2 text-micro"
                  disabled={busy !== null || !zip} onClick={() => void supply("upload")}>
            {busy === "upload" ? <Loader2 className="size-3 animate-spin" /> : <FileUp className="size-3" />}上传
          </Button>
        </div>
      </div>
    </div>
  );
}

function BaselineProposalCard({ run }: { run: Run }) {
  const proposal = run.pending?.payload.proposal;
  const [busy, setBusy] = useState(false);
  const queryClient = useQueryClient();
  if (!proposal) return null;
  const confirm = async () => {
    setBusy(true);
    try {
      await confirmBaseline(run.id);
      toast.success("基线已确认,项目进入研究阶段");
      void queryClient.invalidateQueries();
    } catch (err) {
      toast.error(`确认失败:${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mt-2 space-y-2">
      <table className="w-full text-caption">
        <thead className="text-micro text-muted-foreground">
          <tr><th className="text-left font-medium">数据集</th><th className="text-left font-medium">指标</th>
            <th className="text-left font-medium">命令</th></tr>
        </thead>
        <tbody>
          {proposal.datasets.map((d) => (
            <tr key={d.name} className="align-top">
              <td className="py-1 pr-2 font-medium">{d.name}</td>
              <td className="py-1 pr-2 font-mono tabular-nums">
                {Object.entries(d.metrics).map(([k, v]) => `${k}=${Number(v).toFixed(4)}`).join("  ")}
              </td>
              <td className="py-1 font-mono text-micro text-muted-foreground">{d.command}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {proposal.condaEnv ? <p className="text-micro text-muted-foreground">环境:{proposal.condaEnv}</p> : null}
      {proposal.notes ? <p className="whitespace-pre-wrap text-caption">{proposal.notes}</p> : null}
      <div className="flex items-center gap-2">
        <Button size="sm" className="gap-1" disabled={busy} onClick={() => void confirm()}>
          {busy ? <Loader2 className="size-3.5 animate-spin" /> : <CheckCircle2 className="size-3.5" />}
          确认基线
        </Button>
        <span className="text-micro text-muted-foreground">不满意就在下方写修改意见发给 Agent</span>
      </div>
    </div>
  );
}

function PendingCard({ project, run }: { project: Project; run: Run }) {
  const pending = run.pending;
  if (!pending) return null;
  if (run.status === "waiting_jobs") {
    return (
      <div className="rounded-lg border-[0.5px] border-info/30 bg-info/5 p-3 text-caption">
        <Hourglass className="mr-1 inline size-3.5 align-[-3px] text-info" />
        等待 GPU 实验结束:<span className="font-mono">{(pending.payload.job_ids ?? []).join(", ")}</span>
        <p className="mt-1 text-micro text-muted-foreground">结束后结果会自动交给 Agent;现在发的话会一起带上。</p>
      </div>
    );
  }
  const kind = pending.payload.kind ?? "info";
  return (
    <div className="rounded-lg border-[0.5px] border-warning/40 bg-warning/5 p-3">
      <p className="whitespace-pre-wrap text-caption font-medium">
        <MessageSquare className="mr-1 inline size-3.5 align-[-3px] text-warning" />
        {pending.payload.question ?? "Agent 在等你回复"}
      </p>
      {pending.payload.why ? (
        <p className="mt-1 text-micro text-muted-foreground">依据:{pending.payload.why}</p>
      ) : null}
      {["dataset", "weights", "file"].includes(kind)
        ? <MaterialSupply project={project} run={run} pending={pending} /> : null}
      {kind === "confirm_baseline" ? <BaselineProposalCard run={run} /> : null}
      {kind === "round_review" && pending.payload.report ? (
        <div className="mt-2 rounded-md border-[0.5px] border-surface-border bg-background p-2 text-caption">
          <MiniMarkdown text={pending.payload.report} />
        </div>
      ) : null}
      {kind === "round_review" ? (
        <p className="mt-2 rounded-md bg-background/70 px-2 py-1.5 text-micro">
          本轮已收束——在下方回话即进下一轮(说「继续」或给新思路);
          点上方「请求停止」则结束本次研究,回到启动界面(结论已留在研究日志)。
        </p>
      ) : null}
      {kind === "info" && pending.payload.question?.includes("网络不可达") ? (
        <p className="mt-2 rounded-md bg-background/70 px-2 py-1.5 text-micro">
          网络自动升级:通常加离线前缀
          <code className="mx-1 rounded bg-muted/60 px-1">export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1</code>
          即可继续;回复处置方式后 Agent 会照做。
        </p>
      ) : null}
    </div>
  );
}

export function RunPanel({ project, run }: { project: Project; run: Run }) {
  const queryClient = useQueryClient();
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const lastSeq = events.length ? events[events.length - 1].seq : 0;

  const { data } = useQuery({
    queryKey: ["agent-run", run.id, lastSeq],
    queryFn: () => fetchRunEvents(run.id, lastSeq),
    refetchInterval: (query) =>
      isActive(query.state.data?.run ?? run) ? 2500 : false,
  });
  useEffect(() => {
    if (data?.events.length) {
      setEvents((prev) => {
        const seen = new Set(prev.map((e) => e.seq));
        return [...prev, ...data.events.filter((e) => !seen.has(e.seq))];
      });
    }
  }, [data]);
  useEffect(() => { setEvents([]); }, [run.id]);
  useEffect(() => { bottomRef.current?.scrollIntoView({ block: "nearest" }); }, [events.length]);

  const current = data?.run ?? run;
  const active = isActive(current);
  const status = STATUS_LABEL[current.status] ?? STATUS_LABEL.stopped;

  const send = async () => {
    if (!text.trim()) return;
    setSending(true);
    try {
      await sendRunMessage(current.id, text.trim());
      setText("");
      void queryClient.invalidateQueries({ queryKey: ["agent-run", current.id] });
    } catch (err) {
      toast.error(`发送失败:${(err as Error).message}`);
    } finally {
      setSending(false);
    }
  };
  const stop = async () => {
    try {
      await stopRun(current.id);
      toast.success("已请求停止");
    } catch (err) {
      toast.error(`停止失败:${(err as Error).message}`);
    }
  };

  return (
    <div className="space-y-3 rounded-xl border-[0.5px] border-surface-border bg-card p-3">
      <div className="flex items-center gap-2">
        <Bot className="size-4 text-brand" />
        <span className="text-caption font-semibold">Agent 会话</span>
        <span className={`rounded-full px-2 py-0.5 text-micro font-medium ${status.cls}`}>{status.label}</span>
        {current.status === "running" ? <Loader2 className="size-3.5 animate-spin text-brand" /> : null}
        <span className="ml-auto font-mono text-micro tabular-nums text-muted-foreground">
          {current.steps} 步 · {Math.round((current.tokensIn + current.tokensOut) / 1000)}k tokens
        </span>
        {active ? (
          <Button size="sm" variant="outline" className="h-7 gap-1 px-2 text-micro" onClick={() => void stop()}>
            <Square className="size-3" />停止
          </Button>
        ) : null}
      </div>
      {current.stopReason ? <p className="text-micro text-muted-foreground">{current.stopReason}</p> : null}

      <div className="max-h-[50vh] space-y-1.5 overflow-y-auto pr-1">
        {events.map((e) => <EventRow key={e.seq} event={e} />)}
        <div ref={bottomRef} />
      </div>

      {active ? <PendingCard project={project} run={current} /> : null}

      {active ? (
        <div className="flex gap-1.5">
          <Input value={text} onChange={(e) => setText(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing) void send(); }}
                 placeholder={current.status === "waiting_human" ? "回复 Agent…" : "给 Agent 插一句话(下一步生效)…"}
                 className="h-8 text-caption" />
          <Button size="sm" className="h-8 gap-1" disabled={sending || !text.trim()} onClick={() => void send()}>
            {sending ? <Loader2 className="size-3.5 animate-spin" /> : <Send className="size-3.5" />}发送
          </Button>
        </div>
      ) : null}
    </div>
  );
}
