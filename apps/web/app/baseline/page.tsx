"use client";

/** Baseline = 一个 Agent 会话:它探索代码、选环境、写适配器、跑通基线;缺材料停下来问你,
 * 跑通后交你确认。确认后项目进入研究阶段。 */

import { useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { FlaskConical, FolderKanban, Loader2, Play } from "lucide-react";
import { fetchLatestRun, isActive, startBaseline, type Project } from "@/lib/api";
import { RunPanel } from "@/components/agent-run/panel";
import { ProjectTabs, useProjectSelection } from "@/components/project-picker";
import { EmptyState } from "@/components/empty-state";
import { Button } from "~/vendor/ui/components/ui/button";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "~/vendor/views/layout/page-header";

function StartForm({ project, again }: { project: Project; again: boolean }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const [network, setNetwork] = useState(false);
  const [starting, setStarting] = useState(false);

  const start = async () => {
    setStarting(true);
    try {
      await startBaseline(project.id, { note, network });
      setNote("");
      void queryClient.invalidateQueries({ queryKey: ["run-latest", project.id] });
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
      <div className="flex items-center gap-3">
        <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
          <input type="checkbox" checked={network} onChange={(e) => setNetwork(e.target.checked)} />
          服务器可访问外网(允许 Agent 自行下载)
        </label>
        <Button size="sm" className="ml-auto gap-1" disabled={starting} onClick={() => void start()}>
          {starting ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
          启动
        </Button>
      </div>
    </div>
  );
}

function BaselineSection({ project }: { project: Project }) {
  const { data } = useQuery({
    queryKey: ["run-latest", project.id, "baseline"],
    queryFn: () => fetchLatestRun(project.id, "baseline"),
    refetchInterval: 4000,
  });
  const run = data?.run ?? null;
  return (
    <div className="space-y-3">
      {project.baselinePassedAt ? (
        <p className="rounded-lg bg-success/10 px-3 py-2 text-caption text-success">
          基线已确认,可以去 <Link href={`/research?project=${project.id}`} className="underline">研究</Link>。
          重新跑会在你再次确认后覆盖当前基线。
        </p>
      ) : null}
      {!isActive(run) ? <StartForm project={project} again={!!run} /> : null}
      {run ? <RunPanel key={run.id} project={project} run={run} /> : null}
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
