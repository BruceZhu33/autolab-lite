"use client";

/** Agent 规格(C4b):候选 → canary 对照 → owner 激活;变异只生成 candidate。 */

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Loader2, SlidersHorizontal } from "lucide-react";
import {
  activateSpec, createSpec, fetchCanaries, fetchSpecs, mutateSpec, retireSpec,
  startSpecCanary,
  type AgentCanary, type AgentSpec, type CanaryTaskResult, type SpecKind,
} from "@/lib/api";
import { ApiError } from "@/components/api-error";
import { Badge } from "~/vendor/ui/components/ui/badge";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "~/vendor/views/layout/page-header";

const FILTERS: { key: "all" | SpecKind; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "baseline", label: "baseline" },
  { key: "research", label: "research" },
];

const SPEC_STATUS_LABEL: Record<string, string> = {
  candidate: "候选",
  active: "生效中",
  retired: "已退役",
  rejected: "已拒绝",
};

const SPEC_STATUS_TONE: Record<string, string> = {
  candidate: "bg-info/10 text-info",
  active: "bg-success/10 text-success",
  retired: "bg-muted text-muted-foreground",
  rejected: "bg-destructive/10 text-destructive",
};

const CANARY_STATUS_LABEL: Record<string, string> = {
  running: "运行中",
  done: "完成",
  failed: "失败",
};

const CANARY_STATUS_TONE: Record<string, string> = {
  running: "bg-info/10 text-info",
  done: "bg-success/10 text-success",
  failed: "bg-destructive/10 text-destructive",
};

const REC_TONE: Record<string, string> = {
  promote: "bg-success/10 text-success",
  hold: "bg-warning/10 text-warning",
  reject: "bg-destructive/10 text-destructive",
};

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  return `${iso.slice(5, 10)} ${iso.slice(11, 16)}`;
}

/** config 摘要:键名 + prompt_append 前 80 字。 */
function configSummary(config: Record<string, unknown>): string {
  const keys = Object.keys(config);
  if (keys.length === 0) return "空 config(沿用默认)";
  const prompt = typeof config.prompt_append === "string"
    ? config.prompt_append.trim().slice(0, 80) : "";
  return `${keys.join(", ")}${prompt ? ` · ${prompt}` : ""}`;
}

function toolErrorsText(rows?: unknown[]): string {
  const items = (rows ?? []).map((row) => typeof row === "string" ? row : JSON.stringify(row));
  return items.length ? items.join("; ") : "—";
}

// ------------------------------------------------------------------ 新建候选

function NewSpecForm() {
  const qc = useQueryClient();
  const [label, setLabel] = useState("");
  const [kind, setKind] = useState<SpecKind>("research");
  const [promptAppend, setPromptAppend] = useState("");
  const [toolExclude, setToolExclude] = useState("");
  const [maxSteps, setMaxSteps] = useState("");
  const [keepRecent, setKeepRecent] = useState("");
  const [model, setModel] = useState("");
  const [saving, setSaving] = useState(false);

  const reset = () => {
    setLabel("");
    setPromptAppend("");
    setToolExclude("");
    setMaxSteps("");
    setKeepRecent("");
    setModel("");
  };

  const submit = async () => {
    if (!label.trim()) {
      toast.error("填一个候选名称");
      return;
    }
    const config: Record<string, unknown> = {};
    if (promptAppend.trim()) config.prompt_append = promptAppend;
    const tools = toolExclude.split(",").map((item) => item.trim()).filter(Boolean);
    if (tools.length) config.tool_exclude = tools;
    if (maxSteps.trim()) {
      const value = Number(maxSteps);
      if (!Number.isInteger(value)) {
        toast.error("max_steps 必须是整数");
        return;
      }
      config.max_steps = value;
    }
    if (keepRecent.trim()) {
      const value = Number(keepRecent);
      if (!Number.isInteger(value)) {
        toast.error("keep_recent 必须是整数");
        return;
      }
      config.keep_recent = value;
    }
    if (model.trim()) config.model = model.trim();

    setSaving(true);
    try {
      await createSpec({ kind, label: label.trim(), config });
      toast.success("候选已创建(未生效,先跑 canary 对照)");
      reset();
      await qc.invalidateQueries({ queryKey: ["specs"] });
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="rounded-xl border-[0.5px] border-surface-border bg-surface p-4 shadow-[var(--surface-shadow)]">
      <h2 className="text-label font-semibold">新建候选</h2>
      <p className="mt-0.5 text-caption text-muted-foreground">
        创建的是 candidate,不会自动生效;先对当前 active 跑 canary,再由 owner 激活。
        空字段不写入 config。
      </p>
      <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <label className="flex flex-col gap-1 text-micro text-muted-foreground">
          名称
          <Input value={label} onChange={(e) => setLabel(e.target.value)}
                 placeholder="如:更少步骤" />
        </label>
        <div className="flex flex-col gap-1 text-micro text-muted-foreground">
          kind
          <div className="flex items-center gap-1">
            {(["baseline", "research"] as const).map((value) => (
              <Button key={value} size="xs"
                      variant={kind === value ? "default" : "outline"}
                      onClick={() => setKind(value)}>
                {value}
              </Button>
            ))}
          </div>
        </div>
        <label className="flex flex-col gap-1 text-micro text-muted-foreground">
          max_steps(可空)
          <Input value={maxSteps} inputMode="numeric" placeholder="如 40"
                 onChange={(e) => setMaxSteps(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-micro text-muted-foreground">
          keep_recent(可空)
          <Input value={keepRecent} inputMode="numeric" placeholder="如 20"
                 onChange={(e) => setKeepRecent(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-micro text-muted-foreground sm:col-span-2">
          tool_exclude(逗号分隔)
          <Input value={toolExclude} onChange={(e) => setToolExclude(e.target.value)}
                 placeholder="如:read_paper, grep" />
        </label>
        <label className="flex flex-col gap-1 text-micro text-muted-foreground sm:col-span-2">
          model(可空)
          <Input value={model} onChange={(e) => setModel(e.target.value)}
                 placeholder="如:glm-5.3" spellCheck={false} />
        </label>
        <label className="flex flex-col gap-1 text-micro text-muted-foreground sm:col-span-2 lg:col-span-4">
          prompt_append(追加指令,≤4000 字)
          <textarea value={promptAppend} rows={3}
                    onChange={(e) => setPromptAppend(e.target.value)}
                    placeholder="如:每轮结束前先写一句研究日志再收束。"
                    className="w-full rounded-md border-[0.5px] border-surface-border bg-background px-2 py-1.5 text-caption" />
        </label>
      </div>
      <div className="mt-3 flex items-center justify-end gap-2">
        <Button size="sm" onClick={() => void submit()} disabled={saving}>
          {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
          创建候选
        </Button>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ 规格卡片

function SpecCard({ spec, selected, disabled, onSelect, onActivate, onRetire, onCanary, onMutate }: {
  spec: AgentSpec;
  selected: boolean;
  disabled: boolean;
  onSelect: () => void;
  onActivate: () => void;
  onRetire: () => void;
  onCanary: (mode: "cassette" | "live") => void;
  onMutate: () => void;
}) {
  const canActivate = spec.status === "candidate" || spec.status === "retired";
  const canRetire = spec.status === "candidate" || spec.status === "active";
  const canCanary = spec.status === "candidate";
  const rationale = spec.provenance?.rationale ?? "";
  return (
    <div
      onClick={onSelect}
      className={`cursor-pointer rounded-xl border-[0.5px] bg-surface p-3 shadow-[var(--surface-shadow)] transition-colors ${
        selected ? "border-brand ring-1 ring-brand/30" : "border-surface-border hover:border-brand/40"}`}
    >
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge variant="ghost" className={SPEC_STATUS_TONE[spec.status] ?? ""}>
              {SPEC_STATUS_LABEL[spec.status] ?? spec.status}
            </Badge>
            <span className="truncate text-body font-medium">{spec.label}</span>
            <Badge variant="outline">{spec.kind}</Badge>
            <code className="font-mono text-micro text-muted-foreground">
              {spec.hash.slice(0, 12)}
            </code>
          </div>
          <p className="mt-1 truncate font-mono text-micro text-muted-foreground"
             title={configSummary(spec.config)}>
            {configSummary(spec.config)}
          </p>
          <p className="mt-0.5 text-micro text-muted-foreground">
            parent:<span className="font-mono">{spec.parentId ?? "—"}</span>
            {" · "}来源:{spec.provenance?.source || "手动创建"}
            {spec.provenance?.model ? ` (${spec.provenance.model})` : ""}
          </p>
          {rationale ? (
            <p className="mt-0.5 text-micro text-muted-foreground" title={rationale}>
              理由:{rationale.slice(0, 160)}
            </p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-1" onClick={(e) => e.stopPropagation()}>
          {canActivate ? (
            <Button size="xs" variant="outline" disabled={disabled} onClick={onActivate}>
              激活
            </Button>
          ) : null}
          {canRetire ? (
            <Button size="xs" variant="outline" disabled={disabled} onClick={onRetire}>
              退役
            </Button>
          ) : null}
          {canCanary ? (
            <Button size="xs" variant="outline" disabled={disabled}
                    onClick={() => onCanary("cassette")}>
              canary
            </Button>
          ) : null}
          {canCanary ? (
            <Button size="xs" variant="outline" disabled={disabled}
                    onClick={() => onCanary("live")}>
              live canary
            </Button>
          ) : null}
          <Button size="xs" variant="ghost" disabled={disabled} onClick={onMutate}>
            变异
          </Button>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ canary 历史

function TaskResults({ label, rows }: { label: string; rows?: CanaryTaskResult[] }) {
  if (!rows || rows.length === 0) return null;
  return (
    <div className="min-w-0">
      <p className="text-micro font-medium text-muted-foreground">{label}</p>
      <div className="mt-1 overflow-x-auto rounded-lg border border-surface-border/60">
        <table className="w-full text-left text-micro">
          <thead className="text-muted-foreground">
            <tr>
              <th className="px-2 py-1 font-normal">任务</th>
              <th className="px-2 py-1 font-normal">结果</th>
              <th className="px-2 py-1 font-normal">status</th>
              <th className="px-2 py-1 font-normal">steps</th>
              <th className="px-2 py-1 font-normal">tokens</th>
              <th className="px-2 py-1 font-normal">tool_errors</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.task_id} className="border-t border-surface-border/40">
                <td className="px-2 py-1 font-mono">{row.task_id}</td>
                <td className="px-2 py-1">
                  <Badge variant="ghost"
                         className={row.pass ? "bg-success/10 text-success"
                                             : "bg-destructive/10 text-destructive"}>
                    {row.pass ? "通过" : "失败"}
                  </Badge>
                </td>
                <td className="px-2 py-1">{row.status ?? "—"}</td>
                <td className="px-2 py-1 tabular-nums">{row.steps ?? "—"}</td>
                <td className="px-2 py-1 tabular-nums">{row.tokens ?? "—"}</td>
                <td className="max-w-56 truncate px-2 py-1"
                    title={toolErrorsText(row.tool_errors)}>
                  {toolErrorsText(row.tool_errors)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CanaryCard({ canary }: { canary: AgentCanary }) {
  const recommendation = canary.recommendation ?? canary.result?.recommendation ?? null;
  const reasons = canary.reasons ?? canary.result?.reasons ?? [];
  const tokenRatio = canary.result?.tokenRatio;
  return (
    <div className="rounded-lg border border-surface-border/60 p-2.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant="ghost" className={CANARY_STATUS_TONE[canary.status] ?? ""}>
          {CANARY_STATUS_LABEL[canary.status] ?? canary.status}
        </Badge>
        <Badge variant="outline">{canary.mode}</Badge>
        {recommendation ? (
          <Badge variant="ghost" className={REC_TONE[recommendation] ?? ""}>
            {recommendation}
          </Badge>
        ) : null}
        <span className="ml-auto font-mono text-micro text-muted-foreground">
          {fmtTime(canary.createdAt)}
        </span>
      </div>
      {canary.error ? (
        <p className="mt-1 text-micro text-destructive">{canary.error}</p>
      ) : null}
      {reasons.length ? (
        <ul className="mt-1 list-disc pl-4 text-micro text-muted-foreground">
          {reasons.map((reason, index) => <li key={index}>{reason}</li>)}
        </ul>
      ) : null}
      {tokenRatio !== undefined && tokenRatio !== null ? (
        <p className="mt-1 text-micro text-muted-foreground">
          token 比(候选/基线):{tokenRatio.toFixed(2)}×
        </p>
      ) : null}
      {canary.status === "running" ? (
        <p className="mt-1 flex items-center gap-1 text-micro text-info">
          <Loader2 className="size-3 animate-spin" />运行中…(每 3s 刷新)
        </p>
      ) : null}
      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
        <TaskResults label="候选" rows={canary.result?.candidate} />
        <TaskResults label="基线" rows={canary.result?.baseline} />
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ 页面

export default function SpecsPage() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<"all" | SpecKind>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  const specsQuery = useQuery({
    queryKey: ["specs", filter],
    queryFn: () => fetchSpecs(filter === "all" ? undefined : filter),
    refetchInterval: 5000,
  });
  const specs = specsQuery.data ?? [];

  const canariesQuery = useQuery({
    queryKey: ["canaries", selectedId],
    queryFn: () => fetchCanaries(selectedId ?? undefined),
    enabled: !!selectedId,
    refetchInterval: (query) =>
      (query.state.data ?? []).some((canary) => canary.status === "running") ? 3000 : false,
  });
  const canaries = canariesQuery.data ?? [];
  const selected = specs.find((spec) => spec.id === selectedId) ?? null;

  const run = async (key: string, action: () => Promise<void>, success: string) => {
    setPending(key);
    try {
      await action();
      toast.success(success);
      await qc.invalidateQueries({ queryKey: ["specs"] });
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setPending(null);
    }
  };

  const onActivate = (spec: AgentSpec) =>
    void run(`activate:${spec.id}`, async () => { await activateSpec(spec.id); }, "已激活");

  const onRetire = (spec: AgentSpec) =>
    void run(`retire:${spec.id}`, async () => { await retireSpec(spec.id); }, "已退役");

  const onCanary = (spec: AgentSpec, mode: "cassette" | "live") => {
    if (mode === "live" && !window.confirm(
      `live canary 会用真实模型跑「${spec.label}」与 active 的同任务对照,消耗真实模型额度。继续?`)) {
      return;
    }
    setSelectedId(spec.id);
    void run(`canary:${spec.id}`, async () => {
      await startSpecCanary(spec.id, { mode });
      await qc.invalidateQueries({ queryKey: ["canaries", spec.id] });
    }, mode === "live" ? "live canary 已发起" : "cassette canary 已发起");
  };

  const onMutate = (spec: AgentSpec) => {
    void run(`mutate:${spec.id}`, async () => {
      const candidate = await mutateSpec(spec.id);
      setSelectedId(candidate.id);
      toast.info(`已生成候选「${candidate.label}」,未激活`);
    }, "变异提议完成");
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <SlidersHorizontal className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">Agent 规格</h1>
        <p className="ml-2 hidden truncate text-caption text-muted-foreground md:block">
          候选 → canary 对照 → owner 激活;变异只生成 candidate
        </p>
      </PageHeader>

      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${PAGE_RAIL} space-y-3 py-4`}>
          <NewSpecForm />

          <div className="flex flex-wrap items-center gap-1.5">
            {FILTERS.map((item) => (
              <Button key={item.key} size="sm"
                      variant={filter === item.key ? "default" : "outline"}
                      onClick={() => setFilter(item.key)}>
                {item.label}
              </Button>
            ))}
            <span className="ml-auto text-caption text-muted-foreground">
              {specs.length} 个规格
            </span>
          </div>

          {specsQuery.error ? (
            <ApiError error={specsQuery.error}
                      onRetry={() => void specsQuery.refetch()} />
          ) : specsQuery.isPending ? (
            <p className="text-body text-muted-foreground">加载规格…</p>
          ) : specs.length === 0 ? (
            <p className="rounded-xl border border-dashed border-surface-border px-4 py-10 text-center text-caption text-muted-foreground">
              还没有 Agent 规格——先创建一个候选,再发起 canary 与 active 对照。
            </p>
          ) : (
            <div className="space-y-2">
              {specs.map((spec) => (
                <SpecCard key={spec.id} spec={spec} selected={spec.id === selectedId}
                          disabled={pending !== null}
                          onSelect={() => setSelectedId(spec.id)}
                          onActivate={() => onActivate(spec)}
                          onRetire={() => onRetire(spec)}
                          onCanary={(mode) => onCanary(spec, mode)}
                          onMutate={() => onMutate(spec)} />
              ))}
            </div>
          )}

          {selectedId ? (
            <section className="rounded-xl border-[0.5px] border-surface-border bg-surface p-4 shadow-[var(--surface-shadow)]">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-label font-semibold">
                  Canary 历史{selected ? ` · ${selected.label}` : ""}
                </h2>
                <Button size="xs" variant="ghost" className="ml-auto"
                        onClick={() => setSelectedId(null)}>
                  收起
                </Button>
              </div>
              {canariesQuery.error ? (
                <p className="mt-2 text-caption text-destructive">
                  {(canariesQuery.error as Error).message}
                </p>
              ) : canariesQuery.isPending ? (
                <p className="mt-2 text-caption text-muted-foreground">加载 canary…</p>
              ) : canaries.length === 0 ? (
                <p className="mt-2 text-caption text-muted-foreground">
                  该规格还没有 canary。选中候选后点「canary」发起对照。
                </p>
              ) : (
                <div className="mt-2 space-y-2">
                  {canaries.map((canary) => (
                    <CanaryCard key={canary.id} canary={canary} />
                  ))}
                </div>
              )}
            </section>
          ) : null}
        </div>
      </div>
    </div>
  );
}
