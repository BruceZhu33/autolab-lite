/**
 * AutoLab 六科研阶段的共享展示映射（与 fixture issue-statuses 目录同色）。
 * Multica 视图通过数据（自定义 status）驱动，这里给 AutoLab 自有模块页用。
 */
export const STAGE_LABEL: Record<string, string> = {
  prepare: "准备",
  review: "方案评审",
  baseline: "基线验证",
  iterate: "自动迭代",
  confirm: "最终确认",
  reported: "已完成",
};

export const STAGE_COLOR: Record<string, string> = {
  prepare: "#94a3b8",
  review: "#8b5cf6",
  baseline: "#0ea5e9",
  iterate: "#f59e0b",
  confirm: "#ec4899",
  reported: "#10b981",
};

export function stageLabel(stage: string): string {
  return STAGE_LABEL[stage] ?? stage;
}

export function stageColor(stage: string): string {
  return STAGE_COLOR[stage] ?? "#94a3b8";
}
