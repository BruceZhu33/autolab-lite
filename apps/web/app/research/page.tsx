"use client";

/** 研究 = 一个 Agent 会话:论文/参考库 + 你的想法 → 自由迭代;是否提升由 Python 对照基线判定。 */

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Lightbulb } from "lucide-react";
import { fetchLatestRun, fetchResearchRounds, isActive, type Project } from "@/lib/api";
import { RunPanel } from "@/components/agent-run/panel";
import { ProjectTabs, useProjectSelection } from "@/components/project-picker";
import { ResearchJournal } from "@/components/research/journal";
import { ResearchStartForm } from "@/components/research/start-form";
import { EmptyState } from "@/components/empty-state";
import { Button } from "~/vendor/ui/components/ui/button";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "~/vendor/views/layout/page-header";

function ResearchSection({ project }: { project: Project }) {
  const { data } = useQuery({
    queryKey: ["run-latest", project.id, "research"],
    queryFn: () => fetchLatestRun(project.id, "research"),
    refetchInterval: 4000,
  });
  const run = data?.run ?? null;
  const { data: rounds } = useQuery({
    queryKey: ["research-rounds", project.id, run?.id ?? ""],
    queryFn: () => fetchResearchRounds(project.id, run!.id),
    enabled: !!run,
    refetchInterval: isActive(run) ? 5000 : false,
  });
  return (
    <div className="space-y-3">
      {!isActive(run) ? <ResearchStartForm project={project} /> : null}
      {run ? <RunPanel key={run.id} project={project} run={run} /> : null}
      <ResearchJournal rounds={rounds?.rounds ?? []} />
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
