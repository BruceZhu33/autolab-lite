/**
 * review 域（Phase 2 第五片）：sidecar `GET /research/review/{id}` 聚合 →
 * 评审工作台形状。对话 = 真 review_turns；方案 = 当前协议（定稿目标）；
 * 人工终审 human-pass → 真实 T-FREEZE（POST /plan-protocols/{id}/freeze，
 * F5 基线有效把关）。三评分是 fixture 剧本（真实域无对应物，Phase 3 评审
 * 产品化时按模块评审重做）。
 */
import { sidecarApi } from "./client";

export interface ResearchReviewTurn {
  id: string;
  stage: string;
  role: string;
  content_json: string;
  created_at: string;
}

export interface ResearchReview {
  project: {
    id: string; name: string; status: string; stage: string;
    stage_epoch: number; state_revision: number; direction_text: string;
    created_at: string; updated_at: string;
  };
  turns: ResearchReviewTurn[];
  protocol: { id: string; ver: number; status: string; created_at: string } | null;
  root_protocol_id: string | null;
  baseline: { accepted: boolean; metric: number | null; at: string | null };
  modules: { identity: string; status: string }[];
}

function turnText(t: ResearchReviewTurn): string {
  try {
    const c = JSON.parse(t.content_json) as Record<string, unknown>;
    if (typeof c.text === "string") return c.text;
    if (typeof c.content === "string") return c.content;
    if (Object.keys(c).length) return JSON.stringify(c);
  } catch {
    // 非 JSON 内容原样
  }
  return t.content_json || "（空轮次）";
}

export async function fetchReviewWorkshop(projectId: string): Promise<{
  projectId: string;
  messages: { id: string; role: "user" | "expert"; content: string; createdAt: string }[];
  plan: {
    version: number;
    createdAt: string;
    sections: { key: string; title: string; content: string }[];
  } | null;
  scores: null;
  humanReview: null;
  freeze: { protocolId: string; stateRevision: number } | null;
}> {
  const r = await sidecarApi<ResearchReview>("GET", `/api/v1/research/review/${projectId}`);
  const messages = [
    {
      id: `rv-intro-${projectId}`,
      role: "expert" as const,
      content:
        `评审工作台已接真数据（Phase 2）。${r.baseline.accepted ? `基线已验收${r.baseline.metric !== null ? `（指标 ${r.baseline.metric.toFixed(4)}）` : ""}——讨论在 BASE 上改进什么。` : "⚠️ 尚无已验收基线（F5 将拒绝定稿）。"}` +
        `当前协议 v${r.protocol?.ver ?? "?"}（${r.protocol?.status ?? "无"}）。` +
        `逐节终审通过后「定稿」执行真实 T-FREEZE（F5 把关），项目进入自动迭代。` +
        `模块化评审对话（modules/synthesize）与三评分是 Phase 3 产品化范围。`,
      createdAt: r.project.created_at,
    },
    ...r.turns.map((t) => ({
      id: t.id,
      role: (t.role === "user" ? "user" : "expert") as "user" | "expert",
      content: `[${t.stage}] ${turnText(t)}`,
      createdAt: t.created_at,
    })),
  ];
  const plan = r.protocol
    ? {
        version: r.protocol.ver,
        createdAt: r.protocol.created_at,
        sections: [
          {
            key: "goal",
            title: "目标",
            content: r.project.direction_text || "（方向待补：POST /review 域的模块产出将在此汇总）",
          },
          {
            key: "baseline",
            title: "基线锚点",
            content: r.baseline.accepted
              ? `已验收${r.baseline.metric !== null ? ` · BASE 指标 ${r.baseline.metric.toFixed(4)}` : ""}${r.baseline.at ? ` · ${r.baseline.at}` : ""}`
              : "无已验收基线——定稿将被 F5 拒绝",
          },
          {
            key: "modules",
            title: "评审模块",
            content: r.modules.length
              ? r.modules.map((m) => `${m.identity}（${m.status}）`).join("；")
              : "（无模块：模块化评审是 Phase 3 范围）",
          },
          {
            key: "protocol",
            title: "协议",
            content: `当前协议 v${r.protocol.ver} 状态 ${r.protocol.status}；定稿冻结该协议（T-FREEZE F1–F5）`,
          },
          {
            key: "risk",
            title: "风险",
            content: "预算与口径一致性由 T-FREEZE F2–F5 库级/事务级把关",
          },
        ],
      }
    : null;
  return {
    projectId,
    messages,
    plan,
    scores: null,
    humanReview: null,
    freeze: r.protocol ? { protocolId: r.protocol.id, stateRevision: r.project.state_revision } : null,
  };
}

/** 人工终审定稿 → 真实 T-FREEZE（epoch2：F5 通过直通 SEARCH/自动迭代）。 */
export async function realReviewFreeze(projectId: string) {
  const ws = await sidecarApi<ResearchReview>("GET", `/api/v1/research/review/${projectId}`);
  if (!ws.protocol) {
    return { passed: false, reason: "项目无当前协议（立项缺失 plan）" };
  }
  const r = await sidecarApi<{ project_id: string; project_status: string }>(
    "POST", `/api/v1/plan-protocols/${ws.protocol.id}/freeze`,
    { expected_state_revision: ws.project.state_revision, next_round: 1 },
  );
  return { passed: true, status: r.project_status };
}
