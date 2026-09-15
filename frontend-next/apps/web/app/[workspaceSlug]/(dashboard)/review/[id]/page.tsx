"use client";

/**
 * 方案评审现场（Work 模块，AutoLab 特有）。
 *
 * 对齐口径 2026-09-15：看板「方案评审」卡片点击进入本页。
 * 两段式评审：草案 → 反问清单 → 定稿过门（进入基线验证）。
 * 内嵌项目时间线（回复反问 / 与 Reviewer 讨论）。
 * Phase 2 对接 sidecar review 域（D22 两段式 + D17 论文登记 + D42 文献对照）；
 * 定稿按钮将改走 review service guard。
 */
import { use } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { toast } from "sonner";

import { Badge } from "@multica/ui/components/ui/badge";
import { Button } from "@multica/ui/components/ui/button";
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
  paper: string | null;
}

interface ReviewView {
  projectId: string;
  draft: string;
  questions: { id: string; question: string; status: string }[];
  paper: string | null;
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

export default function ReviewPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const qc = useQueryClient();
  const pathname = usePathname();
  const wsSlug = pathname.split("/")[1] ?? "autolab";

  const { data: project } = useQuery({
    queryKey: ["autolab", "fixture-projects"],
    queryFn: () => fixtureApi<{ projects: ProjectView[] }>("GET", "/api/projects"),
    select: (d) => d.projects.find((p) => p.id === id || p.identifier === id) ?? null,
  });

  const { data: review } = useQuery({
    queryKey: ["autolab", "review", id],
    queryFn: () => fixtureApi<ReviewView>("GET", `/api/projects/${id}/review`),
  });

  if (!project) {
    return (
      <>
        <PageHeader>
          <h1 className="text-body font-medium">方案评审</h1>
        </PageHeader>
        <div className="p-4 md:px-6">
          <p className="text-sm text-muted-foreground">项目不存在或已被删除。</p>
        </div>
      </>
    );
  }

  /** 评审定稿门（Phase 4 改走 review service guard）。 */
  const finalize = async () => {
    try {
      await fixtureApi("PUT", `/api/issues/${project.id}`, { status: "baseline" });
      toast.success(`${project.identifier} 评审定稿`, {
        description: "已进入「基线验证」；下一步派发 BASE 并验收。",
      });
      void qc.invalidateQueries();
    } catch (e) {
      toast.error("定稿失败", {
        description: (e instanceof Error ? e.message : String(e)).slice(0, 300),
      });
    }
  };

  return (
    <>
      <PageHeader>
        <span className="size-1.5 rounded-full" style={{ backgroundColor: stageColor("review") }} />
        <h1 className="text-body font-medium">方案评审</h1>
        <span className="font-mono text-caption text-muted-foreground">{project.identifier}</span>
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          {project.title}
        </p>
        <div className="ml-auto">
          {project.stage === "review" ? (
            <Button size="sm" onClick={() => void finalize()}>
              评审定稿，进入基线验证
            </Button>
          ) : (
            <Badge variant="outline" className="gap-1.5">
              <span className="size-1.5 rounded-full" style={{ backgroundColor: stageColor(project.stage) }} />
              已离开评审阶段（当前：{stageLabel(project.stage)}）
            </Badge>
          )}
        </div>
      </PageHeader>

      <div className="flex min-h-0 flex-1 flex-col gap-4 p-4 md:px-6 lg:flex-row">
        <div className="flex min-w-0 flex-1 flex-col gap-4 overflow-y-auto">
          {/* 草案 */}
          <div className="rounded-lg border">
            <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
              方案草案
              {review?.paper && (
                <span className="ml-2 font-normal">📄 对照论文：{review.paper}</span>
              )}
            </p>
            <pre className="whitespace-pre-wrap break-words px-4 py-3 font-sans text-sm leading-relaxed">
              {review?.draft ?? "草案生成中…"}
            </pre>
          </div>

          {/* 反问清单 */}
          <div className="rounded-lg border">
            <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
              反问清单（在时间线回复后由 Reviewer 收敛）
            </p>
            <ol className="divide-y">
              {(review?.questions ?? []).map((q, i) => (
                <li key={q.id} className="flex items-start gap-3 px-4 py-3">
                  <span className="mt-0.5 font-mono text-caption text-muted-foreground">
                    Q{i + 1}
                  </span>
                  <p className="min-w-0 flex-1 text-sm">{q.question}</p>
                  <Badge variant="secondary" className="shrink-0">
                    {q.status === "open" ? "待回复" : "已回复"}
                  </Badge>
                </li>
              ))}
              {!review && <li className="px-4 py-3 text-sm text-muted-foreground">加载反问清单…</li>}
            </ol>
          </div>

          {/* BASE 来源回链 */}
          <p className="text-caption text-muted-foreground">
            BASE 工作区：<span className="font-mono">{project.workspacePath || "—"}</span>
            {" · "}
            <Link href={`/${wsSlug}/projects?project=${encodeURIComponent(project.id)}`} className="text-primary hover:underline">
              返回项目详情
            </Link>
          </p>
        </div>

        {/* 内嵌时间线 */}
        <div className="flex min-h-64 min-w-0 flex-col rounded-lg border lg:w-96 lg:shrink-0">
          <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
            评审讨论（回复反问 / 与 Reviewer 对齐）
          </p>
          <div className="flex min-h-0 flex-1 flex-col p-4">
            <ProjectTimeline projectId={project.id} />
          </div>
        </div>
      </div>
    </>
  );
}
