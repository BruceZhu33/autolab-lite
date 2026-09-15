"use client";

/**
 * 方案评审工作区（评审模块核心，Agent 与用户交流最深的现场）。
 *
 * 交互流（2026-09-15 口径）：
 *  ① 与专家 Agent（Researcher）自由对话，可附带材料（论文 / 代码文件）；
 *  ② 「生成方案」→ 专家产出结构化方案（可迭代多版），同步触发评分；
 *  ③ 三位评分 Agent（方法 / 统计 / 风险）各维度打分，总分 ≥8 过线；
 *  ④ 过线后人工终审：逐节「认可 / 需修改」，全部认可 → 通过 → 基线验证。
 *
 * Phase 1：对话与评分为 fixture 确定性剧本；Phase 2 接 sidecar review 域
 * 与真实 LLM（专家 Agent 复用 Agents/Skills 体系），端点合同不变。
 */
import { use, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Paperclip, Send, X } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { toast } from "sonner";

import { Badge } from "@multica/ui/components/ui/badge";
import { Button } from "@multica/ui/components/ui/button";
import { Input } from "@multica/ui/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@multica/ui/components/ui/popover";
import { PageHeader } from "@multica/views/layout/page-header";
import { stageColor, stageLabel } from "@/features/autolab/stage";
import { StageTeamBar } from "@/features/autolab/stage-team";

interface ProjectView {
  id: string;
  identifier: string;
  title: string;
  stage: string;
  direction: string;
  workspacePath: string;
  paper: string | null;
}

interface Material {
  kind: "paper" | "code";
  label: string;
}

interface WorkshopMessage {
  id: string;
  role: "user" | "expert";
  content: string;
  materials?: Material[];
  createdAt: string;
}

interface WorkshopState {
  projectId: string;
  messages: WorkshopMessage[];
  plan: { version: number; sections: { key: string; title: string; content: string }[]; createdAt: string } | null;
  scores: { agentId: string; agentName: string; dimension: string; score: number; comment: string }[] | null;
  humanReview: Record<string, "ok" | "change"> | null;
}

const THRESHOLD = 8;

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

function MaterialChip({ m }: { m: Material }) {
  return (
    <span className="inline-flex max-w-56 items-center gap-1 rounded bg-muted px-1.5 py-0.5 font-mono text-caption text-muted-foreground">
      {m.kind === "paper" ? "📄" : "📦"} <span className="truncate">{m.label}</span>
    </span>
  );
}

function ScoreBar({ score }: { score: number }) {
  return (
    <span className="inline-flex h-1.5 w-16 overflow-hidden rounded-full bg-muted">
      <span className="h-full rounded-full bg-primary" style={{ width: `${score * 10}%` }} />
    </span>
  );
}

export default function ReviewWorkshopPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const qc = useQueryClient();
  const pathname = usePathname();
  const wsSlug = pathname.split("/")[1] ?? "autolab";

  const [draft, setDraft] = useState("");
  const [pendingMaterials, setPendingMaterials] = useState<Material[]>([]);
  const [matOpen, setMatOpen] = useState(false);
  const [matKind, setMatKind] = useState<"paper" | "code">("paper");
  const [matLabel, setMatLabel] = useState("");
  const [typing, setTyping] = useState(false);
  const [decisions, setDecisions] = useState<Record<string, "ok" | "change">>({});
  const [planOpen, setPlanOpen] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const { data: project } = useQuery({
    queryKey: ["autolab", "fixture-projects"],
    queryFn: () => fixtureApi<{ projects: ProjectView[] }>("GET", "/api/projects"),
    select: (d) => d.projects.find((p) => p.id === id || p.identifier === id) ?? null,
  });

  const { data: ws } = useQuery({
    queryKey: ["autolab", "review-workshop", id],
    queryFn: () => fixtureApi<WorkshopState>("GET", `/api/projects/${id}/review-workshop`),
  });

  const avg = useMemo(() => {
    if (!ws?.scores?.length) return null;
    return ws.scores.reduce((s, x) => s + x.score, 0) / ws.scores.length;
  }, [ws?.scores]);
  const met = avg !== null && avg >= THRESHOLD;

  // 对话流自动滚底
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [ws?.messages.length, typing]);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["autolab", "review-workshop", id] });
    void qc.invalidateQueries({ queryKey: ["autolab", "fixture-projects"] });
  };

  const send = useMutation({
    mutationFn: (input: { content: string; materials?: Material[] }) =>
      fixtureApi<WorkshopMessage>("POST", `/api/projects/${id}/review-workshop/messages`, input),
    onMutate: () => setTyping(true),
    onSuccess: () => {
      // typing 演出：专家回复已入库，延迟一拍再刷新对话流
      setDraft("");
      const hadMaterials = pendingMaterials;
      setPendingMaterials([]);
      window.setTimeout(() => {
        setTyping(false);
        void qc.invalidateQueries({ queryKey: ["autolab", "review-workshop", id] });
      }, 900);
      if (hadMaterials.length > 0) void hadMaterials; // 材料已随消息发送
    },
    onError: (e: Error) => {
      setTyping(false);
      toast.error("发送失败", { description: e.message.slice(0, 200) });
    },
  });

  const genPlan = useMutation({
    mutationFn: () => fixtureApi("POST", `/api/projects/${id}/review-workshop/plan`),
    onSuccess: () => {
      toast.success("方案已生成，评分 Agent 已给出意见");
      setDecisions({});
      refresh();
    },
  });

  const humanPass = useMutation({
    mutationFn: () =>
      fixtureApi<{ passed: boolean; reason?: string }>(
        "POST",
        `/api/projects/${id}/review-workshop/human-pass`,
        { decisions },
      ),
    onSuccess: (r) => {
      if (r.passed) {
        toast.success("方案评审通过", { description: "项目已进入「基线验证」。" });
      } else {
        toast.error("未通过", { description: r.reason });
      }
      refresh();
    },
  });

  if (!project) {
    return (
      <>
        <PageHeader>
          <FileText className="size-4 text-muted-foreground" />
          <h1 className="text-body font-medium">方案评审</h1>
        </PageHeader>
        <div className="p-4 md:px-6">
          <p className="text-sm text-muted-foreground">项目不存在或已被删除。</p>
        </div>
      </>
    );
  }

  const submit = () => {
    const content = draft.trim();
    if (!content && pendingMaterials.length === 0) return;
    send.mutate({ content: content || "（见材料）", materials: pendingMaterials.length ? pendingMaterials : undefined });
  };

  const addMaterial = () => {
    const label = matLabel.trim();
    if (!label) return;
    setPendingMaterials((prev) => [...prev, { kind: matKind, label }]);
    setMatLabel("");
    setMatOpen(false);
  };

  const stageBadge = (
    <Badge variant="outline" className="gap-1.5">
      <span className="size-1.5 rounded-full" style={{ backgroundColor: stageColor(project.stage) }} />
      {stageLabel(project.stage)}
    </Badge>
  );

  return (
    <>
      <PageHeader>
        <FileText className="size-4 text-muted-foreground" />
        <h1 className="text-body font-medium">方案评审</h1>
        <span className="font-mono text-caption text-muted-foreground">{project.identifier}</span>
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          {project.title}
        </p>
        <StageTeamBar stage="review" />
        <div className="ml-auto">{stageBadge}</div>
      </PageHeader>

      <div className="flex min-h-0 flex-1 gap-4 p-4 md:px-6">
        {/* 左：自由对话区 */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col rounded-lg border">
          <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
            与专家 Agent（Researcher）自由讨论 · 发论文 / 发代码 / 聊思路
          </p>

          <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto p-4">
            {(ws?.messages ?? []).map((m) =>
              m.role === "user" ? (
                <div key={m.id} className="flex flex-col items-end gap-1">
                  {m.materials && m.materials.length > 0 && (
                    <div className="flex flex-wrap justify-end gap-1">
                      {m.materials.map((x, i) => (
                        <MaterialChip key={i} m={x} />
                      ))}
                    </div>
                  )}
                  <p className="max-w-[85%] whitespace-pre-wrap break-words rounded-lg bg-primary/10 px-3 py-2 text-sm">
                    {m.content}
                  </p>
                </div>
              ) : (
                <div key={m.id} className="flex gap-2.5">
                  <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-caption font-medium">
                    R
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="flex items-baseline gap-2">
                      <span className="text-caption font-medium">Researcher</span>
                      <span className="rounded bg-muted px-1 text-[10px] leading-4 text-muted-foreground">专家 Agent</span>
                    </p>
                    <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed">{m.content}</p>
                  </div>
                </div>
              ),
            )}
            {typing && (
              <div className="flex gap-2.5">
                <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-caption font-medium">R</span>
                <p className="flex items-center gap-1 pt-2 text-muted-foreground">
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60" />
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:120ms]" />
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:240ms]" />
                </p>
              </div>
            )}
          </div>

          {/* composer */}
          <div className="space-y-2 border-t p-3">
            {pendingMaterials.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {pendingMaterials.map((x, i) => (
                  <span key={i} className="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 font-mono text-caption text-muted-foreground">
                    {x.kind === "paper" ? "📄" : "📦"} <span className="max-w-48 truncate">{x.label}</span>
                    <button onClick={() => setPendingMaterials((p) => p.filter((_, j) => j !== i))} aria-label="移除材料">
                      <X className="size-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <div className="flex items-center gap-2">
              <Popover open={matOpen} onOpenChange={setMatOpen}>
                <PopoverTrigger render={<Button size="icon-sm" variant="ghost" aria-label="添加材料" />}>
                  <Paperclip className="size-4" />
                </PopoverTrigger>
                <PopoverContent align="start" className="w-72">
                  <div className="flex gap-1.5">
                    <Button size="sm" variant={matKind === "paper" ? "default" : "outline"} onClick={() => setMatKind("paper")}>📄 论文</Button>
                    <Button size="sm" variant={matKind === "code" ? "default" : "outline"} onClick={() => setMatKind("code")}>📦 代码</Button>
                  </div>
                  <div className="mt-2 flex gap-2">
                    <Input
                      value={matLabel}
                      onChange={(e) => setMatLabel(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && addMaterial()}
                      placeholder={matKind === "paper" ? "arXiv 链接或标题" : "文件名或模块（如 train.py）"}
                    />
                    <Button size="sm" onClick={addMaterial}>添加</Button>
                  </div>
                  {matKind === "paper" && project.paper && (
                    <button
                      className="mt-2 block w-full truncate text-left text-caption text-primary hover:underline"
                      onClick={() => setMatLabel(project.paper ?? "")}
                    >
                      使用项目登记论文：{project.paper}
                    </button>
                  )}
                </PopoverContent>
              </Popover>
              <Input
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.nativeEvent.isComposing) submit();
                }}
                placeholder="和 Researcher 聊方案…（回车发送）"
              />
              <Button size="icon-sm" onClick={submit} disabled={send.isPending || (!draft.trim() && pendingMaterials.length === 0)} aria-label="发送">
                <Send className="size-4" />
              </Button>
            </div>
          </div>
        </div>

        {/* 右：方案 / 评分 / 人工终审 */}
        <div className="hidden w-96 shrink-0 flex-col gap-4 overflow-y-auto lg:flex">
          {/* 方案卡 */}
          <div className="rounded-lg border">
            <div className="flex items-center gap-2 border-b px-4 py-2.5">
              <p className="flex-1 text-caption font-medium text-muted-foreground">方案</p>
              {ws?.plan ? (
                <Badge variant="secondary">v{ws.plan.version}</Badge>
              ) : (
                <span className="text-caption text-muted-foreground">未生成</span>
              )}
              <Button size="sm" variant="ghost" onClick={() => genPlan.mutate()} disabled={genPlan.isPending}>
                {ws?.plan ? "重新生成" : "生成方案"}
              </Button>
            </div>
            {ws?.plan ? (
              <div className="divide-y">
                {ws.plan.sections.map((s) => (
                  <div key={s.key}>
                    <button
                      className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm font-medium hover:bg-muted/40"
                      onClick={() => setPlanOpen((prev) => !prev)}
                    >
                      <span className="text-caption text-muted-foreground">{s.title}</span>
                      <span className="ml-auto text-caption text-muted-foreground">{planOpen ? "收起" : "展开"}</span>
                    </button>
                    {planOpen && (
                      <p className="whitespace-pre-wrap break-words px-4 pb-3 text-sm leading-relaxed text-muted-foreground">
                        {s.content}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <p className="px-4 py-3 text-caption text-muted-foreground">
                与专家讨论后点击「生成方案」；方案可随讨论迭代多版。
              </p>
            )}
          </div>

          {/* 评分面板 */}
          <div className="rounded-lg border">
            <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
              评分 Agent 评审
            </p>
            {ws?.scores ? (
              <div className="space-y-3 px-4 py-3">
                {ws.scores.map((s) => (
                  <div key={s.agentId}>
                    <p className="flex items-center gap-2 text-sm">
                      <span className="font-medium">{s.agentName}</span>
                      <span className="text-caption text-muted-foreground">{s.dimension}</span>
                      <span className="ml-auto font-mono text-sm tabular-nums">{s.score.toFixed(1)}</span>
                      <ScoreBar score={s.score} />
                    </p>
                    <p className="mt-0.5 text-caption leading-relaxed text-muted-foreground">{s.comment}</p>
                  </div>
                ))}
                <div className="flex items-baseline gap-2 border-t pt-3">
                  <span className="text-caption text-muted-foreground">总分</span>
                  <span className={`font-mono text-lg font-semibold tabular-nums ${met ? "text-emerald-600" : "text-amber-600"}`}>
                    {avg!.toFixed(1)}
                  </span>
                  <span className="text-caption text-muted-foreground">/ 阈值 {THRESHOLD}</span>
                  <Badge variant={met ? "default" : "secondary"} className="ml-auto">
                    {met ? "已过线" : "未过线"}
                  </Badge>
                </div>
              </div>
            ) : (
              <p className="px-4 py-3 text-caption text-muted-foreground">生成方案后自动触发评分。</p>
            )}
          </div>

          {/* 人工终审 */}
          <div className="rounded-lg border">
            <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">人工终审</p>
            {project.stage !== "review" ? (
              <p className="px-4 py-3 text-caption text-emerald-600">
                已通过方案评审（当前阶段：{stageLabel(project.stage)}）。
              </p>
            ) : !met ? (
              <p className="px-4 py-3 text-caption text-muted-foreground">
                总分达到 {THRESHOLD} 后开启人工终审。
              </p>
            ) : (
              <div className="space-y-3 px-4 py-3">
                <p className="text-caption text-muted-foreground">逐节审阅方案，全部认可后通过。</p>
                {ws?.plan?.sections.map((s) => {
                  const d = decisions[s.key] ?? ws?.humanReview?.[s.key];
                  return (
                    <div key={s.key} className="flex items-center gap-2">
                      <span className="min-w-0 flex-1 truncate text-sm">{s.title}</span>
                      <Button
                        size="sm"
                        variant={d === "ok" ? "default" : "outline"}
                        onClick={() => setDecisions((prev) => ({ ...prev, [s.key]: "ok" }))}
                      >
                        认可
                      </Button>
                      <Button
                        size="sm"
                        variant={d === "change" ? "destructive" : "outline"}
                        onClick={() => setDecisions((prev) => ({ ...prev, [s.key]: "change" }))}
                      >
                        需改
                      </Button>
                    </div>
                  );
                })}
                <Button
                  className="w-full"
                  onClick={() => humanPass.mutate()}
                  disabled={humanPass.isPending || Object.keys(decisions).length === 0}
                >
                  通过方案评审，进入基线验证
                </Button>
              </div>
            )}
          </div>

          <p className="text-caption text-muted-foreground">
            BASE：<span className="font-mono">{project.workspacePath || "—"}</span> ·{" "}
            <Link href={`/${wsSlug}/projects?project=${encodeURIComponent(project.id)}`} className="text-primary hover:underline">
              返回项目详情
            </Link>
          </p>
        </div>
      </div>
    </>
  );
}
