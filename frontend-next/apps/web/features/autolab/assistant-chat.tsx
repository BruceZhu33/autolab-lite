"use client";

/**
 * AutoLab 平台助手对话面板（ZCode 式通用助手口径）。
 * GPU 页内嵌（storageKey=autolab.gpu-assistant.v1）与 /chat 全局页
 * （storageKey=autolab.assistant.v1）共用：每轮对话由 sidecar 注入实时
 * 快照（服务器/GPU/在途 run/项目/状态事件），LLM 走 round_analysis
 * 使用点——见 routers/assistant.py（deviation #35）。
 */
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Send } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@multica/ui/components/ui/badge";
import { Button } from "@multica/ui/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@multica/ui/components/ui/dialog";
import { Input } from "@multica/ui/components/ui/input";
import { Label } from "@multica/ui/components/ui/label";
import { Markdown } from "@multica/ui/markdown";
import { cn } from "@multica/ui/lib/utils";
import {
  assistantStatus,
  chatAssistant,
  putLlmConfig,
  testLlmConfig,
  type AssistantMessage,
} from "@autolab/research/sidecar/assistant";
import { SidecarApiError } from "@autolab/research/sidecar/client";

const SUGGESTIONS = [
  "服务器现在什么状态？",
  "有哪些在途的训练任务？",
  "哪台机器有空闲 GPU？",
  "最近有哪些失败或异常事件？",
];

const EMPTY_LLM_FORM = { base_url: "", model: "", api_key: "" };

function describeChatError(e: unknown): { code: string; message: string } {
  if (e instanceof SidecarApiError) {
    const M: Record<string, string> = {
      LLM_NOT_CONFIGURED:
        "LLM 未配置——点右上「配置 LLM」填 Base URL / Model / API Key（与迭代分析共用 round_analysis 使用点）。",
      LLM_NETWORK: "连不上 LLM 端点：检查 Base URL 与本机网络。",
      LLM_AUTH: "API Key 被拒绝（401/403）：检查 key 是否有效。",
      LLM_RATE_LIMIT: "LLM 限流（429）：稍等片刻再问。",
    };
    return { code: e.code, message: M[e.code] ?? `${e.code}: ${e.message}` };
  }
  return {
    code: "SIDECAR_UNREACHABLE",
    message:
      "无法连接 AutoLab sidecar（默认 127.0.0.1:39876）。确认它在运行（scripts/dev_local.sh）。",
  };
}

export interface AssistantChatProps {
  /** 对话历史各用各的 sessionStorage 键（GPU 页/全局页互不串台）。 */
  storageKey: string;
  /** 面板标题旁的小标签（GPU 页传「GPU 服务」，全局页可省略）。 */
  tag?: string;
  className?: string;
}

export function AssistantChat({ storageKey, tag, className }: AssistantChatProps) {
  const qc = useQueryClient();
  // 历史对话挂载后从 sessionStorage 恢复（SSR 首帧渲染空列表，避免水合不匹配）
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [hydrated, setHydrated] = useState(false);
  const [draft, setDraft] = useState("");
  const [thinking, setThinking] = useState(false);
  const [chatError, setChatError] = useState<{ code: string; message: string } | null>(null);
  const [llmOpen, setLlmOpen] = useState(false);
  const [llmForm, setLlmForm] = useState(EMPTY_LLM_FORM);
  const [llmBusy, setLlmBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const { data: st } = useQuery({
    queryKey: ["autolab", "assistant-status"],
    queryFn: assistantStatus,
    refetchInterval: 15000,
    retry: false,
  });

  useEffect(() => {
    try {
      const raw = window.sessionStorage.getItem(storageKey);
      const parsed = raw ? (JSON.parse(raw) as AssistantMessage[]) : [];
      if (Array.isArray(parsed) && parsed.length > 0) setMessages(parsed);
    } catch {
      // 损坏的历史不留存也不报错
    }
    setHydrated(true);
  }, [storageKey]);

  useEffect(() => {
    try {
      window.sessionStorage.setItem(storageKey, JSON.stringify(messages.slice(-60)));
    } catch {
      // 隐私模式/超配额：对话不留存也不报错
    }
  }, [messages, storageKey]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages.length, thinking, chatError]);

  const openLlmConfig = () => {
    setLlmForm({
      base_url: st?.llm.base_url ?? "",
      model: st?.llm.model ?? "",
      api_key: "",
    });
    setLlmOpen(true);
  };

  const saveLlmConfig = async () => {
    if (!llmForm.base_url || !llmForm.model || !llmForm.api_key) {
      toast.error("Base URL / Model / API Key 都要填（提交会覆盖已存 key）");
      return;
    }
    setLlmBusy(true);
    try {
      await putLlmConfig({ ...llmForm, engine: st?.llm.engine ?? "builtin" });
      await qc.invalidateQueries({ queryKey: ["autolab", "assistant-status"] });
      const t = await testLlmConfig();
      if (t.ok) {
        toast.success(`LLM 连通（${t.model}）`, { description: t.reply });
      } else {
        toast.error("已保存，但连通性测试失败", { description: t.error });
      }
      setLlmOpen(false);
    } catch (e) {
      const msg = e instanceof SidecarApiError ? `${e.code}: ${e.message}` : String(e);
      toast.error("保存 LLM 配置失败", { description: msg.slice(0, 300) });
    } finally {
      setLlmBusy(false);
    }
  };

  const send = async (raw: string) => {
    const content = raw.trim();
    if (!content || thinking) return;
    const next: AssistantMessage[] = [...messages, { role: "user", content }];
    setMessages(next);
    setDraft("");
    setChatError(null);
    setThinking(true);
    try {
      const r = await chatAssistant(next.slice(-20));
      setMessages([...next, { role: "assistant", content: r.reply }]);
      void qc.invalidateQueries({ queryKey: ["autolab", "assistant-status"] });
    } catch (e) {
      setChatError(describeChatError(e));
    } finally {
      setThinking(false);
    }
  };

  return (
    <>
      <section className={cn("flex min-h-0 min-w-0 flex-col rounded-lg border", className)}>
        <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2.5">
          <Bot className="size-4 text-muted-foreground" />
          <p className="text-caption font-medium">平台助手</p>
          {tag && (
            <span className="rounded bg-muted px-1 text-[10px] leading-4 text-muted-foreground">{tag}</span>
          )}
          {st && (
            <span className="font-mono text-caption tabular-nums text-muted-foreground">
              {st.summary.servers} 台 · {st.summary.gpus_idle}/{st.summary.gpus_total} GPU 空闲 ·{" "}
              {st.summary.active_attempts} 在途
            </span>
          )}
          <span className="ml-auto flex items-center gap-2">
            {st && (
              <Badge variant="outline" className="gap-1.5">
                <span
                  className={`size-1.5 rounded-full ${st.llm.has_key ? "bg-emerald-500" : "bg-amber-500"}`}
                />
                {st.llm.has_key ? (st.llm.model ?? "LLM") : "LLM 未配置"}
              </Badge>
            )}
            <Button size="sm" variant="ghost" onClick={openLlmConfig}>配置 LLM</Button>
          </span>
        </div>

        <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto p-4">
          {hydrated && messages.length === 0 && !thinking && !chatError && (
            <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
              <span className="flex size-10 items-center justify-center rounded-full bg-muted">
                <Bot className="size-5 text-muted-foreground" />
              </span>
              <div>
                <p className="text-sm font-medium">问点什么</p>
                <p className="mt-1 text-caption text-muted-foreground">
                  我能看到实时快照：服务器 / GPU / 在途 run / 项目阶段；也当常规助手用。
                </p>
              </div>
              <div className="flex max-w-md flex-wrap justify-center gap-2">
                {SUGGESTIONS.map((s) => (
                  <Button key={s} size="sm" variant="outline" onClick={() => void send(s)}>
                    {s}
                  </Button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m, i) =>
            m.role === "user" ? (
              <div key={i} className="flex justify-end">
                <p className="max-w-[85%] whitespace-pre-wrap break-words rounded-lg bg-primary/10 px-3 py-2 text-sm">
                  {m.content}
                </p>
              </div>
            ) : (
              <div key={i} className="flex gap-2.5">
                <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-muted">
                  <Bot className="size-4 text-muted-foreground" />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="flex items-baseline gap-2">
                    <span className="text-caption font-medium">AutoLab 助手</span>
                    <span className="rounded bg-muted px-1 text-[10px] leading-4 text-muted-foreground">平台助手</span>
                  </p>
                  <Markdown mode="minimal" className="mt-1 min-w-0 text-sm leading-relaxed">
                    {m.content}
                  </Markdown>
                </div>
              </div>
            ),
          )}

          {thinking && (
            <div className="flex gap-2.5">
              <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-muted">
                <Bot className="size-4 text-muted-foreground" />
              </span>
              <p className="flex items-center gap-1 pt-2 text-muted-foreground">
                <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60" />
                <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:120ms]" />
                <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:240ms]" />
              </p>
            </div>
          )}

          {chatError && (
            <div className="flex gap-2.5">
              <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-destructive/10">
                <Bot className="size-4 text-destructive" />
              </span>
              <div className="rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2">
                <p className="text-sm text-destructive">{chatError.message}</p>
                {chatError.code === "LLM_NOT_CONFIGURED" && (
                  <Button size="sm" variant="outline" className="mt-2" onClick={openLlmConfig}>
                    配置 LLM
                  </Button>
                )}
              </div>
            </div>
          )}
        </div>

        <div className="flex items-center gap-2 border-t p-3">
          <Input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) void send(draft);
            }}
            placeholder="问：服务器状态 / 在途任务 / 空闲 GPU，或任何问题…"
          />
          <Button size="icon-sm" onClick={() => void send(draft)} disabled={thinking || !draft.trim()} aria-label="发送">
            <Send className="size-4" />
          </Button>
        </div>
      </section>

      {/* LLM 配置（round_analysis 使用点：迭代分析与平台助手共用） */}
      <Dialog open={llmOpen} onOpenChange={setLlmOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>配置 LLM</DialogTitle>
            <DialogDescription>
              存入 sidecar 的 round_analysis 使用点（迭代分析与平台助手共用）。支持任何 OpenAI 兼容端点（智谱 / DeepSeek / OpenAI / Ollama）。
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <div className="grid gap-1.5">
              <Label>Base URL</Label>
              <Input
                value={llmForm.base_url}
                onChange={(e) => setLlmForm({ ...llmForm, base_url: e.target.value })}
                placeholder="https://open.bigmodel.cn/api/paas/v4"
              />
            </div>
            <div className="grid gap-1.5">
              <Label>Model</Label>
              <Input
                value={llmForm.model}
                onChange={(e) => setLlmForm({ ...llmForm, model: e.target.value })}
                placeholder="glm-4.7"
              />
            </div>
            <div className="grid gap-1.5">
              <Label>API Key</Label>
              <Input
                type="password"
                value={llmForm.api_key}
                onChange={(e) => setLlmForm({ ...llmForm, api_key: e.target.value })}
                placeholder="sk-…（提交会覆盖已存 key）"
              />
            </div>
          </div>
          <DialogFooter>
            <Button onClick={() => void saveLlmConfig()} disabled={llmBusy}>
              {llmBusy ? "保存中…" : "保存并测试"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
