"use client";

/**
 * 基线验证模块入口（Work 分组）：列出处于基线验证阶段的项目，
 * 点击进入基线工作区（BASE 派发 / run 监视 / 验收）。
 */
import { useQuery } from "@tanstack/react-query";
import { FileCode } from "lucide-react";
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
  workspacePath: string;
}

async function fixtureApi<T>(method: string, path: string): Promise<T> {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json" } });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

export default function BaselineIndexPage() {
  const pathname = usePathname();
  const wsSlug = pathname.split("/")[1] ?? "autolab";

  const { data, isPending } = useQuery({
    queryKey: ["autolab", "fixture-projects"],
    queryFn: () => fixtureApi<{ projects: ProjectView[] }>("GET", "/api/projects"),
  });

  const inBaseline = (data?.projects ?? []).filter((p) => p.stage === "baseline");

  return (
    <>
      <PageHeader>
        <FileCode className="size-4 text-muted-foreground" />
        <h1 className="text-body font-medium">基线验证</h1>
        {inBaseline.length > 0 && (
          <span className="font-mono text-caption tabular-nums text-muted-foreground">
            {inBaseline.length}
          </span>
        )}
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          派发 BASE（原始代码）→ 监视 run → 指标验收 → 进入方案评审
        </p>
      </PageHeader>

      <div className="p-4 md:px-6">
        {isPending && <p className="text-sm text-muted-foreground">加载中…</p>}
        {!isPending && inBaseline.length === 0 && (
          <p className="text-sm text-muted-foreground">
            暂无基线验证中的项目。在 Projects 过准备门后，项目会出现在这里。
          </p>
        )}
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {inBaseline.map((p) => (
            <Link
              key={p.id}
              href={`/${wsSlug}/baseline/${p.id}`}
              className="group space-y-3 rounded-lg border p-4 transition-colors hover:bg-muted/40"
            >
              <div className="flex items-center gap-2">
                <Badge variant="outline" className="gap-1.5">
                  <span className="size-1.5 rounded-full" style={{ backgroundColor: stageColor(p.stage) }} />
                  基线验证
                </Badge>
                <span className="font-mono text-caption text-muted-foreground">{p.identifier}</span>
              </div>
              <p className="text-body font-medium group-hover:underline">{p.title}</p>
              {p.direction && (
                <p className="line-clamp-2 text-caption text-muted-foreground">{p.direction}</p>
              )}
              <p className="truncate font-mono text-caption text-muted-foreground">{p.workspacePath}</p>
            </Link>
          ))}
        </div>
      </div>
    </>
  );
}
