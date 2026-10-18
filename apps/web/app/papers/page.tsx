"use client";

/** 论文雷达:PaperPulse 论文站在控制台内的原生视图。
 * 版式按论文站首页解剖移植:专栏卡片区 + 今日热点条 + 按天分组时间线;
 * 排版规格(标题 17px/摘要 14.5px/推荐理由沉底块)对齐论文站,配色走控制台 CDS 令牌。
 * 数据经 control 代理(/api/papers/*, reader 即可),详情与原文在论文站(隧道 3310)。 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Flame, Loader2, Newspaper } from "lucide-react";
import {
  PAPERPULSE_WEB, fetchPaperColumn, fetchPaperColumns, fetchPaperTimeline,
  paperpulseItemUrl, type PaperCard, type PaperColumn, type PaperHotEntry,
} from "@/lib/api/papers";
import { PageHeader, PAGE_GUTTER } from "~/vendor/views/layout/page-header";

/** 阅读栏宽度:与论文站时间线版心一致。 */
const READ_COL = "mx-auto w-full max-w-3xl";

/** 论文站热榜前三名的排名色阶(降权重,与站内一致的手感)。 */
const RANK_COLOR = ["text-[15px] font-black text-brand", "text-[15px] font-black text-brand/70", "text-[15px] font-black text-brand/45"];

function hotHref(e: PaperHotEntry): string {
  return e.storyPublicId ? `${PAPERPULSE_WEB}/story/${e.storyPublicId}`
    : e.itemId ? paperpulseItemUrl(e.itemId) : `${PAPERPULSE_WEB}/hot`;
}

/** 今日热点条:排名 + 标题 + 热度,整行可点进论文站(论文站首页 HotTopics 的移植简化)。 */
function HotStrip({ entries }: { entries: PaperHotEntry[] }) {
  if (!entries?.length) return null;
  return (
    <section className="mb-5 rounded-2xl border-[0.5px] border-surface-border bg-card p-4 shadow-[var(--surface-shadow)]">
      <div className="mb-1 flex items-center gap-2">
        <Flame className="size-3.5 text-brand" />
        <h2 className="text-body font-semibold">当前热点</h2>
        <span className="text-micro text-muted-foreground">按独立来源数计算的热度</span>
      </div>
      <ol>
        {entries.slice(0, 5).map((e, i) => (
          <li key={e.rank}>
            <a href={hotHref(e)} target="_blank" rel="noreferrer"
               className="group -mx-2 grid grid-cols-[22px_minmax(0,1fr)_auto] items-center gap-x-3 rounded-lg px-2 py-1.5 transition-colors hover:bg-muted/50">
              <span className={`text-center leading-none tabular-nums ${RANK_COLOR[i] ?? "text-body font-bold text-muted-foreground"}`}>{e.rank}</span>
              <span className="min-w-0 truncate text-body font-medium text-foreground group-hover:text-brand">{e.title}</span>
              <span className="whitespace-nowrap text-micro tabular-nums text-muted-foreground">
                <span className="text-caption font-semibold text-foreground/70">{Math.round(e.heat)}</span> 热度
              </span>
            </a>
          </li>
        ))}
      </ol>
    </section>
  );
}

/** 专栏卡:论文站首页专栏卡的移植(名称 + 收录数 + 方向简介),点卡切换专栏。 */
function ColumnCard({ active, name, blurb, total, onClick }: {
  active: boolean; name: string; blurb?: string; total: number; onClick: () => void;
}) {
  return (
    <button onClick={onClick}
            className={`rounded-2xl border px-4 py-3 text-left transition-colors ${
              active ? "border-brand bg-brand/5 shadow-[var(--surface-shadow)]"
                     : "border-surface-border bg-card shadow-[var(--surface-shadow)] hover:border-brand/40"}`}>
      <div className="flex items-baseline justify-between gap-2">
        <span className={`text-body-lg font-semibold ${active ? "text-brand" : "text-foreground"}`}>{name}</span>
        <span className="shrink-0 text-caption tabular-nums text-muted-foreground">{total} 篇</span>
      </div>
      {blurb ? <p className="mt-0.5 line-clamp-1 text-micro leading-relaxed text-muted-foreground">{blurb}</p> : null}
    </button>
  );
}

/** 论文卡:论文站 FeedItem 的版式移植(17px 标题 / 14.5px 摘要 / 推荐理由沉底块 / 来源页脚)。 */
function PaperRow({ paper }: { paper: PaperCard }) {
  return (
    <a href={paperpulseItemUrl(paper.id)} target="_blank" rel="noreferrer"
       className="group block rounded-2xl border-[0.5px] border-surface-border bg-card p-4 shadow-[var(--surface-shadow)] transition-colors hover:border-brand/40">
      <div className="flex items-start justify-between gap-3">
        <h3 className="min-w-0 flex-1 text-[17px] font-semibold leading-[1.5] text-foreground group-hover:text-brand">
          {paper.title ?? "(未命名)"}
        </h3>
        {paper.score != null ? (
          <span className={`mt-0.5 shrink-0 rounded-full px-1.5 py-0.5 text-micro font-medium tabular-nums ${
            paper.selected ? "bg-brand/10 text-brand" : "bg-muted text-muted-foreground"}`}>
            {Math.round(paper.score)}
          </span>
        ) : null}
      </div>
      {paper.summary ? (
        <p className="mt-2 line-clamp-3 text-[14.5px] leading-[1.7] text-foreground/75">
          {paper.summary}
        </p>
      ) : null}
      {paper.reason ? (
        <p className="mt-3 rounded-lg bg-muted/50 px-3 py-2 text-[13px] leading-[1.65] text-muted-foreground">
          推荐理由:{paper.reason}
        </p>
      ) : null}
      <div className="mt-2.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-micro text-muted-foreground">
        {paper.source?.name ? <span className="font-medium">{paper.source.name}</span> : null}
        <span aria-hidden>·</span>
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
    <div className="mb-4 mt-1 border-b border-surface-border pb-3">
      <div className="flex items-baseline gap-2">
        <h2 className="text-title font-semibold tracking-tight">{column.name}</h2>
        <span className="text-caption text-muted-foreground tabular-nums">{column.total} 篇</span>
      </div>
      <p className="mt-1 text-caption leading-relaxed text-muted-foreground">{column.definition}</p>
    </div>
  );
}

/** 按天分组(北京时间口径),保持原有顺序。 */
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

export default function PapersPage() {
  const [slug, setSlug] = useState<string | null>(null); // null = 全站精选时间线
  const [page, setPage] = useState(1);

  const columns = useQuery({
    queryKey: ["paper-columns"],
    queryFn: fetchPaperColumns,
  });
  const content = useQuery({
    queryKey: ["paper-content", slug, page],
    queryFn: async (): Promise<{ items: PaperCard[]; pageCount: number; hot: PaperHotEntry[] | null }> => {
      if (slug) {
        const d = await fetchPaperColumn(slug, page);
        return { items: d.items, pageCount: d.pageCount, hot: null };
      }
      const d = await fetchPaperTimeline(page);
      return { items: d.cards.map((c) => c.item), pageCount: 1, hot: d.hot };
    },
  });

  const cards = content.data?.items ?? [];
  const pageCount = content.data?.pageCount ?? 1;
  const hot = content.data?.hot ?? null;
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

      <div className={`flex flex-1 flex-col overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${READ_COL} pb-6 pt-3`}>
          {/* 专栏卡片区:论文站首页的第一入口结构 */}
          <div className="grid gap-2.5 sm:grid-cols-2">
            <ColumnCard active={slug === null} name="全站精选" total={0}
                        blurb="双重评分入选的各方向论文" onClick={() => { setSlug(null); setPage(1); }} />
            {(columns.data?.topics ?? []).filter((c) => c.total > 0).slice(0, 5).map((c) => (
              <ColumnCard key={c.slug} active={slug === c.slug} name={c.name} total={c.total}
                          blurb={c.definition} onClick={() => { setSlug(c.slug); setPage(1); }} />
            ))}
          </div>

          {/* 专栏多时提供 chips 快速切换(与卡片等效,占用一行) */}
          {(columns.data?.topics ?? []).filter((c) => c.total > 0).length > 5 ? (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {(columns.data?.topics ?? []).filter((c) => c.total > 0).slice(5).map((c) => (
                <button key={c.slug}
                        className={`rounded-full px-2.5 py-1 text-caption transition-colors ${
                          slug === c.slug ? "bg-brand text-brand-foreground" : "bg-muted/70 text-muted-foreground hover:text-foreground"}`}
                        onClick={() => { setSlug(c.slug); setPage(1); }}>
                  {c.name}<span className="ml-1 tabular-nums opacity-70">{c.total}</span>
                </button>
              ))}
            </div>
          ) : null}

          {slug === null && hot ? <div className="mt-4"><HotStrip entries={hot} /></div> : null}
          {active ? <div className="mt-4"><ColumnIntro column={active} /></div> : null}

          {content.isLoading ? (
            <div className="flex justify-center py-12">
              <Loader2 className="size-5 animate-spin text-muted-foreground" />
            </div>
          ) : content.isError ? (
            <p className="py-12 text-center text-caption text-muted-foreground">
              论文站暂不可达({(content.error as Error).message})
            </p>
          ) : cards.length === 0 ? (
            <div className="flex flex-col items-center gap-2 py-14 text-center">
              <Newspaper className="size-6 text-muted-foreground/50" />
              <p className="text-caption text-muted-foreground">
                {active ? `「${active.name}」还没有达线的论文,新论文分析后会自动进来` : "还没有精选论文"}
              </p>
            </div>
          ) : null}

          {groups.map((g) => (
            <section key={g.day} className="mt-5 first:mt-3">
              <div className="sticky top-0 z-10 -mx-1 bg-app-shell/90 px-1 py-1.5 backdrop-blur">
                <span className="text-caption font-medium text-muted-foreground">{g.day}</span>
              </div>
              <div className="space-y-3.5">
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
