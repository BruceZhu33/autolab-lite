"use client";

/**
 * 基线验证工作区（基线模块核心）。
 *
 * 流程（deviation #34 顺序）：BASE 绑定原始代码库——
 *  ① 派发 BASE（多 seed，时间驱动模拟进度）；
 *  ② 监视 run（RUNNING / FINISHED / REPAIRING）与指标；
 *  ③ 全部完成后汇总（均值 + seed 区间），对照验收线；
 *  ④ 验收通过 → 方案评审；驳回 → 回准备（时间线留痕）。
 * 阻塞卡态（REPAIRING 无出口等）以「需要你」覆盖层呈现（v0.2 §12）。
 *
 * Phase 2 对接 sidecar：env-setup / runs 域 / tx_baseline_acceptance。
 */
import { use, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileCode, PlayCircle } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { toast } from "sonner";

import { Badge } from "@multica/ui/components/ui/badge";
import { Button } from "@multica/ui/components/ui/button";
import { Input } from "@multica/ui/components/ui/input";
import { PageHeader } from "@multica/views/layout/page-header";
import { ProjectTimeline } from "@/features/autolab/project-timeline";
import { stageColor, stageLabel } from "@/features/autolab/stage";

interface ProjectView {
  id: string;
  identifier: string;
  title: string;
  stage: string;
  direction: string;
  workspacePath: string;
}

interface BaselineRunView {
  id: string;
  seed: number;
  serverName: string;
  gpu: string;
  dispatchedAt: string;
  finishAt: string | null;
  metric: number | null;
}

interface WorkshopView {
  projectId: string;
  runs: BaselineRunView[];
  accepted: { avg: number; threshold: number; at: string } | null;
  blocker: { kind: string; title: string; reason: string; actions: string[] } | null;
  summary: { avg: number; min: number; max: number } | null;
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

function runStatus(r: BaselineRunView): "RUNNING" | "FINISHED" | "REPAIRING" {
  if (r.finishAt === null) return "REPAIRING";
  return Date.now() >= new Date(r.finishAt).getTime() ? "FINISHED" : "RUNNING";
}

function RunStatusBadge({ r }: { r: BaselineRunView }) {
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
  const [threshold, setThreshold] = useState("0.75");

  const { data: project } = useQuery({
    queryKey: ["autolab", "fixture-projects"],
    queryFn: () => fixtureApi<{ projects: ProjectView[] }>("GET", "/api/projects"),
    select: (d) => d.projects.find((p) => p.id === id || p.identifier === id) ?? null,
  });

  // 5s 轮询：时间驱动的 run 进度自然推进
  const { data: ws } = useQuery({
    queryKey: ["autolab", "baseline-workshop", id],
    queryFn: () => fixtureApi<WorkshopView>("GET", `/api/projects/${id}/baseline-workshop`),
    refetchInterval: 5000,
  });

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["autolab", "baseline-workshop", id] });
    void qc.invalidateQueries({ queryKey: ["autolab", "fixture-projects"] });
    void qc.invalidateQueries({ queryKey: ["autolab", "timeline", id] });
  };

  const dispatch = useMutation({
    mutationFn: () => fixtureApi("POST", `/api/projects/${id}/baseline-workshop/dispatch`),
    onSuccess: () => {
      toast.success("BASE 已派发", { description: "多 seed 运行中；完成后汇总指标供验收。" });
      refresh();
    },
    onError: (e: Error) => toast.error("派发失败", { description: e.message.slice(0, 200) }),
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
        reason: "指标未达线（演示驳回路径）",
      }),
    onSuccess: () => {
      toast.info("已驳回", { description: "项目回退「准备」阶段，时间线已留痕。" });
      refresh();
    },
  });

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

  const allDone = ws !== undefined && ws.runs.length > 0 && ws.summary !== null;

  return (
    <>
      <PageHeader>
        <FileCode className="size-4 text-muted-foreground" />
        <h1 className="text-body font-medium">基线验证</h1>
        <span className="font-mono text-caption text-muted-foreground">{project.identifier}</span>
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          {project.title}
        </p>
        <div className="ml-auto">
          <Badge variant="outline" className="gap-1.5">
            <span className="size-1.5 rounded-full" style={{ backgroundColor: stageColor(project.stage) }} />
            {stageLabel(project.stage)}
          </Badge>
        </div>
      </PageHeader>

      <div className="flex min-h-0 flex-1 flex-col gap-4 p-4 md:px-6 lg:flex-row">
        <div className="flex min-w-0 flex-1 flex-col gap-4 overflow-y-auto">
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

          {/* BASE 派发 / run 监视 */}
          <div className="rounded-lg border">
            <div className="flex items-center gap-2 border-b px-4 py-2.5">
              <p className="flex-1 text-caption font-medium text-muted-foreground">
                BASE 运行（原始代码 · 多 seed）
              </p>
              <span className="font-mono text-caption text-muted-foreground">{project.workspacePath}</span>
              {(!ws || ws.runs.length === 0) && project.stage === "baseline" && (
                <Button size="sm" onClick={() => dispatch.mutate()} disabled={dispatch.isPending}>
                  <PlayCircle className="size-3.5" />
                  派发 BASE
                </Button>
              )}
            </div>
            {ws && ws.runs.length > 0 ? (
              <table className="w-full text-sm">
                <thead className="bg-muted/50 text-caption text-muted-foreground">
                  <tr>
                    <th className="px-4 py-1.5 text-left font-normal">Seed</th>
                    <th className="px-4 py-1.5 text-left font-normal">服务器 / GPU</th>
                    <th className="px-4 py-1.5 text-left font-normal">状态</th>
                    <th className="px-4 py-1.5 text-left font-normal">macro-F1</th>
                    <th className="px-4 py-1.5 text-left font-normal">派发时间</th>
                  </tr>
                </thead>
                <tbody>
                  {ws.runs.map((r) => {
                    const s = runStatus(r);
                    return (
                      <tr key={r.id} className="border-t">
                        <td className="px-4 py-2 font-mono text-xs">seed {r.seed}</td>
                        <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                          {r.serverName} · {r.gpu}
                        </td>
                        <td className="px-4 py-2"><RunStatusBadge r={r} /></td>
                        <td className="px-4 py-2 font-mono tabular-nums">
                          {s === "FINISHED" ? r.metric?.toFixed(3) : "—"}
                        </td>
                        <td className="px-4 py-2 text-caption text-muted-foreground">
                          {new Date(r.dispatchedAt).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            ) : (
              <p className="px-4 py-3 text-caption text-muted-foreground">
                BASE 尚未派发。派发后以原始代码运行多 seed，建立对比锚点（deviation #34：BASE 绑定代码库而非 plan）。
              </p>
            )}
          </div>

          {/* 指标汇总 + 验收 */}
          {ws && ws.runs.length > 0 && (
            <div className="rounded-lg border">
              <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
                指标汇总与验收
              </p>
              {ws.accepted ? (
                <p className="px-4 py-3 text-caption text-emerald-600">
                  已验收：均值 {ws.accepted.avg} ≥ 验收线 {ws.accepted.threshold}（{new Date(ws.accepted.at).toLocaleString("zh-CN")}）。
                </p>
              ) : !allDone ? (
                <p className="px-4 py-3 text-caption text-muted-foreground">
                  等待全部 run 完成（时间驱动模拟，页面自动刷新进度）…
                </p>
              ) : (
                <div className="space-y-3 px-4 py-3">
                  <p className="flex items-baseline gap-3">
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
                      验收通过，进入方案评审
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => reject.mutate()} disabled={reject.isPending}>
                      驳回（回准备）
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}

          <p className="text-caption text-muted-foreground">
            <Link href={`/${wsSlug}/projects?project=${encodeURIComponent(project.id)}`} className="text-primary hover:underline">
              返回项目详情
            </Link>
          </p>
        </div>

        {/* 内嵌时间线：Agent 运行汇报 / 用户介入 */}
        <div className="flex min-h-64 min-w-0 flex-col rounded-lg border lg:w-96 lg:shrink-0">
          <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
            运行时间线（Agent 汇报 / 人工介入）
          </p>
          <div className="flex min-h-0 flex-1 flex-col p-4">
            <ProjectTimeline projectId={project.id} />
          </div>
        </div>
      </div>
    </>
  );
}
