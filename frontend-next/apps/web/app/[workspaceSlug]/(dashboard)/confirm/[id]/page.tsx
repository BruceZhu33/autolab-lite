"use client";

/**
 * 最终确认工作区（final-read 一次性释放 + 报告 + 完成归档）。
 * 左对话（AutoLab 引导三步）/ 右面板：final-read 警告卡（不可逆 guard）、
 * 最终指标、报告、过门。Phase 2 对接 tx_final_read + report service。
 */
import { use, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileCheck, Send } from "lucide-react";
import { usePathname } from "next/navigation";
import { toast } from "sonner";

import { Badge } from "@multica/ui/components/ui/badge";
import { Button } from "@multica/ui/components/ui/button";
import { Input } from "@multica/ui/components/ui/input";
import { PageHeader } from "@multica/views/layout/page-header";
import { StageTeamBar } from "@/features/autolab/stage-team";
import { stageColor, stageLabel } from "@/features/autolab/stage";

interface ProjectView {
  id: string;
  identifier: string;
  title: string;
  stage: string;
}

interface MsgView {
  id: string;
  role: "user" | "agent";
  content: string;
  createdAt: string;
}

interface WorkshopView {
  projectId: string;
  bestMetric: number;
  baseline: number;
  finalRead: { status: string; metric: number | null; finishAt: string } | null;
  finalReadDone: boolean;
  finalReadRunning: boolean;
  report: { generatedAt: string; path: string; finalMetric: number; baseline: number; summary: string } | null;
  messages: MsgView[];
}

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

export default function ConfirmWorkshopPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const qc = useQueryClient();
  const [draft, setDraft] = useState("");
  const [typing, setTyping] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const { data: project } = useQuery({
    queryKey: ["autolab", "fixture-projects"],
    queryFn: () => fixtureApi<{ projects: ProjectView[] }>("GET", "/api/projects"),
    select: (d) => d.projects.find((p) => p.id === id || p.identifier === id) ?? null,
  });

  const { data: ws } = useQuery({
    queryKey: ["autolab", "confirm-workshop", id],
    queryFn: () => fixtureApi<WorkshopView>("GET", `/api/projects/${id}/confirm-workshop`),
    refetchInterval: 5000,
    refetchIntervalInBackground: true,
  });

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [ws?.messages.length, typing]);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["autolab", "confirm-workshop", id] });
    void qc.invalidateQueries({ queryKey: ["autolab", "fixture-projects"] });
  };

  const send = useMutation({
    mutationFn: (content: string) =>
      fixtureApi<MsgView>("POST", `/api/projects/${id}/confirm-workshop/messages`, { content }),
    onMutate: () => setTyping(true),
    onSuccess: () => {
      setDraft("");
      window.setTimeout(() => {
        setTyping(false);
        refresh();
      }, 900);
    },
    onError: (e: Error) => {
      setTyping(false);
      toast.error("发送失败", { description: e.message.slice(0, 200) });
    },
  });

  if (!project) {
    return (
      <>
        <PageHeader>
          <FileCheck className="size-4 text-muted-foreground" />
          <h1 className="text-body font-medium">最终确认</h1>
        </PageHeader>
        <div className="p-4 md:px-6">
          <p className="text-sm text-muted-foreground">项目不存在或已被删除。</p>
        </div>
      </>
    );
  }

  const submit = () => {
    const content = draft.trim();
    if (!content) return;
    send.mutate(content);
  };

  return (
    <>
      <PageHeader>
        <FileCheck className="size-4 text-muted-foreground" />
        <h1 className="text-body font-medium">最终确认</h1>
        <span className="font-mono text-caption text-muted-foreground">{project.identifier}</span>
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          {project.title}
        </p>
        <StageTeamBar stage="confirm" />
        <div className="ml-auto">
          <Badge variant="outline" className="gap-1.5">
            <span className="size-1.5 rounded-full" style={{ backgroundColor: stageColor(project.stage) }} />
            {stageLabel(project.stage)}
          </Badge>
        </div>
      </PageHeader>

      <div className="flex min-h-0 flex-1 gap-4 p-4 md:px-6">
        {/* 左：对话 */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col rounded-lg border">
          <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
            与 AutoLab 对话 · final-read（一次性）→ 报告 → 完成归档
          </p>
          <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto p-4">
            {(ws?.messages ?? []).map((m) =>
              m.role === "user" ? (
                <div key={m.id} className="flex justify-end">
                  <p className="max-w-[85%] whitespace-pre-wrap break-words rounded-lg bg-primary/10 px-3 py-2 text-sm">
                    {m.content}
                  </p>
                </div>
              ) : (
                <div key={m.id} className="flex gap-2.5">
                  <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-caption font-medium">A</span>
                  <div className="min-w-0 flex-1">
                    <p className="flex items-baseline gap-2">
                      <span className="text-caption font-medium">AutoLab</span>
                      <span className="rounded bg-muted px-1 text-[10px] leading-4 text-muted-foreground">执行 Agent</span>
                    </p>
                    <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed">{m.content}</p>
                  </div>
                </div>
              ),
            )}
            {typing && (
              <div className="flex gap-2.5">
                <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-caption font-medium">A</span>
                <p className="flex items-center gap-1 pt-2 text-muted-foreground">
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60" />
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:120ms]" />
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:240ms]" />
                </p>
              </div>
            )}
          </div>
          <div className="flex items-center gap-2 border-t p-3">
            <Input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.nativeEvent.isComposing) submit();
              }}
              placeholder="说：执行 final-read / 生成报告 / 完成…"
            />
            <Button size="icon-sm" onClick={submit} disabled={send.isPending || !draft.trim()} aria-label="发送">
              <Send className="size-4" />
            </Button>
          </div>
        </div>

        {/* 右：执行面板 */}
        <div className="hidden w-96 shrink-0 flex-col gap-4 overflow-y-auto lg:flex">
          {/* 迭代结果摘要 */}
          <div className="rounded-lg border">
            <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">迭代结果</p>
            <div className="space-y-2 px-4 py-3 text-sm">
              <p className="flex items-baseline gap-2">
                <span className="text-caption text-muted-foreground">验证集最佳</span>
                <span className="font-mono text-lg font-semibold tabular-nums">{ws?.bestMetric.toFixed(4)}</span>
                <span className="font-mono text-caption text-emerald-600">
                  Δ+{((ws?.bestMetric ?? 0) - (ws?.baseline ?? 0)).toFixed(4)}
                </span>
              </p>
              <p className="text-caption text-muted-foreground">BASE {ws?.baseline.toFixed(4)}</p>
            </div>
          </div>

          {/* final-read 警告卡 */}
          <div className={`rounded-lg border p-4 ${ws?.finalReadDone ? "" : "border-amber-500/40 bg-amber-500/5"}`}>
            <p className="flex items-center gap-2 text-sm font-medium">
              <span className={`size-1.5 rounded-full ${ws?.finalReadDone ? "bg-emerald-500" : ws?.finalReadRunning ? "animate-pulse bg-amber-500" : "bg-amber-500"}`} />
              final-read · test 集一次性释放
            </p>
            {ws?.finalReadDone ? (
              <div className="mt-2 space-y-1 text-sm">
                <p className="flex items-baseline gap-2">
                  <span className="text-caption text-muted-foreground">最终指标</span>
                  <span className="font-mono text-lg font-semibold tabular-nums">
                    {ws.finalRead?.metric?.toFixed(4)}
                  </span>
                </p>
                <p className="text-caption text-emerald-600">已释放（不可逆，不变量②）。</p>
              </div>
            ) : ws?.finalReadRunning ? (
              <p className="mt-1 text-caption text-muted-foreground">评测中，结果稍后出现…</p>
            ) : (
              <>
                <p className="mt-1 text-caption leading-relaxed text-muted-foreground">
                  ⚠️ test 集只能释放这一次（不变量②）。释放后以当前最佳快照做最终评测，结果不可修改。
                </p>
                <Button size="sm" variant="outline" className="mt-2" onClick={() => send.mutate("执行 final-read")}>
                  执行 final-read
                </Button>
              </>
            )}
          </div>

          {/* 报告 */}
          <div className="rounded-lg border">
            <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">报告</p>
            {ws?.report ? (
              <div className="space-y-2 px-4 py-3 text-sm">
                <p className="font-mono text-caption text-muted-foreground">{ws.report.path}</p>
                <p className="leading-relaxed text-muted-foreground">{ws.report.summary}</p>
              </div>
            ) : (
              <div className="space-y-2 px-4 py-3">
                <p className="text-caption text-muted-foreground">
                  {ws?.finalReadDone ? "final-read 已完成，可生成报告。" : "需先完成 final-read。"}
                </p>
                {ws?.finalReadDone && (
                  <Button size="sm" variant="outline" onClick={() => send.mutate("生成报告")}>
                    生成报告
                  </Button>
                )}
              </div>
            )}
          </div>

          {/* 过门 */}
          {ws?.report && project.stage === "confirm" && (
            <div className="rounded-lg border">
              <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">完成归档</p>
              <div className="space-y-2 px-4 py-3">
                <p className="text-caption text-muted-foreground">
                  报告已生成，项目可归档为「已完成」；全程时间线保留回放。
                </p>
                <Button size="sm" className="w-full" onClick={() => send.mutate("完成")}>
                  完成归档
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
