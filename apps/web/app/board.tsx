"use client";

/** 看板:每个项目一行,三格阶段(立项 → Baseline → 研究)。状态全部来自后端派生。 */

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, CheckCircle2, Circle, FolderPlus, Loader2 } from "lucide-react";
import { fetchBoard, type BoardCard, type StageKey, type StageState } from "@/lib/api";
import { timeAgo } from "@/lib/time";
import { ApiError } from "@/components/api-error";
import { EmptyState } from "@/components/empty-state";
import { Button } from "~/vendor/ui/components/ui/button";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "~/vendor/views/layout/page-header";

const STAGES: Array<{ key: StageKey; label: string }> = [
  { key: "setup", label: "立项" },
  { key: "baseline", label: "Baseline" },
  { key: "research", label: "研究" },
];

const STATE_STYLE: Record<StageState, { icon: typeof Circle; cls: string }> = {
  done: { icon: CheckCircle2, cls: "text-success" },
  active: { icon: Loader2, cls: "text-brand" },
  attention: { icon: AlertCircle, cls: "text-warning" },
  pending: { icon: Circle, cls: "text-muted-foreground/50" },
};

function StageCellView({ cell }: { cell: BoardCard["stages"][StageKey] }) {
  const style = STATE_STYLE[cell.state];
  const Icon = style.icon;
  return (
    <Link href={cell.link}
          className="flex items-center gap-1.5 rounded-md px-2 py-1.5 text-caption hover:bg-muted/50">
      <Icon className={`size-3.5 shrink-0 ${style.cls} ${cell.state === "active" ? "animate-spin" : ""}`} />
      <span className={cell.state === "pending" ? "text-muted-foreground" : ""}>{cell.summary}</span>
    </Link>
  );
}

export function Board() {
  const { data: cards = [], error, isLoading } = useQuery({
    queryKey: ["board"], queryFn: fetchBoard, refetchInterval: 3000,
  });
  const waiting = cards.filter((c) => c.needsYou).length;

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <h1 className="text-title-sm font-semibold tracking-tight">实验看板</h1>
        <span className="ml-2 text-caption text-muted-foreground">
          {isLoading ? "加载中…" : `${cards.length} 个项目${waiting ? ` · ${waiting} 个等你处理` : ""}`}
        </span>
      </PageHeader>
      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${PAGE_RAIL} py-4`}>
          {error && cards.length === 0 ? (
            <ApiError error={error} />
          ) : !isLoading && cards.length === 0 ? (
            <EmptyState icon={FolderPlus} title="还没有项目"
                        description="先在项目页立项:选服务器,放好代码与论文。"
                        action={<Link href="/projects"><Button size="sm">去立项</Button></Link>} />
          ) : (
            <table className="w-full table-fixed border-separate border-spacing-y-1.5">
              <thead>
                <tr className="text-left text-micro text-muted-foreground">
                  <th className="w-[22%] px-3 font-medium">项目</th>
                  {STAGES.map((s) => <th key={s.key} className="px-2 font-medium">{s.label}</th>)}
                </tr>
              </thead>
              <tbody>
                {cards.map((card) => (
                  <tr key={card.projectId}
                      className={`bg-surface shadow-[var(--surface-shadow)] ${card.needsYou ? "outline outline-1 outline-warning/40" : ""}`}>
                    <td className="rounded-l-lg px-3 py-2">
                      {/* 项目名直达该项目当前阶段页;三格阶段各自有独立链接 */}
                      <Link href={card.stages[card.stage].link} className="group block">
                        <p className="truncate text-body font-medium group-hover:underline group-hover:underline-offset-2">
                          {card.title}
                        </p>
                        <p className="text-micro text-muted-foreground">{timeAgo(card.updatedAt)}</p>
                      </Link>
                    </td>
                    {STAGES.map((s, i) => (
                      <td key={s.key} className={i === STAGES.length - 1 ? "rounded-r-lg" : ""}>
                        <StageCellView cell={card.stages[s.key]} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
