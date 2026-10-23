"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ChevronRight, GitBranch } from "lucide-react";
import { fetchJobs, fetchPlans, fetchProjects, type Job, type Plan } from "@/lib/api";
import { ApiError } from "@/components/api-error";
import { Badge } from "~/vendor/ui/components/ui/badge";
import { PageHeader } from "~/vendor/views/layout/page-header";

/** 状态 → 徽章配色:全部走语义 token,深浅主题共用一套。 */
const STATUS_TONE: Record<string, string> = {
  SUCCEEDED: "bg-success/10 text-success",
  FAILED: "bg-destructive/10 text-destructive",
  TIMED_OUT: "bg-destructive/10 text-destructive",
  RUNNING: "bg-info/10 text-info",
  QUEUED: "bg-muted text-muted-foreground",
  CANCELLING: "bg-warning/10 text-warning",
};

const STATUS_LABEL: Record<string, string> = {
  SUCCEEDED: "成功",
  FAILED: "失败",
  TIMED_OUT: "超时",
  RUNNING: "运行中",
  QUEUED: "排队中",
  CANCELLING: "取消中",
};

const PLAN_STATUS: Record<string, { label: string; cls: string }> = {
  DRAFT: { label: "草稿", cls: "bg-warning/10 text-warning" },
  APPROVED: { label: "已批准", cls: "bg-info/10 text-info" },
  ABANDONED: { label: "已废弃", cls: "bg-muted text-muted-foreground" },
};

const TAB_TEXT = {
  jobs: "每个 Job 的运行事实:状态 · 退出码 · 起止与时长",
  plans: "研究阶段的实验计划:草案 → 批准 → 派发为 Job",
} as const;

function fmtDateTime(iso?: string | null): string {
  if (!iso) return "—";
  return `${iso.slice(5, 10)} ${iso.slice(11, 19)}`;
}

/** ISO 起止 → 人类可读时长;未结束返回 null。 */
function duration(started?: string | null, finished?: string | null): string | null {
  if (!started || !finished) return null;
  const ms = Date.parse(finished) - Date.parse(started);
  if (!Number.isFinite(ms) || ms < 0) return null;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

/** 矩阵实参 → 「seed=1 lr=0.1」;解析失败或为空返回空串。 */
function fmtParams(params: Job["params_json"]): string {
  if (!params) return "";
  const obj = typeof params === "string" ? (() => {
    try { return JSON.parse(params) as Record<string, unknown>; } catch { return null; }
  })() : params;
  if (!obj) return "";
  return Object.entries(obj).map(([k, v]) => `${k}=${String(v)}`).join(" ");
}

export default function RunsPage() {
  const router = useRouter();
  const [tab, setTab] = useState<"jobs" | "plans">("jobs");
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [plans, setPlans] = useState<Plan[] | null>(null);
  const [projectNames, setProjectNames] = useState<Record<string, string>>({});
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(() => {
    fetchJobs()
      .then((data) => {
        setJobs(data);
        setError(null);
      })
      .catch((e: unknown) => setError(e));
    fetchPlans().then(setPlans).catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 2000);
    return () => clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    fetchProjects()
      .then((projects) => setProjectNames(
        Object.fromEntries(projects.map((p) => [p.id, p.name]))))
      .catch(() => {});
  }, []);

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <GitBranch className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">运行与计划</h1>
        <div className="ml-2 flex items-center gap-0.5 rounded-lg border border-surface-border bg-surface p-0.5">
          {(["jobs", "plans"] as const).map((key) => (
            <button key={key} onClick={() => setTab(key)}
                    className={`rounded-md px-2.5 py-1 text-caption transition-colors ${
                      tab === key ? "bg-muted font-medium text-foreground"
                                  : "text-muted-foreground hover:text-foreground"}`}>
              {key === "jobs"
                ? `实验 Job${jobs?.length ? ` ${jobs.length}` : ""}`
                : `计划${plans?.length ? ` ${plans.length}` : ""}`}
            </button>
          ))}
        </div>
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          {TAB_TEXT[tab]}
        </p>
      </PageHeader>

      <div className="flex-1 overflow-y-auto px-4 pb-4 pt-4">
        {error && (jobs === null || jobs.length === 0) ? (
          <ApiError error={error} onRetry={refresh} />
        ) : tab === "jobs" ? (
          <div className="mx-auto w-full max-w-[1100px] overflow-hidden rounded-xl border border-surface-border bg-surface shadow-[var(--surface-shadow)]">
            <table className="w-full text-left text-body">
              <thead className="border-b border-surface-border text-micro uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-normal">Job</th>
                  <th className="px-3 py-2 font-normal">状态</th>
                  <th className="px-3 py-2 font-normal">退出码</th>
                  <th className="px-3 py-2 font-normal">开始</th>
                  <th className="px-3 py-2 font-normal">结束</th>
                  <th className="px-3 py-2 font-normal">时长</th>
                  <th className="w-8 px-2 py-2" aria-hidden />
                </tr>
              </thead>
              <tbody>
                {(jobs ?? []).map((job) => (
                  <tr
                    key={job.id}
                    onClick={() => router.push(`/runs/${job.id}`)}
                    className="cursor-pointer border-b border-surface-border/60 transition-colors
                               last:border-0 hover:bg-muted/40"
                  >
                    <td className="min-w-0 px-3 py-2">
                      <div className="flex min-w-0 flex-col">
                        <span className="truncate font-mono text-caption">
                          #{job.ordinal}
                          {fmtParams(job.params_json) ? ` · ${fmtParams(job.params_json)}` : ""}
                        </span>
                        <span className="truncate font-mono text-micro text-muted-foreground">
                          {job.id}
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      <Badge variant="ghost" className={STATUS_TONE[job.status] ?? ""}>
                        {STATUS_LABEL[job.status] ?? job.status}
                      </Badge>
                      {job.hold_note ? (
                        <p className="mt-0.5 max-w-44 text-micro text-warning" title={job.hold_note}>
                          {job.hold_note}
                        </p>
                      ) : null}
                    </td>
                    <td className="px-3 py-2 font-mono text-caption tabular-nums">
                      {job.exit_code ?? "—"}
                    </td>
                    <td className="px-3 py-2 font-mono text-caption tabular-nums">
                      {fmtDateTime(job.started_at)}
                    </td>
                    <td className="px-3 py-2 font-mono text-caption tabular-nums">
                      {fmtDateTime(job.finished_at)}
                    </td>
                    <td className="px-3 py-2 font-mono text-caption tabular-nums">
                      {duration(job.started_at, job.finished_at) ?? "—"}
                    </td>
                    <td className="px-2 py-2 text-muted-foreground">
                      <ChevronRight className="size-3.5" />
                    </td>
                  </tr>
                ))}
                {jobs && jobs.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="px-3 py-10 text-center text-caption text-muted-foreground">
                      还没有 Job——在计划页批准一个实验计划后,派发的 Job 会出现在这里。
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="mx-auto w-full max-w-[1100px] overflow-hidden rounded-xl border border-surface-border bg-surface shadow-[var(--surface-shadow)]">
            <table className="w-full text-left text-body">
              <thead className="border-b border-surface-border text-micro uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-normal">计划</th>
                  <th className="px-3 py-2 font-normal">项目</th>
                  <th className="px-3 py-2 font-normal">状态</th>
                  <th className="px-3 py-2 font-normal">版本</th>
                  <th className="px-3 py-2 font-normal">批准</th>
                  <th className="px-3 py-2 font-normal">创建</th>
                  <th className="w-8 px-2 py-2" aria-hidden />
                </tr>
              </thead>
              <tbody>
                {(plans ?? []).map((plan) => (
                  <tr
                    key={plan.id}
                    onClick={() => router.push(`/plans/${plan.id}`)}
                    className="cursor-pointer border-b border-surface-border/60 transition-colors
                               last:border-0 hover:bg-muted/40"
                  >
                    <td className="min-w-0 px-3 py-2">
                      <div className="flex min-w-0 flex-col">
                        <span className="truncate text-caption">{plan.summary || "(无摘要)"}</span>
                        <span className="truncate font-mono text-micro text-muted-foreground">
                          {plan.id}
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2 text-caption">
                      {projectNames[plan.project_id] ?? plan.project_id}
                    </td>
                    <td className="px-3 py-2">
                      <Badge variant="ghost" className={PLAN_STATUS[plan.status]?.cls ?? ""}>
                        {PLAN_STATUS[plan.status]?.label ?? plan.status}
                      </Badge>
                    </td>
                    <td className="px-3 py-2 font-mono text-caption tabular-nums">
                      v{plan.revision_no}
                    </td>
                    <td className="px-3 py-2 text-caption">{plan.approved_by ?? "—"}</td>
                    <td className="px-3 py-2 font-mono text-caption tabular-nums">
                      {fmtDateTime(plan.created_at)}
                    </td>
                    <td className="px-2 py-2 text-muted-foreground">
                      <ChevronRight className="size-3.5" />
                    </td>
                  </tr>
                ))}
                {plans && plans.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="px-3 py-10 text-center text-caption text-muted-foreground">
                      还没有计划——研究 Agent 提出的实验计划会出现在这里,批准后派发为 Job。
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
