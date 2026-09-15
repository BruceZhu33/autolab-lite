"use client";

/**
 * 评审模块入口（Work 分组）：列出处于方案评审阶段的项目，点击进入
 * 评审工作区（Agent 协作现场）。
 */
import { useQuery } from "@tanstack/react-query";
import { FileText } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Badge } from "@multica/ui/components/ui/badge";
import { PageHeader } from "@multica/views/layout/page-header";
import { stageColor } from "@/features/autolab/stage";

interface ProjectView {
  id: string;
  identifier: string;
  title: string;
  stage: string;
  direction: string;
  paper: string | null;
}

async function fixtureApi<T>(method: string, path: string): Promise<T> {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json" } });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

export default function ReviewIndexPage() {
  const pathname = usePathname();
  const wsSlug = pathname.split("/")[1] ?? "autolab";

  const { data, isPending } = useQuery({
    queryKey: ["autolab", "fixture-projects"],
    queryFn: () => fixtureApi<{ projects: ProjectView[] }>("GET", "/api/projects"),
  });

  const inReview = (data?.projects ?? []).filter((p) => p.stage === "review");

  return (
    <>
      <PageHeader>
        <FileText className="size-4 text-muted-foreground" />
        <h1 className="text-body font-medium">方案评审</h1>
        {inReview.length > 0 && (
          <span className="font-mono text-caption tabular-nums text-muted-foreground">
            {inReview.length}
          </span>
        )}
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          基于已验收 BASE 与专家 Agent 讨论改进 → 评分 → 人工终审
        </p>
      </PageHeader>

      <div className="p-4 md:px-6">
        {isPending && <p className="text-sm text-muted-foreground">加载中…</p>}
        {!isPending && inReview.length === 0 && (
          <p className="text-sm text-muted-foreground">
            暂无评审中的项目。基线验收通过后，项目会出现在这里。
          </p>
        )}
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {inReview.map((p) => (
            <Link
              key={p.id}
              href={`/${wsSlug}/review/${p.id}`}
              className="group space-y-3 rounded-lg border p-4 transition-colors hover:bg-muted/40"
            >
              <div className="flex items-center gap-2">
                <Badge variant="outline" className="gap-1.5">
                  <span className="size-1.5 rounded-full" style={{ backgroundColor: stageColor(p.stage) }} />
                  方案评审
                </Badge>
                <span className="font-mono text-caption text-muted-foreground">{p.identifier}</span>
              </div>
              <p className="text-body font-medium group-hover:underline">{p.title}</p>
              {p.direction && (
                <p className="line-clamp-2 text-caption text-muted-foreground">{p.direction}</p>
              )}
              {p.paper && (
                <p className="line-clamp-1 text-caption text-muted-foreground">📄 {p.paper}</p>
              )}
            </Link>
          ))}
        </div>
      </div>
    </>
  );
}
