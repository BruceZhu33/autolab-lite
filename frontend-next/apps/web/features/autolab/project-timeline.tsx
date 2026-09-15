"use client";

/**
 * 项目时间线（内嵌组件）：用户评论 + Agent 可见产出，各阶段模块页共用
 * （对齐口径 2026-09-15：时间线嵌入工作现场，数据源是统一的
 * threads/messages——Phase 1 走 fixture comments，Phase 3 换 sidecar
 * threads API，组件合同不变）。
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Button } from "@multica/ui/components/ui/button";
import { Input } from "@multica/ui/components/ui/input";

interface FixtureComment {
  id: string;
  author_type: "member" | "agent" | "system";
  author_id: string;
  content: string;
  type: string;
  created_at: string;
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

const AGENT_NAMES: Record<string, string> = {
  "agent-autolab": "AutoLab",
  "agent-researcher": "Researcher",
  "agent-engineer": "Engineer",
  "agent-reviewer": "Reviewer",
};

function authorName(c: FixtureComment): string {
  if (c.author_type === "member") return "Juan";
  return AGENT_NAMES[c.author_id] ?? "AutoLab";
}

export function ProjectTimeline({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState("");

  const { data: comments = [] } = useQuery({
    queryKey: ["autolab", "timeline", projectId],
    queryFn: () => fixtureApi<FixtureComment[]>("GET", `/api/issues/${projectId}/comments`),
  });

  const post = useMutation({
    mutationFn: (content: string) =>
      fixtureApi<FixtureComment>("POST", `/api/issues/${projectId}/comments`, { content }),
    onSuccess: () => {
      setDraft("");
      void qc.invalidateQueries({ queryKey: ["autolab", "timeline", projectId] });
    },
    onError: (e: Error) => toast.error("评论失败", { description: e.message.slice(0, 200) }),
  });

  const submit = () => {
    const content = draft.trim();
    if (!content) return;
    post.mutate(content);
  };

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <div className="flex-1 space-y-3 overflow-y-auto">
        {comments.length === 0 && (
          <p className="text-caption text-muted-foreground">暂无讨论。发表评论给 Agent 下达任务或补充信息。</p>
        )}
        {comments.map((c) => (
          <div key={c.id} className="flex gap-2.5">
            <span
              className={`mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full text-caption font-medium ${
                c.author_type === "member" ? "bg-primary text-primary-foreground" : "bg-muted"
              }`}
              aria-hidden="true"
            >
              {authorName(c).slice(0, 1)}
            </span>
            <div className="min-w-0 flex-1">
              <p className="flex items-baseline gap-2">
                <span className="text-caption font-medium">{authorName(c)}</span>
                {c.author_type === "agent" && (
                  <span className="rounded bg-muted px-1 text-[10px] leading-4 text-muted-foreground">Agent</span>
                )}
                <span className="text-caption text-muted-foreground">
                  {new Date(c.created_at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                </span>
              </p>
              <p className="mt-0.5 whitespace-pre-wrap break-words text-sm">{c.content}</p>
            </div>
          </div>
        ))}
      </div>
      <div className="flex gap-2 border-t pt-3">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) submit();
          }}
          placeholder="补充材料 / 给 Agent 下达任务…"
        />
        <Button size="sm" onClick={submit} disabled={post.isPending || !draft.trim()}>
          发送
        </Button>
      </div>
    </div>
  );
}
