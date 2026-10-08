"use client";

import { useMemo } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { advanceProjectStage, fetchBoard, type BoardCardView } from "@/lib/api";
import { adaptBoardCards } from "@/lib/board";
import { BoardView } from "~/vendor/views/issues/board-view";
import { statusLabelOf, BOARD_STATUSES } from "~/vendor/views/issues/status";
import type { DragMoveUpdates } from "~/vendor/views/issues/utils/drag-utils";
import { PageHeader } from "~/vendor/views/layout/page-header";

/**
 * Board:研究流程看板。列 = Work 栏目对应的六个阶段;?stage= 下钻单列。
 * 项目走到第几个阶段,就在对应列里各有一张它的卡(历史列的卡也在)——
 * 项目推进后 Baseline 列仍有它的卡,回补数据集不丢位置感。
 * 首屏只有 /api/board 一个业务请求,2s 轮询直至事件推送接入。
 */

const STAGE_KEYS = ["prepare", "baseline", "review", "iterate", "confirm", "done"] as const;

/** 项目横向色:同一项目所有阶段卡共享的底色 + 左缘色条(色相按项目 id
 * 稳定散列),与列的竖向底色叠加,横向一眼可 traced 出一个项目。 */
const TINT_HUES = [212, 262, 285, 340, 160, 20, 45, 190];

function projectTint(projectId: string): { bg: string; edge: string } {
  let hash = 0;
  for (const ch of projectId) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  const hue = TINT_HUES[hash % TINT_HUES.length];
  return {
    bg: `hsl(${hue} 55% 45% / 0.07)`,
    edge: `hsl(${hue} 60% 50% / 0.65)`,
  };
}

/** 行对齐占位卡:项目已穿过该列但当前无卡 → 渲染为空槽保持同项目横向同行。 */
function gapCard(pid: string, stageKey: string): BoardCardView {
  return {
    projectId: `${pid}:gap:${stageKey}`,
    planId: "",
    specHash: "",
    title: "",
    summary: "",
    column: stageKey as BoardCardView["column"],
    badges: [],
    jobs: { done: 0, total: 0, running: 0, failed: 0 },
    gpuLabels: [],
    latestMetric: null,
    updatedAt: "",
  };
}

/**
 * 按项目行 × 阶段列展开:同项目的所有卡横向对齐(同行)。
 *
 * 方法:先按 projectId 稳定排序建立全局项目行序,再按「项目 → 阶段」
 * 二重循环输出:每列内,项目的卡(或 gap)按行序排列 → buildColumns
 * 按数组序分列 → 各列中同项目的卡处于相同的行号。
 */
function expandByStage(cards: BoardCardView[]): BoardCardView[] {
  const stable = [...cards].sort((a, b) => a.projectId.localeCompare(b.projectId));

  // 每项目:各阶段的展开卡 + 最高列号
  const rows = new Map<string, {
    maxCol: number;
    byStage: Map<string, BoardCardView>;
  }>();
  for (const card of stable) {
    const stages = card.stages ?? {};
    const tint = projectTint(card.projectId);
    if (!rows.has(card.projectId)) {
      rows.set(card.projectId, { maxCol: -1, byStage: new Map() });
    }
    const row = rows.get(card.projectId)!;
    const reached = STAGE_KEYS.filter((k) => stages[k] && stages[k].state !== "pending");
    if (reached.length === 0) {
      row.byStage.set("prepare", { ...card, tint });
      row.maxCol = 0;
      continue;
    }
    for (const k of reached) {
      const col = STAGE_KEYS.indexOf(k);
      row.maxCol = Math.max(row.maxCol, col);
      row.byStage.set(k, {
        ...card,
        projectId: `${card.projectId}:${k}`,
        tint,
        column: k,
        badges: k === card.column
          ? card.badges
          : stages[k].state === "attention"
            ? [{ label: "需处理", tone: "danger" as const }]
            : [],
        summary: stages[k].summary || card.summary,
        latestMetric: k === card.column ? card.latestMetric : null,
      });
    }
  }

  // 列 × 行输出:每列包含所有项目(有卡→卡,无卡→gap),同项目恒在同一行。
  // 关键:即使项目还没走到该列,只要任何其他项目到了,也要为它占位——
  // 否则列内行号会因"缺席项目"而错位(用户反馈的核心问题)。
  const result: BoardCardView[] = [];
  const projectIds = [...rows.keys()];
  for (let col = 0; col < STAGE_KEYS.length; col++) {
    const stageKey = STAGE_KEYS[col];
    const anyCard = projectIds.some((pid) => rows.get(pid)!.byStage.has(stageKey));
    if (!anyCard) continue; // 该列没有任何项目的卡 → 整列不渲染
    for (const pid of projectIds) {
      result.push(rows.get(pid)!.byStage.get(stageKey) ?? gapCard(pid, stageKey));
    }
  }
  return result;
}

export function BoardClient() {
  const searchParams = useSearchParams();
  const stageFilter = searchParams.get("stage");
  const isValidStage = stageFilter && BOARD_STATUSES.some((s) => s.key === stageFilter);

  const queryClient = useQueryClient();
  const { data: cards = [], error, isLoading } = useQuery({
    queryKey: ["board"],
    queryFn: fetchBoard,
    refetchInterval: 2000,
  });
  const expanded = useMemo(() => expandByStage(cards), [cards]);
  const { issues, progressMap } = useMemo(() => adaptBoardCards(expanded), [expanded]);

  const onMoveIssue = (
    _issueId: string,
    _updates: DragMoveUpdates,
    onSettled?: () => void,
  ) => {
    // P0:排序/换列不写回。列位置由事实派生,立即恢复真实状态,不伪造。
    toast.info("排序调整暂未写回:卡片位置由 Plan/Job 实况派生", {
      id: "board-move-hint",
    });
    onSettled?.();
  };

  const onAdvance = async (issueId: string, to: "done") => {
    const card = cards.find((c) => c.projectId === issueId.split(":")[0]);
    if (!card) return;
    try {
      const result = await advanceProjectStage(card.projectId, to, card.column);
      toast.success("已记录结论:本轮实验归档");
      if (!result.replayed) {
        void queryClient.invalidateQueries({ queryKey: ["board"] });
      }
    } catch (e) {
      toast.error((e as Error).message);
      void queryClient.invalidateQueries({ queryKey: ["board"] });
    }
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <h1 className="text-title-sm font-semibold tracking-tight">实验看板</h1>
        {isValidStage ? (
          <>
            <span className="ml-2 inline-flex items-center rounded-full bg-accent px-2 py-0.5 text-caption font-medium text-accent-foreground">
              {statusLabelOf(stageFilter)}
            </span>
            <Link
              href="/"
              className="ml-2 text-caption text-muted-foreground hover:text-foreground hover:underline"
            >
              显示全部阶段
            </Link>
          </>
        ) : (
          <span className="ml-2 text-caption text-muted-foreground">
            {isLoading ? "加载中…" : `${issues.filter((i) => i.title).length} 张卡 · ${cards.length} 个项目`}
          </span>
        )}
      </PageHeader>
      {error && issues.filter((i) => i.title).length === 0 ? (
        <div className="px-4">
          <p className="text-body text-destructive">{error.message}</p>
        </div>
      ) : (
        <BoardView
          issues={issues}
          onMoveIssue={onMoveIssue}
          childProgressMap={progressMap}
          onAdvance={(_id, to) => void onAdvance(_id, to)}
          stageFilter={isValidStage ? stageFilter : null}
        />
      )}
    </div>
  );
}
