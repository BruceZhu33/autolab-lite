"use client";

import { useMemo } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { advanceProjectStage, fetchBoard, type BoardCardView } from "@/lib/api";
import { adaptBoardCards } from "@/lib/board";
import { ApiError } from "@/components/api-error";
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

/** 每个项目的卡按已达阶段展开:一项目多卡,分居各列;当前阶段卡带徽章。 */
function expandByStage(cards: BoardCardView[]): BoardCardView[] {
  // 列内行号 = issues 顺序的投影,而后端按 updated_at 排项目——每次轮询
  // 顺序一变,同项目的卡在各列的行号就互相错开(横向 trace 断裂)。展开前
  // 按项目 id 稳定排序:行号恒定,同项目的卡始终横向对齐成"一行"。
  const stable = [...cards].sort(
    (a, b) => a.projectId.localeCompare(b.projectId)
      || a.planId.localeCompare(b.planId));
  return stable.flatMap((card) => {
    const stages = card.stages ?? {};
    const tint = projectTint(card.projectId);
    const reached = STAGE_KEYS.filter(
      (k) => stages[k] && stages[k].state !== "pending");
    if (reached.length === 0) {
      return [{ ...card, tint }];
    }
    return reached.map((k) => ({
      ...card,
      projectId: `${card.projectId}:${k}`,
      tint,
      column: k,
      // attention 格子(如循环终态失败)在对应阶段卡上亮出「需处理」,
      // 主视图无感 = 用户看到的「停了不动了」
      badges: k === card.column
        ? card.badges
        : stages[k].state === "attention"
          ? [{ label: "需处理", tone: "danger" as const }]
          : [],
      summary: stages[k].summary || card.summary,
      latestMetric: k === card.column ? card.latestMetric : null,
    }));
  });
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
            {error
              ? "连接异常,内容可能滞后"
              : isLoading && cards.length === 0
                ? "加载中…"
                : `${issues.length} 张卡 · ${cards.length} 个项目`}
          </span>
        )}
      </PageHeader>
      {error && issues.length === 0 ? (
        <ApiError
          error={error}
          onRetry={() => void queryClient.invalidateQueries({ queryKey: ["board"] })}
        />
      ) : isLoading && cards.length === 0 ? (
        <div className="flex flex-1 gap-4 overflow-x-auto p-2" aria-hidden>
          {STAGE_KEYS.map((k) => (
            <div
              key={k}
              className="h-full w-[280px] shrink-0 rounded-xl border border-surface-border bg-muted/30"
            >
              <div className="space-y-2 p-2">
                <div className="h-5 w-24 animate-pulse rounded-md bg-muted" />
                <div className="h-20 animate-pulse rounded-lg bg-surface" />
                <div className="h-20 animate-pulse rounded-lg bg-surface" />
              </div>
            </div>
          ))}
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
