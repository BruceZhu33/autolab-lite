"use client";

/** 论文雷达(PaperPulse)客户端:经 control 代理只读访问论文站公开数据。 */

import { request } from "./client";

/** 论文站浏览器入口(SSH 隧道 3310 端口);详情页跳转用。 */
export const PAPERPULSE_WEB = "http://localhost:3310";
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

export const fetchPaperColumns = () =>
  request<{ topics: PaperColumn[] }>("/api/papers/columns");

export const fetchPaperColumn = (slug: string, page = 1) =>
  request<{ topic: PaperColumn; items: PaperCard[]; page: number; pageCount: number }>(
    `/api/papers/columns/${slug}?page=${page}`);

export const fetchPaperTimeline = (page = 1) =>
  request<{ cards: Array<{ item: PaperCard }> }>(`/api/papers/timeline?page=${page}&limit=20`);
