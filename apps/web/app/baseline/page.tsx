"use client";

/** Baseline = 一个 Agent 会话:它探索代码、选环境、写适配器、跑通基线;缺材料停下来问你,
 * 跑通后交你确认。确认后项目进入研究阶段。 */

import { useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { FlaskConical, FolderKanban, History, Loader2, Play, SlidersHorizontal } from "lucide-react";
import {
  fetchLatestRun, fetchProjectRuns, fetchSpecs, isActive, startBaseline,
  type Project, type Run,
} from "@/lib/api";
import { RunPanel } from "@/components/agent-run/panel";
import { DatasetBoard } from "@/components/agent-run/datasets";
import { ProjectTabs, useProjectSelection } from "@/components/project-picker";
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

function formatBaselineRunOption(run: Run, idx: number, total: number): string {
  const st = STATUS_SHORT[run.status] ?? run.status;
  const time = run.createdAt ? `${run.createdAt.slice(5, 10)} ${run.createdAt.slice(11, 16)}` : "";
  return `#${total - idx}${idx === 0 ? "(最新)" : ""} · ${st} · ${run.steps}步 (${time})`;
}

function StartForm({ project, again }: { project: Project; again: boolean }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const [network, setNetwork] = useState(false);
  const [agentSpecId, setAgentSpecId] = useState("");
  const [starting, setStarting] = useState(false);

  const specsQuery = useQuery({
    queryKey: ["specs", "baseline"],
    queryFn: () => fetchSpecs("baseline"),
  });
  const availableSpecs = (specsQuery.data ?? []).filter(
    (s) => s.status === "active" || s.status === "candidate",
  );
  const activeSpec = availableSpecs.find((s) => s.status === "active");

  const start = async () => {
    setStarting(true);
    try {
      await startBaseline(project.id, {
        note,
        network,
        agent_spec_id: agentSpecId || undefined,
      });
      setNote("");
      void queryClient.invalidateQueries({ queryKey: ["run-latest", project.id] });
      void queryClient.invalidateQueries({ queryKey: ["project-runs", project.id] });
    } catch (err) {
      toast.error(`启动失败:${(err as Error).message}`);
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="space-y-2 rounded-xl border-[0.5px] border-surface-border bg-card p-3">
      <p className="text-caption font-semibold">{again ? "重新跑 Baseline" : "启动 Baseline Agent"}</p>
      <p className="text-micro text-muted-foreground">
        Agent 会读代码、选环境、写适配器、冒烟后正式跑;缺数据集/权重/信息会停下来问你。
      </p>
      <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2}
                placeholder="补充说明(可选):要跑哪些数据集、训练口径、已知的坑…"
                className="w-full rounded-md border-[0.5px] border-surface-border bg-background px-2 py-1.5 text-caption" />
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
          <input type="checkbox" checked={network} onChange={(e) => setNetwork(e.target.checked)} />
          服务器可访问外网(允许 Agent 自行下载)
        </label>
        {availableSpecs.length > 0 ? (
          <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
            <SlidersHorizontal className="size-3" />
            Agent 规格
            <select
              value={agentSpecId}
              onChange={(e) => setAgentSpecId(e.target.value)}
              className="h-7 rounded-md border-[0.5px] border-surface-border bg-background px-1.5 text-micro"
            >
              <option value="">
                默认 ({activeSpec ? `当前生效:${activeSpec.label}` : "系统内置"})
              </option>
              {availableSpecs.map((sp) => (
                <option key={sp.id} value={sp.id}>
                  [{sp.status === "active" ? "生效中" : "候选"}] {sp.label} ({sp.hash.slice(0, 8)})
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <Button size="sm" className="ml-auto gap-1" disabled={starting} onClick={() => void start()}>
          {starting ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
          启动
        </Button>
      </div>
    </div>
  );
}

function BaselineSection({ project }: { project: Project }) {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const { data } = useQuery({
    queryKey: ["run-latest", project.id, "baseline"],
    queryFn: () => fetchLatestRun(project.id, "baseline"),
    refetchInterval: 4000,
  });
  const latestRun = data?.run ?? null;

  const runsQuery = useQuery({
    queryKey: ["project-runs", project.id, "baseline"],
    queryFn: () => fetchProjectRuns(project.id, "baseline", 20),
    refetchInterval: isActive(latestRun) ? 5000 : false,
  });
  const runs = runsQuery.data?.runs ?? (latestRun ? [latestRun] : []);
  const run = (selectedRunId ? runs.find((r) => r.id === selectedRunId) : null) ?? latestRun;
  const viewingHistorical = !!run && !!latestRun && run.id !== latestRun.id;

  return (
    <div className="space-y-3">
      {project.baselinePassedAt ? (
        <p className="rounded-lg bg-success/10 px-3 py-2 text-caption text-success">
          基线已确认,可以去 <Link href={`/research?project=${project.id}`} className="underline">研究</Link>。
          重新跑会在你再次确认后覆盖当前基线。
        </p>
      ) : null}
      {!isActive(latestRun) ? <StartForm project={project} again={!!latestRun} /> : null}
      {runs.length > 1 ? (
        <div className="flex flex-wrap items-center gap-2 rounded-xl border-[0.5px] border-surface-border bg-card px-3 py-2">
          <History className="size-3.5 text-muted-foreground" />
          <span className="text-micro font-medium text-muted-foreground">Baseline 会话回放:</span>
          <select
            value={run?.id ?? ""}
            onChange={(e) => setSelectedRunId(e.target.value || null)}
            className="h-7 min-w-0 flex-1 rounded-md border-[0.5px] border-surface-border bg-background px-2 font-mono text-micro sm:max-w-md"
          >
            {runs.map((r, idx) => (
              <option key={r.id} value={r.id}>
                {formatBaselineRunOption(r, idx, runs.length)}
              </option>
            ))}
          </select>
          {viewingHistorical ? (
            <Button size="xs" variant="outline" onClick={() => setSelectedRunId(null)}>
              回到最新会话
            </Button>
          ) : null}
        </div>
      ) : null}
      {run ? <RunPanel key={run.id} project={project} run={run} /> : null}
      <DatasetBoard project={project} />
    </div>
  );
}

export default function BaselinePage() {
  const { projects, selected, select } = useProjectSelection((p) => !!p.workdir);
  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <FlaskConical className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">Baseline</h1>
        <p className="ml-2 hidden truncate text-caption text-muted-foreground md:block">
          Agent 跑通基线,缺材料问你,跑通后你确认
        </p>
      </PageHeader>
      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${PAGE_RAIL} space-y-3 py-4`}>
          {selected ? (
            <>
              <ProjectTabs projects={projects} selected={selected} onSelect={select} />
              <BaselineSection key={selected.id} project={selected} />
            </>
          ) : (
            <EmptyState icon={FolderKanban} title="还没有可跑基线的项目"
                        description="先在项目页立项,把代码放到服务器工作区。"
                        action={<Link href="/projects"><Button size="sm">去立项</Button></Link>} />
          )}
        </div>
      </div>
    </div>
  );
}
