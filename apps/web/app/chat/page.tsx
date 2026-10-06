"use client";

/**
 * Chat:本地 claude-code 实例的自由对话界面(M6)。
 *
 * 左侧会话列表,右侧消息流。每个会话 = 一个长驻 headless claude 进程
 * (--resume 串联多轮),可选本地工作区做 cwd、可选 Agent 配置(提示词/
 * GLM key),autolab MCP 默认挂载——所以既能干活也能答「服务器状态/实验进度」。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ArrowUp, Bot, Folder, FolderOpen, Home, Loader2, MessageCircle, Plus, Square, Terminal,
  Trash2, Wrench,
} from "lucide-react";

import {
  browseFs, createChatSession, deleteChatSession, fetchAgents, fetchChatSession,
  fetchChatSessions, fetchChatHealth, fetchProjects, patchChatSession, sendChatMessage,
  stopChatSession,
  type ChatMessage, type ChatSessionItem,
} from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import { Label } from "~/vendor/ui/components/ui/label";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "~/vendor/ui/components/ui/dialog";

/** 空状态提示词:覆盖「问状态」与「让它干活」两类第一句话。 */
const CHAT_STARTERS = [
  "服务器状态怎么样?",
  "最新一轮实验跑得如何?",
  "帮我看看 Baseline 卡在哪一步",
  "把项目的自动迭代跑起来:先看循环状态再给我方案",
];

// --------------------------------------------------------------- markdown

/** 轻量渲染:``` 代码块、行内 `code`、**粗体**。不引第三方依赖。 */
function InlineText({ text }: { text: string }) {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).filter(Boolean);
  return (
    <>
      {parts.map((part, i) => {
        if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
          return (
            <code key={i}
                  className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">
              {part.slice(1, -1)}
            </code>
          );
        }
        if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
          return <strong key={i}>{part.slice(2, -2)}</strong>;
        }
        return <span key={i}>{part}</span>;
      })}
    </>
  );
}

function MarkdownBlock({ text }: { text: string }) {
  const segments = text.split(/```/);
  return (
    <div className="space-y-2">
      {segments.map((seg, i) =>
        i % 2 === 1 ? (
          <pre key={i} className="overflow-x-auto rounded-lg border border-surface-border
                                  bg-muted/60 p-3 font-mono text-caption leading-relaxed">
            {seg.replace(/^\w*\n/, "").replace(/\n$/, "")}
          </pre>
        ) : (
          seg.split("\n").filter((l) => l.trim()).map((line, j) => (
            <p key={`${i}-${j}`} className="whitespace-pre-wrap">
              <InlineText text={line} />
            </p>
          ))
        ),
      )}
    </div>
  );
}

// ------------------------------------------------------------ tool events

function summarizeInput(input: unknown): string {
  const s = typeof input === "string" ? input : JSON.stringify(input);
  return s.length > 140 ? `${s.slice(0, 140)}…` : s;
}

function ToolEventCard({ event }: { event: ChatMessage["events"][number] }) {
  const [open, setOpen] = useState(false);
  const isUse = event.kind === "tool_use";
  const title = isUse ? event.name : "结果";
  const preview = isUse ? summarizeInput(event.input) : summarizeInput(event.content);
  return (
    <div className="rounded-lg border border-surface-border/70 bg-muted/30 text-caption">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-muted-foreground
                   hover:bg-muted/60"
      >
        {isUse ? <Wrench className="size-3 shrink-0" /> : <Terminal className="size-3 shrink-0" />}
        <span className="shrink-0 font-medium">{title}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-micro">{preview}</span>
        <span className="shrink-0 text-micro">{open ? "收起" : "展开"}</span>
      </button>
      {open ? (
        <pre className="max-h-64 overflow-auto border-t border-surface-border/70 px-2.5 py-2
                        font-mono text-micro leading-relaxed whitespace-pre-wrap">
          {isUse ? JSON.stringify(event.input, null, 2) : event.content}
        </pre>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------- message row

/** 上下文窗口 HUD:claude stream-json 的 usage tokens 换算成占用比例。 */
function ContextHud({ context, running }: {
  context?: { used_tokens: number; window_tokens: number; percent: number };
  running: boolean;
}) {
  if (!context || context.window_tokens <= 0) return null;
  const { used_tokens, window_tokens, percent } = context;
  const fmt = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`);
  const tone = percent >= 85 ? "bg-warning" : percent >= 60 ? "bg-brand" : "bg-brand/70";
  return (
    <span
      className="hidden shrink-0 items-center gap-1.5 md:inline-flex"
      title={`上下文窗口占用 ${fmt(used_tokens)} / ${fmt(window_tokens)} tokens`
              + "(来自最近一次请求的 usage;接近上限时建议开新会话)"}
    >
      {running ? <Loader2 className="size-3 animate-spin text-brand" /> : null}
      <span className="h-1.5 w-20 overflow-hidden rounded-full bg-muted">
        <span className={`block h-full rounded-full ${tone}`}
              style={{ width: `${Math.min(100, percent)}%` }} />
      </span>
      <span className={`font-mono text-micro tabular-nums ${
        percent >= 85 ? "text-warning" : "text-muted-foreground"
      }`}>
        {fmt(used_tokens)}/{fmt(window_tokens)}
      </span>
    </span>
  );
}

function statusLine(msg: ChatMessage): string | null {  if (msg.status === "running") return "正在执行…";
  if (msg.status === "stopped") return "已手动停止";
  if (msg.status === "error") return "执行出错";
  const meta = msg.meta as { num_turns?: number; cost_usd?: number };
  if (meta?.num_turns) {
    const cost = typeof meta.cost_usd === "number" ? ` · $${meta.cost_usd.toFixed(4)}` : "";
    return `${meta.num_turns} 轮${cost}`;
  }
  return null;
}

function MessageRow({ msg }: { msg: ChatMessage }) {
  if (msg.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-brand px-3.5 py-2
                        text-body text-brand-foreground whitespace-pre-wrap">
          {msg.content}
        </div>
      </div>
    );
  }
  const line = statusLine(msg);
  return (
    <div className="flex gap-2.5">
      <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full
                       bg-muted text-muted-foreground">
        {msg.status === "running"
          ? <Loader2 className="size-3.5 animate-spin" />
          : <Bot className="size-3.5" />}
      </span>
      <div className="min-w-0 flex-1 space-y-2">
        {msg.content
          ? <div className="text-body leading-relaxed"><MarkdownBlock text={msg.content} /></div>
          : msg.status === "running"
            ? <p className="text-caption text-muted-foreground">思考中…</p>
            : null}
        {msg.events.length > 0 ? (
          <div className="space-y-1.5">
            {msg.events.map((ev, i) => <ToolEventCard key={i} event={ev} />)}
          </div>
        ) : null}
        {line ? (
          <p className={`text-micro ${msg.status === "error" ? "text-destructive" : "text-muted-foreground/70"}`}>
            {line}
          </p>
        ) : null}
      </div>
    </div>
  );
}

// ------------------------------------------------------------- folder picker

/** 本机文件夹浏览器:面包屑 + 子目录列表 + 快捷入口,点「选择此目录」回填。 */
function FolderBrowserDialog({ open, onOpenChange, initialPath, onPick }: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  initialPath?: string;
  onPick: (path: string) => void;
}) {
  const [current, setCurrent] = useState(initialPath || "");
  const [manual, setManual] = useState("");
  const browse = useQuery({
    queryKey: ["chat-fs", current, open],
    queryFn: () => browseFs(current),
    enabled: open,
    retry: false,
  });
  useEffect(() => {
    if (open) {
      setCurrent(initialPath || "");
      setManual("");
    }
  }, [open, initialPath]);
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: fetchProjects,
    select: (all) => all.filter((p) => p.server.transport_mode === "local" && p.workdir),
  });

  const crumbs = (browse.data?.path ?? current).split("/").filter(Boolean);
  const go = (path: string) => {
    setManual("");
    setCurrent(path);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>选择工作区</DialogTitle>
          <DialogDescription>浏览本机目录,选定 claude 的工作文件夹。</DialogDescription>
        </DialogHeader>

        {/* 快捷入口:家目录 + local 项目工作区 */}
        <div className="flex flex-wrap gap-1.5">
          <button type="button" onClick={() => go("")}
                  className="inline-flex items-center gap-1 rounded-full bg-muted/60 px-2 py-1
                             text-micro text-muted-foreground hover:bg-muted">
            <Home className="size-3" /> 家目录
          </button>
          {(projects.data ?? []).slice(0, 4).map((p) => (
            <button key={p.id} type="button" onClick={() => go(p.workdir)}
                    title={p.workdir}
                    className="inline-flex max-w-48 items-center gap-1 rounded-full bg-brand/10
                               px-2 py-1 text-micro text-brand hover:bg-brand/20">
              <FolderOpen className="size-3 shrink-0" />
              <span className="truncate">{p.name}</span>
            </button>
          ))}
        </div>

        {/* 面包屑 */}
        <div className="flex flex-wrap items-center gap-0.5 rounded-lg border border-surface-border
                        bg-muted/30 px-2 py-1.5 text-caption">
          <button type="button" onClick={() => go("/")} className="px-1 hover:text-brand">/</button>
          {crumbs.map((part, i) => (
            <span key={i} className="flex items-center">
              <button type="button"
                      onClick={() => go("/" + crumbs.slice(0, i + 1).join("/"))}
                      className="max-w-32 truncate px-1 hover:text-brand">
                {part}
              </button>
              {i < crumbs.length - 1 ? <span className="text-muted-foreground/50">/</span> : null}
            </span>
          ))}
        </div>

        {/* 目录列表 */}
        <div className="h-64 overflow-y-auto rounded-lg border border-surface-border">
          {browse.isLoading ? (
            <div className="flex h-full items-center justify-center">
              <Loader2 className="size-4 animate-spin text-muted-foreground" />
            </div>
          ) : browse.isError ? (
            <div className="flex h-full flex-col items-center justify-center gap-2 p-4
                            text-center text-caption text-muted-foreground">
              {browse.error instanceof Error ? browse.error.message : "读取目录失败"}
              <Button size="sm" variant="secondary" onClick={() => browse.refetch()}>
                重试
              </Button>
            </div>
          ) : (browse.data?.entries ?? []).length === 0 ? (
            <p className="flex h-full items-center justify-center text-caption text-muted-foreground">
              此目录下没有子目录
            </p>
          ) : (
            (browse.data?.entries ?? []).map((e) => (
              <button key={e.path} type="button" onClick={() => go(e.path)}
                      className="flex w-full items-center gap-2 border-b border-surface-border/40
                                 px-3 py-2 text-left text-body last:border-0 hover:bg-muted/50">
                <Folder className="size-4 shrink-0 text-muted-foreground" />
                <span className="truncate">{e.name}</span>
              </button>
            ))
          )}
        </div>

        {/* 手动路径 + 确认 */}
        <div className="flex items-center gap-2">
          <Input value={manual} placeholder={browse.data?.path ?? "或直接粘贴绝对路径"}
                 onChange={(e) => setManual(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter" && manual.trim()) go(manual.trim()); }} />
          <Button variant="secondary" size="icon" title="上一级"
                  disabled={!browse.data?.parent}
                  onClick={() => browse.data?.parent && go(browse.data.parent)}>
            <ArrowUp className="size-4" />
          </Button>
          <Button onClick={() => onPick(manual.trim() || browse.data?.path || "")}
                  disabled={browse.isError && !manual.trim()}>
            选择此目录
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ----------------------------------------------------------- new session

function NewSessionDialog({ open, onOpenChange, onCreated }: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: (id: string) => void;
}) {
  const [cwd, setCwd] = useState("");
  const [agentId, setAgentId] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const agents = useQuery({ queryKey: ["agents"], queryFn: fetchAgents });
  // 内置 chatagent(供应商/Key 在 Agents 页编辑)排最前并作为默认选择
  const sortedAgents = (agents.data ?? []).slice().sort((a, b) =>
    (b.id === "agent-builtin-chat" ? 1 : 0) - (a.id === "agent-builtin-chat" ? 1 : 0));
  const builtin = sortedAgents.find((a) => a.id === "agent-builtin-chat");
  useEffect(() => {
    if (!agentId && builtin) setAgentId(builtin.id);
  }, [agentId, builtin]);
  const create = useMutation({
    mutationFn: () => createChatSession({
      cwd: cwd.trim() || undefined,
      agent_id: agentId || undefined,
    }),
    onSuccess: ({ session }) => {
      onCreated(session.id);
      onOpenChange(false);
      setCwd("");
      setAgentId("");
    },
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>新会话</DialogTitle>
          <DialogDescription>
            一个会话就是一个本地 claude code 实例,多轮对话共享上下文。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="chat-cwd">工作区(可选)</Label>
            <div className="flex gap-2">
              <Input
                id="chat-cwd" placeholder="/Users/you/project" value={cwd}
                onChange={(e) => setCwd(e.target.value)}
              />
              <Button variant="secondary" size="icon" title="浏览本机目录"
                      onClick={() => setPickerOpen(true)}>
                <FolderOpen className="size-4" />
              </Button>
            </div>
            <p className="text-micro text-muted-foreground">
              claude 在该目录下读写文件、跑命令;点文件夹图标从本机选,留空则不限定。
            </p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="chat-agent">Agent 配置(可选)</Label>
            <select
              id="chat-agent" value={agentId} onChange={(e) => setAgentId(e.target.value)}
              className="h-9 w-full rounded-md border border-surface-border bg-background px-3
                         text-body outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
            >
              <option value="">默认(本机 claude 登录)</option>
              {sortedAgents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}{a.id === "agent-builtin-chat" ? "(内置)" : ""}{a.hasApiKey ? " · Key" : ""}
                </option>
              ))}
            </select>
            <p className="text-micro text-muted-foreground">
              选 Agents 页配置的提示词 / GLM Key;autolab 工具(服务器、实验)始终可用。
            </p>
          </div>
        </div>
          <DialogFooter>
            <Button onClick={() => create.mutate()} disabled={create.isPending}>
              {create.isPending ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}
              创建
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <FolderBrowserDialog
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        initialPath={cwd || undefined}
        onPick={(path) => {
          setCwd(path);
          setPickerOpen(false);
        }}
      />
    </>
  );
}

// ------------------------------------------------------------- session view

function Composer({ session }: { session: ChatSessionItem }) {
  const [text, setText] = useState("");
  const queryClient = useQueryClient();
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["chat-session", session.id] });
    void queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
  }, [queryClient, session.id]);
  const send = useMutation({
    mutationFn: () => sendChatMessage(session.id, text.trim()),
    onSuccess: () => { setText(""); refresh(); },
    onError: (e: Error) => toast.error(e.message),
  });
  const stop = useMutation({
    mutationFn: () => stopChatSession(session.id),
    onSuccess: refresh,
  });
  const canSend = text.trim().length > 0 && !session.running;
  return (
    <div className="border-t border-surface-border bg-background p-3">
      <div className="flex items-end gap-2">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && canSend) {
              e.preventDefault();
              send.mutate();
            }
          }}
          rows={Math.min(6, Math.max(1, text.split("\n").length))}
          placeholder={session.running ? "回复生成中…可先停止" : "问点什么,或让它干活(⏎ 发送,⇧⏎ 换行)"}
          className="max-h-40 min-h-9 flex-1 resize-none rounded-xl border border-surface-border
                     bg-muted/30 px-3 py-2 text-body outline-none
                     placeholder:text-muted-foreground/60 focus-visible:ring-2 focus-visible:ring-brand/40"
        />
        {session.running ? (
          <Button variant="secondary" size="icon" title="停止生成"
                  onClick={() => stop.mutate()} disabled={stop.isPending}>
            <Square className="size-4" />
          </Button>
        ) : (
          <Button size="icon" title="发送" disabled={!canSend || send.isPending}
                  onClick={() => send.mutate()}>
            {send.isPending
              ? <Loader2 className="size-4 animate-spin" />
              : <ArrowUp className="size-4" />}
          </Button>
        )}
      </div>
    </div>
  );
}

function SessionView({ sessionId, onDeleted }: { sessionId: string; onDeleted: () => void }) {
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: ["chat-session", sessionId],
    queryFn: () => fetchChatSession(sessionId),
    // 生成中快速轮询拿工具调用进度;空闲时靠手动 invalidate
    refetchInterval: (query) => (query.state.data?.session.running ? 1200 : false),
  });
  const del = useMutation({
    mutationFn: () => deleteChatSession(sessionId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      onDeleted();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const bottomRef = useRef<HTMLDivElement>(null);
  const messages = detail.data?.messages ?? [];
  const lastSeq = messages.length ? messages[messages.length - 1].seq : 0;
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [lastSeq, detail.data?.session.running]);

  if (detail.isLoading) {
    return <div className="flex flex-1 items-center justify-center text-muted-foreground">
      <Loader2 className="size-5 animate-spin" />
    </div>;
  }
  if (detail.isError || !detail.data) {
    return <div className="flex flex-1 items-center justify-center p-6 text-center
                           text-caption text-muted-foreground">
      会话加载失败:{String(detail.error)}
    </div>;
  }
  const { session } = detail.data;
  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-surface-border px-4">
        <p className="min-w-0 flex-1 truncate text-body font-medium">
          {session.title || "新会话"}
        </p>
        {session.cwd ? (
          <span className="hidden items-center gap-1 truncate rounded-full bg-muted/60 px-2 py-0.5
                           text-micro text-muted-foreground md:inline-flex" title={session.cwd}>
            <FolderOpen className="size-3 shrink-0" />
            {session.cwd}
          </span>
        ) : null}
        {session.agent_name ? (
          <span className="hidden items-center gap-1 rounded-full bg-brand/15 px-2 py-0.5
                           text-micro text-brand md:inline-flex">
            <Bot className="size-3" />{session.agent_name}
          </span>
        ) : null}
        <ContextHud context={session.context} running={session.running} />
        <Button variant="ghost" size="icon-sm" title="删除会话"
                className="text-muted-foreground hover:text-destructive"
                onClick={() => del.mutate()} disabled={del.isPending}>
          <Trash2 className="size-3.5" />
        </Button>
      </header>

      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        {messages.length === 0 ? (
          <p className="pt-16 text-center text-caption text-muted-foreground">
            说点什么吧——比如「服务器状态怎么样?」或「帮我在工作区里写个脚本」。
          </p>
        ) : messages.map((m) => <MessageRow key={m.seq} msg={m} />)}
        <div ref={bottomRef} />
      </div>

      <Composer session={session} />
    </div>
  );
}

// -------------------------------------------------------------------- page

export default function ChatPage() {
  const [activeId, setActiveId] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const sessions = useQuery({
    queryKey: ["chat-sessions"],
    queryFn: fetchChatSessions,
    refetchInterval: 4000,
  });
  const health = useQuery({ queryKey: ["chat-health"], queryFn: fetchChatHealth });
  const list = sessions.data ?? [];
  const active = useMemo(
    () => list.find((s) => s.id === activeId) ?? null,
    [list, activeId],
  );
  const [starting, setStarting] = useState<string | null>(null);
  // 提示词 chip = 新建默认会话并直接发出第一条消息,空状态不是死胡同
  const startWith = async (first: string) => {
    if (starting) return;
    setStarting(first);
    try {
      const { session } = await createChatSession({});
      await sendChatMessage(session.id, first);
      setActiveId(session.id);
      void sessions.refetch();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setStarting(null);
    }
  };

  return (
    <div className="flex h-full min-h-0">
      <aside className="flex w-60 shrink-0 flex-col border-r border-surface-border">
        <div className="flex h-12 items-center justify-between border-b border-surface-border px-3">
          <span className="text-caption font-semibold text-muted-foreground">会话</span>
          <Button size="icon-sm" variant="ghost" title="新会话"
                  onClick={() => setDialogOpen(true)}>
            <Plus className="size-4" />
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto p-1.5">
          {sessions.isLoading ? (
            <div className="flex justify-center pt-6"><Loader2 className="size-4 animate-spin text-muted-foreground" /></div>
          ) : list.length === 0 ? (
            <p className="px-2 pt-6 text-center text-micro text-muted-foreground">
              还没有会话,点 + 新建一个
            </p>
          ) : list.map((s) => (
            <button
              key={s.id} type="button" onClick={() => setActiveId(s.id)}
              className={`mb-0.5 flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left
                          text-caption ${s.id === activeId
                            ? "bg-muted font-medium"
                            : "hover:bg-muted/50"}`}
            >
              <MessageCircle className="size-3.5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate">{s.title || "新会话"}</span>
              {s.running ? <Loader2 className="size-3 shrink-0 animate-spin text-brand" /> : null}
            </button>
          ))}
        </div>
        {health.data && !health.data.claude_available ? (
          <p className="border-t border-surface-border p-2 text-micro text-warning">
            未检测到 claude CLI,会话将无法运行
          </p>
        ) : null}
      </aside>

      {active ? (
        <SessionView sessionId={active.id} onDeleted={() => setActiveId(null)} />
      ) : (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
          <span className="flex size-12 items-center justify-center rounded-2xl bg-brand/15 text-brand">
            <MessageCircle className="size-6" />
          </span>
          <div>
            <p className="text-body-lg font-medium">Chat</p>
            <p className="mx-auto mt-1 max-w-md text-caption leading-relaxed text-muted-foreground">
              和一个跑在本机的 claude code 实例对话:选个工作区让它干活,
              或直接问服务器状态、实验跑得怎么样。
            </p>
          </div>
          <Button onClick={() => setDialogOpen(true)}>
            <Plus className="size-4" /> 新会话
          </Button>
          <div className="mt-2 flex max-w-lg flex-wrap items-center justify-center gap-1.5">
            {CHAT_STARTERS.map((s) => (
              <button
                key={s} type="button" disabled={starting !== null}
                onClick={() => void startWith(s)}
                className="inline-flex items-center gap-1.5 rounded-full border border-surface-border
                           bg-surface px-2.5 py-1 text-caption text-muted-foreground
                           hover:border-brand/40 hover:text-foreground disabled:opacity-60"
              >
                {starting === s ? <Loader2 className="size-3 animate-spin" /> : null}
                {s}
              </button>
            ))}
          </div>
          <p className="text-micro text-muted-foreground">点提示词即刻开会话并发出第一条消息</p>
        </div>
      )}

      <NewSessionDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onCreated={(id) => {
          setActiveId(id);
          void sessions.refetch();
        }}
      />
    </div>
  );
}
