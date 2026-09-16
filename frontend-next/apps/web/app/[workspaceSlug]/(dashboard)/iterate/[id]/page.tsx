"use client";

/**
 * 自动迭代工作区（迭代模块核心）——评审定稿后的自主训练现场。
 *
 * 交互模型：AutoLab 全自动闭环（提案→JUDGE→冒烟→派发→评测→采纳/回滚），
 * 用户在此监督与干预——左对话（轮次汇报/指令/暂停停止），右执行面板：
 * 迭代状态（最佳 vs BASE）、轮次表、指标趋势、预算（C+R≤B）、
 * 停滞「需要你」、终止后「进入最终确认」过门。
 * Phase 2 对接 sidecar llm_loop / runs / evaluator 域。
 */
import { use, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Send, Zap } from "lucide-react";
import { usePathname } from "next/navigation";
import { toast } from "sonner";

import { Badge } from "@multica/ui/components/ui/badge";
import { Button } from "@multica/ui/components/ui/button";
import { Input } from "@multica/ui/components/ui/input";
import { PageHeader } from "@multica/views/layout/page-header";
import { StageTeamBar } from "@/features/autolab/stage-team";
import { stageColor, stageLabel } from "@/features/autolab/stage";

interface ProjectView {
  id: string;
  identifier: string;
  title: string;
  stage: string;
  direction: string;
}

interface RoundView {
  round: number;
  direction: string;
  startedAt: string;
  finishAt: string;
  metric: number | null;
  delta: number | null;
  adopted: boolean | null;
  note: string;
}

interface MsgView {
  id: string;
  role: "user" | "agent";
  content: string;
  createdAt: string;
}

interface WorkshopView {
  projectId: string;
  baseline: { avg: number; dataset: string };
  rounds: RoundView[];
  status: "idle" | "running" | "paused" | "stalled" | "concluded";
  budget: { totalH: number; usedH: number };
  blocker: { title: string; reason: string; actions: string[] } | null;
  messages: MsgView[];
}

async function fixtureApi<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const e = (await r.json().catch(() => ({}))) as { error?: string };
    throw new Error(typeof e.error === "string" ? e.error : `HTTP ${r.status}`);
  }
  return r.json() as Promise<T>;
}

function roundDone(r: RoundView): boolean {
  return Date.now() >= new Date(r.finishAt).getTime();
}

const STATUS_VIEW: Record<string, { label: string; cls: string }> = {
  idle: { label: "未开始", cls: "text-muted-foreground" },
  running: { label: "迭代中", cls: "text-emerald-600" },
  paused: { label: "已暂停", cls: "text-amber-600" },
  stalled: { label: "停滞 · 需要你", cls: "text-destructive" },
  concluded: { label: "已终止", cls: "text-primary" },
};

export default function IterateWorkshopPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const qc = useQueryClient();
  const pathname = usePathname();
  const [draft, setDraft] = useState("");
  const [typing, setTyping] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const { data: project } = useQuery({
    queryKey: ["autolab", "fixture-projects"],
    queryFn: () => fixtureApi<{ projects: ProjectView[] }>("GET", "/api/projects"),
    select: (d) => d.projects.find((p) => p.id === id || p.identifier === id) ?? null,
  });

  const { data: ws } = useQuery({
    queryKey: ["autolab", "iteration-workshop", id],
    queryFn: () => fixtureApi<WorkshopView>("GET", `/api/projects/${id}/iteration-workshop`),
    refetchInterval: 5000,
    refetchIntervalInBackground: true,
  });

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [ws?.messages.length, typing]);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["autolab", "iteration-workshop", id] });
    void qc.invalidateQueries({ queryKey: ["autolab", "fixture-projects"] });
  };

  const send = useMutation({
    mutationFn: (content: string) =>
      fixtureApi<MsgView>("POST", `/api/projects/${id}/iteration-workshop/messages`, { content }),
    onMutate: () => setTyping(true),
    onSuccess: () => {
      setDraft("");
      window.setTimeout(() => {
        setTyping(false);
        refresh();
      }, 900);
    },
    onError: (e: Error) => {
      setTyping(false);
      toast.error("发送失败", { description: e.message.slice(0, 200) });
    },
  });

  if (!project) {
    return (
      <>
        <PageHeader>
          <Zap className="size-4 text-muted-foreground" />
          <h1 className="text-body font-medium">自动迭代</h1>
        </PageHeader>
        <div className="p-4 md:px-6">
          <p className="text-sm text-muted-foreground">项目不存在或已被删除。</p>
        </div>
      </>
    );
  }

  const doneRounds = (ws?.rounds ?? []).filter(roundDone);
  const best = ws
    ? Math.max(ws.baseline.avg, ...doneRounds.filter((r) => r.adopted).map((r) => r.metric ?? 0))
    : 0;
  const bestDelta = ws ? best - ws.baseline.avg : 0;
  const budgetPct = ws ? Math.min(100, (ws.budget.usedH / ws.budget.totalH) * 100) : 0;
  const statusView = STATUS_VIEW[ws?.status ?? "idle"] ?? STATUS_VIEW.idle!;
  const canConfirm = ws?.status === "concluded" && project.stage === "iterate";

  const submit = () => {
    const content = draft.trim();
    if (!content) return;
    send.mutate(content);
  };

  return (
    <>
      <PageHeader>
        <Zap className="size-4 text-muted-foreground" />
        <h1 className="text-body font-medium">自动迭代</h1>
        <span className="font-mono text-caption text-muted-foreground">{project.identifier}</span>
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          {project.title}
        </p>
        <StageTeamBar stage="iterate" />
        <div className="ml-auto">
          <Badge variant="outline" className="gap-1.5">
            <span className="size-1.5 rounded-full" style={{ backgroundColor: stageColor(project.stage) }} />
            {stageLabel(project.stage)}
          </Badge>
        </div>
      </PageHeader>

      <div className="flex min-h-0 flex-1 gap-4 p-4 md:px-6">
        {/* 左：对话（轮次汇报 / 指令 / 控制） */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col rounded-lg border">
          <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
            与 AutoLab 协调 Agent 对话 · 它在闭环里汇报每轮结果，接受你的指令
          </p>

          <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto p-4">
            {(ws?.messages ?? []).map((m) =>
              m.role === "user" ? (
                <div key={m.id} className="flex justify-end">
                  <p className="max-w-[85%] whitespace-pre-wrap break-words rounded-lg bg-primary/10 px-3 py-2 text-sm">
                    {m.content}
                  </p>
                </div>
              ) : (
                <div key={m.id} className="flex gap-2.5">
                  <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-caption font-medium">A</span>
                  <div className="min-w-0 flex-1">
                    <p className="flex items-baseline gap-2">
                      <span className="text-caption font-medium">AutoLab</span>
                      <span className="rounded bg-muted px-1 text-[10px] leading-4 text-muted-foreground">执行 Agent</span>
                    </p>
                    <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed">{m.content}</p>
                  </div>
                </div>
              ),
            )}
            {typing && (
              <div className="flex gap-2.5">
                <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-caption font-medium">A</span>
                <p className="flex items-center gap-1 pt-2 text-muted-foreground">
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60" />
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:120ms]" />
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:240ms]" />
                </p>
              </div>
            )}
          </div>

          <div className="flex items-center gap-2 border-t p-3">
            <Input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.nativeEvent.isComposing) submit();
              }}
              placeholder="说：开始迭代 / 进度怎么样 / 下一轮试试 mixup / 暂停 / 停止 / 进入最终确认…"
            />
            <Button size="icon-sm" onClick={submit} disabled={send.isPending || !draft.trim()} aria-label="发送">
              <Send className="size-4" />
            </Button>
          </div>
        </div>

        {/* 右：执行状态面板 */}
        <div className="hidden w-96 shrink-0 flex-col gap-4 overflow-y-auto lg:flex">
          {/* 迭代状态 */}
          <div className="rounded-lg border">
            <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">迭代状态</p>
            <div className="space-y-2 px-4 py-3 text-sm">
              <p className="flex items-center gap-2">
                <span className={`size-1.5 rounded-full ${ws?.status === "running" ? "animate-pulse bg-emerald-500" : ws?.status === "stalled" ? "bg-destructive" : "bg-muted-foreground/50"}`} />
                <span className={statusView.cls}>{statusView.label}</span>
                <span className="ml-auto text-caption text-muted-foreground">
                  {doneRounds.length}/{ws?.rounds.length ?? 0} 轮完成
                </span>
              </p>
              {ws && (
                <p className="flex items-baseline gap-2">
                  <span className="text-caption text-muted-foreground">最佳</span>
                  <span className="font-mono text-lg font-semibold tabular-nums">{best.toFixed(4)}</span>
                  <span className={`font-mono text-caption tabular-nums ${bestDelta >= 0 ? "text-emerald-600" : "text-destructive"}`}>
                    {bestDelta >= 0 ? "+" : ""}{bestDelta.toFixed(4)} vs BASE {ws.baseline.avg.toFixed(4)}
                  </span>
                </p>
              )}
              {ws && (
                <p className="flex items-center gap-2">
                  <span className="text-caption text-muted-foreground">预算</span>
                  <span className="font-mono text-caption tabular-nums">{ws.budget.usedH}/{ws.budget.totalH} GPU·h</span>
                  <span className="inline-flex h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                    <span className="h-full rounded-full bg-primary" style={{ width: `${budgetPct}%` }} />
                  </span>
                </p>
              )}
            </div>
          </div>

          {/* 停滞「需要你」 */}
          {ws?.blocker && (
            <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-4">
              <p className="flex items-center gap-2 text-sm font-medium text-destructive">
                <span className="size-1.5 rounded-full bg-destructive" />
                需要你 · {ws.blocker.title}
              </p>
              <p className="mt-1 text-caption leading-relaxed text-muted-foreground">{ws.blocker.reason}</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {ws.blocker.actions.map((a) => (
                  <Button key={a} size="sm" variant="outline" disabled title="Phase 2 接 sidecar 恢复端点后启用">
                    {a}
                  </Button>
                ))}
              </div>
            </div>
          )}

          {/* 轮次表 */}
          <div className="rounded-lg border">
            <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
              轮次（提案 → JUDGE → 冒烟 → 派发 → 评测）
            </p>
            {(ws?.rounds ?? []).length === 0 ? (
              <p className="px-4 py-3 text-caption text-muted-foreground">未开始。</p>
            ) : (
              <div className="divide-y">
                {(ws?.rounds ?? []).slice().reverse().map((r) => {
                  const done = roundDone(r);
                  const isBest = done && r.adopted && r.metric === best;
                  return (
                    <div key={r.round} className="space-y-1 px-4 py-2.5 text-sm">
                      <p className="flex items-center gap-2">
                        <span className="font-mono text-caption">#{r.round}</span>
                        <span className="min-w-0 flex-1 truncate text-caption text-muted-foreground">{r.direction}</span>
                        {done ? (
                          isBest ? (
                            <Badge variant="default" className="bg-emerald-600">最佳</Badge>
                          ) : r.adopted ? (
                            <Badge variant="secondary">采纳</Badge>
                          ) : (
                            <Badge variant="outline" className="text-muted-foreground">回滚</Badge>
                          )
                        ) : (
                          <Badge variant="secondary" className="gap-1">
                            <span className="size-1.5 animate-pulse rounded-full bg-amber-500" />
                            进行中
                          </Badge>
                        )}
                      </p>
                      <p className="flex items-baseline gap-2 font-mono text-caption tabular-nums">
                        {done ? (
                          <>
                            <span className={r.adopted ? "text-foreground" : "text-muted-foreground"}>
                              {r.metric?.toFixed(3)}
                            </span>
                            <span className={(r.delta ?? 0) >= 0 ? "text-emerald-600" : "text-destructive"}>
                              {(r.delta ?? 0) >= 0 ? "+" : ""}{r.delta?.toFixed(4)}
                            </span>
                          </>
                        ) : (
                          <span className="text-muted-foreground">评测待出…</span>
                        )}
                      </p>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* 指标趋势 */}
          {ws && doneRounds.length > 0 && (
            <div className="rounded-lg border">
              <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">指标趋势</p>
              <div className="px-4 py-3">
                <div className="flex items-end gap-1.5">
                  {[
                    { v: ws.baseline.avg, label: "BASE", hit: false },
                    ...doneRounds.map((r) => ({ v: r.metric ?? ws.baseline.avg, label: `#${r.round}`, hit: r.metric === best })),
                  ].map((p, i) => {
                    const min = ws.baseline.avg - 0.01;
                    const max = best + 0.005;
                    const h = 12 + ((p.v - min) / Math.max(0.001, max - min)) * 36;
                    return (
                      <div key={i} className="flex flex-col items-center gap-1">
                        <span className="font-mono text-[10px] tabular-nums text-muted-foreground">{p.v.toFixed(3)}</span>
                        <span
                          className={`w-5 rounded-sm ${p.hit ? "bg-emerald-500" : i === 0 ? "bg-muted-foreground/40" : "bg-primary/60"}`}
                          style={{ height: h }}
                        />
                        <span className="font-mono text-[10px] text-muted-foreground">{p.label}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          )}

          {/* 终止 → 最终确认 */}
          {canConfirm && (
            <div className="rounded-lg border">
              <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">迭代完成</p>
              <div className="space-y-2 px-4 py-3">
                <p className="text-caption text-muted-foreground">
                  终止条件已触发（最佳 {best.toFixed(4)}，Δ{bestDelta >= 0 ? "+" : ""}{bestDelta.toFixed(4)}）。进入最终确认做 test 集一次性释放（final-read）。
                </p>
                <Button
                  size="sm"
                  className="w-full"
                  onClick={() => send.mutate("进入最终确认")}
                >
                  进入最终确认
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
