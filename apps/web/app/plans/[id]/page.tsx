"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Check, ChevronRight, Copy, FileText, FlaskConical, Loader2, ShieldCheck,
  Timer, Users,
} from "lucide-react";
import {
  approvePlan, fetchPlanDetail, fetchProjects, type PlanDetailView,
} from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "~/vendor/ui/components/ui/dialog";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "@/vendor/views/layout/page-header";

const STATUS_VIEW: Record<string, { label: string; cls: string }> = {
  DRAFT: { label: "待批准", cls: "bg-warning/10 text-warning" },
  APPROVED: { label: "已批准", cls: "bg-info/10 text-info" },
  ABANDONED: { label: "已废弃", cls: "bg-muted text-muted-foreground" },
};

const JOB_STATUS_TONE: Record<string, string> = {
  QUEUED: "bg-muted text-muted-foreground",
  DISPATCHING: "bg-warning/10 text-warning",
  RUNNING: "bg-warning/10 text-warning",
  CANCELLING: "bg-warning/10 text-warning",
  SUCCEEDED: "bg-success/10 text-success",
  FAILED: "bg-destructive/10 text-destructive",
  CANCELLED: "bg-muted text-muted-foreground",
  TIMED_OUT: "bg-destructive/10 text-destructive",
};

function Card({ title, icon, children, className = "" }: {
  title: string; icon?: React.ReactNode;
  children: React.ReactNode; className?: string;
}) {
  return (
    <section className={`rounded-xl border-[0.5px] border-surface-border bg-surface shadow-[var(--surface-shadow)] ${className}`}>
      <div className="flex items-center gap-1.5 border-b border-surface-border px-4 py-2.5">
        {icon}
        <h2 className="text-caption font-semibold text-muted-foreground">{title}</h2>
      </div>
      <div className="px-4 py-3">{children}</div>
    </section>
  );
}

function asText(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value, null, 2);
}

/** spec_json 统一解析为对象。 */
function normalizeSpec(raw: unknown): Record<string, unknown> | null {
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      return typeof parsed === "object" && parsed !== null
        ? (parsed as Record<string, unknown>) : null;
    } catch {
      return null;
    }
  }
  if (typeof raw === "object" && raw !== null) return raw as Record<string, unknown>;
  return null;
}

/** 参数矩阵 → 每参数的取值列表与组合数。 */
function matrixOf(spec: Record<string, unknown> | null): {
  parameters: [string, unknown[]][]; combinations: number;
} {
  const def = (spec?.job_definition ?? {}) as {
    kind?: string; parameters?: Record<string, unknown>;
  };
  const entries = Object.entries(def.parameters ?? {})
    .map(([k, v]) => [k, Array.isArray(v) ? v : [v]] as [string, unknown[]]);
  const combinations = entries.reduce((n, [, values]) => n * values.length, 1);
  return { parameters: entries, combinations: Math.max(combinations, 1) };
}

function MetricChip({ name, direction }: { name: string; direction?: string }) {
  const up = direction !== "minimize";
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-brand/10 px-2 py-0.5 font-mono text-micro font-medium text-brand">
      {name}
      <span className="text-brand/70">{up ? "↑" : "↓"}</span>
    </span>
  );
}

function ApproveDialog({ plan }: { plan: PlanDetailView }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [copied, setCopied] = useState(false);
  const queryClient = useQueryClient();

  const copyHash = async () => {
    try {
      await navigator.clipboard.writeText(plan.spec_hash);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("复制失败,请手动选择复制");
    }
  };

  const confirm = async () => {
    if (typed.trim() !== plan.spec_hash) {
      toast.error("spec hash 不匹配:请粘贴完整 hash 以确认本轮执行契约");
      return;
    }
    setSubmitting(true);
    try {
      const result = await approvePlan(plan.id, typed.trim());
      toast.success(`已批准:${result.jobs.length} 个 Job 已生成`);
      setOpen(false);
      setTyped("");
      void queryClient.invalidateQueries({ queryKey: ["board"] });
      void queryClient.invalidateQueries({ queryKey: ["plan", plan.id] });
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
          <ShieldCheck className="size-3.5" />批准执行
        </Button>
      } />
      <DialogContent>
        <DialogHeader>
          <DialogTitle>批准 Plan revision {plan.revision_no}</DialogTitle>
          <DialogDescription>
            一次确认覆盖整批 Job。批准后执行契约冻结(spec hash 不变),改变口径需创建下一版 Plan。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <p className="text-caption font-medium text-muted-foreground">期望 spec hash</p>
            <div className="mt-1 flex items-start gap-2">
              <code className="min-w-0 flex-1 break-all rounded-lg bg-muted/50 p-2 text-micro leading-relaxed">
                {plan.spec_hash}
              </code>
              <Button variant="outline" size="icon-sm" onClick={() => void copyHash()} aria-label="复制 hash">
                {copied ? <Check className="size-3.5 text-success" /> : <Copy className="size-3.5" />}
              </Button>
            </div>
          </div>
          <div>
            <p className="text-caption font-medium text-muted-foreground">粘贴完整 hash 以确认</p>
            <Input
              className="mt-1 font-mono text-caption"
              placeholder={plan.spec_hash.slice(0, 16) + "…"}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              spellCheck={false}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => setOpen(false)}>取消</Button>
          <Button size="sm" disabled={submitting || !typed.trim()} onClick={() => void confirm()}>
            {submitting ? <Loader2 className="size-3.5 animate-spin" /> : null}
            确认批准
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function PlanPage() {
  const params = useParams<{ id: string }>();
  const id = Array.isArray(params.id) ? params.id[0] : params.id;
  const { data: plan, error, isLoading } = useQuery({
    queryKey: ["plan", id],
    queryFn: () => fetchPlanDetail(id),
    enabled: !!id,
    refetchInterval: (query) =>
      query.state.data?.status === "DRAFT" ? 5000 : false,
    select: (data) => ({ ...data, spec_json: normalizeSpec(data.spec_json) }),
  });
  const { data: projects = [] } = useQuery({
    queryKey: ["projects"],
    queryFn: fetchProjects,
    enabled: !!plan,
  });

  const projectName = useMemo(
    () => projects.find((p) => p.id === plan?.project_id)?.name,
    [projects, plan?.project_id],
  );
  const spec = plan?.spec_json ?? null;
  const matrix = useMemo(() => matrixOf(spec), [spec]);
  const timeoutSeconds = Number(spec?.timeout_seconds ?? spec?.timeout ?? 0) || 0;
  const maxParallel = Number(
    ((spec?.resources ?? {}) as { max_parallel?: number }).max_parallel ?? 1) || 1;
  const metrics = (spec?.metrics ?? []) as
    Array<{ name?: string; direction?: string }>;
  const criteria = (spec?.success_criteria ?? []) as unknown[];
  const anchor = (spec?.code_anchor ?? {}) as { non_git_fingerprint?: string };
  const fingerprint = anchor.non_git_fingerprint;

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-body text-muted-foreground">
        <Loader2 className="mr-2 size-4 animate-spin" /> 加载 Plan…
      </div>
    );
  }
  if (error || !plan) {
    return (
      <div className={PAGE_GUTTER}>
        <p className="text-body text-destructive">{error?.message ?? "Plan 不存在"}</p>
      </div>
    );
  }
  const statusView = STATUS_VIEW[plan.status] ?? { label: plan.status, cls: "bg-muted" };
  const estHours = (matrix.combinations * timeoutSeconds) / 3600;

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <h1 className="text-title-sm font-semibold tracking-tight">实验计划</h1>
        <span className="text-caption text-muted-foreground">
          {projectName ?? plan.project_id}
        </span>
        <span className="rounded-full bg-muted/60 px-2 py-0.5 text-micro text-muted-foreground">
          revision {plan.revision_no}
        </span>
        <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-micro font-medium ${statusView.cls}`}>
          {statusView.label}
        </span>
        {plan.status === "DRAFT" ? (
          <span className="ml-auto"><ApproveDialog plan={plan} /></span>
        ) : plan.experiment_ref ? (
          <span className="ml-auto inline-flex items-center gap-1 text-caption text-muted-foreground">
            <FileText className="size-3.5" />
            <code>{plan.experiment_ref}</code>
          </span>
        ) : null}
      </PageHeader>

      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${PAGE_RAIL} space-y-3 py-4`}>
          {/* 契约指纹:审批仪式的锚点,弱化但不隐藏 */}
          <p className="flex flex-wrap items-center gap-1.5 text-micro text-muted-foreground">
            <span>spec hash</span>
            <code className="break-all" title={plan.spec_hash}>
              {plan.spec_hash.slice(0, 24)}…{plan.spec_hash.slice(-8)}
            </code>
            {fingerprint ? (
              <>
                <span className="ml-2">代码指纹</span>
                <code title={fingerprint}>{fingerprint.slice(0, 12)}…</code>
              </>
            ) : null}
          </p>

          {/* 假设卡:审批者最先要读的东西 */}
          <section className="rounded-xl border-[0.5px] border-brand/30 bg-brand/5 p-4">
            <div className="flex items-center gap-1.5">
              <FlaskConical className="size-3.5 text-brand" />
              <h2 className="text-caption font-semibold text-muted-foreground">研究假设(事前登记,不可事后改口)</h2>
            </div>
            <p className="mt-1.5 text-body leading-relaxed">
              {asText(spec?.hypothesis)}
            </p>
            <div className="mt-2.5 flex flex-wrap items-center gap-1.5 text-micro text-muted-foreground">
              <span className="rounded-full bg-muted/60 px-2 py-0.5">
                基线对照 <code>{asText(spec?.baseline)}</code>
              </span>
              {metrics.filter((m) => m.name).map((m) => (
                <MetricChip key={m.name} name={m.name!} direction={m.direction} />
              ))}
            </div>
            {spec?.change_from_previous ? (
              <p className="mt-2 border-t border-brand/20 pt-2 text-caption leading-relaxed text-muted-foreground">
                <span className="font-medium text-foreground/80">相对上一轮:</span>
                {asText(spec.change_from_previous)}
              </p>
            ) : null}
          </section>

          <div className="grid gap-3 lg:grid-cols-2">
            {/* 执行契约:命令 + 矩阵 + 成本 */}
            <Card title="执行契约" icon={<ChevronRight className="size-3.5 text-muted-foreground" />}>
              <pre className="overflow-x-auto rounded-lg bg-muted/50 p-2.5 font-mono text-caption leading-relaxed">
                {Array.isArray(spec?.command_template)
                  ? (spec.command_template as string[]).join(" ")
                  : asText(spec?.command_template)}
              </pre>
              <div className="mt-3 space-y-1.5">
                {matrix.parameters.map(([name, values]) => (
                  <div key={name} className="flex flex-wrap items-center gap-1.5">
                    <code className="min-w-14 font-mono text-caption font-medium">{name}</code>
                    {values.map((v) => (
                      <span key={String(v)}
                            className="rounded-full border-[0.5px] border-surface-border bg-background px-2 py-0.5 font-mono text-micro">
                        {String(v)}
                      </span>
                    ))}
                  </div>
                ))}
                {matrix.parameters.length === 0 ? (
                  <p className="text-caption text-muted-foreground">单 Job,无矩阵参数</p>
                ) : null}
              </div>
              <p className="mt-3 flex flex-wrap items-center gap-3 border-t border-surface-border pt-2 text-micro text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  <Users className="size-3" />{matrix.combinations} 个 Job
                </span>
                {timeoutSeconds > 0 ? (
                  <span className="inline-flex items-center gap-1">
                    <Timer className="size-3" />
                    单 Job 上限 {timeoutSeconds >= 3600
                      ? `${(timeoutSeconds / 3600).toFixed(1)}h`
                      : `${Math.round(timeoutSeconds / 60)}m`}
                    ,串行估 {estHours >= 1
                      ? `${estHours.toFixed(1)}h`
                      : `${Math.round(estHours * 60)}m`}
                  </span>
                ) : null}
                {maxParallel > 1 ? <span>并行 {maxParallel}</span> : null}
              </p>
            </Card>

            {/* 判据与风险 */}
            <Card title="事前成功判据 / 风险与回退">
              {criteria.length > 0 ? (
                <ul className="space-y-1">
                  {criteria.map((c, i) => (
                    <li key={i} className="flex items-start gap-1.5 text-caption leading-relaxed">
                      <Check className="mt-0.5 size-3 shrink-0 text-success" />
                      {asText(c)}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-caption text-muted-foreground">(未登记)</p>
              )}
              {spec?.risk_and_fallback ? (
                <p className="mt-3 whitespace-pre-wrap border-t border-surface-border pt-2.5 text-caption leading-relaxed text-muted-foreground">
                  <span className="font-medium text-foreground/80">风险与回退:</span>
                  {asText(spec.risk_and_fallback)}
                </p>
              ) : null}
            </Card>
          </div>

          {/* Jobs 表:加参数列,参数是审批者关心的口径 */}
          <section className="rounded-xl border-[0.5px] border-surface-border bg-surface shadow-[var(--surface-shadow)]">
            <div className="flex items-center gap-2 border-b border-surface-border px-4 py-2.5">
              <h2 className="text-caption font-semibold text-muted-foreground">
                展开的 Jobs({plan.jobs.length})
              </h2>
              {plan.jobs.length === 0 && plan.status === "DRAFT" ? (
                <span className="text-micro text-muted-foreground">批准后生成</span>
              ) : null}
            </div>
            {plan.jobs.length === 0 ? (
              <p className="px-4 py-6 text-center text-caption text-muted-foreground">
                {plan.status === "DRAFT" ? "批准后生成" : "无"}
              </p>
            ) : (
              <table className="w-full text-caption">
                <thead>
                  <tr className="border-b border-surface-border text-left text-micro text-muted-foreground">
                    <th className="px-4 py-2 font-medium">#</th>
                    <th className="px-2 py-2 font-medium">参数</th>
                    <th className="px-2 py-2 font-medium">状态</th>
                    <th className="px-2 py-2 font-medium">退出码</th>
                    <th className="px-2 py-2 font-medium">开始</th>
                    <th className="px-4 py-2 font-medium">详情</th>
                  </tr>
                </thead>
                <tbody>
                  {plan.jobs.map((job) => {
                    let params: Record<string, unknown> = {};
                    try {
                      params = typeof job.params_json === "string"
                        ? JSON.parse(job.params_json)
                        : (job.params_json ?? {});
                    } catch { /* 保持空 */ }
                    return (
                      <tr key={job.id} className="border-b border-surface-border/50 last:border-0">
                        <td className="px-4 py-2 tabular-nums">{job.ordinal}</td>
                        <td className="px-2 py-2">
                          <span className="font-mono text-micro">
                            {Object.entries(params).map(([k, v]) => `${k}=${String(v)}`).join(" ")}
                          </span>
                        </td>
                        <td className="px-2 py-2">
                          <span className={`inline-flex rounded-full px-2 py-0.5 text-micro font-medium ${JOB_STATUS_TONE[job.status] ?? "bg-muted"}`}>
                            {job.status}
                          </span>
                        </td>
                        <td className="px-2 py-2 tabular-nums">{job.exit_code ?? "—"}</td>
                        <td className="px-2 py-2">
                          {job.started_at ? new Date(job.started_at).toLocaleString("zh-CN") : "—"}
                        </td>
                        <td className="px-4 py-2">
                          <Link href={`/runs/${job.id}`} className="text-info hover:underline">
                            查看
                          </Link>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>

          {/* 原始 spec:排错用,默认折叠 */}
          <details className="rounded-xl border-[0.5px] border-surface-border bg-surface/60">
            <summary className="cursor-pointer select-none px-4 py-2.5 text-caption font-semibold text-muted-foreground">
              原始 spec JSON(排错用)
            </summary>
            <pre className="max-h-96 overflow-auto px-4 pb-4 text-micro leading-relaxed text-muted-foreground">
              {JSON.stringify(spec, null, 2)}
            </pre>
          </details>
        </div>
      </div>
    </div>
  );
}
