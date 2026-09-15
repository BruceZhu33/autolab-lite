/**
 * 阶段 Agent 班底（Phase 1 为阶段模板；每项目可配置是下一步口径）。
 * 各阶段工作区头部显示本阶段参与的 Agent，让"谁在干活"一目了然。
 */
export interface StageMember {
  name: string;
  role: string;
}

export const STAGE_TEAMS: Record<string, StageMember[]> = {
  prepare: [{ name: "AutoLab", role: "登记协调" }],
  baseline: [{ name: "AutoLab", role: "执行" }],
  review: [
    { name: "Researcher", role: "专家" },
    { name: "Method Reviewer", role: "评分 · 方法" },
    { name: "Stats Reviewer", role: "评分 · 统计" },
    { name: "Risk Reviewer", role: "评分 · 风险" },
  ],
  iterate: [
    { name: "Engineer", role: "改码" },
    { name: "AutoLab", role: "派发" },
  ],
  confirm: [{ name: "AutoLab", role: "final-read" }],
};

export function StageTeamBar({ stage }: { stage: string }) {
  const team = STAGE_TEAMS[stage];
  if (!team || team.length === 0) return null;
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      {team.map((m) => (
        <span
          key={m.name + m.role}
          className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-caption text-muted-foreground"
        >
          <span className="flex size-4 items-center justify-center rounded-full bg-muted text-[10px] font-medium">
            {m.name.slice(0, 1)}
          </span>
          {m.name}
          <span className="text-muted-foreground/70">· {m.role}</span>
        </span>
      ))}
    </span>
  );
}
