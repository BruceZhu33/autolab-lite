"use client";

/**
 * 基线验证工作区（基线模块核心，对话驱动）。
 *
 * 交互模型（2026-09-15 口径）：用户与 AutoLab 协调 Agent 对话，
 * Agent 去 GPU 服务器执行——「跑基线」→ 派发多 seed BASE → 汇报进度 →
 * 完成后主动汇报指标 →「验收通过」过门进方案评审（对话/按钮双通道）。
 * 右侧是执行状态面板（run 表 / 指标 / 验收 / 阻塞「需要你」）。
 * Run 进度时间驱动模拟；Phase 2 对接 sidecar env-setup / runs 域。
 */
import { use, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileCode, Send } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { toast } from "sonner";

import { Badge } from "@multica/ui/components/ui/badge";
import { Button } from "@multica/ui/components/ui/button";
import { Input } from "@multica/ui/components/ui/input";
import { PageHeader } from "@multica/views/layout/page-header";
import { stageColor, stageLabel } from "@/features/autolab/stage";
import { StageTeamBar } from "@/features/autolab/stage-team";

interface ProjectView {
  id: string;
  identifier: string;
  title: string;
  stage: string;
  direction: string;
  workspacePath: string;
}

interface RunView {
  id: string;
  seed: number;
  serverName: string;
  gpu: string;
  dispatchedAt: string;
  finishAt: string | null;
  metric: number | null;
  artifacts: { dir: string; metricsJson: boolean; finalCkpt: boolean; trainLog: boolean };
}

interface ResultView {
  dataset: string;
  dir: string;
  seeds: { seed: number; metric: number | null; artifacts: RunView["artifacts"] }[];
  complete: boolean;
  missing: string[];
}

interface MsgView {
  id: string;
  role: "user" | "agent";
  content: string;
  createdAt: string;
}

interface ImportView {
  sourceKind: "local" | "github";
  sourceRef: string;
  serverName: string;
  remotePath: string;
  files: number;
  sizeMb: number;
  startedAt: string;
  finishAt: string;
}

interface WorkshopView {
  projectId: string;
  codeImport: ImportView | null;
  dataset: { name: string; path: string; sizeMb: number; confirmedAt: string } | null;
  runs: RunView[];
  accepted: { avg: number; threshold: number; at: string } | null;
  blocker: { kind: string; title: string; reason: string; actions: string[] } | null;
  messages: MsgView[];
  summary: { avg: number; min: number; max: number } | null;
  result: ResultView | null;
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

function importStat(imp: ImportView | null): "none" | "SYNCING" | "SYNCED" {
  if (!imp) return "none";
  return Date.now() >= new Date(imp.finishAt).getTime() ? "SYNCED" : "SYNCING";
}

function runStatus(r: RunView): "RUNNING" | "FINISHED" | "REPAIRING" {
  if (r.finishAt === null) return "REPAIRING";
  return Date.now() >= new Date(r.finishAt).getTime() ? "FINISHED" : "RUNNING";
}

function RunStatusBadge({ r }: { r: RunView }) {
  const s = runStatus(r);
  if (s === "FINISHED") return <Badge variant="default" className="bg-emerald-600">FINISHED</Badge>;
  if (s === "REPAIRING") return <Badge variant="destructive">REPAIRING</Badge>;
  return (
    <Badge variant="secondary" className="gap-1">
      <span className="size-1.5 animate-pulse rounded-full bg-amber-500" />
      RUNNING
    </Badge>
  );
}

export default function BaselineWorkshopPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const qc = useQueryClient();
  const pathname = usePathname();
  const wsSlug = pathname.split("/")[1] ?? "autolab";
  const [draft, setDraft] = useState("");
  const [typing, setTyping] = useState(false);
  const [threshold, setThreshold] = useState("0.75");
  const scrollRef = useRef<HTMLDivElement>(null);

  const { data: project } = useQuery({
    queryKey: ["autolab", "fixture-projects"],
    queryFn: () => fixtureApi<{ projects: ProjectView[] }>("GET", "/api/projects"),
    select: (d) => d.projects.find((p) => p.id === id || p.identifier === id) ?? null,
  });

  // 5s 轮询：时间驱动 run 进度 + Agent 完成汇报（惰性触发）自然出现
  const { data: ws } = useQuery({
    queryKey: ["autolab", "baseline-workshop", id],
    queryFn: () => fixtureApi<WorkshopView>("GET", `/api/projects/${id}/baseline-workshop`),
    // 训练监控页：后台标签也要刷新（Agent 汇报不因失焦缺席）
    refetchInterval: 5000,
    refetchIntervalInBackground: true,
  });

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [ws?.messages.length, typing]);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["autolab", "baseline-workshop", id] });
    void qc.invalidateQueries({ queryKey: ["autolab", "fixture-projects"] });
    void qc.invalidateQueries({ queryKey: ["autolab", "timeline", id] });
  };

  const send = useMutation({
    mutationFn: (content: string) =>
      fixtureApi<MsgView>("POST", `/api/projects/${id}/baseline-workshop/messages`, { content }),
    onMutate: () => setTyping(true),
    onSuccess: () => {
      setDraft("");
      // typing 演出：Agent 回复已入库（可能带动作副作用），延迟一拍刷新
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

  const dispatch = useMutation({
    mutationFn: () => fixtureApi("POST", `/api/projects/${id}/baseline-workshop/dispatch`),
    onSuccess: () => {
      toast.success("BASE 已派发", { description: "多 seed 运行中，Agent 会汇报进展。" });
      refresh();
    },
  });

  const accept = useMutation({
    mutationFn: () =>
      fixtureApi<{ passed: boolean; reason?: string }>(
        "POST",
        `/api/projects/${id}/baseline-workshop/accept`,
        { threshold: Number(threshold) || 0.75 },
      ),
    onSuccess: (r) => {
      if (r.passed) {
        toast.success("基线验收通过", { description: "已进入「方案评审」，带着基线数字讨论改进。" });
      } else {
        toast.error("验收未通过", { description: r.reason });
      }
      refresh();
    },
  });

  const reject = useMutation({
    mutationFn: () =>
      fixtureApi("POST", `/api/projects/${id}/baseline-workshop/reject`, {
        reason: "指标未达线（面板驳回）",
      }),
    onSuccess: () => {
      toast.info("已驳回", { description: "项目回退「准备」阶段，时间线已留痕。" });
      refresh();
    },
  });

  const allDone = useMemo(
    () => ws !== undefined && ws.runs.length > 0 && ws.summary !== null,
    [ws],
  );

  if (!project) {
    return (
      <>
        <PageHeader>
          <FileCode className="size-4 text-muted-foreground" />
          <h1 className="text-body font-medium">基线验证</h1>
        </PageHeader>
        <div className="p-4 md:px-6">
          <p className="text-sm text-muted-foreground">项目不存在或已被删除。</p>
        </div>
      </>
    );
  }

  const submit = () => {
    const content = draft.trim();
    if (!content) return;
    send.mutate(content);
  };

  return (
    <>
      <PageHeader>
        <FileCode className="size-4 text-muted-foreground" />
        <h1 className="text-body font-medium">基线验证</h1>
        <span className="font-mono text-caption text-muted-foreground">{project.identifier}</span>
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          {project.title}
        </p>
        <StageTeamBar stage="baseline" />
        <div className="ml-auto">
          <Badge variant="outline" className="gap-1.5">
            <span className="size-1.5 rounded-full" style={{ backgroundColor: stageColor(project.stage) }} />
            {stageLabel(project.stage)}
          </Badge>
        </div>
      </PageHeader>

      <div className="flex min-h-0 flex-1 gap-4 p-4 md:px-6">
        {/* 左：与 AutoLab 协调 Agent 对话（它去 GPU 服务器执行） */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col rounded-lg border">
          <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
            与 AutoLab 协调 Agent 对话 · 它在 GPU 服务器上执行基线训练
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
                  <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-caption font-medium">
                    A
                  </span>
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
              placeholder="跟 AutoLab 说：导入代码 / 用 clinic / 跑基线 / 验收通过…"
            />
            <Button size="icon-sm" onClick={submit} disabled={send.isPending || !draft.trim()} aria-label="发送">
              <Send className="size-4" />
            </Button>
          </div>
        </div>

        {/* 右：执行状态面板 */}
        <div className="hidden w-96 shrink-0 flex-col gap-4 overflow-y-auto lg:flex">
          {/* 阻塞覆盖层（需要你） */}
          {ws?.blocker && (
            <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-4">
              <p className="flex items-center gap-2 text-sm font-medium text-destructive">
                <span className="size-1.5 rounded-full bg-destructive" />
                需要你 · {ws.blocker.title}
              </p>
              <p className="mt-1 text-caption leading-relaxed text-muted-foreground">{ws.blocker.reason}</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {ws.blocker.actions.map((a) => (
                  <Button key={a} size="sm" variant="outline" disabled
                          title="Phase 2 接 sidecar 恢复端点后启用">
                    {a}
                  </Button>
                ))}
              </div>
            </div>
          )}

          {/* 环境准备（三步流的前两步） */}
          <div className="rounded-lg border">
            <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
              环境准备（代码导入 · 数据集）
            </p>
            <div className="divide-y text-sm">
              <div className="flex items-center gap-2 px-4 py-2.5">
                <span className="w-14 text-caption text-muted-foreground">代码</span>
                {importStat(ws?.codeImport ?? null) === "none" && (
                  <span className="text-caption text-muted-foreground">未导入——对话里说「导入代码」</span>
                )}
                {importStat(ws?.codeImport ?? null) === "SYNCING" && (
                  <Badge variant="secondary" className="gap-1">
                    <span className="size-1.5 animate-pulse rounded-full bg-amber-500" />
                    SYNCING
                  </Badge>
                )}
                {importStat(ws?.codeImport ?? null) === "SYNCED" && ws?.codeImport && (
                  <>
                    <Badge variant="default" className="bg-emerald-600">SYNCED</Badge>
                    <span className="truncate font-mono text-caption text-muted-foreground">
                      {ws.codeImport.serverName}:{ws.codeImport.remotePath} · {ws.codeImport.files} 文件 · {ws.codeImport.sizeMb} MB
                    </span>
                  </>
                )}
              </div>
              <div className="flex items-center gap-2 px-4 py-2.5">
                <span className="w-14 text-caption text-muted-foreground">数据集</span>
                {ws?.dataset ? (
                  <>
                    <Badge variant="outline">{ws.dataset.name}</Badge>
                    <span className="truncate font-mono text-caption text-muted-foreground">
                      {ws.dataset.path} · {ws.dataset.sizeMb} MB
                    </span>
                  </>
                ) : (
                  <span className="text-caption text-muted-foreground">
                    未确认——导入完成后 Agent 会引导选择
                  </span>
                )}
              </div>
            </div>
          </div>

          {/* BASE 运行表 */}
          <div className="rounded-lg border">
            <div className="flex items-center gap-2 border-b px-4 py-2.5">
              <p className="flex-1 text-caption font-medium text-muted-foreground">
                BASE 运行（原始代码 · 多 seed）
              </p>
              {(!ws || ws.runs.length === 0) && project.stage === "baseline"
                && importStat(ws?.codeImport ?? null) === "SYNCED" && ws?.dataset && (
                <Button size="sm" variant="ghost" onClick={() => dispatch.mutate()} disabled={dispatch.isPending}>
                  派发
                </Button>
              )}
            </div>
            {ws && ws.runs.length > 0 ? (
              <div className="divide-y">
                {ws.runs.map((r) => {
                  const s = runStatus(r);
                  return (
                    <div key={r.id} className="flex items-center gap-2 px-4 py-2 text-sm">
                      <span className="font-mono text-xs">seed {r.seed}</span>
                      <span className="truncate font-mono text-caption text-muted-foreground">
                        {r.serverName}
                      </span>
                      <span className="ml-auto"><RunStatusBadge r={r} /></span>
                      <span className="w-14 text-right font-mono tabular-nums">
                        {s === "FINISHED" ? r.metric?.toFixed(3) : "—"}
                      </span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="px-4 py-3 text-caption text-muted-foreground">
                未派发。三步流：对话「导入代码」→ Agent 引导选数据集 →「跑基线」（deviation #34：BASE 绑定代码库而非 plan）。
              </p>
            )}
          </div>

          {/* 基线结果文件夹（验收证据） */}
          {ws?.result && ws.result.seeds.length > 0 && (
            <div className="rounded-lg border">
              <p className="flex items-center gap-2 border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
                基线结果文件夹
                <span className="truncate font-mono font-normal">{ws.result.dir}</span>
                <Badge variant={ws.result.complete ? "default" : "secondary"}
                       className={ws.result.complete ? "bg-emerald-600" : "text-amber-600"}>
                  {ws.result.complete ? "完整" : "不完整"}
                </Badge>
              </p>
              <div className="divide-y">
                {ws.result.seeds.map((s) => (
                  <div key={s.seed} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-caption">
                    <span className="font-mono">seed {s.seed}</span>
                    <span className={s.artifacts.metricsJson ? "text-emerald-600" : "text-destructive"}>
                      {s.artifacts.metricsJson ? "✓" : "✗"} metrics.json
                    </span>
                    <span className={s.artifacts.finalCkpt ? "text-emerald-600" : "text-destructive"}>
                      {s.artifacts.finalCkpt ? "✓" : "✗"} final.ckpt
                    </span>
                    <span className={s.artifacts.trainLog ? "text-emerald-600" : "text-muted-foreground"}>
                      {s.artifacts.trainLog ? "✓" : "✗"} train.log
                    </span>
                    <span className="ml-auto font-mono tabular-nums">
                      {s.metric !== null ? s.metric.toFixed(3) : "—"}
                    </span>
                  </div>
                ))}
              </div>
              {!ws.result.complete && (
                <p className="border-t px-4 py-2 text-caption text-amber-600">
                  验收条件未满足：{ws.result.missing.join("；")}
                </p>
              )}
            </div>
          )}

          {/* 指标汇总与验收 */}
          {ws && ws.runs.length > 0 && (
            <div className="rounded-lg border">
              <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
                指标汇总与验收
              </p>
              {ws.accepted ? (
                <p className="px-4 py-3 text-caption text-emerald-600">
                  已验收：均值 {ws.accepted.avg} ≥ 验收线 {ws.accepted.threshold}。
                </p>
              ) : !allDone ? (
                <p className="px-4 py-3 text-caption text-muted-foreground">
                  等待全部 run 完成（Agent 汇报后自动出现汇总）…
                </p>
              ) : (
                <div className="space-y-3 px-4 py-3">
                  <p className="flex items-baseline gap-2">
                    <span className="font-mono text-lg font-semibold tabular-nums">
                      {ws.summary!.avg.toFixed(4)}
                    </span>
                    <span className="text-caption text-muted-foreground">
                      seed 区间 {ws.summary!.min.toFixed(3)} ~ {ws.summary!.max.toFixed(3)}
                    </span>
                  </p>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-caption text-muted-foreground">验收线</span>
                    <Input
                      value={threshold}
                      onChange={(e) => setThreshold(e.target.value)}
                      className="h-7 w-20 font-mono text-sm"
                    />
                    <Button size="sm" onClick={() => accept.mutate()} disabled={accept.isPending}>
                      验收通过
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => reject.mutate()} disabled={reject.isPending}>
                      驳回
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}

          <p className="text-caption text-muted-foreground">
            BASE：<span className="font-mono">{project.workspacePath || "—"}</span> ·{" "}
            <Link href={`/${wsSlug}/projects?project=${encodeURIComponent(project.id)}`} className="text-primary hover:underline">
              返回项目详情
            </Link>
          </p>
        </div>
      </div>
    </>
  );
}
