"use client";

/** 论文雷达(PaperPulse)客户端:经 control 代理只读访问论文站公开数据。 */

import { request } from "./client";

/** 论文站浏览器入口;部署地址经 NEXT_PUBLIC_PAPERPULSE_WEB 在构建期注入,
 *  默认值为文档示例地址(RFC 5737),本机部署务必注入真实入口。 */
export const PAPERPULSE_WEB =
  process.env.NEXT_PUBLIC_PAPERPULSE_WEB ?? "http://192.0.2.46:3310";
export const paperpulseItemUrl = (id: string) => `${PAPERPULSE_WEB}/items/${id}`;

export type PaperColumn = {
  slug: string;
  name: string;
  group: string;
  definition: string;
  total: number;
  recent: number;
  latestAt: string | null;
};

export type PaperCard = {
  id: string;
  title: string | null;
  summary: string | null;
  reason: string | null;
  tags: string[];
  score: number | null;
  selected: boolean;
  publishedAt: string | null;
  timelineAt: string;
  source?: { name?: string } | null;
};

/** 论文站首页热点条目(简化:排名/标题/热度/跳转)。 */
export type PaperHotEntry = {
  rank: number;
  title: string;
  heat: number;
  storyPublicId?: string | null;
  itemId?: string | null;
};

export const fetchPaperColumns = () =>
  request<{ topics: PaperColumn[] }>("/api/papers/columns");

export const fetchPaperColumn = (slug: string, page = 1) =>
  request<{ topic: PaperColumn; items: PaperCard[]; page: number; pageCount: number }>(
    `/api/papers/columns/${slug}?page=${page}`);

export const fetchPaperTimeline = (page = 1) =>
  request<{ cards: Array<{ item: PaperCard }>; hot: PaperHotEntry[] | null }>(
    `/api/papers/timeline?page=${page}&limit=20`);

/** 论文站内容统计(全站精选卡的真实计数来源)。 */
export const fetchPaperStats = () =>
  request<{ sources: number; items: number; selected: number; dailies: number }>("/api/papers/stats");

/** 信源目录条目(「信源与说明」视图)。 */
export type PaperSource = {
  id: string;
  name: string;
  kind: string;
  tier: string;
  tags: string[];
  health: string;
};

export const fetchPaperSources = () =>
  request<{ sources: PaperSource[] }>("/api/papers/sources");

/** 知识回流:雷达论文 → 知识库(autolab 回流空间,知识对话可检索)。 */
export const importPaperToKb = (itemId: string) =>
  request<{ ok: boolean; filename: string }>("/api/owner/papers/import", {
    method: "POST", body: { item_id: itemId },
  });

/** 知识回流:雷达论文 → 项目工作区 .autolab/papers/(研究 Agent read_paper 可读)。 */
export const adoptPaperToProject = (itemId: string, projectId: string) =>
  request<{ ok: boolean; filename: string; notified: boolean }>("/api/owner/papers/adopt", {
    method: "POST", body: { item_id: itemId, project_id: projectId },
  });
