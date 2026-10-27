"use client";

/** 论文雷达:PaperPulse 论文站在控制台内的原生视图。
 * 版式按论文站首页解剖移植:专栏卡片区 + 今日热点条 + 按天分组时间线;
 * 排版规格(标题 17px/摘要 14.5px/推荐理由沉底块)对齐论文站,配色走控制台 CDS 令牌。
 * 数据经 control 代理(/api/papers/*, reader 即可),详情与原文在论文站(隧道 3310)。 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BookDown, ExternalLink, Flame, Lightbulb, Loader2, Newspaper } from "lucide-react";
import { toast } from "sonner";
import {
  PAPERPULSE_WEB, adoptPaperToProject, fetchPaperColumn, fetchPaperColumns, fetchPaperSources,
  fetchPaperStats, fetchPaperTimeline, importPaperToKb, paperpulseItemUrl,
  type PaperCard, type PaperColumn, type PaperHotEntry, type PaperSource,
} from "@/lib/api/papers";
import { fetchProjects } from "@/lib/api";
import { SignalRiver, type RiverSource } from "@/components/papers/signal-river";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "~/vendor/ui/components/ui/dialog";
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
  active: boolean; name: string; blurb?: string; total: number | null; onClick: () => void;
}) {
  return (
    <button onClick={onClick}
            className={`rounded-2xl border px-4 py-3 text-left transition-colors ${
              active ? "border-brand bg-brand/5 shadow-[var(--surface-shadow)]"
                     : "border-surface-border bg-card shadow-[var(--surface-shadow)] hover:border-brand/40"}`}>
      <div className="flex items-baseline justify-between gap-2">
        <span className={`text-body-lg font-semibold ${active ? "text-brand" : "text-foreground"}`}>{name}</span>
        <span className="shrink-0 text-caption tabular-nums text-muted-foreground">
          {total == null ? "—" : `${total} 篇`}
        </span>
      </div>
      {blurb ? <p className="mt-0.5 line-clamp-1 text-micro leading-relaxed text-muted-foreground">{blurb}</p> : null}
    </button>
  );
}

/** 论文卡:论文站 FeedItem 的版式移植(17px 标题 / 14.5px 摘要 / 推荐理由沉底块 / 来源页脚)。
 * 页脚右侧是知识回流动作:送进研究(项目论文区)/ 存知识库(知识对话可检索)。 */
function PaperRow({ paper, onAdopt }: { paper: PaperCard; onAdopt: (p: PaperCard) => void }) {
  const [savingKb, setSavingKb] = useState(false);
  const saveToKb = async () => {
    setSavingKb(true);
    try {
      await importPaperToKb(paper.id);
      toast.success("已存入知识库,在「知识对话」可检索");
    } catch (e) {
      toast.error(`存入知识库失败:${(e as Error).message}`);
    } finally {
      setSavingKb(false);
    }
  };
  return (
    <div className="group rounded-2xl border-[0.5px] border-surface-border bg-card p-4 shadow-[var(--surface-shadow)] transition-colors hover:border-brand/40">
      <a href={paperpulseItemUrl(paper.id)} target="_blank" rel="noreferrer" className="block">
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
      </a>
      <div className="mt-2.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-micro text-muted-foreground">
        {paper.source?.name ? <span className="font-medium">{paper.source.name}</span> : null}
        <span aria-hidden>·</span>
        {paper.tags.slice(0, 4).map((t) => (
          <span key={t} className="rounded-full bg-muted/70 px-1.5 py-0.5">{t}</span>
        ))}
        <div className="ml-auto flex items-center gap-1">
          <button onClick={() => onAdopt(paper)} title="放到项目的 .autolab/papers/,研究 Agent 可读;有活跃研究会话会即时通知"
                  className="inline-flex items-center gap-1 rounded-full px-2 py-1 text-micro text-muted-foreground transition-colors hover:bg-brand/10 hover:text-brand">
            <Lightbulb className="size-3.5" />送进研究
          </button>
          <button onClick={saveToKb} disabled={savingKb} title="存入知识库(autolab 回流空间),知识对话可检索"
                  className="inline-flex items-center gap-1 rounded-full px-2 py-1 text-micro text-muted-foreground transition-colors hover:bg-brand/10 hover:text-brand disabled:opacity-50">
            {savingKb ? <Loader2 className="size-3.5 animate-spin" /> : <BookDown className="size-3.5" />}
            存知识库
          </button>
        </div>
      </div>
    </div>
  );
}

/** 「送进研究」项目选择:选完即写入该项目论文区并提示结果。 */
function AdoptDialog({ paper, onClose }: { paper: PaperCard | null; onClose: () => void }) {
  const projects = useQuery({ queryKey: ["projects"], queryFn: fetchProjects });
  const [busyId, setBusyId] = useState<string | null>(null);
  const adopt = async (projectId: string, name: string) => {
    if (!paper) return;
    setBusyId(projectId);
    try {
      const r = await adoptPaperToProject(paper.id, projectId);
      toast.success(r.notified
        ? `已放入「${name}」的论文区,研究 Agent 已收到通知`
        : `已放入「${name}」的论文区,下次研究开始时会读到`);
      onClose();
    } catch (e) {
      toast.error(`送进研究失败:${(e as Error).message}`);
    } finally {
      setBusyId(null);
    }
  };
  return (
    <Dialog open={!!paper} onOpenChange={(next) => (next ? null : onClose())}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>送进研究</DialogTitle>
          <DialogDescription>
            《{paper?.title ?? ""}》放到哪个项目?写入项目的 .autolab/papers/(全文提炼),
            研究 Agent 用 read_paper 阅读;项目有活跃研究会话时会即时收到通知。
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-72 overflow-y-auto">
          {projects.isLoading ? (
            <div className="flex justify-center py-6"><Loader2 className="size-4 animate-spin text-muted-foreground" /></div>
          ) : (projects.data ?? []).length === 0 ? (
            <p className="py-6 text-center text-caption text-muted-foreground">还没有项目——先到「项目」页立项</p>
          ) : (
            <ul className="space-y-1">
              {(projects.data ?? []).map((p) => (
                <li key={p.id}>
                  <button onClick={() => adopt(p.id, p.name)} disabled={busyId !== null}
                          className="flex w-full items-center justify-between rounded-lg border-[0.5px] border-surface-border px-3 py-2 text-left text-body transition-colors hover:border-brand/40 hover:bg-brand/5 disabled:opacity-50">
                    <span className="min-w-0 truncate">{p.name}</span>
                    {busyId === p.id ? <Loader2 className="size-3.5 animate-spin text-muted-foreground" /> : <Lightbulb className="size-3.5 text-muted-foreground" />}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </DialogContent>
    </Dialog>
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

/** 河下方四步卡(与画布四阶段对齐,hover 联动高亮)。 */
const STEPS = [
  { no: "01", title: "采集", text: "arXiv 主要学科、化工与医学影像专属分类、五个核心期刊与社区精选源都在盯;同一篇论文的多个入口只留一条。" },
  { no: "02", title: "归并", text: "抓到的都存下来,同一项工作的 arXiv 版本、期刊版本与解读归到一起:一篇论文全网转载,你只需要读一次。" },
  { no: "03", title: "精选", text: "模型按学术重要性独立打两次分:方法有多新、证据有多硬、多少研究者会关心、能不能马上用;灌水与增量拼装进不来。" },
  { no: "04", title: "成刊", text: "每天 08:00 出论文日报;专栏按方向收录 45 分以上的论文,全站精选只放 60 分以上。" },
];

/** 「最近精选」轮播:信源河里每有一束脉冲汇入报纸,这里换下一条。 */
function Latest({ item }: {
  item?: { id: string; title: string | null; source?: { name?: string } | null } | null;
}) {
  if (!item?.title) return null;
  return (
    <a href={paperpulseItemUrl(item.id)} target="_blank" rel="noreferrer" className="group block">
      <span className="text-micro font-semibold tracking-[0.2em] text-brand">最近精选</span>
      <span key={item.id} className="mt-1.5 block [animation:message-fade-up_0.45s_var(--ease-enter)_both]">
        <span className="line-clamp-2 text-[13.5px] font-semibold leading-[1.55] text-foreground transition-colors group-hover:text-brand">
          {item.title}
        </span>
        {item.source?.name ? (
          <span className="mt-1 block truncate text-micro text-muted-foreground">{item.source.name}</span>
        ) : null}
      </span>
    </a>
  );
}

/** 「信源与说明」区块(论文流下方):大标题 + 信源河动画 + 四步联动 + 信源目录 + 补充入口。 */
function AboutView({ sources, stats }: {
  sources: PaperSource[] | undefined;
  stats: { sources: number; items: number; selected: number; dailies: number } | undefined;
}) {
  const [focus, setFocus] = useState<number | null>(null);
  const [at, setAt] = useState(0);
  const shownAt = useRef(0);

  const latest = useQuery({
    queryKey: ["paper-latest"],
    queryFn: async () => (await fetchPaperTimeline(1)).cards.map((c) => c.item),
  });

  // 脉冲约每秒一束汇入报纸;标题轮换最快 2.8s 一次。
  const onArrive = useCallback(() => {
    const now = Date.now();
    if (now - shownAt.current < 2800 || (latest.data?.length ?? 0) < 2) return;
    shownAt.current = now;
    setAt((i) => (i + 1) % (latest.data?.length ?? 1));
  }, [latest.data?.length]);

  const riverSources: RiverSource[] = useMemo(
    () => (sources ?? []).map((s) => ({ name: s.name, kind: s.kind, heatOnly: false })),
    [sources]);

  const KIND_LABEL: Record<string, string> = { rss: "RSS 订阅", json_list: "开放接口", web_list: "网页解析" };
  const byKind = useMemo(() => {
    const groups = new Map<string, PaperSource[]>();
    for (const s of sources ?? []) {
      const k = KIND_LABEL[s.kind] ?? s.kind;
      groups.set(k, [...(groups.get(k) ?? []), s]);
    }
    return [...groups.entries()];
  }, [sources]);

  return (
    <div className="space-y-8 pb-6">
      <header>
        <span className="text-micro font-semibold tracking-[0.22em] text-brand">关于论文雷达</span>
        <h1 className="mt-3 max-w-[22em] text-display-sm font-bold leading-[1.2] tracking-tight text-foreground [text-wrap:balance] sm:text-[38px]">
          AI 每天涌出几百篇新论文,
          <br />
          <span className="text-brand">值得读的,只有几篇。</span>
        </h1>
        <p className="mt-4 max-w-[36em] text-body-lg leading-[1.85] text-muted-foreground">
          论文雷达替你盯着 {stats?.sources ?? "…"} 个信源:arXiv 学科、化工与医学影像专属分类、核心期刊与社区精选,
          抓取、归并、模型双重评分,过线的写中文导读;目前收录 {stats?.items ?? "…"} 篇,精选 {stats?.selected ?? "…"} 篇,
          每天早上出一份论文日报。
        </p>
        <div className="mt-5 flex flex-wrap gap-2.5">
          <a href={`${PAPERPULSE_WEB}/daily`} target="_blank" rel="noreferrer"
             className="inline-flex items-center gap-1.5 rounded-full border-[0.5px] border-surface-border bg-card px-4 py-2 text-caption font-medium text-foreground shadow-[var(--surface-shadow)] transition-colors hover:bg-muted/50">
            读最新论文日报<ExternalLink className="size-3.5" />
          </a>
        </div>
      </header>

      <section aria-label="论文雷达怎么工作">
        <div className="relative rounded-2xl border-[0.5px] border-surface-border bg-card shadow-[var(--surface-shadow)]">
          <SignalRiver sources={riverSources} focus={focus} onArrive={onArrive}
                        className="h-[230px] sm:h-[300px] lg:h-[340px]">
            <div className="absolute left-[75%] top-[calc(42%+40px)] hidden w-[23%] lg:block">
              <Latest item={latest.data?.[at]} />
            </div>
          </SignalRiver>
          <p className="sr-only">
            示意图:每条线是一个信源;线汇成一束束,代表同一项工作的多个入口;
            经过「精选」闸门只有少数几束通过,汇入每天 08:00 出刊的论文日报。
          </p>
        </div>
        <div className="mt-2 border-t border-surface-border lg:hidden">
          <div className="py-3"><Latest item={latest.data?.[at]} /></div>
        </div>
        <ol className="mt-4 grid grid-cols-1 divide-y divide-surface-border border-y border-surface-border sm:grid-cols-2 sm:divide-y-0 lg:grid-cols-4 lg:divide-x">
          {STEPS.map((s, i) => (
            <li key={s.no}
                onPointerEnter={() => setFocus(i)}
                onPointerLeave={() => setFocus(null)}
                className={`px-1 py-5 transition-colors sm:px-4 ${focus === i ? "bg-brand/5" : ""}`}>
              <div className="flex items-baseline gap-2.5">
                <span className="text-micro font-bold tracking-[0.12em] text-brand tabular-nums">{s.no}</span>
                <h3 className="text-body-lg font-semibold text-foreground">{s.title}</h3>
              </div>
              <p className="mt-1.5 text-caption leading-relaxed text-muted-foreground">{s.text}</p>
            </li>
          ))}
        </ol>
      </section>

      <section>
        <h2 className="text-title font-semibold tracking-tight">信源目录</h2>
        <p className="mt-1 text-caption text-muted-foreground">
          河里每条线对应下面一个信源(悬停画布可看名称)。绿点 = 抓取正常;灰点 = 暂时不可达(自动退避重试)。
          分级 T1/T1_5 对应全站精选门槛 60/65 分。
        </p>
        <div className="mt-3 space-y-4">
          {byKind.map(([kind, list]) => (
            <div key={kind}>
              <p className="text-caption font-medium text-muted-foreground">{kind} · {list.length} 个</p>
              <div className="mt-1.5 grid gap-1.5 sm:grid-cols-2">
                {list.map((s) => (
                  <div key={s.id}
                       className="flex items-center gap-2 rounded-lg border-[0.5px] border-surface-border bg-card px-2.5 py-1.5">
                    <span className={`size-1.5 shrink-0 rounded-full ${
                      s.health === "ok" ? "bg-success" : "bg-muted-foreground/40"}`}
                          title={s.health === "ok" ? "正常" : `健康度:${s.health}`} />
                    <span className="min-w-0 flex-1 truncate text-caption text-foreground">{s.name}</span>
                    <span className="shrink-0 rounded-full bg-muted/70 px-1.5 py-0.5 text-micro tabular-nums text-muted-foreground">{s.tier}</span>
                  </div>
                ))}
              </div>
            </div>
          ))}
          {sources === undefined ? (
            <div className="flex justify-center py-6"><Loader2 className="size-4 animate-spin text-muted-foreground" /></div>
          ) : null}
        </div>
      </section>

      <section className="rounded-2xl border-[0.5px] border-surface-border bg-card p-4 shadow-[var(--surface-shadow)]">
        <h2 className="text-body font-semibold">想补充信源?</h2>
        <p className="mt-1.5 text-caption leading-relaxed text-muted-foreground">
          课题组同学想加自己方向的信源(某本期刊、某个会议、某类 arXiv 分类),把网址发到论文站反馈页,
          管理员核实后一行配置即可上线;期刊只要有 ISSN 就能接。
        </p>
        <a href={`${PAPERPULSE_WEB}/feedback`} target="_blank" rel="noreferrer"
           className="mt-3 inline-flex items-center gap-1.5 rounded-full bg-brand px-3.5 py-1.5 text-caption font-medium text-brand-foreground transition-opacity hover:opacity-90">
          去论文站提议信源<ExternalLink className="size-3" />
        </a>
      </section>
    </div>
  );
}

export default function PapersPage() {
  const [slug, setSlug] = useState<string | null>(null); // null = 全站精选时间线
  const [page, setPage] = useState(1);
  const [adoptTarget, setAdoptTarget] = useState<PaperCard | null>(null);

  const columns = useQuery({
    queryKey: ["paper-columns"],
    queryFn: fetchPaperColumns,
  });
  const stats = useQuery({
    queryKey: ["paper-stats"],
    queryFn: fetchPaperStats,
  });
  const sourcesList = useQuery({
    queryKey: ["paper-sources"],
    queryFn: fetchPaperSources,
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
            <ColumnCard active={slug === null} name="全站精选" total={stats.data?.selected ?? null}
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
                {g.cards.map((p) => <PaperRow key={p.id} paper={p} onAdopt={setAdoptTarget} />)}
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

          {/* 论文流下方:信源与说明整段(标题/信源河/四步/信源目录/补充入口) */}
          <div className="mt-10 border-t border-surface-border pt-8">
            <AboutView sources={sourcesList.data?.sources} stats={stats.data} />
          </div>
        </div>
      </div>
      <AdoptDialog paper={adoptTarget} onClose={() => setAdoptTarget(null)} />
    </div>
  );
}
