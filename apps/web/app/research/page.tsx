"use client";

/** 研究 = 一个 Agent 会话:论文/参考库 + 你的想法 → 自由迭代;是否提升由 Python 对照基线判定。 */

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { BadgeCheck, FlaskConical, History, Lightbulb, ShieldCheck } from "lucide-react";
import {
  fetchLatestRun, fetchMetricContract, fetchProjectDatasets, fetchProjectRuns, fetchResearchRounds,
  isActive, type Project, type Run,
} from "@/lib/api";
import { RunPanel } from "@/components/agent-run/panel";
import { ProjectTabs, useProjectSelection } from "@/components/project-picker";
import { CampaignPanel } from "@/components/research/campaign-panel";
import { ResearchCasesPanel } from "@/components/research/cases-panel";
import { ResearchJournal } from "@/components/research/journal";
import { ResearchStartForm } from "@/components/research/start-form";
import { AdoptButton } from "@/components/research/adopt-button";
import { ValidateLaunch } from "@/components/research/validate-dialog";
import { EmptyState } from "@/components/empty-state";
import { Button } from "~/vendor/ui/components/ui/button";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "~/vendor/views/layout/page-header";

const STATUS_SHORT: Record<string, string> = {
  running: "工作中",
  waiting_human: "等你回复",
  waiting_jobs: "等 GPU",
  done: "完成",
  failed: "失败",
  stopped: "已停止",
};

function formatRunOption(run: Run, idx: number, total: number): string {
  const mode = run.config?.mode === "validation"
    ? "全量验证"
    : String(run.config?.tier ?? "research");
  const st = STATUS_SHORT[run.status] ?? run.status;
  const res = (run.result ?? null) as {
    bestGain?: number | null;
    champion?: { meanGain?: number | null; label?: string | null };
  } | null;
  const gain = res?.champion?.meanGain ?? res?.bestGain ?? null;
  const gainStr = typeof gain === "number" && !Number.isNaN(gain)
    ? ` · ${gain >= 0 ? "+" : ""}${gain.toFixed(4)}`
    : "";
  const time = run.createdAt ? `${run.createdAt.slice(5, 10)} ${run.createdAt.slice(11, 16)}` : "";
  return `#${total - idx}${idx === 0 ? "(最新)" : ""} · ${mode} · ${st}${gainStr} · ${run.steps}步 (${time})`;
}

function BaselineCard({ project }: { project: Project }) {
  const { data } = useQuery({
    queryKey: ["datasets", project.id],
    queryFn: () => fetchProjectDatasets(project.id),
  });
  const { data: contractData } = useQuery({
    queryKey: ["baseline-contract", project.id],
    queryFn: () => fetchMetricContract(project.id),
  });
  const badge = data?.baseline;
  const contract = contractData?.contract ?? null;
  const confirmed = (data?.datasets ?? []).filter((d) => d.state === "confirmed");
  if (!badge) return null;
  return (
    <div className="rounded-xl border-[0.5px] border-surface-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <FlaskConical className="size-4 text-brand" />
        <span className="text-caption font-semibold">对比基线</span>
        <span className={`rounded-full px-2 py-0.5 text-micro font-medium ${
          badge.kind === "adopted" ? "bg-success/10 text-success"
            : badge.kind === "forked" ? "bg-info/10 text-info" : "bg-muted/60 text-muted-foreground"}`}>
          {badge.version} · {badge.label}({badge.kindLabel} {badge.date})
        </span>
        {badge.forkedFrom ? (
          <span className="text-micro text-muted-foreground">← 分叉自 {badge.forkedFrom}</span>
        ) : null}
        {contract ? (
          <Link
            href={`/baseline?project=${project.id}`}
            className="rounded-full bg-success/10 px-2 py-0.5 font-mono text-micro font-medium text-success hover:underline"
            title="点击前往 Baseline 页查看或修改评测指标契约"
          >
            <ShieldCheck className="mr-1 inline size-3 align-[-1px]" />
            契约: {contract.primary} ({contract.direction === "maximize" ? "↑" : "↓"})
          </Link>
        ) : (
          <Link
            href={`/baseline?project=${project.id}`}
            className="rounded-full bg-muted/60 px-2 py-0.5 text-micro text-muted-foreground hover:underline"
          >
            未固化指标契约 (去配置 →)
          </Link>
        )}
        <span className="ml-auto text-micro text-muted-foreground">
          研究会话的实验都和下面这组数字比
        </span>
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {confirmed.map((d) => (
          <span key={d.name} className="rounded-full bg-muted/50 px-2 py-0.5 font-mono text-micro tabular-nums"
                title={d.command}>
            {d.name}{" "}
            {Object.entries(d.metrics ?? {})
              .map(([k, v]) => `${k} ${typeof v === "number" ? v.toFixed(4) : v}`)
              .join(" · ")}
          </span>
        ))}
      </div>
    </div>
  );
}

function ResearchSection({ project }: { project: Project }) {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [allRoundsMode, setAllRoundsMode] = useState(false);

  const { data } = useQuery({
    queryKey: ["run-latest", project.id, "research"],
    queryFn: () => fetchLatestRun(project.id, "research"),
    refetchInterval: 4000,
  });
  const latestRun = data?.run ?? null;

  const runsQuery = useQuery({
    queryKey: ["project-runs", project.id, "research"],
    queryFn: () => fetchProjectRuns(project.id, "research", 20),
    refetchInterval: isActive(latestRun) ? 5000 : false,
  });
  const runs = runsQuery.data?.runs ?? (latestRun ? [latestRun] : []);

  const run = (selectedRunId ? runs.find((r) => r.id === selectedRunId) : null) ?? latestRun;
  const viewingHistorical = !!run && !!latestRun && run.id !== latestRun.id;

  const { data: rounds } = useQuery({
    queryKey: ["research-rounds", project.id, allRoundsMode ? "all" : (run?.id ?? "")],
    queryFn: () => fetchResearchRounds(project.id, allRoundsMode ? undefined : run!.id),
    enabled: !!run || allRoundsMode,
    refetchInterval: isActive(run) ? 5000 : false,
  });

  const result = (run?.result ?? null) as {
    success?: boolean; adopted?: boolean; adoptedAt?: string;
    champion?: {
      round: number; label?: string | null; dataset?: string | null;
      n: number; meanGain: number;
    };
    validation?: { datasets?: string[] };
  } | null;
  const validated = run?.config?.mode === "validation";
  const canValidate = !!run && !isActive(latestRun) && !validated && !!result?.success;
  const canAdopt = !!run && !isActive(latestRun) && validated && !result?.adopted;

  return (
    <div className="space-y-3">
      <BaselineCard project={project} />
      {!isActive(latestRun) ? <ResearchStartForm project={project} /> : null}
      <CampaignPanel project={project} />

      {runs.length > 1 ? (
        <div className="flex flex-wrap items-center gap-2 rounded-xl border-[0.5px] border-surface-border bg-card px-3 py-2">
          <History className="size-3.5 text-muted-foreground" />
          <span className="text-micro font-medium text-muted-foreground">研究会话回放:</span>
          <select
            value={run?.id ?? ""}
            onChange={(e) => setSelectedRunId(e.target.value || null)}
            className="h-7 min-w-0 flex-1 rounded-md border-[0.5px] border-surface-border bg-background px-2 font-mono text-micro sm:max-w-md"
          >
            {runs.map((r, idx) => (
              <option key={r.id} value={r.id}>
                {formatRunOption(r, idx, runs.length)}
              </option>
            ))}
          </select>
          {viewingHistorical ? (
            <>
              <span className="rounded-full bg-warning/15 px-2 py-0.5 text-micro font-medium text-warning">
                正在回看历史会话
              </span>
              <Button
                size="xs"
                variant="outline"
                onClick={() => setSelectedRunId(null)}
              >
                回到最新会话
              </Button>
            </>
          ) : null}
        </div>
      ) : null}

      {run ? <RunPanel key={run.id} project={project} run={run} /> : null}
      {canAdopt ? (
        <div className={`space-y-2 rounded-xl border-[0.5px] p-3 ${
          result?.success ? "border-success/40 bg-success/5" : "border-warning/50 bg-warning/5"}`}>
          <div className="flex flex-wrap items-center gap-2">
            <BadgeCheck className={`size-4 ${result?.success ? "text-success" : "text-warning"}`} />
            <p className="min-w-0 flex-1 text-caption">
              验证完成{result?.success ? "" : "(结论:未通过)"},是否把这套实测
              <span className="font-medium">采纳为新的基线</span>?
              {!result?.success ? "——你可以拍板采纳,验证结论会原样留痕。" : ""}
            </p>
            <AdoptButton project={project} runId={run.id} />
          </div>
          <ul className="space-y-0.5 pl-6 text-micro text-muted-foreground">
            {(result?.validation?.datasets ?? []).map((d) => <li key={d}>{d}</li>)}
          </ul>
          <p className="text-micro text-muted-foreground">
            采纳 = 每个数据集的基线更新为本次实测均值(旧快照入历史);下一轮研究将对比新基线。
          </p>
        </div>
      ) : null}
      {validated && result?.adopted ? (
        <div className="flex items-center gap-2 rounded-xl border-[0.5px] border-success/40 bg-success/5 p-3">
          <BadgeCheck className="size-4 text-success" />
          <p className="text-caption">
            已采纳为新基线{result.adoptedAt ? `(${result.adoptedAt.slice(0, 16).replace("T", " ")})` : ""}
            ——研究页可以开始下一轮迭代,对比对象已是新基线。
          </p>
        </div>
      ) : null}
      {canValidate ? (
        <div className="flex flex-wrap items-center gap-2 rounded-xl border-[0.5px] border-success/40 bg-success/5 p-3">
          <BadgeCheck className="size-4 text-success" />
          <p className="min-w-0 flex-1 text-caption">
            研究已判定有提升
            {result?.champion ? (
              <>——冠军「{result.champion.label ?? "?"}」[{result.champion.dataset ?? "-"}]
                {" "}{result.champion.n} seed 均值增益
                <span className="font-mono">
                  {result.champion.meanGain >= 0 ? "+" : ""}{result.champion.meanGain.toFixed(4)}
                </span>(第 {result.champion.round} 轮)</>
            ) : null}
            。是否在全部数据集上验证该方案?
          </p>
          <ValidateLaunch project={project} sourceRunId={run.id} />
        </div>
      ) : null}
      <ResearchCasesPanel project={project} />
      <ResearchJournal
        rounds={rounds?.rounds ?? []}
        projectTitle={project.name}
        headerExtra={
          runs.length > 1 ? (
            <div className="inline-flex rounded-md border-[0.5px] border-surface-border bg-muted/40 p-0.5 text-micro">
              <button
                type="button"
                onClick={() => setAllRoundsMode(false)}
                className={`rounded px-2 py-0.5 transition-colors ${
                  !allRoundsMode ? "bg-background font-medium text-foreground shadow-xs" : "text-muted-foreground"
                }`}
              >
                当前会话轮次
              </button>
              <button
                type="button"
                onClick={() => setAllRoundsMode(true)}
                className={`rounded px-2 py-0.5 transition-colors ${
                  allRoundsMode ? "bg-background font-medium text-foreground shadow-xs" : "text-muted-foreground"
                }`}
              >
                项目全部历史轮次
              </button>
            </div>
          ) : undefined
        }
      />
    </div>
  );
}

export default function ResearchPage() {
  const { projects, selected, select } = useProjectSelection((p) => !!p.baselinePassedAt);
  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <Lightbulb className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">研究</h1>
        <p className="ml-2 hidden truncate text-caption text-muted-foreground md:block">
          给论文/参考库和你的想法,Agent 自由迭代;提升由 Python 对照基线判定
        </p>
      </PageHeader>
      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${PAGE_RAIL} space-y-3 py-4`}>
          {selected ? (
            <>
              <ProjectTabs projects={projects} selected={selected} onSelect={select} />
              <ResearchSection key={selected.id} project={selected} />
            </>
          ) : (
            <EmptyState icon={Lightbulb} title="还没有可研究的项目"
                        description="基线确认后的项目会出现在这里。"
                        action={<Link href="/baseline"><Button size="sm">去跑 Baseline</Button></Link>} />
          )}
        </div>
      </div>
    </div>
  );
}
