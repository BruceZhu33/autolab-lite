"use client";

/**
 * AutoLab 阶段过门操作条（Project Workspace 顶部）。
 *
 * 为什么存在：Multica 的状态入口藏在 Properties 面板的 Status 选择器里，
 * 而 AutoLab 的阶段推进是显式科研门（准备→评审→基线→迭代→确认→完成），
 * 必须是一目了然的主操作（v0.2 §4.3：阶段切换必须来自明确动作）。
 *
 * Phase 1 语义：直接改 issue 状态（fixture PUT）。
 * Phase 4 语义：按钮将改为调用 sidecar availableActions（真实状态机 guard：
 * 评审定稿、基线验收、final-read 等），前端不再直改状态。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@multica/ui/components/ui/button";

interface GateIssue {
  id: string;
  identifier: string;
  title: string;
  status: string;
}

interface StageGate {
  next: string;
  nextLabel: string;
  action: string;
  hint: string;
  color: string;
}

/** 六阶段推进门（顺序沿 v0.2 兼容状态机；reported 为终态）。 */
const STAGE_GATES: Record<string, StageGate> = {
  // 2026-09-15 口径：BASE 绑定原始代码库而非 plan——准备门确认代码库后
  // 直接派发 BASE；评审时专家已手握基线数字，方案讨论"在 BASE 上改进什么"；
  // plan 迭代不重跑 BASE（仅评测口径变化失效）。
  prepare: {
    next: "baseline",
    nextLabel: "基线验证",
    action: "确认代码库信息，开始基线验证",
    hint: "核对原始代码库登记（工作区 / 规模 / 入口 / 论文）后，派发 BASE 跑原始代码",
    color: "#0ea5e9",
  },
  baseline: {
    next: "review",
    nextLabel: "方案评审",
    action: "基线验收通过，进入方案评审",
    hint: "BASE 指标达到验收线（tx_baseline_acceptance guard）；带着基线数字与专家讨论改进",
    color: "#8b5cf6",
  },
  review: {
    next: "iterate",
    nextLabel: "自动迭代",
    action: "评审定稿，进入自动迭代",
    hint: "方案经评分与人工终审后，进入自主训练闭环（迭代工作区监督）",
    color: "#f59e0b",
  },
  iterate: {
    next: "confirm",
    nextLabel: "最终确认",
    action: "结束迭代，进入最终确认",
    hint: "搜索终止条件触发（预算 / 指标收敛 / 轮次上限）",
    color: "#ec4899",
  },
  confirm: {
    next: "reported",
    nextLabel: "已完成",
    action: "final-read 完成，产出报告",
    hint: "test 集一次性释放后定稿报告",
    color: "#10b981",
  },
};

const STAGE_LABEL: Record<string, string> = {
  prepare: "准备",
  review: "方案评审",
  baseline: "基线验证",
  iterate: "自动迭代",
  confirm: "最终确认",
  reported: "已完成",
};

async function fixtureApi<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const e = (await r.json().catch(() => ({}))) as { error?: string };
    throw new Error(typeof e.error === "string" ? e.error : `HTTP ${r.status}`);
  }
  return r.json() as Promise<T>;
}

export function AutolabStageGate({ issueId }: { issueId: string }) {
  const qc = useQueryClient();
  const { data: issue } = useQuery({
    queryKey: ["autolab", "stage-gate", issueId],
    queryFn: () => fixtureApi<GateIssue | null>("GET", `/api/issues/${issueId}`),
    refetchInterval: 5000,
  });

  const gate = issue ? STAGE_GATES[issue.status] : undefined;
  if (!issue || !gate) return null;

  const advance = async () => {
    try {
      await fixtureApi("PUT", `/api/issues/${issue.id}`, { status: gate.next });
      toast.success(`${issue.identifier} 已进入「${gate.nextLabel}」`);
      // fixture 模式无 WS 失效通知，主动刷新全量（内存数据源零成本）
      void qc.invalidateQueries();
    } catch (e) {
      toast.error("推进失败", {
        description: (e instanceof Error ? e.message : String(e)).slice(0, 300),
      });
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-3 border-b bg-muted/30 px-4 py-2.5 md:px-6">
      <span className="flex items-center gap-1.5 text-caption text-muted-foreground">
        <span className="size-1.5 rounded-full" style={{ backgroundColor: gate.color }} />
        当前阶段：{STAGE_LABEL[issue.status] ?? issue.status}
      </span>
      <span className="hidden min-w-0 flex-1 truncate text-caption text-muted-foreground md:block">
        {gate.hint}
      </span>
      <Button size="sm" className="ml-auto" onClick={() => void advance()}>
        {gate.action}
        <ArrowRight className="size-3.5" />
      </Button>
    </div>
  );
}
