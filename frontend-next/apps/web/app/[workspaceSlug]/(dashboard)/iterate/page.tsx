"use client";

/**
 * 自动迭代模块入口（Work 分组）：列出处于自动迭代阶段的项目。
 */
import { useQuery } from "@tanstack/react-query";
import { Zap } from "lucide-react";
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
}

async function fixtureApi<T>(method: string, path: string): Promise<T> {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json" } });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

export default function IterateIndexPage() {
  const pathname = usePathname();
  const wsSlug = pathname.split("/")[1] ?? "autolab";

  const { data, isPending } = useQuery({
    queryKey: ["autolab", "fixture-projects"],
    queryFn: () => fixtureApi<{ projects: ProjectView[] }>("GET", "/api/projects"),
  });

  const iterating = (data?.projects ?? []).filter((p) => p.stage === "iterate");

  return (
    <>
      <PageHeader>
        <Zap className="size-4 text-muted-foreground" />
        <h1 className="text-body font-medium">自动迭代</h1>
        {iterating.length > 0 && (
          <span className="font-mono text-caption tabular-nums text-muted-foreground">
            {iterating.length}
          </span>
        )}
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          全自动闭环（提案→JUDGE→冒烟→派发→评测）· 监督轮次与指标 · 指令/暂停/停止
        </p>
      </PageHeader>

      <div className="p-4 md:px-6">
        {isPending && <p className="text-sm text-muted-foreground">加载中…</p>}
        {!isPending && iterating.length === 0 && (
          <p className="text-sm text-muted-foreground">
            暂无迭代中的项目。方案评审定稿后，项目会出现在这里。
          </p>
        )}
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {iterating.map((p) => (
            <Link
              key={p.id}
              href={`/${wsSlug}/iterate/${p.id}`}
              className="group space-y-3 rounded-lg border p-4 transition-colors hover:bg-muted/40"
            >
              <div className="flex items-center gap-2">
                <Badge variant="outline" className="gap-1.5">
                  <span className="size-1.5 rounded-full" style={{ backgroundColor: stageColor(p.stage) }} />
                  自动迭代
                </Badge>
                <span className="font-mono text-caption text-muted-foreground">{p.identifier}</span>
              </div>
              <p className="text-body font-medium group-hover:underline">{p.title}</p>
              {p.direction && (
                <p className="line-clamp-2 text-caption text-muted-foreground">{p.direction}</p>
              )}
            </Link>
          ))}
        </div>
      </div>
    </>
  );
}
