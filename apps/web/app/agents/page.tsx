"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Bot, BotOff, KeyRound, Loader2, MoreHorizontal, Pencil, Plus, Terminal } from "lucide-react";
import {
  createAgent, deleteAgent, fetchAgents, fetchChatAgentConfig,
  updateChatAgentConfig, type AgentItem, type ChatAgentConfig,
} from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import { Label } from "~/vendor/ui/components/ui/label";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle,
} from "~/vendor/ui/components/ui/dialog";
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from "~/vendor/ui/components/ui/dropdown-menu";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL, PAGE_TOOLBAR } from "@/vendor/views/layout/page-header";
import { EmptyState } from "@/components/empty-state";
import { ApiError } from "@/components/api-error";

/** 与后端 chat.BUILTIN_AGENT_ID 一致:内置 Chat Agent,不可删除,行内编辑配置。 */
const BUILTIN_CHAT_AGENT_ID = "agent-builtin-chat";

const isBuiltin = (agent: AgentItem) => agent.id === BUILTIN_CHAT_AGENT_ID;

function initials(name: string): string {
  const parts = name.trim().split(/[\s-_]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "A") + (parts[1]?.[0] ?? "")).toUpperCase();
}

function AgentRow({ agent, onDelete, onEditBuiltin }: {
  agent: AgentItem;
  onDelete: (a: AgentItem) => void;
  onEditBuiltin: () => void;
}) {
  const builtin = isBuiltin(agent);
  return (
    <div className="group/row flex h-16 items-center gap-3 border-b border-surface-border/50 px-3 last:border-0 hover:bg-muted/30">
      <span className="relative flex size-9 shrink-0 items-center justify-center rounded-full bg-brand/15 text-caption font-semibold text-brand">
        {initials(agent.name)}
        <span
          className={`absolute -bottom-0.5 -right-0.5 size-2.5 rounded-full border-2 border-background ${
            agent.hasApiKey ? "bg-success" : "bg-muted-foreground/40"
          }`}
          title={agent.hasApiKey ? "已配置 API Key" : "未配置 API Key(用环境变量)"}
        />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <p className="truncate text-body font-medium">{agent.name}</p>
          {builtin ? (
            <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-brand/10 px-1.5 py-0.5 text-micro font-medium text-brand">
              内置
            </span>
          ) : null}
          <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-surface-border px-2 py-0.5 text-micro text-muted-foreground">
            <Terminal className="size-3" />
            {agent.shell === "claude" ? "Claude Code" : agent.shell} · {agent.model.toUpperCase()}
          </span>
        </div>
        <p className="truncate text-caption text-muted-foreground">
          {agent.description || agent.systemPrompt.slice(0, 80) || "—"}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-3 text-micro text-muted-foreground">
        <span
          className={`inline-flex w-14 items-center gap-1 ${agent.hasApiKey ? "text-success" : "text-muted-foreground/60"}`}
          title={agent.hasApiKey ? "API Key 已存入本机凭据" : "未配置 API Key"}
        >
          <KeyRound className="size-3" />
          {agent.hasApiKey ? "已配 Key" : "无 Key"}
        </span>
        <span className="hidden w-16 justify-end text-right font-mono tabular-nums md:inline">
          {agent.created_at.slice(0, 10)}
        </span>
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button
                variant="ghost" size="icon-sm" aria-label="Agent 操作"
                className="rounded-full text-muted-foreground opacity-0 transition-opacity group-hover/row:opacity-100"
              >
                <MoreHorizontal className="size-3.5" />
              </Button>
            }
          />
          <DropdownMenuContent align="end">
            {builtin ? (
              <DropdownMenuItem onClick={onEditBuiltin}>
                <Pencil className="size-3.5" />
                编辑供应商 / Key
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem onClick={() => toast.info("Agent 会话执行待接入:将用存储的 Key 拉起 Claude Code,扫描项目工作区、检查数据集、跑通 Baseline")}>
                <Bot className="size-3.5" />
                启动会话(待接线)
              </DropdownMenuItem>
            )}
            {!builtin ? (
              <DropdownMenuItem className="text-destructive" onClick={() => onDelete(agent)}>
                <BotOff className="size-3.5" />
                删除
              </DropdownMenuItem>
            ) : null}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}

/** 内置 chatagent 的配置:供应商预设 / 自定义端点 / 模型 / API Key。 */
function ChatAgentConfigDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const config = useQuery({
    queryKey: ["chat-agent-config"],
    queryFn: fetchChatAgentConfig,
    enabled: open,
  });
  const [provider, setProvider] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const loaded = config.data;

  // 配置到达后初始化表单(切换 provider 时 model/baseUrl 跟随预设)
  useEffect(() => {
    if (!loaded) return;
    setProvider((prev) => prev || loaded.provider);
  }, [loaded]);
  useEffect(() => {
    if (!loaded || !provider) return;
    const preset = loaded.providers[provider];
    const fromConfig = loaded.provider === provider;
    setBaseUrl(fromConfig ? loaded.base_url : preset?.base_url || "");
    setModel(fromConfig ? loaded.model : preset?.default_model || "");
  }, [provider, loaded]);

  const submit = async () => {
    setSubmitting(true);
    try {
      await updateChatAgentConfig({
        provider,
        base_url: provider === "custom" ? baseUrl : undefined,
        model: model || undefined,
        api_key: apiKey.trim() || undefined,
      });
      toast.success("chatagent 配置已保存", {
        description: apiKey.trim() ? "API Key 已存入本机凭据(0600)" : undefined,
      });
      setApiKey("");
      onClose();
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
      void queryClient.invalidateQueries({ queryKey: ["chat-agent-config"] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const preset = loaded?.providers[provider];

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? null : onClose())}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>chatagent 设置</DialogTitle>
          <DialogDescription>
            Chat 页默认智能体(内置,不可删除)。换供应商、填 Key 即刻生效,
            正在进行的会话不受影响。
          </DialogDescription>
        </DialogHeader>
        {config.isLoading || !loaded ? (
          <div className="flex justify-center py-8">
            <Loader2 className="size-4 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <div className="grid gap-3">
            <div className="grid gap-1.5">
              <Label>供应商</Label>
              <select
                className="h-9 w-full rounded-md border border-surface-border bg-background px-3
                           text-body outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
              >
                {Object.entries(loaded.providers).map(([key, p]) => (
                  <option key={key} value={key}>{p.label}</option>
                ))}
              </select>
              {provider === "local" ? (
                <p className="text-micro text-muted-foreground">
                  用本机 claude CLI 的登录态(终端跑一次 claude 登录即可),无需 Key。
                </p>
              ) : null}
            </div>
            {provider === "custom" ? (
              <div className="grid gap-1.5">
                <Label>Base URL(Anthropic 兼容端点)</Label>
                <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
                       placeholder="https://your-relay.example/api" spellCheck={false} />
              </div>
            ) : provider !== "local" ? (
              <div className="grid gap-1.5">
                <Label>端点</Label>
                <Input value={baseUrl} disabled
                       className="font-mono text-caption text-muted-foreground"
                       spellCheck={false} />
              </div>
            ) : null}
            {provider !== "local" ? (
              <div className="grid gap-1.5">
                <Label>模型{preset?.default_model ? "(可改)" : ""}</Label>
                <Input value={model} onChange={(e) => setModel(e.target.value)}
                       placeholder={preset?.default_model || "glm-4.6"} spellCheck={false} />
              </div>
            ) : null}
            {provider !== "local" ? (
              <div className="grid gap-1.5">
                <Label>
                  API Key{loaded.has_api_key ? "(已配置,留空保持不变)" : ""}
                </Label>
                <Input type="password" value={apiKey}
                       onChange={(e) => setApiKey(e.target.value)}
                       placeholder={loaded.has_api_key ? "••••••(不变)" : "粘贴 Key"}
                       spellCheck={false} />
              </div>
            ) : null}
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>取消</Button>
          <Button size="sm" disabled={submitting || !provider} onClick={() => void submit()}
                  className="gap-1.5">
            {submitting ? <Loader2 className="size-3.5 animate-spin" /> : <Pencil className="size-3.5" />}
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CreateAgentDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [shell, setShell] = useState("claude");
  const [model, setModel] = useState("glm");
  const [apiKey, setApiKey] = useState("");
  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (!name.trim() || !prompt.trim()) {
      toast.error("名称与提示词为必填");
      return;
    }
    setSubmitting(true);
    try {
      const result = await createAgent({
        name: name.trim(),
        description: description.trim(),
        system_prompt: prompt.trim(),
        shell,
        model,
        api_key: apiKey.trim() || undefined,
      });
      toast.success(`已创建 ${result.agent.name}`, {
        description: apiKey.trim()
          ? "API Key 已存入本机凭据文件(0600),不会出现在任何 API 响应里"
          : "未配置 Key,运行时将使用宿主环境变量",
      });
      onClose();
      setName(""); setDescription(""); setApiKey(""); setPrompt("");
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? null : onClose())}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>新建 Agent</DialogTitle>
          <DialogDescription>
            每个 Agent 是一套「编码外壳 + 模型 + 提示词」的组合,当前外壳统一为
            Claude Code 承载 GLM(Coding Plan key)。创建后用于对项目发起研究会话
            (会话执行下一迭代接入)。
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-1.5">
              <Label>名称</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="baseline-runner" />
            </div>
            <div className="grid gap-1.5">
              <Label>简介(一行)</Label>
              <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="负责基线复现与指标核对" />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-1.5">
              <Label>编码外壳</Label>
              <select
                className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                value={shell}
                onChange={(e) => setShell(e.target.value)}
              >
                <option value="claude">Claude Code(承载 GLM)</option>
              </select>
            </div>
            <div className="grid gap-1.5">
              <Label>模型</Label>
              <select
                className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                value={model}
                onChange={(e) => setModel(e.target.value)}
              >
                <option value="glm">GLM(Coding Plan)</option>
              </select>
            </div>
          </div>
          <div className="grid gap-1.5">
            <Label>API Key(可选;存本机凭据,运行时也可用宿主环境变量)</Label>
            <Input
              type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)}
              placeholder="GLM Coding Plan Key(留空则用环境变量)" spellCheck={false}
            />
          </div>
          <div className="grid gap-1.5">
            <Label>提示词(系统提示,定义这个 Agent 的研究行为)</Label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={6}
              spellCheck={false}
              placeholder={"你是严谨的科研协作者。接到 Baseline 申请后:\n1. 扫描服务器上的项目工作区,检查环境与数据集是否齐备;\n2. 数据集缺失时提醒用户补充,不要瞎猜路径;\n3. 环境就绪后自己先跑通一遍基线(至少 1 个数据集);\n4. 拿到基准指标后汇报,失败先怀疑自己并写清失败经验。"}
              className="w-full resize-y rounded-md border border-input bg-transparent p-2 font-mono text-caption leading-relaxed outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>取消</Button>
          <Button size="sm" disabled={submitting} onClick={() => void submit()} className="gap-1.5">
            {submitting ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
            创建
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function AgentsPage() {
  const [open, setOpen] = useState(false);
  const [builtinOpen, setBuiltinOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<AgentItem | null>(null);
  const queryClient = useQueryClient();
  const { data: agents = [], isLoading, error } = useQuery({
    queryKey: ["agents"],
    queryFn: fetchAgents,
    refetchInterval: 10000,
  });

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    try {
      await deleteAgent(deleteTarget.id);
      toast.success(`已删除 ${deleteTarget.name}`);
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setDeleteTarget(null);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <Bot className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">Agents</h1>
        {agents.length > 0 ? (
          <span className="font-mono text-caption tabular-nums text-muted-foreground">{agents.length}</span>
        ) : null}
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          编码外壳 + 模型 + 提示词的研究协作者
        </p>
        <div className="ml-auto">
          <Button size="sm" className="gap-1.5" onClick={() => setOpen(true)}>
            <Plus className="size-3.5" />
            新建 Agent
          </Button>
        </div>
      </PageHeader>

      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${PAGE_RAIL} flex min-h-[60vh] flex-col py-4`}>
          {error ? (
            <ApiError
              error={error}
              onRetry={() => void queryClient.invalidateQueries({ queryKey: ["agents"] })}
            />
          ) : isLoading ? (
            <p className="text-body text-muted-foreground">加载 Agents…</p>
          ) : agents.length === 0 ? (
            <EmptyState
              icon={Bot}
              title="还没有 Agent"
              description="创建一个研究协作者:Claude Code 外壳承载 GLM,写清楚它的提示词——评估论文可行性、提 Baseline、跑迭代、记录失败经验。"
              action={
                <Button size="sm" className="gap-1.5" onClick={() => setOpen(true)}>
                  <Plus className="size-3.5" />
                  新建 Agent
                </Button>
              }
            />
          ) : (
            <div className={PAGE_TOOLBAR}>
              <span className="text-caption text-muted-foreground">
                {agents.length} 个 Agent · 会话执行待接入(创建/管理已可用)
              </span>
            </div>
          )}
          {agents.length > 0 && (
            <div className="overflow-hidden rounded-xl border-[0.5px] border-surface-border bg-surface shadow-[var(--surface-shadow)]">
              {agents.map((agent) => (
                <AgentRow key={agent.id} agent={agent} onDelete={setDeleteTarget}
                          onEditBuiltin={() => setBuiltinOpen(true)} />
              ))}
            </div>
          )}
        </div>
      </div>

      <CreateAgentDialog open={open} onClose={() => setOpen(false)} />
      <ChatAgentConfigDialog open={builtinOpen} onClose={() => setBuiltinOpen(false)} />

      <Dialog open={!!deleteTarget} onOpenChange={(next) => (next ? null : setDeleteTarget(null))}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>删除 {deleteTarget?.name}?</DialogTitle>
            <DialogDescription>将移除 Agent 条目并清理其 API Key 凭据。</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setDeleteTarget(null)}>取消</Button>
            <Button size="sm" onClick={() => void confirmDelete()}>删除</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
