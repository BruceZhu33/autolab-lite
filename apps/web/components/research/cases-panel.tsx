"use client";

/** 结构化研究记忆浏览器 (Phase C1):
 * - 按 (假设, 改动, 数据集, 变体) 指纹去重展示历史实验案例
 * - 支持关键词检索、按数据集过滤、按结果分类过滤 (有提升 / 证据不足 / 未提升 / 违规或失败)
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  BrainCircuit, ChevronDown, ChevronUp, Search,
} from "lucide-react";
import {
  fetchProjectDatasets, fetchResearchCases,
  type Project, type ResearchCase,
} from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";

const FAILURE_META: Record<string, { label: string; cls: string }> = {
  success: { label: "有提升 ✓", cls: "bg-success/10 text-success" },
  insufficient_evidence: { label: "证据不足 (<3 seed)", cls: "bg-warning/15 text-warning" },
  not_improved: { label: "未提升", cls: "bg-muted/60 text-muted-foreground" },
  guardrail: { label: "护栏违规", cls: "bg-destructive/10 text-destructive" },
  eval_failed: { label: "评测失败", cls: "bg-destructive/10 text-destructive" },
  no_valid_experiment: { label: "无有效实验", cls: "bg-muted/60 text-muted-foreground" },
  no_evidence: { label: "无证据", cls: "bg-muted/60 text-muted-foreground" },
};

function fmtGain(v: number | null | undefined) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return `${v >= 0 ? "+" : ""}${v.toFixed(4)}`;
}

export function ResearchCasesPanel({ project }: { project: Project }) {
  const [expanded, setExpanded] = useState(false);
  const [query, setQuery] = useState("");
  const [dataset, setDataset] = useState("");
  const [classFilter, setClassFilter] = useState<string>("all");

  const { data: dsData } = useQuery({
    queryKey: ["datasets", project.id],
    queryFn: () => fetchProjectDatasets(project.id),
  });
  const datasets = useMemo(
    () => (dsData?.datasets ?? []).filter((d) => d.state === "confirmed").map((d) => d.name),
    [dsData?.datasets],
  );

  const casesQuery = useQuery({
    queryKey: ["research-cases", project.id, query, dataset],
    queryFn: () => fetchResearchCases(project.id, { query, dataset: dataset || undefined, limit: 30 }),
  });

  const allCases = casesQuery.data?.cases ?? [];
  const filteredCases = useMemo(() => {
    if (classFilter === "all") return allCases;
    if (classFilter === "success") return allCases.filter((c) => c.success || c.failureClass === "success");
    return allCases.filter((c) => c.failureClass === classFilter);
  }, [allCases, classFilter]);

  const successCount = allCases.filter((c) => c.success).length;
  const notImprovedCount = allCases.filter((c) => c.failureClass === "not_improved").length;
  const insufficientCount = allCases.filter((c) => c.failureClass === "insufficient_evidence").length;

  if (allCases.length === 0 && !query && !dataset) return null;

  return (
    <div className="space-y-2.5 rounded-2xl border-[0.5px] border-surface-border bg-card p-3.5 shadow-[var(--surface-shadow)]">
      <div className="flex flex-wrap items-center gap-2">
        <BrainCircuit className="size-4 text-brand" />
        <span className="text-caption font-semibold">结构化研究记忆库 (Case Memory)</span>
        <span className="rounded-full bg-muted/60 px-2 py-0.5 font-mono text-micro text-muted-foreground">
          {allCases.length} 条去重案例
        </span>
        {successCount > 0 ? (
          <span className="rounded-full bg-success/10 px-2 py-0.5 font-mono text-micro text-success">
            有提升 {successCount}
          </span>
        ) : null}
        {insufficientCount > 0 ? (
          <span className="rounded-full bg-warning/15 px-2 py-0.5 font-mono text-micro text-warning">
            待补多 seed {insufficientCount}
          </span>
        ) : null}
        {notImprovedCount > 0 ? (
          <span className="rounded-full bg-muted/50 px-2 py-0.5 font-mono text-micro text-muted-foreground">
            未提升 {notImprovedCount}
          </span>
        ) : null}
        <span className="ml-auto hidden text-micro text-muted-foreground md:inline">
          Agent 每轮开始前自动检索此库以避免重复试错
        </span>
        <Button
          size="xs"
          variant="outline"
          className="gap-1"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? <ChevronUp className="size-3" /> : <ChevronDown className="size-3" />}
          {expanded ? "收起记忆库" : `检索 / 浏览 (${allCases.length})`}
        </Button>
      </div>

      {expanded ? (
        <div className="space-y-2.5 border-t-[0.5px] border-surface-border pt-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-48 flex-1">
              <Search className="pointer-events-none absolute top-2 left-2.5 size-3.5 text-muted-foreground" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="检索假设、改动方案、变体名或失败原因…"
                className="h-7 pl-8 text-micro"
              />
            </div>
            {datasets.length > 1 ? (
              <select
                value={dataset}
                onChange={(e) => setDataset(e.target.value)}
                className="h-7 rounded-md border-[0.5px] border-surface-border bg-background px-2 font-mono text-micro"
              >
                <option value="">全部数据集</option>
                {datasets.map((d) => (
                  <option key={d} value={d}>{d}</option>
                ))}
              </select>
            ) : null}
            <div className="inline-flex rounded-md border-[0.5px] border-surface-border bg-muted/40 p-0.5 text-micro">
              {[
                { id: "all", label: "全部" },
                { id: "success", label: "有提升" },
                { id: "insufficient_evidence", label: "证据不足" },
                { id: "not_improved", label: "未提升" },
              ].map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => setClassFilter(tab.id)}
                  className={`rounded px-2 py-0.5 transition-colors ${
                    classFilter === tab.id
                      ? "bg-background font-medium text-foreground shadow-xs"
                      : "text-muted-foreground"
                  }`}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          </div>

          {filteredCases.length === 0 ? (
            <p className="py-3 text-center text-micro text-muted-foreground">没有匹配的实验记忆案例</p>
          ) : (
            <div className="max-h-80 space-y-2 overflow-y-auto pr-1">
              {filteredCases.map((c: ResearchCase) => {
                const meta = FAILURE_META[c.failureClass] ?? {
                  label: c.failureClass || "未知",
                  cls: "bg-muted/60 text-muted-foreground",
                };
                return (
                  <div
                    key={c.id}
                    className="space-y-1 rounded-xl border-[0.5px] border-surface-border bg-background/60 p-2.5 text-micro"
                  >
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className={`rounded-full px-2 py-0.5 font-medium ${meta.cls}`}>
                        {meta.label}
                      </span>
                      {c.dataset ? (
                        <span className="rounded bg-muted/60 px-1.5 py-0.5 font-mono">
                          {c.dataset}
                        </span>
                      ) : null}
                      {c.variant ? (
                        <span className="rounded bg-brand/10 px-1.5 py-0.5 font-mono text-brand">
                          {c.variant}
                        </span>
                      ) : null}
                      {c.meanGain !== null ? (
                        <span
                          className={`font-mono font-medium tabular-nums ${
                            c.meanGain > 0 ? "text-success" : "text-muted-foreground"
                          }`}
                        >
                          增益 {fmtGain(c.meanGain)}
                          {c.metrics?.n ? ` (${c.metrics.n} seed)` : ""}
                        </span>
                      ) : null}
                      {c.ciLow !== null && c.ciHigh !== null ? (
                        <span className="font-mono text-muted-foreground tabular-nums">
                          95% CI [{fmtGain(c.ciLow)}, {fmtGain(c.ciHigh)}]
                        </span>
                      ) : null}
                      <span className="ml-auto font-mono text-muted-foreground/80">
                        第 {c.roundNo} 轮 · #{c.fingerprint.slice(0, 8)}
                      </span>
                    </div>
                    <p className="text-caption">
                      <span className="font-medium text-muted-foreground">假设: </span>
                      {c.hypothesis || "-"}
                    </p>
                    {c.changes ? (
                      <p className="text-muted-foreground">
                        <span className="font-medium">改动: </span>
                        {c.changes}
                      </p>
                    ) : null}
                    {c.analysis ? (
                      <p className="text-muted-foreground/80">
                        <span className="font-medium">分析: </span>
                        {c.analysis}
                      </p>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
