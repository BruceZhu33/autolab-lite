"use client";

/** 论文雷达:PaperPulse 论文站在控制台内的原生视图。
 * 版式对齐论文站:居中阅读栏 + 专栏介绍头 + 按天分组的时间线;
 * 数据经 control 代理(/api/papers/*, reader 即可),详情与原文在论文站(隧道 3310)。 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Loader2, Newspaper } from "lucide-react";
import {
  PAPERPULSE_WEB, fetchPaperColumn, fetchPaperColumns, fetchPaperTimeline,
  paperpulseItemUrl, type PaperCard, type PaperColumn,
} from "@/lib/api/papers";
import { PageHeader, PAGE_GUTTER } from "~/vendor/views/layout/page-header";

/** 阅读栏宽度:与 KB 聊天版心一致,长摘要不散架。 */
const READ_COL = "mx-auto w-full max-w-3xl";

function ScoreBadge({ score, selected }: { score: number | null; selected: boolean }) {
  if (score == null) return null;
  return (
    <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-micro font-medium tabular-nums ${
      selected ? "bg-brand/10 text-brand" : "bg-muted text-muted-foreground"}`}>
      {Math.round(score)}
    </span>
  );
}

/** 按天分组(北京时间口径的日期键),并保持原有顺序。 */
function groupByDay(cards: PaperCard[]): Array<{ day: string; cards: PaperCard[] }> {
  const groups: Array<{ day: string; cards: PaperCard[] }> = [];
  for (const c of cards) {
    const d = new Date(c.timelineAt || c.publishedAt || "");
    const key = Number.isNaN(d.getTime())
      ? ""
      : d.toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "long", timeZone: "Asia/Shanghai" });
    const last = groups[groups.length - 1];
    if (last && last.day === key) last.cards.push(c);
    else groups.push({ day: key || "更早", cards: [c] });
  }
  return groups;
}

function PaperRow({ paper }: { paper: PaperCard }) {
  return (
    <a href={paperpulseItemUrl(paper.id)} target="_blank" rel="noreferrer"
       className="group block rounded-2xl border-[0.5px] border-surface-border bg-card p-4 shadow-[var(--surface-shadow)] transition-colors hover:border-brand/40">
      <div className="flex items-start gap-2.5">
        <h3 className="min-w-0 flex-1 text-body-lg font-medium leading-snug text-foreground group-hover:text-brand">
          {paper.title ?? "(未命名)"}
        </h3>
        <ScoreBadge score={paper.score} selected={paper.selected} />
      </div>
      {paper.summary ? (
        <p className="mt-2 line-clamp-3 text-[13px] leading-relaxed text-muted-foreground">
          {paper.summary}
        </p>
      ) : null}
      {paper.reason ? (
        <p className="mt-2.5 line-clamp-2 rounded-lg bg-muted/50 px-2.5 py-1.5 text-micro leading-relaxed text-muted-foreground">
          {paper.reason}
        </p>
      ) : null}
      <div className="mt-2.5 flex flex-wrap items-center gap-1.5 text-micro text-muted-foreground">
        {paper.source?.name ? <span className="font-medium">{paper.source.name}</span> : null}
        {paper.tags.slice(0, 4).map((t) => (
          <span key={t} className="rounded-full bg-muted/70 px-1.5 py-0.5">{t}</span>
        ))}
      </div>
    </a>
  );
}

/** 专栏介绍头:对齐论文站专栏页(名称 + 定义 + 收录数)。 */
function ColumnIntro({ column }: { column: PaperColumn }) {
  return (
    <div className="mb-4 border-b border-surface-border pb-3">
      <div className="flex items-baseline gap-2">
        <h2 className="text-title font-semibold tracking-tight">{column.name}</h2>
        <span className="text-caption text-muted-foreground tabular-nums">{column.total} 篇</span>
      </div>
      <p className="mt-1 text-caption leading-relaxed text-muted-foreground">{column.definition}</p>
    </div>
  );
}

export default function PapersPage() {
  const [slug, setSlug] = useState<string | null>(null); // null = 全站精选时间线
  const [page, setPage] = useState(1);

  const columns = useQuery({
    queryKey: ["paper-columns"],
    queryFn: fetchPaperColumns,
  });
  const content = useQuery({
    queryKey: ["paper-content", slug, page],
    queryFn: async (): Promise<{ items: PaperCard[]; pageCount: number }> => {
      if (slug) {
        const d = await fetchPaperColumn(slug, page);
        return { items: d.items, pageCount: d.pageCount };
      }
      const d = await fetchPaperTimeline(page);
      return { items: d.cards.map((c) => c.item), pageCount: 1 };
    },
  });

  const cards = content.data?.items ?? [];
  const pageCount = content.data?.pageCount ?? 1;
  const active = columns.data?.topics.find((c) => c.slug === slug);
  const groups = useMemo(() => groupByDay(cards), [cards]);

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <Newspaper className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">论文雷达</h1>
        <p className="ml-2 hidden truncate text-caption text-muted-foreground md:block">
          每日论文精选 · 专栏订阅 · 中文导读
        </p>
        <a href={PAPERPULSE_WEB} target="_blank" rel="noreferrer"
           className="ml-auto inline-flex items-center gap-1 text-micro text-muted-foreground hover:text-brand">
          在论文站打开<ExternalLink className="size-3" />
        </a>
      </PageHeader>

      <div className={`flex flex-1 flex-col overflow-hidden ${PAGE_GUTTER}`}>
        {/* 专栏切换:横排 chips,与论文站首页的专栏导航同构 */}
        <div className={`flex flex-wrap items-center gap-1.5 py-2.5 ${READ_COL}`}>
          <button
            className={`rounded-full px-3 py-1.5 text-caption transition-colors ${
              slug === null ? "bg-brand text-brand-foreground" : "bg-muted/70 text-muted-foreground hover:text-foreground"}`}
            onClick={() => { setSlug(null); setPage(1); }}>
            全站精选
          </button>
          {(columns.data?.topics ?? []).filter((c) => c.total > 0).map((c) => (
            <button key={c.slug}
                    className={`rounded-full px-3 py-1.5 text-caption transition-colors ${
                      slug === c.slug ? "bg-brand text-brand-foreground" : "bg-muted/70 text-muted-foreground hover:text-foreground"}`}
                    onClick={() => { setSlug(c.slug); setPage(1); }}>
              {c.name}
              <span className="ml-1.5 tabular-nums opacity-70">{c.total}</span>
            </button>
          ))}
        </div>

        <div className={`min-h-0 flex-1 overflow-y-auto pb-6 ${READ_COL}`}>
          {content.isLoading ? (
            <div className="flex justify-center py-12">
              <Loader2 className="size-5 animate-spin text-muted-foreground" />
            </div>
          ) : content.isError ? (
            <p className="py-12 text-center text-caption text-muted-foreground">
              论文站暂不可达({(content.error as Error).message})
            </p>
          ) : active ? <ColumnIntro column={active} /> : null}

          {content.isSuccess && cards.length === 0 ? (
            <div className="flex flex-col items-center gap-2 py-14 text-center">
              <Newspaper className="size-6 text-muted-foreground/50" />
              <p className="text-caption text-muted-foreground">
                {active ? `「${active.name}」还没有达线的论文,新论文分析后会自动进来` : "还没有精选论文"}
              </p>
            </div>
          ) : null}

          {groups.map((g) => (
            <section key={g.day} className="mt-2 first:mt-0">
              <div className="sticky top-0 z-10 -mx-1 bg-app-shell/90 px-1 py-1.5 backdrop-blur">
                <span className="text-caption font-medium text-muted-foreground">{g.day}</span>
              </div>
              <div className="space-y-3">
                {g.cards.map((p) => <PaperRow key={p.id} paper={p} />)}
              </div>
            </section>
          ))}

          {pageCount > 1 ? (
            <div className="mt-5 flex items-center justify-center gap-3 text-caption">
              <button className="rounded-full px-3 py-1.5 text-muted-foreground hover:bg-muted disabled:opacity-40"
                      disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>上一页</button>
              <span className="tabular-nums text-muted-foreground">{page} / {pageCount}</span>
              <button className="rounded-full px-3 py-1.5 text-muted-foreground hover:bg-muted disabled:opacity-40"
                      disabled={page >= pageCount} onClick={() => setPage((p) => p + 1)}>下一页</button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
