"use client";

/**
 * 已完成归档视图（只读）：最终报告卡（指标/摘要/产物路径，服务器前缀）
 * + 全程时间线回放。Phase 2 对接 sidecar reports 域。
 */
import { use } from "react";
import { useQuery } from "@tanstack/react-query";
import { FileArchive } from "lucide-react";
import { usePathname } from "next/navigation";

import { Badge } from "@multica/ui/components/ui/badge";
import { PageHeader } from "@multica/views/layout/page-header";
import { ProjectTimeline } from "@/features/autolab/project-timeline";
import { stageColor } from "@/features/autolab/stage";

interface ProjectView {
  id: string;
  identifier: string;
  title: string;
  stage: string;
  direction: string;
}

interface ArchiveView {
  projectId: string;
  reportedAt: string;
  finalMetric: number;
  baseline: number;
  reportPath: string;
  summary: string;
}

async function fixtureApi<T>(method: string, path: string): Promise<T> {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json" } });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

export default function ReportedArchivePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const pathname = usePathname();

  const { data: project } = useQuery({
    queryKey: ["autolab", "fixture-projects"],
    queryFn: () => fixtureApi<{ projects: ProjectView[] }>("GET", "/api/projects"),
    select: (d) => d.projects.find((p) => p.id === id || p.identifier === id) ?? null,
  });

  const { data: archive } = useQuery({
    queryKey: ["autolab", "archive", id],
    queryFn: () => fixtureApi<ArchiveView>("GET", `/api/projects/${id}/archive`),
  });

  if (!project) {
    return (
      <>
        <PageHeader>
          <FileArchive className="size-4 text-muted-foreground" />
          <h1 className="text-body font-medium">已完成</h1>
        </PageHeader>
        <div className="p-4 md:px-6">
          <p className="text-sm text-muted-foreground">项目不存在或已被删除。</p>
        </div>
      </>
    );
  }

  const delta = archive ? archive.finalMetric - archive.baseline : 0;

  return (
    <>
      <PageHeader>
        <FileArchive className="size-4 text-muted-foreground" />
        <h1 className="text-body font-medium">已完成</h1>
        <span className="font-mono text-caption text-muted-foreground">{project.identifier}</span>
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          {project.title}
        </p>
        <div className="ml-auto">
          <Badge variant="outline" className="gap-1.5">
            <span className="size-1.5 rounded-full" style={{ backgroundColor: stageColor("reported") }} />
            已归档（只读）
          </Badge>
        </div>
      </PageHeader>

      <div className="flex min-h-0 flex-1 flex-col gap-4 p-4 md:px-6 lg:flex-row">
        <div className="flex min-w-0 flex-1 flex-col gap-4 overflow-y-auto">
          {/* 最终报告 */}
          <div className="rounded-lg border">
            <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">最终报告</p>
            {archive ? (
              <div className="space-y-3 px-4 py-4 text-sm">
                <p className="flex items-baseline gap-3">
                  <span className="text-caption text-muted-foreground">最终指标（test）</span>
                  <span className="font-mono text-xl font-semibold tabular-nums">
                    {archive.finalMetric.toFixed(4)}
                  </span>
                  <span className="font-mono text-caption text-emerald-600">
                    Δ+{delta.toFixed(4)} vs BASE {archive.baseline.toFixed(4)}
                  </span>
                </p>
                <p className="leading-relaxed text-muted-foreground">{archive.summary}</p>
                <p className="font-mono text-caption text-muted-foreground">
                  📄 {archive.reportPath}
                </p>
                <p className="text-caption text-muted-foreground">
                  归档于 {archive.reportedAt ? new Date(archive.reportedAt).toLocaleString("zh-CN") : "—"}
                </p>
              </div>
            ) : (
              <p className="px-4 py-3 text-caption text-muted-foreground">加载归档数据…</p>
            )}
          </div>

          <p className="text-caption text-muted-foreground">
            产物（代码快照 / 指标 / 报告）均在服务器工作区，可随时追溯。
          </p>
        </div>

        {/* 全程时间线回放 */}
        <div className="flex min-h-64 min-w-0 flex-col rounded-lg border lg:w-96 lg:shrink-0">
          <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
            全程时间线（回放）
          </p>
          <div className="flex min-h-0 flex-1 flex-col p-4">
            <ProjectTimeline projectId={project.id} />
          </div>
        </div>
      </div>
    </>
  );
}
