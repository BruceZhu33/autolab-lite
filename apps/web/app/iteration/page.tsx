"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Archive, Bot, CheckCircle2, Compass, FileCode, FileText, FlaskConical,
  GitBranch, Lightbulb, Loader2, PauseCircle, PlayCircle, Repeat,
  ShieldCheck, XCircle,
} from "lucide-react";
import {
  advanceProjectStage, createIterationPlan, discardDraftPlan, dismissBacklogItem,
  fetchIterationState, fetchProjects, implementProposalChanges,
  recordPlanConclusion, resolveIterationLoop, startIterationLoop,
  stopIterationLoop, type BacklogItem, type IterationComparison,
  type IterationImplementation, type IterationLoopInfo, type IterationPlanItem,
  type IterationState, type LoopDecision, fetchJobDetail, fetchPlanDetail,
  retryJob,
} from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle, DialogTrigger,
} from "~/vendor/ui/components/ui/dialog";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "@/vendor/views/layout/page-header";
import { MiniMarkdown } from "@/components/mini-markdown";
import { LiveSessionCard } from "@/components/live-session-card";
import { EmptyState } from "@/components/empty-state";

const JOB_TONE: Record<string, string> = {
  QUEUED: "bg-muted text-muted-foreground",
  DISPATCHING: "bg-warning/10 text-warning",
  RUNNING: "bg-warning/10 text-warning",
  CANCELLING: "bg-warning/10 text-warning",
  SUCCEEDED: "bg-success/10 text-success",
  FAILED: "bg-destructive/10 text-destructive",
  CANCELLED: "bg-muted text-muted-foreground",
  TIMED_OUT: "bg-destructive/10 text-destructive",
};

const VERDICT_VIEW: Record<string, { label: string; cls: string }> = {
  improved: { label: "超越基线", cls: "bg-success/10 text-success" },
  not_improved: { label: "未超越基线", cls: "bg-warning/10 text-warning" },
  inconclusive: { label: "证据不足", cls: "bg-muted text-muted-foreground" },
  running: { label: "运行中", cls: "bg-warning/10 text-warning" },
};

const STEPS = ["实现改动", "生成计划", "批准", "执行", "对比基线", "记录结论"] as const;

function fmt(value: unknown): string {
  return typeof value === "number" ? Number(value.toFixed(4)).toString() : String(value);
}

function deltaText(delta: number | null): string {
  if (delta === null || Number.isNaN(delta)) return "—";
  const sign = delta >= 0 ? "+" : "";
  return `${sign}${Number(delta.toFixed(4))}`;
}

/** 迭代流程当前推进到哪一步(0 基)。 */
function currentStep(state: IterationState | undefined): number {
  if (!state) return 0;
  const latest = state.plans.find((p) => p.id === state.latestPlanId);
  if (!latest) return state.implementation ? 1 : 0;
  if (latest.status === "DRAFT") return 2;
  const done = latest.jobs.done === latest.jobs.total && latest.jobs.total > 0;
  if (!done) return 3;
  if (state.conclusion?.conclusion && state.conclusion.conclusion !== "pending") return 5;
  return 4;
}


/** 失败 Job 卡:给出"为什么失败 + 怎么办"——失败不是终态,是待处理。 */
function FailedJobsCard({ projectId, planId }: { projectId: string; planId: string }) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const { data: plan } = useQuery({
    queryKey: ["plan", planId],
    queryFn: () => fetchPlanDetail(planId),
    refetchInterval: 5000,
  });
  const failed = (plan?.jobs ?? []).filter((j) =>
    j.status === "FAILED" || j.status === "TIMED_OUT");
  const firstFailed = failed[0];
  const { data: detail } = useQuery({
    queryKey: ["job-detail", firstFailed?.id ?? ""],
    queryFn: () => fetchJobDetail(firstFailed!.id),
    enabled: !!firstFailed,
  });

  const onRetry = async (jobId: string) => {
    setBusy(jobId);
    try {
      const r = await retryJob(jobId);
      toast.success("已重试:新 Job 已入队", { description: r.newJobId });
      void queryClient.invalidateQueries({ queryKey: ["plan", planId] });
      void queryClient.invalidateQueries({ queryKey: ["iteration", projectId] });
    } catch (e) {
      toast.error("重试失败", { description: (e as Error).message });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mt-2 rounded-lg border border-destructive/30 bg-destructive/5 p-2.5">
      <p className="text-caption font-medium text-destructive">
        {failed.length} 个 Job 失败 —— 先看日志里的报错原因再决定:
      </p>
      <div className="mt-1.5 space-y-1.5">
        {failed.map((j) => (
          <div key={j.id} className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-micro text-muted-foreground">
              #{j.ordinal} exit {j.exit_code ?? "?"}
            </span>
            <span className="min-w-0 flex-1 truncate text-micro text-muted-foreground"
                  title={j.failure_summary ?? ""}>
              {j.failure_summary || "(无失败摘要,看日志)"}
            </span>
            <Link href={`/runs?job=${j.id}`}
                  className="text-micro text-info hover:underline">
              查看日志
            </Link>
            <button type="button"
                    className="rounded-md border border-brand/40 bg-brand/5 px-2 py-0.5 text-micro font-medium text-brand transition-colors hover:bg-brand/15 disabled:opacity-40"
                    disabled={busy !== null}
                    onClick={() => void onRetry(j.id)}>
              {busy === j.id ? "重试中…" : "重试"}
            </button>
          </div>
        ))}
      </div>
      {detail?.diagnosis ? (
        <div className="mt-2 rounded-lg border-[0.5px] border-warning/40 bg-warning/5 p-2">
          <p className="text-caption font-medium text-warning">
            诊断:{detail.diagnosis.title}
          </p>
          <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-micro text-muted-foreground">
            {detail.diagnosis.steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        </div>
      ) : null}
      <p className="mt-1.5 text-micro text-muted-foreground">
        修好原因后点「重试」;或在改代码相位让实现 Agent 修。
      </p>
    </div>
  );
}

function StepStrip({ step }: { step: number }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {STEPS.map((label, i) => (
        <span key={label} className="flex items-center gap-1.5">
          <span className={`flex size-4.5 items-center justify-center rounded-full text-micro font-semibold ${
            i < step ? "bg-success/15 text-success"
            : i === step ? "bg-brand/15 text-brand"
            : "bg-muted/60 text-muted-foreground"}`}>
            {i < step ? <CheckCircle2 className="size-3" /> : i + 1}
          </span>
          <span className={`text-micro ${i === step ? "font-semibold text-foreground" : "text-muted-foreground"}`}>
            {label}
          </span>
          {i < STEPS.length - 1 ? <span className="text-micro text-muted-foreground/50">›</span> : null}
        </span>
      ))}
    </div>
  );
}

/* ---------------------------------------- 左:执行依据(方向/已采纳方案) */

/** 方向的紧凑展示:迭代页的主角是执行,方向只是一条可展开的背景带。 */
function DirectionBand({ state }: { state: IterationState }) {
  const direction = state.direction ?? null;
  if (!direction?.text) return null;
  return (
    <details className="group rounded-xl border-[0.5px] border-surface-border bg-surface">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2">
        <Compass className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="text-caption font-semibold">方向</span>
        {direction.confirmedAt ? (
          <span className="rounded-full bg-success/10 px-1.5 py-0.5 text-micro font-medium text-success">已确认</span>
        ) : (
          <span className="rounded-full bg-warning/10 px-1.5 py-0.5 text-micro font-medium text-warning">草稿</span>
        )}
        <span className="min-w-0 flex-1 truncate text-caption text-muted-foreground">
          {direction.text.replace(/[#*\n]/g, " ").slice(0, 80)}…
        </span>
        {state.baselineMetrics
          ? Object.entries(state.baselineMetrics).slice(0, 3).map(([name, value]) => (
              <span key={name} className="hidden shrink-0 rounded-full bg-info/10 px-2 py-0.5 font-mono text-micro text-info sm:inline">
                {name} {String(value)}
              </span>
            ))
          : null}
        <span className="shrink-0 text-micro text-muted-foreground group-open:hidden">展开</span>
        <span className="hidden shrink-0 text-micro text-muted-foreground group-open:inline">收起</span>
      </summary>
      <div className="max-h-72 overflow-y-auto border-t border-surface-border/60 p-3">
        <MiniMarkdown text={direction.text} />
      </div>
    </details>
  );
}

function ProposalPanel({ state }: { state: IterationState }) {
  const proposal = state.proposal;
  const direction = state.direction ?? null;
  if (!proposal && !direction?.text) return null;
  // D-028:老项目显示已采纳方案,新项目显示已确认的方向陈述
  if (!proposal) {
    return <DirectionBand state={state} />;
  }
  return (
    <section className="flex min-h-0 min-w-0 flex-col rounded-xl border-[0.5px] border-surface-border bg-surface">
      <div className="flex items-center gap-2 border-b border-surface-border px-3 py-2">
        <FileText className="size-4 text-muted-foreground" />
        <h2 className="text-body font-semibold">已采纳方案</h2>
        {proposal.status === "accepted" ? (
          <span className="rounded-full bg-success/10 px-1.5 py-0.5 text-micro font-medium text-success">已通过</span>
        ) : null}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <h3 className="text-body font-semibold leading-snug">{proposal.title}</h3>
        {state.baselineMetrics
          ? (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {Object.entries(state.baselineMetrics).map(([name, value]) => (
                <span key={name} className="rounded-full bg-info/10 px-2 py-0.5 font-mono text-micro text-info">
                  基线 {name}: {String(value)}
                </span>
              ))}
            </div>
          )
          : null}
        {proposal.reportMd ? (
          <div className="mt-2">
            <MiniMarkdown text={proposal.reportMd} />
          </div>
        ) : (
          <p className="mt-2 text-caption text-muted-foreground">
            项目还没有执行依据:回方向页提炼/手写方向并确认后再来制定迭代计划。
          </p>
        )}
      </div>
    </section>
  );
}

/* ---------------------------------------- 自动迭代循环 */

const LOOP_PHASE: Record<string, string> = {
  implement: "改代码", plan: "定计划", await_jobs: "执行实验", conclude: "分析结果",
};
const LOOP_STATUS: Record<string, { label: string; cls: string }> = {
  running: { label: "运行中", cls: "bg-brand/10 text-brand" },
  stopping: { label: "停止中", cls: "bg-warning/10 text-warning" },
  completed: { label: "已完成", cls: "bg-success/10 text-success" },
  stopped: { label: "已停止", cls: "bg-muted text-muted-foreground" },
  failed: { label: "已终止", cls: "bg-destructive/10 text-destructive" },
};

/* ---------------- 等待 owner 决策(D-021 C 期:越包络/越方向) */

function AwaitBanner({ projectId, loop }: {
  projectId: string; loop: IterationLoopInfo;
}) {
  const queryClient = useQueryClient();
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [redirecting, setRedirecting] = useState(false);
  const [newDirection, setNewDirection] = useState("");

  const act = async (action: "resume" | "redirect" | "stop",
                     direction?: string) => {
    setBusyAction(action);
    try {
      await resolveIterationLoop(projectId, { action, direction });
      toast.success(action === "resume" ? "已放行,循环继续"
        : action === "redirect" ? "已改方向并放行" : "已停止");
      void queryClient.invalidateQueries({ queryKey: ["iteration", projectId] });
      setRedirecting(false);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <div className="mt-2 rounded-lg border-[0.5px] border-warning/50 bg-warning/10 p-2.5">
      <p className="flex items-start gap-1.5 text-caption font-medium text-warning">
        <PauseCircle className="mt-0.5 size-3.5 shrink-0" />
        循环暂停,等你决策:{loop.awaitReason}
      </p>
      {redirecting ? (
        <div className="mt-2 space-y-1.5">
          <input
            value={newDirection}
            onChange={(e) => setNewDirection(e.target.value)}
            placeholder="新的大方向(必填):如「仍走配置驱动,聚焦数据采样比」"
            className="w-full rounded-lg border border-input bg-background px-2.5 py-1.5 text-caption outline-none placeholder:text-muted-foreground/60 focus-visible:ring-1 focus-visible:ring-ring"
          />
          <div className="flex gap-1.5">
            <Button size="sm" className="h-7 gap-1 px-2 text-micro"
                    disabled={busyAction !== null || !newDirection.trim()}
                    onClick={() => void act("redirect", newDirection.trim())}>
              {busyAction === "redirect"
                ? <Loader2 className="size-3 animate-spin" /> : null}
              确认改方向并继续
            </Button>
            <Button variant="outline" size="sm" className="h-7 px-2 text-micro"
                    disabled={busyAction !== null} onClick={() => setRedirecting(false)}>
              取消
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-2 flex flex-wrap gap-1.5">
          <Button size="sm" className="h-7 gap-1 px-2 text-micro"
                  disabled={busyAction !== null} onClick={() => void act("resume")}>
            {busyAction === "resume"
              ? <Loader2 className="size-3 animate-spin" /> : <PlayCircle className="size-3" />}
            放行继续(一次性)
          </Button>
          <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-micro"
                  disabled={busyAction !== null} onClick={() => setRedirecting(true)}>
            改方向并继续
          </Button>
          <Button variant="outline" size="sm"
                  className="h-7 gap-1 px-2 text-micro text-destructive hover:text-destructive"
                  disabled={busyAction !== null} onClick={() => void act("stop")}>
            停止循环
          </Button>
        </div>
      )}
      <p className="mt-1.5 text-micro text-muted-foreground">
        放行 = 越包络/越方向仅本次生效;tokens/GPU 包络下轮仍会触发,直到你提高包络或停止。
      </p>
    </div>
  );
}

/* ---------------------------------------- 实验积压(D-020:与 failures.md 对称) */

function BacklogCard({ projectId, backlog }: {
  projectId: string; backlog: BacklogItem[];
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [dismissing, setDismissing] = useState<string | null>(null);
  const proposed = backlog.filter((b) => b.status === "proposed");
  if (backlog.length === 0) return null;

  const dismiss = async (itemId: string) => {
    setDismissing(itemId);
    try {
      await dismissBacklogItem(projectId, itemId);
      void queryClient.invalidateQueries({ queryKey: ["iteration", projectId] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setDismissing(null);
    }
  };

  return (
    <div className="rounded-xl border-[0.5px] border-surface-border bg-background/40">
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left"
              onClick={() => setOpen(!open)}>
        <ChevronRightMark open={open} />
        <Lightbulb className="size-4 shrink-0 text-warning" />
        <span className="min-w-0 flex-1 truncate text-caption font-semibold">
          实验积压(方向内待试,owner 挑选)
        </span>
        <span className="rounded-full bg-muted/60 px-2 py-0.5 text-micro text-muted-foreground">
          {proposed.length} 待试 / 共 {backlog.length}
        </span>
      </button>
      {open ? (
        <div className="space-y-1.5 border-t border-surface-border px-3 py-2">
          {backlog.map((item) => (
            <div key={item.id} className={`flex items-start gap-2 rounded-lg px-2 py-1.5 ${
              item.status === "dismissed" ? "opacity-50" : "bg-muted/30"}`}>
              <div className="min-w-0 flex-1">
                <p className={`text-caption ${item.status === "dismissed" ? "line-through" : "font-medium"}`}>
                  {item.title}
                </p>
                {item.detail ? (
                  <p className="text-micro leading-relaxed text-muted-foreground">{item.detail}</p>
                ) : null}
              </div>
              {item.status === "proposed" ? (
                <Button variant="outline" size="sm"
                        className="h-6 shrink-0 px-1.5 text-micro text-muted-foreground"
                        disabled={dismissing === item.id}
                        onClick={() => void dismiss(item.id)}>
                  {dismissing === item.id ? <Loader2 className="size-3 animate-spin" /> : "撤销"}
                </Button>
              ) : (
                <span className="shrink-0 text-micro text-muted-foreground/60">已撤销</span>
              )}
            </div>
          ))}
          <p className="text-micro text-muted-foreground/60">
            实现 Agent 每轮可附变体提议;想跑哪个,把它的意思写进大方向或反馈即可(不自动执行)。
          </p>
        </div>
      ) : null}
    </div>
  );
}

function ChevronRightMark({ open }: { open: boolean }) {
  return (
    <svg className={`size-3.5 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-90" : ""}`}
         viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="m9 18 6-6-6-6" />
    </svg>
  );
}

/** D-028 本轮思路卡:每轮迭代把「改了什么/为什么/结果/下一轮」四件事
 * 一眼摆出来——思路反馈是迭代的一等公民,不是藏在决策链里的附属品。 */
function RoundThinkingCard({ state }: { state: IterationState }) {
  const impl = state.implementation;
  const decision = state.decisions[0] ?? null;
  const next = decision?.directive?.next_directive ?? null;
  const comparison = state.comparison;
  const rows: Array<{ label: string; content: React.ReactNode }> = [];
  if (impl) {
    const files = (impl.files ?? []).slice(0, 4).map((f) => f.path).join("、");
    rows.push({
      label: "改了什么",
      content: (
        <>
          {impl.summary}
          {files ? <span className="block font-mono text-micro text-muted-foreground">{files}</span> : null}
        </>
      ),
    });
  }
  if (decision) {
    rows.push({
      label: "为什么",
      content: next?.goal ? `${next.goal} —— ${decision.rationale}` : decision.rationale,
    });
  }
  if (comparison) {
    const total = (comparison.succeeded ?? 0) + (comparison.failed ?? 0);
    rows.push({
      label: "结果",
      content: `${comparison.verdict === "improved" ? "超越基线" : comparison.verdict === "not_improved" ? "未超越" : comparison.verdict === "running" ? "运行中" : "证据不足"} · 成功 ${comparison.succeeded ?? "—"}/${total || "—"} · 指标 ${comparison.metricName ?? "—"}`,
    });
  } else if (state.conclusion?.conclusion && state.conclusion.conclusion !== "pending") {
    rows.push({ label: "结果", content: `结论:${state.conclusion.conclusion}` });
  }
  if (next?.approach) {
    rows.push({ label: "下一轮", content: next.approach });
  } else if (state.conclusion?.next_decision) {
    rows.push({ label: "下一步", content: state.conclusion.next_decision });
  }
  if (rows.length === 0) return null;
  const history = state.decisions.slice(1, 5);
  return (
    <section className="mb-3 rounded-xl border-[0.5px] border-brand/25 bg-surface p-3">
      <div className="mb-2 flex items-center gap-2">
        <Lightbulb className="size-4 text-brand" />
        <h2 className="text-caption font-semibold">本轮思路</h2>
        {decision ? (
          <span className="text-micro text-muted-foreground">第 {decision.roundNo} 轮</span>
        ) : null}
      </div>
      <div className="space-y-1.5">
        {rows.map((row) => (
          <div key={row.label} className="flex gap-2 text-caption leading-relaxed">
            <span className="w-14 shrink-0 font-medium text-muted-foreground">{row.label}</span>
            <span className="min-w-0 flex-1">{row.content}</span>
          </div>
        ))}
      </div>
      {history.length > 0 ? (
        <details className="mt-2 border-t border-surface-border/60 pt-1.5">
          <summary className="cursor-pointer text-micro text-muted-foreground">
            前几轮思路({history.length})
          </summary>
          <ul className="mt-1 space-y-1">
            {history.map((d) => (
              <li key={d.id} className="flex gap-2 text-micro text-muted-foreground">
                <span className="shrink-0 font-mono">R{d.roundNo}</span>
                <span className="min-w-0 flex-1 truncate" title={d.rationale}>
                  {d.directive?.next_directive?.goal || d.rationale}
                </span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

function LoopCard({ projectId, loop, decisions, busy, onStop, onRestart,
                    blockingDraft, onDiscardAndRestart }: {
  projectId: string; loop: IterationLoopInfo; decisions: LoopDecision[];
  busy: boolean; onStop: () => void; onRestart: () => void;
  /** 遗留的 DRAFT 迭代计划:终态重启会被它卡住,需要显式批准或丢弃。 */
  blockingDraft: IterationPlanItem | null;
  onDiscardAndRestart: () => void;
}) {
  const status = LOOP_STATUS[loop.status] ?? LOOP_STATUS.stopped;
  const active = loop.status === "running" || loop.status === "stopping";
  const latestDecision = decisions[0] ?? null;
  return (
    <div className={`rounded-xl border-[0.5px] p-3 ${
      loop.status === "completed" ? "border-success/40 bg-success/5"
      : loop.status === "failed" ? "border-destructive/30 bg-destructive/5"
      : "border-brand/30 bg-brand/5"}`}>
      <div className="flex flex-wrap items-center gap-2">
        {active
          ? <Loader2 className="size-4 shrink-0 animate-spin text-brand" />
          : loop.status === "completed"
            ? <CheckCircle2 className="size-4 shrink-0 text-success" />
            : <XCircle className={`size-4 shrink-0 ${loop.status === "failed" ? "text-destructive" : "text-muted-foreground"}`} />}
        <span className="text-caption font-semibold">自动迭代</span>
        <span className={`rounded-full px-2 py-0.5 text-micro font-medium ${status.cls}`}>
          {status.label}
        </span>
        {loop.codeReady ? (
          <span className="rounded-full bg-muted/60 px-2 py-0.5 text-micro font-medium text-muted-foreground">
            改动冻结(实验专用)
          </span>
        ) : null}
        <span className="text-micro text-muted-foreground">
          第 {Math.max(loop.roundNo, loop.status === "running" ? 1 : 0)}
          {loop.maxRounds > 0 ? `/${loop.maxRounds}` : ""}
          轮{loop.maxRounds === 0 ? "(不限)" : ""}
          {active ? ` · ${LOOP_PHASE[loop.phase] ?? loop.phase}` : ""}
          {loop.improvementThreshold && loop.improvementThreshold > 0
            ? ` · 显著阈值 Δ≥${loop.improvementThreshold}` : ""}
          {loop.datasets && loop.datasets.length > 0
            ? ` · 数据集:${loop.datasets.join("、")}` : ""}
          {loop.tokensUsed ? ` · tokens ${loop.tokensUsed}` : ""}
        </span>
        {loop.consecutiveFailures > 0 ? (
          <span className="text-micro text-warning">连续整轮失败 {loop.consecutiveFailures}/{loop.maxConsecutiveFailures}</span>
        ) : null}
        {active ? (
          <Button variant="outline" size="sm" className="ml-auto h-7 gap-1 px-2 text-micro"
                  disabled={busy || loop.status === "stopping"} onClick={onStop}>
            {loop.status === "stopping" ? "停止中…" : "停止"}
          </Button>
        ) : blockingDraft ? (
          // 终态 + 遗留草案:直接重启会被 draft 拒绝,给出两条明确出路
          <span className="ml-auto flex items-center gap-1.5">
            <Link href={`/plans/${blockingDraft.id}`}>
              <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-micro">
                批准草案 →
              </Button>
            </Link>
            <Button size="sm" className="h-7 gap-1 px-2 text-micro"
                    disabled={busy} onClick={onDiscardAndRestart}>
              <Repeat className="size-3" />
              丢弃草案并重启
            </Button>
          </span>
        ) : (
          <Button size="sm" className="ml-auto h-7 gap-1 px-2 text-micro"
                  disabled={busy} onClick={onRestart}>
            <Repeat className="size-3" />
            {loop.status === "failed" ? "重新启动迭代" : "开新一轮迭代"}
          </Button>
        )}
      </div>
      {loop.directionText ? (
        <p className="mt-1.5 text-micro text-muted-foreground">
          <span className="font-medium text-foreground/80">方向(owner 钉定):</span>
          {loop.directionText}
        </p>
      ) : null}
      {latestDecision ? (
        <div className={`mt-2 rounded-lg border-[0.5px] p-2.5 ${
          latestDecision.status === "fallback"
            ? "border-surface-border bg-muted/30"
            : "border-brand/30 bg-background/40"}`}>
          <p className="mb-1 flex flex-wrap items-center gap-1.5 text-micro font-semibold text-muted-foreground">
            <Bot className="size-3 text-brand" />
            第 {latestDecision.roundNo} 轮策略 Directive
            <span className={`rounded-full px-1.5 py-px font-medium ${
              latestDecision.verdict === "stop_dead_end"
                ? "bg-destructive/10 text-destructive"
                : "bg-brand/10 text-brand"}`}>
              {latestDecision.verdict === "stop_dead_end" ? "建议放弃此路"
                : latestDecision.status === "fallback" ? "策略层回落模板" : "继续"}
            </span>
            {!latestDecision.inScope ? (
              <span className="rounded-full bg-warning/10 px-1.5 py-px font-medium text-warning">
                自评越出方向,请复核
              </span>
            ) : null}
            {latestDecision.scopeSignal === "drift" ? (
              <span className="rounded-full bg-warning/10 px-1.5 py-px font-medium text-warning">
                落点漂移信号
              </span>
            ) : null}
            {latestDecision.directive.confidence ? (
              <span className="text-muted-foreground/70">
                置信 {latestDecision.directive.confidence}
              </span>
            ) : null}
          </p>
          {latestDecision.status === "fallback" ? (
            <p className="text-caption leading-relaxed text-muted-foreground">
              策略会话未能产出有效 Directive,下一轮使用固定模板反馈(留痕可考)。
            </p>
          ) : (
            <div className="space-y-0.5">
              {latestDecision.directive.next_directive?.goal ? (
                <p className="text-caption leading-relaxed">
                  <span className="font-medium">下轮假设:</span>
                  {latestDecision.directive.next_directive.goal}
                </p>
              ) : null}
              {latestDecision.rationale ? (
                <p className="text-micro leading-relaxed text-muted-foreground">
                  依据:{latestDecision.rationale}
                </p>
              ) : null}
              {latestDecision.directive.abandon?.length ? (
                <p className="text-micro leading-relaxed text-muted-foreground">
                  已放弃子路径:{latestDecision.directive.abandon.join("、")}
                </p>
              ) : null}
            </div>
          )}
        </div>
      ) : null}
      {loop.awaitingOwner ? (
        <AwaitBanner projectId={projectId} loop={loop} />
      ) : null}
      {loop.stopReason ? (
        <p className={`mt-1.5 text-micro ${
          loop.status === "failed" ? "text-destructive" : "text-muted-foreground"}`}>
          {loop.stopReason}
        </p>
      ) : active ? (
        <p className="mt-1.5 text-micro text-muted-foreground">
          Agent 自主循环:改代码 → 计划(授权内自动批准)→ 执行 → 策略 Directive → 下一轮;
          指标超越基线即停,每轮证据都写入实验文件。
        </p>
      ) : null}
      {!active && loop.summary ? (
        <div className="mt-2 rounded-lg border-[0.5px] border-surface-border bg-background/40 p-2.5">
          <p className="mb-1 text-micro font-semibold text-muted-foreground">
            循环分析(为什么提升 / 为什么没提升)
          </p>
          <p className="whitespace-pre-wrap text-caption leading-relaxed">{loop.summary}</p>
        </div>
      ) : null}
    </div>
  );
}

/* ---------------------------------------- 实现改动面板 */

function ImplementationCard({ impl, onRedo, busy }: {
  impl: IterationImplementation; onRedo: () => void; busy: boolean;
}) {
  const sessionBadges = [
    impl.tool_calls != null ? `工具 ${impl.tool_calls}` : null,
    impl.smoke_runs != null ? `冒烟 ${impl.smoke_runs}` : null,
    (impl.tokens_in ?? 0) + (impl.tokens_out ?? 0) > 0
      ? `tokens ${(impl.tokens_in ?? 0) + (impl.tokens_out ?? 0)}` : null,
  ].filter(Boolean) as string[];
  return (
    <div className="rounded-xl border-[0.5px] border-success/30 bg-success/5 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <CheckCircle2 className="size-4 shrink-0 text-success" />
        <span className="min-w-0 flex-1 text-caption font-semibold">{impl.summary}</span>
        <Button variant="outline" size="sm" className="h-7 px-2 text-micro"
                disabled={busy} onClick={onRedo}>
          重做实现
        </Button>
      </div>
      {sessionBadges.length > 0 ? (
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
          <span className="rounded-full bg-brand/10 px-2 py-0.5 text-micro font-medium text-brand">会话模式</span>
          {sessionBadges.map((b) => (
            <span key={b} className="rounded-full bg-muted/60 px-2 py-0.5 font-mono text-micro text-muted-foreground">{b}</span>
          ))}
        </div>
      ) : null}
      <div className="mt-1.5 space-y-0.5">
        {impl.files.map((f) => (
          <p key={f.path} className="text-micro text-muted-foreground">
            <span className={`mr-1.5 rounded px-1 py-px font-medium ${
              f.action === "create" ? "bg-info/10 text-info" : "bg-warning/10 text-warning"}`}>
              {f.action === "create" ? "新建" : "修改"}
            </span>
            <code className="font-mono">{f.path}</code>
            {f.reason ? <span className="ml-1.5">— {f.reason}</span> : null}
          </p>
        ))}
      </div>
      <p className="mt-1.5 text-micro text-muted-foreground/80">
        原文件已备份(.autolab/patches/),语法检查通过;改动已写入服务器工作区。
      </p>
    </div>
  );
}


function ImplementPanel({ state, impl, loop, busy, onImplement, onGenerate,
                          onStartLoop, onStopLoop }: {
  state: IterationState; impl: IterationImplementation | null;
  loop: IterationLoopInfo | null; busy: string | null;
  onImplement: () => void; onGenerate: () => void;
  onStartLoop: (mode: "collab" | "intermediate" | "heavy", direction: string,
                envelope?: { matrix_scale_cap?: number; gpu_hours?: number },
                codeReady?: boolean) => void;
  onStopLoop: () => void;
}) {
  const [seconds, setSeconds] = useState(0);
  /** 三粒度:collab 人机协作 / intermediate 中级 / heavy 重型(过夜)。 */
  const [mode, setMode] = useState<"collab" | "intermediate" | "heavy">("collab");
  /** D-020:owner 显式钉定的大方向(空=用已采纳方案作为方向边界)。 */
  const [direction, setDirection] = useState("");
  /** D-021:策略包络(0=不设限)。 */
  const [cap, setCap] = useState("");
  const [gpuHours, setGpuHours] = useState("");
  /** D-027:改动冻结——实验专用循环。 */
  const [codeReady, setCodeReady] = useState(false);
  useEffect(() => {
    if (busy !== "implement") return;
    const timer = setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, [busy]);

  const loopActive = loop && (loop.status === "running" || loop.status === "stopping");
  const MODES: Array<{ key: "collab" | "intermediate" | "heavy"; title: string;
                       desc: string; badge: string }> = [
    { key: "collab", title: "人机协作", badge: "2 轮",
      desc: "跑 2 轮就停,给你\"为什么提升/没提升\"的分析,人来决策下一步" },
    { key: "intermediate", title: "中级迭代", badge: "5 轮",
      desc: "自迭代 5 轮才停,结束给完整分析" },
    { key: "heavy", title: "重型迭代", badge: "不限轮",
      desc: "过夜挂机:一直练,只有显著提升(Δ≥0.01)才停;小幅提升记下继续冲" },
  ];

  return (
    <div className="space-y-3">
      {loopActive ? null : (
        <>
          <div className="rounded-xl border-[0.5px] border-brand/30 bg-brand/5 p-3">
            <div className="flex items-center gap-2">
              <Bot className="size-4 shrink-0 text-brand" />
              <span className="min-w-0 flex-1 text-caption font-semibold">
                自动迭代:选一个粒度启动
              </span>
            </div>
            <div className="mt-2 grid gap-1.5 sm:grid-cols-3">
              {MODES.map((m) => (
                <button key={m.key} type="button"
                        onClick={() => setMode(m.key)}
                        className={`rounded-lg border-[0.5px] p-2 text-left transition-colors ${
                          mode === m.key
                            ? "border-brand/60 bg-brand/10"
                            : "border-surface-border bg-background/40 hover:border-brand/40 hover:bg-surface-hover"}`}>
                  <div className="flex items-center gap-1.5">
                    <span className="text-caption font-semibold">{m.title}</span>
                    <span className={`rounded-full px-1.5 py-0.5 text-micro font-medium ${
                      mode === m.key ? "bg-brand/15 text-brand" : "bg-muted/60 text-muted-foreground"}`}>
                      {m.badge}
                    </span>
                  </div>
                  <p className="mt-0.5 text-micro leading-relaxed text-muted-foreground">{m.desc}</p>
                </button>
              ))}
            </div>
            <div className="mt-2 flex items-center gap-2">
              <Button size="sm" className="h-7 gap-1 px-2.5 text-caption"
                      disabled={busy !== null}
                      onClick={() => {
                        const capN = Number(cap), gpuN = Number(gpuHours);
                        onStartLoop(mode, direction.trim(),
                          (capN > 0 || gpuN > 0)
                            ? { ...(capN > 0 ? { matrix_scale_cap: capN } : {}),
                                ...(gpuN > 0 ? { gpu_hours: gpuN } : {}) }
                            : undefined, codeReady);
                      }}>
                {busy === "loop" ? <Loader2 className="size-3.5 animate-spin" /> : <Repeat className="size-3.5" />}
                {busy === "loop" ? "启动中…" : "启动自动迭代"}
              </Button>
              <p className="text-micro text-muted-foreground">
                任何模式结束都会生成分析:为什么提升 / 为什么没提升、下一步建议
              </p>
            </div>
            <input
              value={direction}
              onChange={(e) => setDirection(e.target.value)}
              placeholder="大方向(可选):钉定本轮不逾越的边界,如「沿论文 X 的 semi-supervised 路线,只动 trainers/ 与 configs/」;缺省用已采纳方案"
              className="mt-1.5 w-full rounded-lg border border-input bg-background px-2.5 py-1.5 text-caption outline-none placeholder:text-muted-foreground/60 focus-visible:ring-1 focus-visible:ring-ring"
            />
            <div className="mt-1.5 flex items-center gap-2">
              <span className="shrink-0 text-micro text-muted-foreground">包络(可选)</span>
              <input
                value={cap} onChange={(e) => setCap(e.target.value)}
                inputMode="numeric" placeholder="矩阵上限,如 2"
                className="w-32 rounded-lg border border-input bg-background px-2.5 py-1 text-caption outline-none placeholder:text-muted-foreground/60 focus-visible:ring-1 focus-visible:ring-ring"
              />
              <input
                value={gpuHours} onChange={(e) => setGpuHours(e.target.value)}
                inputMode="decimal" placeholder="GPU 小时,如 1.5"
                className="w-32 rounded-lg border border-input bg-background px-2.5 py-1 text-caption outline-none placeholder:text-muted-foreground/60 focus-visible:ring-1 focus-visible:ring-ring"
              />
              <span className="text-micro text-muted-foreground/60">估算按历史实测;越界暂停等你放行</span>
              <label className="ml-auto flex shrink-0 cursor-pointer items-center gap-1.5 text-micro text-muted-foreground">
                <input type="checkbox" checked={codeReady}
                       onChange={(e) => setCodeReady(e.target.checked)}
                       className="size-3.5 accent-[var(--brand)]" />
                代码已就绪(本轮不改码,实验专用)
              </label>
            </div>
          </div>
          {/* 手动单步收进折叠:循环是主路径,单步实现/制定计划是调试手段,
              平铺与启动卡抢注意力(用户反馈"迭代执行有些冗余") */}
          <details className="rounded-lg border-[0.5px] border-surface-border bg-surface/60">
            <summary className="cursor-pointer list-none px-3 py-2 text-caption font-medium text-muted-foreground">
              手动单步(高级:只实现改动 / 只制定计划)
              {busy === "implement" ? ` · Agent 实现改动中 ${seconds}s…` : ""}
              {busy === "plan" ? " · Agent 制定中…" : ""}
            </summary>
            <div className="space-y-2 border-t border-surface-border/60 px-3 py-2.5">
              <p className="text-micro leading-relaxed text-muted-foreground">
                实现Agent读方向与工作区真实代码,直接修改/新建文件落地(原文件备份到
                .autolab/patches/,写入后语法检查,失败自动回滚);也可基于已改好的代码单发制定计划。
              </p>
              {impl ? <ImplementationCard impl={impl} onRedo={onImplement} busy={busy !== null} /> : null}
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" className="gap-1.5"
                        disabled={busy !== null} onClick={onImplement}>
                  {busy === "implement" ? <Loader2 className="size-3.5 animate-spin" /> : <FileCode className="size-3.5" />}
                  Agent 实现改动
                </Button>
                <Button size="sm" variant="outline" className="gap-1.5"
                        disabled={busy !== null} onClick={onGenerate}>
                  {busy === "plan" ? <Loader2 className="size-3.5 animate-spin" /> : <Bot className="size-3.5" />}
                  Agent 制定迭代计划
                </Button>
              </div>
            </div>
          </details>
        </>
      )}
    </div>
  );
}

/* ---------------------------------------- 记录结论对话框 */

function ConclusionDialog({ state, latest, comparison }: {
  state: IterationState; latest: IterationPlanItem;
  comparison: IterationComparison | null;
}) {
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const queryClient = useQueryClient();
  const defaultConclusion =
    comparison?.verdict === "improved" ? "supported"
    : comparison?.verdict === "not_improved" ? "rejected" : "inconclusive";
  const [conclusion, setConclusion] = useState(defaultConclusion);
  const [nextDecision, setNextDecision] = useState(
    defaultConclusion === "supported" ? "promote" : "iterate");
  const [analysis, setAnalysis] = useState("");
  useEffect(() => {
    if (open) {
      setConclusion(defaultConclusion);
      setNextDecision(defaultConclusion === "supported" ? "promote" : "iterate");
      setAnalysis("");
    }
  }, [open, defaultConclusion]);

  const submit = async () => {
    setSubmitting(true);
    try {
      const result = await recordPlanConclusion(latest.id, {
        conclusion, next_decision: nextDecision,
        analysis: analysis.trim() || undefined,
      });
      toast.success(`实验 ${result.experimentId} 已记录结论:${conclusion}`, {
        description: result.failureKnowledgeWritten
          ? "失败经验已写入 knowledge/failures.md" : undefined,
      });
      setOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["iteration", state.projectId] });
      void queryClient.invalidateQueries({ queryKey: ["board"] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={
        <Button size="sm" className="gap-1.5">
          <FlaskConical className="size-3.5" />记录结论
        </Button>
      } />
      <DialogContent>
        <DialogHeader>
          <DialogTitle>记录实验结论 · {latest.experimentRef.split("/").pop()}</DialogTitle>
          <DialogDescription>
            结论与证据写回实验文件(研究事实);失败经验会追加到 knowledge/failures.md。
            归档到 Done 是独立的下一步。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <label className="block">
            <span className="text-caption font-medium text-muted-foreground">结论</span>
            <select
              value={conclusion}
              onChange={(e) => setConclusion(e.target.value)}
              className="mt-1 w-full rounded-lg border border-input bg-background px-2.5 py-1.5 text-caption outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <option value="supported">supported(假设获得支持)</option>
              <option value="rejected">rejected(被否决)</option>
              <option value="inconclusive">inconclusive(证据不足)</option>
            </select>
          </label>
          <label className="block">
            <span className="text-caption font-medium text-muted-foreground">下一步</span>
            <select
              value={nextDecision}
              onChange={(e) => setNextDecision(e.target.value)}
              className="mt-1 w-full rounded-lg border border-input bg-background px-2.5 py-1.5 text-caption outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <option value="iterate">iterate(继续迭代)</option>
              <option value="promote">promote(推广该结论)</option>
              <option value="stop">stop(停止该方向)</option>
            </select>
          </label>
          <label className="block">
            <span className="text-caption font-medium text-muted-foreground">
              结果分析(留空自动按指标对比生成)
            </span>
            <textarea
              value={analysis} onChange={(e) => setAnalysis(e.target.value)} rows={4}
              placeholder="例如:seed 2/3 的 val_dice 均超过基线,增益稳定……"
              className="mt-1 w-full resize-none rounded-lg border border-input bg-background px-2.5 py-1.5 text-caption leading-relaxed outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => setOpen(false)}>取消</Button>
          <Button size="sm" disabled={submitting} onClick={() => void submit()}>
            {submitting ? <Loader2 className="size-3.5 animate-spin" /> : null}
            写入实验文件
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ---------------------------------------- 右:迭代执行 */

function ComparisonTable({ comparison }: { comparison: IterationComparison }) {
  const joint = comparison.rows.some((r) => r.dataset && r.dataset !== "default");
  return (
    <div className="overflow-x-auto rounded-xl border-[0.5px] border-surface-border">
      <table className="w-full text-caption">
        <thead>
          <tr className="border-b border-surface-border text-left text-micro text-muted-foreground">
            <th className="px-3 py-2 font-medium">#</th>
            {joint ? <th className="px-2 py-2 font-medium">数据集</th> : null}
            <th className="px-2 py-2 font-medium">参数</th>
            <th className="px-2 py-2 font-medium">状态</th>
            <th className="px-2 py-2 font-medium text-right">{comparison.metricName ?? "指标"}</th>
            <th className="px-2 py-2 font-medium text-right">基线</th>
            <th className="px-2 py-2 font-medium text-right">Δ</th>
            <th className="px-3 py-2 font-medium text-center">判定</th>
          </tr>
        </thead>
        <tbody>
          {comparison.rows.map((row) => {
            const params = Object.entries(row.params)
              .filter(([k]) => !joint || (k !== "dataset" && k !== "command"))
              .map(([k, v]) => `${k}=${String(v)}`).join(" ") || "-";
            return (
              <tr key={row.jobId} className="border-b border-surface-border/50 last:border-0">
                <td className="px-3 py-2 tabular-nums">{row.ordinal}</td>
                {joint ? (
                  <td className="px-2 py-2 font-mono text-micro font-medium">{row.dataset}</td>
                ) : null}
                <td className="px-2 py-2 font-mono text-micro">{params}</td>
                <td className="px-2 py-2">
                  <span className={`inline-flex rounded-full px-2 py-0.5 text-micro font-medium ${JOB_TONE[row.status] ?? "bg-muted"}`}>
                    {row.status}
                  </span>
                </td>
                <td className="px-2 py-2 text-right font-mono tabular-nums">{fmt(row.metricValue)}</td>
                <td className="px-2 py-2 text-right font-mono tabular-nums text-muted-foreground">{fmt(row.baseline)}</td>
                <td className={`px-2 py-2 text-right font-mono tabular-nums ${
                  row.delta === null ? "text-muted-foreground"
                  : row.delta > 0 === (comparison.direction !== "minimize") && row.delta !== 0 ? "text-success" : ""
                }`}>{deltaText(row.delta)}</td>
                <td className="px-3 py-2 text-center">
                  {row.improved === null
                    ? <span className="text-micro text-muted-foreground">—</span>
                    : row.improved
                      ? <CheckCircle2 className="mx-auto size-4 text-success" />
                      : <XCircle className="mx-auto size-4 text-muted-foreground" />}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function FlowPanel({ projectId, state }: { projectId: string; state: IterationState }) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [seconds, setSeconds] = useState(0);
  const [newRound, setNewRound] = useState(false);
  /** 联合迭代的数据集选择(null = 确认池全部)。 */
  const [datasets, setDatasets] = useState<string[] | null>(null);
  const latest = state.plans.find((p) => p.id === state.latestPlanId) ?? null;
  const loopActive = state.loop?.status === "running" || state.loop?.status === "stopping";
  const step = currentStep(state);
  const comparison = state.comparison;
  // 新计划出现(新一轮草案生成)即退出"开新一轮"模式
  useEffect(() => { setNewRound(false); }, [state.latestPlanId]);

  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, [busy]);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["iteration", projectId] });
    void queryClient.invalidateQueries({ queryKey: ["board"] });
    void queryClient.invalidateQueries({ queryKey: ["projects"] });
  };

  const implement = async () => {
    setBusy("implement");
    setSeconds(0);
    toast.info("实现 Agent 正在读报告与代码…", {
      description: "修改/新建文件 → 备份 → 语法检查(失败自动回滚),约 1-3 分钟",
      duration: 6000,
    });
    try {
      const result = await implementProposalChanges(projectId);
      toast.success(`已实现方案改动:${result.files.length} 个文件`, {
        description: result.summary,
      });
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const generate = async () => {
    setBusy("plan");
    setSeconds(0);
    toast.info("Agent 正在读评审报告与已实现的改动制定实验计划…", {
      description: "读报告+新代码 → 生成计划 → 写实验文件,约需 1-2 分钟", duration: 6000,
    });
    try {
      const result = await createIterationPlan(projectId, datasets ?? undefined);
      toast.success(`已创建迭代计划:${result.experimentId}(待批准)`, {
        description: `${result.jobsPreview} 个 Job 将在批准后执行${
          result.datasets && result.datasets.length > 1
            ? `(${result.datasets.join(" × ")} 参数矩阵)` : ""}`,
      });
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const startLoop = async (loopMode: "collab" | "intermediate" | "heavy",
                           direction: string,
                           envelope?: { matrix_scale_cap?: number; gpu_hours?: number },
                           codeReady?: boolean) => {
    setBusy("loop");
    try {
      await startIterationLoop(projectId, {
        mode: loopMode, datasets: datasets ?? undefined,
        direction: direction || undefined,
        envelope,
        code_ready: codeReady || undefined,
      });
      const label = loopMode === "heavy" ? "重型迭代(不限轮,显著提升才停)"
        : loopMode === "intermediate" ? "中级迭代(5 轮)" : "人机协作(2 轮)";
      toast.success(`自动迭代已启动:${label}`, {
        description: `Agent 将自主改代码→跑→分析→再改${
          datasets ? `,数据集限定:${datasets.join("、")}` : ""};结束时给出提升/未提升分析`,
        duration: 7000,
      });
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const stopLoop = async () => {
    setBusy("loopstop");
    try {
      await stopIterationLoop(projectId);
      toast.info("已请求停止:执行中的轮跑完并记录结论后停止");
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  // 终态重启的清障:遗留 DRAFT 会挡住 loop/start,丢弃(→ABANDONED)后直接开新一轮
  const blockingDraft =
    state.plans.find((p) => p.status === "DRAFT") ?? null;

  const discardBlockingDraft = async () => {
    if (!blockingDraft) return;
    setBusy("discard-draft");
    try {
      await discardDraftPlan(blockingDraft.id);
      toast.success("已丢弃遗留草案,可重新启动迭代");
      refresh();
      setNewRound(true);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const autoConclude = async () => {
    if (!latest) return;
    setBusy("conclude");
    toast.info("Agent 正在总结本轮实验…", { description: "结论+证据写回实验文件,改进则直接归档", duration: 5000 });
    try {
      const result = await recordPlanConclusion(latest.id, { auto: true });
      toast.success(`已记录结论:${result.conclusion}${result.archived ? " 并归档" : ""}`, {
        description: result.analysis.slice(0, 120),
        duration: 8000,
      });
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const archive = async () => {
    setBusy("archive");
    try {
      await advanceProjectStage(projectId, "done", "confirm");
      toast.success("已归档到 Done");
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="flex min-h-0 min-w-0 flex-col rounded-xl border-[0.5px] border-surface-border bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-surface-border px-3 py-2">
        <div className="flex items-center gap-2">
          <Repeat className="size-4 text-muted-foreground" />
          <h2 className="text-body font-semibold">迭代执行</h2>
        </div>
        <StepStrip step={step} />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {/* 循环状态卡:只要存在循环(进行中/已完成/已终止)就常显 */}
        {state.loop ? (
          <div className="mb-3">
            <LoopCard projectId={projectId} loop={state.loop}
                      decisions={state.decisions ?? []}
                      busy={busy !== null} onStop={() => void stopLoop()}
                      blockingDraft={blockingDraft}
                      onDiscardAndRestart={() => void discardBlockingDraft()}
                      onRestart={() => {
                        setNewRound(true);
                        window.scrollTo({ top: 0, behavior: "smooth" });
                      }} />
          </div>
        ) : null}
        {/* D-028 本轮思路卡:改了什么/为什么/结果/下一轮,一等公民 */}
        <div className="mb-3">
          <RoundThinkingCard state={state} />
        </div>
        {/* 迭代启动区:无活动循环时始终显示(方向或方案任一即过门)——
            修复"三粒度不见了":原实现被 proposal.reportMd(老评审流程字段)
            门住,方向流程项目整段不渲染;且仅在无计划/开新一轮时出现,
            终态后入口深藏。现在终态即"换思路继续"的一等入口。 */}
        {!loopActive && (state.direction?.text || state.proposal) ? (
          <div className="mb-3 space-y-2.5">
            {/* 数据集选择:基线确认池,跑过什么才能迭代什么(owner 核心关切) */}
            <div className="flex flex-wrap items-center gap-1.5 rounded-lg border-[0.5px] border-surface-border bg-surface px-2.5 py-2">
              <span className="shrink-0 text-micro font-medium text-muted-foreground">
                迭代数据集(基线跑通的才能选)
              </span>
              {state.datasetPool.map((d) => {
                const selected = (datasets ?? state.datasetPool.map((x) => x.name)).includes(d.name);
                return (
                  <button key={d.name} type="button"
                          disabled={busy !== null || loopActive}
                          onClick={() => {
                            const all = datasets ?? state.datasetPool.map((x) => x.name);
                            const next = selected ? all.filter((n) => n !== d.name) : [...all, d.name];
                            setDatasets(next.length ? next : all);
                          }}
                          title={d.metrics ? Object.entries(d.metrics).map(([k, v]) => `${k}=${String(v)}`).join(" ") : undefined}
                          className={`rounded-full px-2.5 py-1 font-mono text-micro font-medium transition-colors ${
                            selected ? "bg-accent text-accent-foreground"
                              : "bg-muted/50 text-muted-foreground hover:bg-muted"}`}>
                    {d.name}
                  </button>
                );
              })}
              <span className="ml-auto text-micro text-muted-foreground">
                {datasets ? `${datasets.length}/${state.datasetPool.length} 选中` : "全部"}
              </span>
            </div>
            {state.loop ? (
              <p className="text-micro font-medium text-muted-foreground">
                第 {state.plans.length + 1} 轮 · 上轮终止:{state.loop.stopReason?.slice(0, 40) ?? "已完成"}
                ——方向是大框架,换思路继续(实现 Agent 会带上轮结论与失败经验)
              </p>
            ) : null}
            <ImplementPanel state={state} impl={state.implementation} loop={state.loop}
                            busy={busy} onImplement={() => void implement()}
                            onGenerate={() => void generate()}
                            onStartLoop={(m, d, env, cr) => void startLoop(m, d, env, cr)}
                            onStopLoop={() => void stopLoop()} />
          </div>
        ) : null}
        {/* Agent 会话实时面板:看着 Agent 干活(工具时间线),可注入引导/停止 */}
        <div className="mb-3">
          <LiveSessionCard projectId={projectId} />
        </div>
        {/* 实验积压:实现 Agent 的变体提议(D-020),owner 挑选 */}
        <div className="mb-3">
          <BacklogCard projectId={projectId} backlog={state.backlog ?? []} />
        </div>
        {(!latest || newRound) && !(state.direction?.text || state.proposal) ? (
          <div className="mb-3 flex flex-col items-center justify-center py-10 text-center">
            <p className="max-w-sm text-caption text-muted-foreground">
              项目还没有执行依据:先回方向页提炼/手写方向并确认,实现 Agent 依它改代码。
            </p>
          </div>
        ) : null}

        {latest ? (
          <div className="space-y-3">
            {/* 计划卡 */}
            <div className="rounded-xl border-[0.5px] border-surface-border bg-background/40 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className={`rounded-full px-2 py-0.5 text-micro font-medium ${JOB_TONE[latest.status] ?? "bg-muted"}`}>
                  {latest.status}
                </span>
                <span className="min-w-0 flex-1 truncate text-caption font-semibold">
                  {latest.summary || `revision ${latest.revisionNo}`}
                </span>
                <Link href={`/plans/${latest.id}`} className="text-micro text-info hover:underline">
                  Plan 页 {latest.status === "DRAFT" ? "(去批准)" : "›"}
                </Link>
              </div>
              <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-micro text-muted-foreground">
                <span className="font-mono">{latest.experimentRef}</span>
                <span>spec {latest.specHash.slice(0, 10)}…</span>
                <span>Job {latest.jobs.done}/{latest.jobs.total}{latest.jobs.running ? ` · ${latest.jobs.running} 运行中` : ""}</span>
                {latest.jobs.failed > 0 ? (
                  <span className="font-medium text-destructive">· {latest.jobs.failed} 个失败</span>
                ) : null}
              </div>
              {latest.jobs.failed > 0 && !latest.jobs.running ? (
                <FailedJobsCard key={latest.id} projectId={state.projectId}
                                planId={latest.id} />
              ) : null}
            </div>

            {/* ②/③ 执行与对比 */}
            {comparison ? <ComparisonTable comparison={comparison} /> : null}

            {/* ④ 对比结论横幅 */}
            {comparison && comparison.allTerminal ? (
              <div className={`flex flex-wrap items-center gap-2 rounded-xl px-3 py-2 ${
                comparison.verdict === "improved" ? "bg-success/10"
                : comparison.verdict === "not_improved" ? "bg-warning/10"
                : "bg-muted/50"}`}>
                <span className={`rounded-full px-2 py-0.5 text-micro font-semibold ${
                  (VERDICT_VIEW[comparison.verdict] ?? VERDICT_VIEW.inconclusive).cls}`}>
                  {(VERDICT_VIEW[comparison.verdict] ?? VERDICT_VIEW.inconclusive).label}
                </span>
                <span className="min-w-0 flex-1 text-caption text-muted-foreground">
                  {comparison.succeeded}/{comparison.rows.length} 成功;
                  主指标 {comparison.metricName}(方向 {comparison.direction}),
                  基线 {fmt(comparison.baselineValue)};
                  {comparison.anyImproved ? "至少 1 个配置超越基线" : "没有配置超越基线"}
                </span>
              </div>
            ) : comparison ? (
              <p className="text-caption text-muted-foreground">
                执行中:{comparison.rows.filter((r) => r.status === "RUNNING").length} 运行 /
                {comparison.rows.length} 总数;指标在 Job 终态后回填。
              </p>
            ) : latest.status === "DRAFT" ? (
              <p className="text-caption text-muted-foreground">
                草案待批准:到 Plan 页核对命令模板/矩阵/超时后,粘贴 spec hash 批准。
                批准后 Job 自动派发到项目所在服务器。
              </p>
            ) : null}

            {/* ⑤ 结论与归档 */}
            {comparison && comparison.allTerminal ? (
              state.conclusion?.conclusion && state.conclusion.conclusion !== "pending" ? (
                <div className="flex flex-wrap items-center gap-2 rounded-xl bg-muted/40 px-3 py-2">
                  <FlaskConical className="size-4 text-muted-foreground" />
                  <span className="text-caption">
                    已记录结论:<span className="font-semibold">{state.conclusion.conclusion}</span>
                    {state.conclusion.next_decision
                      ? <span className="text-muted-foreground">(next: {state.conclusion.next_decision})</span> : null}
                  </span>
                  <span className="ml-auto flex gap-2">
                    {state.stage === "confirm" ? (
                      <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-caption"
                              disabled={busy !== null} onClick={() => void archive()}>
                        {busy === "archive" ? <Loader2 className="size-3 animate-spin" /> : <Archive className="size-3" />}
                        归档到 Done
                      </Button>
                    ) : state.stage === "done" ? (
                      <span className="text-micro text-success">已归档 ✓</span>
                    ) : null}
                    <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-caption"
                            disabled={busy !== null || newRound}
                            onClick={() => setNewRound(true)}>
                      <Repeat className="size-3" />开新一轮迭代
                    </Button>
                    <Link href="/review">
                      <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-caption">
                        <ShieldCheck className="size-3" />回方向(下一轮)
                      </Button>
                    </Link>
                  </span>
                </div>
              ) : (
                <div className="flex items-center justify-between gap-2">
                  {comparison.verdict === "improved" ? (
                    <>
                      <p className="min-w-0 flex-1 text-caption text-muted-foreground">
                        已超越基线:一键让 Agent 总结结论(证据写回实验文件)并直接归档,无需手动确认。
                      </p>
                      <Button size="sm" className="gap-1.5"
                              disabled={busy !== null} onClick={() => void autoConclude()}>
                        {busy === "conclude"
                          ? <Loader2 className="size-3.5 animate-spin" />
                          : <CheckCircle2 className="size-3.5" />}
                        {busy === "conclude" ? "Agent 总结中…" : "Agent 总结并归档"}
                      </Button>
                    </>
                  ) : (
                    <>
                      <p className="text-caption text-muted-foreground">
                        实验已全部终态:对照上表记录结论(证据会引用本轮 Job 与指标)。
                      </p>
                      <ConclusionDialog state={state} latest={latest} comparison={comparison} />
                    </>
                  )}
                </div>
              )
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}

/* ---------------------------------------- 页面 */

export default function IterationPage() {
  const { data: projects = [] } = useQuery({
    queryKey: ["projects"],
    queryFn: fetchProjects,
    refetchInterval: 8000,
  });
  // 只看已采纳方案的项目(迭代以评审报告为输入)
  const candidates = projects.filter((p) => p.stage === "review" || p.stage === "iterate"
    || p.stage === "prepare" || p.stage === "confirm" || p.stage === "done");
  const [selectedProjectId, setSelectedProjectId] = useState("");
  // 看板卡片跳转带 ?project= 预选
  useEffect(() => {
    const from = new URLSearchParams(window.location.search).get("project");
    if (from) setSelectedProjectId(from);
  }, []);
  const selected = candidates.find((p) => p.id === selectedProjectId) ?? candidates[0];
  const { data: state } = useQuery({
    queryKey: ["iteration", selected?.id ?? ""],
    queryFn: () => fetchIterationState(selected!.id),
    enabled: !!selected,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return false;
      if (data.loop && (data.loop.status === "running" || data.loop.status === "stopping")) {
        return 4000;
      }
      const latest = data.plans.find((p) => p.id === data.latestPlanId);
      if (!latest || latest.status === "DRAFT") return false;
      if (latest.jobs.done < latest.jobs.total) return 4000;
      return data.conclusion?.conclusion && data.conclusion.conclusion !== "pending"
        ? false : 8000;
    },
  });
  // 执行依据双门:老项目已采纳方案,或新项目方向已确认(D-028)
  const acceptedOnly = (state
    ? state.proposal !== null || !!state.direction?.confirmedAt
    : false);

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <Repeat className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">自动迭代</h1>
        {candidates.length > 1 ? (
          <div className="ml-1 flex gap-1">
            {candidates.map((p) => (
              <button key={p.id} type="button" onClick={() => setSelectedProjectId(p.id)}
                      className={`rounded-full px-2 py-0.5 text-caption font-medium ${
                        p.id === selected?.id ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-muted"}`}>
                {p.name}
              </button>
            ))}
          </div>
        ) : selected ? (
          <span className="ml-1 text-caption text-muted-foreground">{selected.name}</span>
        ) : null}
      </PageHeader>

      {candidates.length === 0 ? (
        <div className={`${PAGE_GUTTER} flex flex-1`}>
          <EmptyState
            icon={Repeat}
            title="还没有可迭代的项目"
            description="方向已确立(或老项目方案已采纳)的项目会出现在这里,Agent 依据方向制定迭代实验计划。"
            action={
              <Link href="/review">
                <Button variant="outline" size="sm" className="gap-1.5">
                  <Compass className="size-3.5" />
                  去确立方向
                </Button>
              </Link>
            }
          />
        </div>
      ) : selected ? (
        <div className={`${PAGE_GUTTER} flex min-h-0 flex-1 flex-col`}>
          {!acceptedOnly ? (
            <div className={`${PAGE_RAIL} flex flex-1 flex-col`}>
              <EmptyState
                icon={Repeat}
                title={`${selected.name} 还没有确立方向`}
                description="完成 Baseline 后,在方向页从论文与代码提炼研究方向并确认,即可回到这里开迭代。"
                action={
                  selected.planId ? (
                    <Link href={`/plans/${selected.planId}`}>
                      <Button variant="outline" size="sm" className="gap-1.5">
                        <GitBranch className="size-3.5" />
                        批准实验计划草案(Plan 页)
                      </Button>
                    </Link>
                  ) : (
                    <Link href="/review">
                      <Button variant="outline" size="sm" className="gap-1.5">
                        <Compass className="size-3.5" />
                        去确立方向
                      </Button>
                    </Link>
                  )
                }
              />
            </div>
          ) : state ? (
            /* 聚焦迭代执行:方向/方案收成一条可展开的背景带,不再占左栏 */
            <div className="flex min-h-0 flex-1 flex-col gap-3 py-3">
              <ProposalPanel state={state} />
              <FlowPanel projectId={selected.id} state={state} />
            </div>
          ) : (
            <div className="flex flex-1 items-center justify-center text-caption text-muted-foreground">
              <Loader2 className="mr-2 size-4 animate-spin" />加载迭代状态…
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
