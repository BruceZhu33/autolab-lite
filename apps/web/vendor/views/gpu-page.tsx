"use client";

/**
 * GPU 服务器管理（AutoLab 产品基础设施，v0.3 迁移第一个真实 sidecar 接入面）。
 * 数据经 @autolab/research/sidecar 直连本机 sidecar（capability token 鉴权），
 * 不经过 Multica fixture。视觉沿 Multica 页面语言（PageHeader + 边框行 + Badge）。
 *
 * 2026-09-15 用户口径：服务器卡不能只显示登记显存总量——接入实时遥测
 * （sidecar GET /servers/{id}/telemetry：SSH 拉 nvidia-smi + df；GPU 遥测
 * 落 gpus.extra_json，A1 预留位）。手动刷新为主（staleTime 30s），不做
 * 自动轮询——SSH 拉取有成本，轮询属 monitor worker 的活（后置项）。
 * 平台助手统一在 /chat 全局页（deviation #35）。
 */
import { useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Cloud,
  Code2,
  Copy,
  Cpu,
  ExternalLink,
  Info,
  Monitor,
  MoreVertical,
  RefreshCw,
  Server,
  Terminal,
} from "lucide-react";
import { toast } from "sonner";

import { EmptyState } from "@/components/empty-state";
import { Badge } from "~/vendor/ui/components/ui/badge";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "~/vendor/ui/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "~/vendor/ui/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "~/vendor/ui/components/ui/alert-dialog";
import { Input } from "~/vendor/ui/components/ui/input";
import { Label } from "~/vendor/ui/components/ui/label";
import { Progress } from "~/vendor/ui/components/ui/progress";
import { PageHeader } from "~/vendor/views/layout/page-header";
import { whoami } from "~/lib/api/system";
import {
  agentVersion,
  deleteServer,
  describeOpResult,
  getServerWorkspaceAccess,
  listServerDetails,
  registerServer,
  serverOp,
  type GpuTelemetry,
  type ServerDetail,
  type ServerOp,
  type ServerWorkspaceAccess,
} from "~/vendor/views/gpu-sidecar";
import { SidecarApiError } from "~/vendor/views/gpu-sidecar";
import { SitonholyPanel } from "~/vendor/views/sitonholy-panel";
import { SitonholyTerminalPane } from "~/vendor/views/sitonholy-terminal";

function copyTextSafe(text: string, label: string, silent = false): boolean {
  if (!text) return false;
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      void navigator.clipboard.writeText(text).catch(() => {});
      if (!silent) toast.success(`已复制${label}`);
      return true;
    }
  } catch {
    // ignore clipboard API error under non-HTTPS
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (ok && !silent) toast.success(`已复制${label}`);
    return ok;
  } catch {
    return false;
  }
}

const STATUS_DOT: Record<string, string> = {
  REACHABLE: "bg-emerald-500",
  UNREACHABLE: "bg-red-500",
  UNKNOWN: "bg-muted-foreground/40",
};

const EMPTY_FORM = {
  name: "",
  host: "",
  port: "22",
  username: "",
  auth_kind: "key" as "key" | "password",
  secret: "",
  transport_mode: "ssh" as "ssh" | "local",
  agent_port: "7810",
  agent_token: "",
  manifest_hmac_key: "",
};

function transportLabel(s: ServerDetail): string {
  const base =
    s.transport_mode === "local" ? "本机" : `${s.username}@${s.host}:${s.port}`;
  const agent = s.agent_port ? ` · agent:${s.agent_port}` : " · 无 agent";
  const ver = agentVersion(s) ? ` · agent v${agentVersion(s)}` : "";
  return `${base}${agent}${ver}`;
}

function fmtMem(mb: number): string {
  const gb = mb / 1024;
  return gb >= 1024 ? `${(gb / 1024).toFixed(1)}T` : `${gb.toFixed(1)}G`;
}

function memColor(pct: number): string {
  if (pct >= 95) return "text-destructive";
  if (pct >= 85) return "text-amber-600 dark:text-amber-400";
  return "text-foreground";
}

function fmtVramTier(mb: number): string {
  if (mb <= 0) return "";
  return `${Math.round(mb / 1024)} GB`;
}

function serverStatusLabel(status: ServerDetail["status"]): string {
  if (status === "REACHABLE") return "在线可用";
  if (status === "UNREACHABLE") return "离线";
  return "待检测";
}

function ClusterOverview({ servers }: { servers: ServerDetail[] }) {
  const totalServers = servers.length;
  const onlineServers = servers.filter((s) => s.status === "REACHABLE").length;
  const totalGpus = servers.reduce(
    (sum, s) => sum + (s.gpus.length > 0 ? s.gpus.length : (s.gpu_slots ?? 0)),
    0,
  );
  const idleGpus = servers.reduce(
    (sum, s) => sum + s.gpus.filter((g) => g.status === "IDLE").length,
    0,
  );
  const busyGpus = servers.reduce(
    (sum, s) => sum + s.gpus.filter((g) => g.status !== "IDLE").length,
    0,
  );
  const usedVramMb = servers.reduce(
    (sum, s) => sum + s.gpus.reduce((acc, g) => acc + (g.memory_used_mb || 0), 0),
    0,
  );
  const totalVramMb = servers.reduce(
    (sum, s) => sum + s.gpus.reduce((acc, g) => acc + (g.memory_total_mb || 0), 0),
    0,
  );
  const vramPct = totalVramMb > 0 ? Math.round((usedVramMb / totalVramMb) * 100) : 0;

  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div className="rounded-lg border bg-card p-3.5">
        <div className="text-caption text-muted-foreground">训练服务器</div>
        <div className="mt-1 flex items-baseline gap-2">
          <span className="font-mono text-2xl font-semibold tabular-nums">{onlineServers}</span>
          <span className="text-caption text-muted-foreground">/ {totalServers} 台在线</span>
        </div>
      </div>

      <div className="rounded-lg border bg-card p-3.5">
        <div className="text-caption text-muted-foreground">GPU 总卡数</div>
        <div className="mt-1 flex items-baseline gap-2">
          <span className="font-mono text-2xl font-semibold tabular-nums">{totalGpus}</span>
          <span className="text-caption text-muted-foreground">张加速卡</span>
        </div>
      </div>

      <div className="rounded-lg border bg-card p-3.5">
        <div className="text-caption text-muted-foreground">当前空闲可用</div>
        <div className="mt-1 flex items-baseline gap-2">
          <span
            className={`font-mono text-2xl font-semibold tabular-nums ${
              idleGpus > 0 ? "text-emerald-600 dark:text-emerald-400" : ""
            }`}
          >
            {idleGpus}
          </span>
          <span className="text-caption text-muted-foreground">
            {busyGpus > 0 ? `张空闲 · ${busyGpus} 张训练中` : totalGpus > 0 ? "张空闲" : "等待采集"}
          </span>
        </div>
      </div>

      <div className="rounded-lg border bg-card p-3.5">
        <div className="flex items-center justify-between text-caption text-muted-foreground">
          <span>总显存占用</span>
          {totalVramMb > 0 && <span className="font-mono tabular-nums">{vramPct}%</span>}
        </div>
        <div className="mt-1 font-mono text-sm font-semibold tabular-nums">
          {totalVramMb > 0 ? `${fmtMem(usedVramMb)} / ${fmtMem(totalVramMb)}` : "暂未采集"}
        </div>
        <Progress className="mt-2 h-1.5" value={vramPct} />
      </div>
    </div>
  );
}

function GpuTile({ g }: { g: GpuTelemetry }) {
  const usedPct =
    g.memory_total_mb > 0 ? Math.round((g.memory_used_mb / g.memory_total_mb) * 100) : 0;
  const tier = fmtVramTier(g.memory_total_mb);
  const title =
    g.model_name && g.model_name !== "GPU" ? `GPU #${g.gpu_index} · ${g.model_name}` : `GPU #${g.gpu_index}`;
  const isIdle = g.status === "IDLE";

  return (
    <div className="flex flex-col justify-between gap-2.5 rounded-lg border bg-muted/20 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <Cpu className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="truncate font-mono text-sm font-medium">{title}</span>
          {tier && (
            <Badge variant="outline" className="px-1.5 py-0 font-mono text-[11px]">
              {tier}
            </Badge>
          )}
        </div>
        <Badge
          variant={isIdle ? "secondary" : "default"}
          className={
            isIdle
              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
              : ""
          }
        >
          {isIdle ? "空闲可用" : "训练占用"}
        </Badge>
      </div>

      <div className="space-y-1">
        <div className="flex items-center justify-between text-caption">
          <span className="text-muted-foreground">显存</span>
          <span className={`font-mono tabular-nums ${memColor(usedPct)}`}>
            {fmtMem(g.memory_used_mb)} / {fmtMem(g.memory_total_mb)} ({usedPct}%)
          </span>
        </div>
        <Progress className="h-1.5" value={usedPct} />
      </div>

      <div className="flex items-center justify-between font-mono text-[11px] text-muted-foreground">
        <span>算力负载 {g.utilization_pct}%</span>
        <span>{g.temperature_c ? `${g.temperature_c}°C` : "—"}</span>
      </div>
    </div>
  );
}

function ServerCard({
  s,
  isAdmin,
  busy,
  isRefreshing,
  termOpen,
  onToggleTerminal,
  onOpenAccess,
  onRefresh,
  act,
  setUpgradeTarget,
  setDeleteTarget,
}: {
  s: ServerDetail;
  isAdmin: boolean;
  busy: string | null;
  isRefreshing: boolean;
  termOpen: boolean;
  onToggleTerminal: (s: ServerDetail, access?: ServerWorkspaceAccess) => void;
  onOpenAccess: (s: ServerDetail, mode: "vscode" | "remote", prefetched?: ServerWorkspaceAccess) => void;
  onRefresh: () => void;
  act: (s: ServerDetail, op: ServerOp, body?: unknown) => Promise<void>;
  setUpgradeTarget: (s: ServerDetail) => void;
  setDeleteTarget: (s: ServerDetail) => void;
}) {
  const idleCount = s.gpus.filter((g) => g.status === "IDLE").length;
  const totalSlots = s.gpus.length > 0 ? s.gpus.length : (s.gpu_slots ?? 0);

  const accessQuery = useQuery({
    queryKey: ["server-workspace-access", s.id],
    queryFn: () => getServerWorkspaceAccess(s.id),
    enabled: s.status === "REACHABLE",
    staleTime: 60_000,
    retry: false,
  });
  const access = accessQuery.data;

  return (
    <div className="space-y-3 rounded-xl border bg-card p-4">
      <div className="flex flex-wrap items-center gap-2.5">
        <span className={`inline-block size-2.5 rounded-full ${STATUS_DOT[s.status] ?? STATUS_DOT.UNKNOWN}`} />
        <span className="font-semibold tracking-tight">{s.name}</span>
        <Badge
          variant={s.status === "REACHABLE" ? "secondary" : "outline"}
          className={
            s.status === "REACHABLE"
              ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
              : ""
          }
        >
          {serverStatusLabel(s.status)}
        </Badge>
        <Badge variant="outline" className="font-mono">
          {s.gpus.length > 0 ? `${idleCount}/${s.gpus.length} 卡空闲` : `${totalSlots} 卡`}
        </Badge>
        <span className="text-caption text-muted-foreground">
          {isAdmin ? (
            <span className="font-mono text-xs">{transportLabel(s)}</span>
          ) : (
            <span>{s.transport_mode === "local" ? "本地训练节点" : "远端训练节点"}</span>
          )}
        </span>

        <span className="ml-auto flex flex-wrap items-center gap-1.5">
          {(access?.web_terminal_supported || access === undefined) && s.status === "REACHABLE" && (
            <Button
              size="sm"
              variant={termOpen ? "default" : "outline"}
              onClick={() => onToggleTerminal(s, access)}
              title="在当前页面打开交互式 Web 终端"
            >
              <Terminal className="mr-1 size-3.5" />
              {termOpen ? "收起终端" : "终端"}
            </Button>
          )}

          {access?.vscode_url ? (
            <a
              href={access.vscode_url}
              target="_blank"
              rel="noreferrer"
              onClick={() => {
                if (access.vscode_password) {
                  copyTextSafe(access.vscode_password, "VSCode 登录密码", true);
                  toast.success(`VSCode 密码：${access.vscode_password}`, {
                    description: "已自动复制到剪贴板，可在新标签页直接粘贴登录。",
                    duration: 15_000,
                  });
                }
                onOpenAccess(s, "vscode", access);
              }}
            >
              <Button size="sm" variant="outline" title="打开 Web VSCode 并查看/复制登录密码">
                <Code2 className="mr-1 size-3.5" />
                VSCode
              </Button>
            </a>
          ) : (
            <Button
              size="sm"
              variant="outline"
              onClick={() => onOpenAccess(s, "vscode", access)}
              title="查看 VSCode 连接方式与密码"
            >
              <Code2 className="mr-1 size-3.5" />
              VSCode
            </Button>
          )}

          <Button
            size="sm"
            variant="outline"
            onClick={() => onOpenAccess(s, "remote", access)}
            title="打开远程桌面 / SSH 连接信息与登录密码提示"
          >
            <Monitor className="mr-1 size-3.5" />
            远程电脑
          </Button>

          {isAdmin && (
            <>
              <Button size="sm" variant="outline" disabled={isRefreshing} onClick={onRefresh}>
                <Activity className="mr-1 size-3.5" />
                {isRefreshing ? "遥测中…" : "刷新遥测"}
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger
                  render={
                    <Button size="sm" variant="outline" aria-label={`更多操作 ${s.name}`} disabled={busy !== null}>
                      <MoreVertical className="size-3.5" />
                      管理
                    </Button>
                  }
                />
                <DropdownMenuContent align="end">
                  <DropdownMenuItem
                    disabled={busy === `${s.id}:connectivity-test`}
                    onClick={() => void act(s, "connectivity-test")}
                  >
                    连通性测试
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    disabled={!s.agent_port || busy === `${s.id}:runner/probe`}
                    onClick={() => void act(s, "runner/probe")}
                  >
                    Runner 探活
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    disabled={busy === `${s.id}:runner/install`}
                    onClick={() => void act(s, "runner/install")}
                  >
                    部署 / 修复 Agent
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    disabled={busy === `${s.id}:runner/upgrade`}
                    onClick={() => setUpgradeTarget(s)}
                  >
                    升级
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    variant="destructive"
                    disabled={busy === `${s.id}:delete`}
                    onClick={() => setDeleteTarget(s)}
                  >
                    删除服务器
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </>
          )}
        </span>
      </div>

      {termOpen && (
        <SitonholyTerminalPane
          scope="server"
          taskId={s.id}
          taskName={s.name}
          nativeTerminalUrl={access?.terminal_url || undefined}
          onClose={() => onToggleTerminal(s, access)}
        />
      )}

      {s.gpus.length === 0 ? (
        <div className="rounded-md border border-dashed bg-muted/10 px-3 py-2.5 text-caption text-muted-foreground">
          {totalSlots > 0
            ? `已登记 ${totalSlots} 张 GPU，当前暂未采集到实时显存/负载数据。`
            : "当前暂无实时显卡数据。"}
          {isAdmin && " 可点右上「管理 → 连通性测试」排查 SSH 或本地 Runner。"}
        </div>
      ) : (
        <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
          {s.gpus.map((g) => (
            <GpuTile key={g.id} g={g} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function GpuPage() {
  const qc = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [upgradeTarget, setUpgradeTarget] = useState<ServerDetail | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ServerDetail | null>(null);
  const [terminalServerId, setTerminalServerId] = useState<string | null>(null);
  const [accessModal, setAccessModal] = useState<{
    server: ServerDetail;
    mode: "vscode" | "remote";
    data: ServerWorkspaceAccess | null;
    loading: boolean;
    error: string | null;
  } | null>(null);

  const handleToggleTerminal = async (
    s: ServerDetail,
    prefetched?: ServerWorkspaceAccess,
  ) => {
    if (terminalServerId === s.id) {
      setTerminalServerId(null);
      return;
    }
    if (prefetched && !prefetched.web_terminal_supported) {
      void handleOpenAccess(s, "remote", prefetched);
      return;
    }
    if (!prefetched) {
      try {
        const fetched = await getServerWorkspaceAccess(s.id);
        if (!fetched.web_terminal_supported) {
          void handleOpenAccess(s, "remote", fetched);
          return;
        }
      } catch (e) {
        toast.error("打开终端失败", {
          description: e instanceof Error ? e.message : String(e),
        });
        return;
      }
    }
    setTerminalServerId(s.id);
  };

  const handleOpenAccess = async (
    s: ServerDetail,
    mode: "vscode" | "remote",
    prefetched?: ServerWorkspaceAccess,
  ) => {
    if (prefetched) {
      setAccessModal({ server: s, mode, data: prefetched, loading: false, error: null });
      return;
    }
    setAccessModal({ server: s, mode, data: null, loading: true, error: null });
    try {
      const data = await getServerWorkspaceAccess(s.id);
      setAccessModal({ server: s, mode, data, loading: false, error: null });
    } catch (e) {
      setAccessModal({
        server: s,
        mode,
        data: null,
        loading: false,
        error: e instanceof Error ? e.message : String(e),
      });
    }
  };

  const me = useQuery({
    queryKey: ["whoami"],
    queryFn: whoami,
    retry: false,
    staleTime: 60_000,
  });
  const isAdmin = me.data?.kind === "owner";

  const { data: servers, isPending, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["autolab", "servers"],
    queryFn: listServerDetails,
    refetchInterval: 5000,
  });

  const act = async (s: ServerDetail, op: ServerOp, body?: unknown) => {
    setBusy(`${s.id}:${op}`);
    const OP_LABEL: Record<ServerOp, string> = {
      "connectivity-test": "连通性测试",
      "runner/probe": "Runner 探活",
      "runner/install": "部署 Runner",
      "runner/upgrade": "升级 Runner",
    };
    try {
      const r = await serverOp(s.id, op, body);
      const view = describeOpResult(op, r);
      const toastFn = view.ok ? toast.success : toast.error;
      toastFn(`${s.name} · ${OP_LABEL[op]}：${view.title}`, {
        description: view.detail?.slice(0, 300),
      });
      await qc.invalidateQueries({ queryKey: ["autolab", "servers"] });
    } catch (e) {
      const msg = e instanceof SidecarApiError ? e.message : String(e);
      toast.error(`${s.name} · ${OP_LABEL[op]}失败`, {
        description: msg.slice(0, 300),
        duration: 8000,
      });
    } finally {
      setBusy(null);
    }
  };

  const confirmDelete = async () => {
    const s = deleteTarget;
    if (!s) return;
    setBusy(`${s.id}:delete`);
    try {
      const r = await deleteServer(s.id);
      toast.success(`已删除 ${r.name ?? s.name} 的注册（凭据一并清理）`);
      await qc.invalidateQueries({ queryKey: ["autolab", "servers"] });
    } catch (e) {
      if (e instanceof SidecarApiError && e.code === "HAS_REFERENCES") {
        toast.error(`${s.name} 有训练/环境历史，不可删除`, {
          description: `${e.message}（审计链约束：有投产记录的服务器保留）`,
          duration: 8000,
        });
      } else {
        const msg = e instanceof SidecarApiError ? e.message : String(e);
        toast.error(`删除 ${s.name} 失败`, { description: msg.slice(0, 300), duration: 8000 });
      }
    } finally {
      setBusy(null);
      setDeleteTarget(null);
    }
  };

  const submit = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      await registerServer({
        name: form.name,
        host: form.host,
        port: form.port,
        username: form.username,
        auth_kind: form.auth_kind,
        secret: form.secret,
        transport_mode: form.transport_mode,
        agent_port: form.agent_port,
        agent_token: form.agent_token,
        manifest_hmac_key: form.manifest_hmac_key,
      });
      toast.success(`已注册 ${form.name}`);
      setOpen(false);
      setForm(EMPTY_FORM);
      await qc.invalidateQueries({ queryKey: ["autolab", "servers"] });
    } catch (e) {
      const msg = e instanceof SidecarApiError ? e.message : String(e);
      setFormError(msg.slice(0, 300));
      toast.error("注册失败", { description: msg.slice(0, 300), duration: 8000 });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <PageHeader>
        <span className="relative flex size-4 items-center justify-center">
          {isFetching ? (
            <RefreshCw className="size-4 animate-spin text-muted-foreground" />
          ) : (
            <Server className="size-4 text-muted-foreground" />
          )}
        </span>
        <h1 className="text-title-sm font-semibold tracking-tight">GPU 服务器</h1>
        {!me.isLoading && me.data && !isAdmin ? (
          <Badge variant="outline">只读视图</Badge>
        ) : null}
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          {isAdmin
            ? "实时查看显卡负载，管理训练节点与思腾合力算力池"
            : "实时查看团队当前可用的训练服务器与显卡空闲状态"}
        </p>
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" variant="outline" disabled={isFetching} onClick={() => void refetch()}>
            <RefreshCw className={`mr-1 size-3.5 ${isFetching ? "animate-spin" : ""}`} />
            {isFetching ? "刷新中…" : "刷新状态"}
          </Button>
          {isAdmin && (
            <>
              <Link href="/gpus/sitonholy">
                <Button size="sm" variant="outline">
                  <Cloud className="mr-1 size-3.5" />
                  申请 / 管理云算力
                </Button>
              </Link>
              <Dialog open={open} onOpenChange={(v) => { setOpen(v); if (!v) setFormError(null); }}>
                <DialogTrigger render={<Button size="sm">＋ 注册服务器</Button>} />
            <DialogContent className="sm:max-w-lg">
              <DialogHeader>
                <DialogTitle>注册服务器</DialogTitle>
                <DialogDescription>
                  SSH:注册时 TOFU 固定主机密钥并验证认证,遥测走远端 nvidia-smi;Runner 可稍后部署。
                  local:需要与已启动 Runner 一致的端口/token/HMAC。
                </DialogDescription>
              </DialogHeader>
              <div className="grid gap-3">
                <div className="grid grid-cols-2 gap-3">
                  <div className="grid gap-1.5">
                    <Label>名称</Label>
                    <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="gpu-a" />
                  </div>
                  <div className="grid gap-1.5">
                    <Label>模式</Label>
                    <select
                      className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                      value={form.transport_mode}
                      onChange={(e) => setForm({ ...form, transport_mode: e.target.value as "ssh" | "local" })}
                    >
                      <option value="ssh">ssh（生产）</option>
                      <option value="local">local（开发）</option>
                    </select>
                  </div>
                </div>
                <div className="grid grid-cols-3 gap-3">
                  <div className="grid gap-1.5">
                    <Label>Host</Label>
                    <Input value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} placeholder="192.168.x.x" />
                  </div>
                  <div className="grid gap-1.5">
                    <Label>端口</Label>
                    <Input value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} />
                  </div>
                  <div className="grid gap-1.5">
                    <Label>用户</Label>
                    <Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="juan" />
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div className="grid gap-1.5">
                    <Label>认证</Label>
                    <select
                      className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                      value={form.auth_kind}
                      onChange={(e) => setForm({ ...form, auth_kind: e.target.value as "key" | "password" })}
                    >
                      <option value="key">密钥(路径或内容)</option>
                      <option value="password">密码</option>
                    </select>
                  </div>
                  <div className="grid gap-1.5">
                    <Label>凭据(密钥内容或密码)</Label>
                    <Input type="password" autoComplete="new-password" value={form.secret} onChange={(e) => setForm({ ...form, secret: e.target.value })} placeholder="密码或私钥内容" />
                  </div>
                </div>
                <div className="grid grid-cols-3 gap-3">
                  <div className="grid gap-1.5">
                    <Label>Runner 端口</Label>
                    <Input value={form.agent_port} onChange={(e) => setForm({ ...form, agent_port: e.target.value })} placeholder="7810" />
                  </div>
                  <div className="grid gap-1.5">
                    <Label>Runner token</Label>
                    <Input type="password" autoComplete="new-password" value={form.agent_token} onChange={(e) => setForm({ ...form, agent_token: e.target.value })} />
                  </div>
                  <div className="grid gap-1.5">
                    <Label>HMAC key{form.transport_mode === "local" ? "(local 必填)" : ""}</Label>
                    <Input type="password" autoComplete="new-password" value={form.manifest_hmac_key} onChange={(e) => setForm({ ...form, manifest_hmac_key: e.target.value })} />
                  </div>
                </div>
              </div>
              {formError && (
                <p className="text-caption text-destructive" data-testid="register-error">
                  {formError}
                </p>
              )}
              <DialogFooter>
                <Button onClick={submit} disabled={submitting}>
                  {submitting ? "注册中…（ssh 模式含 TOFU/认证/GPU 探测，可达数十秒）" : "注册"}
                </Button>
              </DialogFooter>
            </DialogContent>
              </Dialog>
            </>
          )}
        </div>
      </PageHeader>

      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-4 pb-6 pt-2">
          {!me.isLoading && me.data && !isAdmin && (
            <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-muted/30 px-3.5 py-2.5 text-caption text-muted-foreground">
              <Info className="size-4 shrink-0 text-primary" />
              <span>
                当前为普通成员视角：仅管理员可申请或销毁云端算力；您可直接查看可用训练节点，并使用卡片右侧的「终端」「VSCode（含密码提示）」「远程电脑」一键连入计算环境。
              </span>
            </div>
          )}

          {servers && servers.length > 0 && <ClusterOverview servers={servers} />}

          {isPending && <p className="text-sm text-muted-foreground">加载服务器列表…</p>}

          {isError && (
            <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm">
              <p className="font-medium">
                {error instanceof SidecarApiError && error.code === "UNAUTHORIZED"
                  ? "请先登录 AutoLab 账号"
                  : "无法连接 Control API"}
              </p>
              <p className="mt-1 text-muted-foreground">
                {error instanceof Error ? error.message : String(error)}
              </p>
              {!(error instanceof SidecarApiError && error.code === "UNAUTHORIZED") && (
                <p className="mt-1 text-muted-foreground">
                  请确认 autolab-control 在运行（默认 http://127.0.0.1:8710），且 Settings 中已保存有效 token。
                </p>
              )}
              <div className="mt-3 flex flex-wrap gap-2">
                {error instanceof SidecarApiError && error.code === "UNAUTHORIZED" && (
                  <Link href="/settings">
                    <Button size="sm">前往 Settings 登录</Button>
                  </Link>
                )}
                <Button size="sm" variant="outline" onClick={() => void refetch()}>
                  重试
                </Button>
              </div>
            </div>
          )}

          {servers?.length === 0 && (
            <div className="flex flex-col py-6">
              <EmptyState
                icon={Server}
                title="暂无可用训练服务器"
                description={
                  isAdmin
                    ? "可点击右上角「申请 / 管理云算力」快速创建思腾合力卡，或点「注册服务器」接入已有节点。"
                    : "管理员尚未接入可用训练服务器，请联系管理员申请并接入算力节点。"
                }
              />
            </div>
          )}

          {servers?.map((s) => (
            <ServerCard
              key={s.id}
              s={s}
              isAdmin={isAdmin}
              busy={busy}
              isRefreshing={isFetching}
              termOpen={terminalServerId === s.id}
              onToggleTerminal={(srv, acc) => void handleToggleTerminal(srv, acc)}
              onOpenAccess={(srv, mode, acc) => void handleOpenAccess(srv, mode, acc)}
              onRefresh={() => void refetch()}
              act={act}
              setUpgradeTarget={setUpgradeTarget}
              setDeleteTarget={setDeleteTarget}
            />
          ))}

          {isAdmin && (
            <SitonholyPanel
              isAdmin={isAdmin}
              onServersChanged={() => {
                void qc.invalidateQueries({ queryKey: ["autolab", "servers"] });
                void qc.invalidateQueries({ queryKey: ["gpu-servers"] });
              }}
            />
          )}
      </div>

      {/* 面向全体用户的终端 / VSCode / 远程电脑连接与密码提示弹窗 */}
      <Dialog open={!!accessModal} onOpenChange={(v) => !v && setAccessModal(null)}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {accessModal?.mode === "vscode" ? "Web VSCode 入口与登录密码" : "远程电脑与工作区连接入口"}
              {accessModal?.server ? ` · ${accessModal.server.name}` : ""}
            </DialogTitle>
            <DialogDescription>
              所有工作区成员均可从主界面直接打开计算环境并查看对应网页端登录密码（不包含管理员底层运维凭据）。
            </DialogDescription>
          </DialogHeader>

          {accessModal?.loading ? (
            <p className="py-4 text-sm text-muted-foreground">正在获取服务器连接入口与密码信息…</p>
          ) : accessModal?.error ? (
            <p className="py-3 text-sm text-destructive">{accessModal.error}</p>
          ) : accessModal?.data ? (
            <div className="space-y-3 text-sm">
              {/* 1. Web VSCode */}
              {accessModal.data.vscode_url && (
                <div className="space-y-2 rounded-lg border bg-muted/20 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5 font-medium">
                      <Code2 className="size-4 text-primary" />
                      <span>Web VSCode（编辑器 + 终端）</span>
                    </div>
                    <a href={accessModal.data.vscode_url} target="_blank" rel="noreferrer">
                      <Button size="sm">
                        <ExternalLink className="mr-1.5 size-3.5" />
                        打开 VSCode
                      </Button>
                    </a>
                  </div>
                  <div className="truncate font-mono text-xs text-muted-foreground">
                    {accessModal.data.vscode_url}
                  </div>
                  {accessModal.data.vscode_password && (
                    <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-background px-3 py-2">
                      <div className="text-xs">
                        <span className="text-muted-foreground">登录密码：</span>
                        <code className="select-all font-mono text-sm font-semibold text-foreground">
                          {accessModal.data.vscode_password}
                        </code>
                      </div>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          copyTextSafe(accessModal.data!.vscode_password, "VSCode 密码")
                        }
                      >
                        <Copy className="mr-1 size-3.5" />
                        复制密码
                      </Button>
                    </div>
                  )}
                </div>
              )}

              {/* 2. 远程桌面 / 远程电脑 (noVNC) */}
              <div className="space-y-2 rounded-lg border p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-1.5 font-medium">
                    <Monitor className="size-4 text-primary" />
                    <span>远程电脑 / 图形桌面 (noVNC · 可开 Firefox 连校园网)</span>
                  </div>
                  {accessModal.data.novnc_url ? (
                    <a
                      href={accessModal.data.novnc_url}
                      target="_blank"
                      rel="noreferrer"
                      onClick={() => {
                        if (accessModal.data?.vnc_password) {
                          copyTextSafe(accessModal.data.vnc_password, "VNC 密码", true);
                          toast.success(`VNC 密码：${accessModal.data.vnc_password}`, {
                            description: "已自动复制；进入 noVNC 桌面后可开 Firefox 登录校园网。",
                            duration: 15_000,
                          });
                        }
                      }}
                    >
                      <Button size="sm" variant="default">
                        <ExternalLink className="mr-1.5 size-3.5" />
                        打开远程桌面
                      </Button>
                    </a>
                  ) : (
                    <Badge variant="outline">未映射桌面端口</Badge>
                  )}
                </div>

                {accessModal.data.novnc_url && accessModal.data.vnc_password && (
                  <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-muted/20 px-3 py-2">
                    <div className="text-xs">
                      <span className="text-muted-foreground">VNC 密码：</span>
                      <code className="select-all font-mono text-sm font-semibold text-foreground">
                        {accessModal.data.vnc_password}
                      </code>
                    </div>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        copyTextSafe(accessModal.data!.vnc_password, "VNC 密码")
                      }
                    >
                      <Copy className="mr-1 size-3.5" />
                      复制密码
                    </Button>
                  </div>
                )}

                {accessModal.data.novnc_via_vscode && (
                  <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-muted-foreground">
                    提示：首次开远程桌面前，请先点上方「打开 VSCode」填入一次 VSCode 登录密码完成网关会话授权（同浏览器只需一次），再点「打开远程桌面」并填入 VNC 密码即可使用桌面内置 Firefox 登录校园网。
                  </div>
                )}

                <p className="text-xs text-muted-foreground">{accessModal.data.remote_hint}</p>

                {accessModal.data.ssh_hint && (
                  <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-muted/20 px-3 py-2 text-xs">
                    <code className="select-all font-mono">{accessModal.data.ssh_hint}</code>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => copyTextSafe(accessModal.data!.ssh_hint, "SSH 命令")}
                    >
                      <Copy className="mr-1 size-3" />
                      复制 SSH
                    </Button>
                  </div>
                )}
              </div>

              {/* 3. Jupyter / 网页终端 */}
              {(accessModal.data.web_terminal_supported ||
                accessModal.data.jupyter_url ||
                accessModal.data.terminal_url) && (
                <div className="space-y-2 rounded-lg border p-3">
                  <div className="font-medium">网页终端与辅助工具</div>
                  <div className="flex flex-wrap gap-2">
                    {accessModal.data.web_terminal_supported && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => {
                          setTerminalServerId(accessModal.server.id);
                          setAccessModal(null);
                        }}
                      >
                        <Terminal className="mr-1.5 size-3.5" />
                        在本页打开交互终端
                      </Button>
                    )}
                    {accessModal.data.terminal_url && (
                      <a href={accessModal.data.terminal_url} target="_blank" rel="noreferrer">
                        <Button size="sm" variant="outline">
                          <ExternalLink className="mr-1.5 size-3.5" />
                          平台原生终端
                        </Button>
                      </a>
                    )}
                    {accessModal.data.jupyter_url && (
                      <a href={accessModal.data.jupyter_url} target="_blank" rel="noreferrer">
                        <Button size="sm" variant="outline">
                          <ExternalLink className="mr-1.5 size-3.5" />
                          打开 JupyterLab
                        </Button>
                      </a>
                    )}
                  </div>
                  {accessModal.data.jupyter_password && (
                    <div className="text-xs text-muted-foreground">
                      Jupyter 密码/Token：
                      <code className="ml-1 select-all font-mono text-foreground">
                        {accessModal.data.jupyter_password}
                      </code>
                    </div>
                  )}
                </div>
              )}

              {accessModal.data.work_roots.length > 0 && (
                <div className="rounded-md bg-muted/25 px-3 py-2 text-xs text-muted-foreground">
                  可用持久化 / 工作目录：
                  <span className="ml-1 font-mono text-foreground">
                    {accessModal.data.work_roots.join(" · ")}
                  </span>
                </div>
              )}
            </div>
          ) : null}

          <DialogFooter>
            <Button variant="outline" onClick={() => setAccessModal(null)}>
              关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {isAdmin && (
        <>
          <AlertDialog open={!!upgradeTarget} onOpenChange={(v) => !v && setUpgradeTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>升级 {upgradeTarget?.name} 上的 gpu-agent？</AlertDialogTitle>
            <AlertDialogDescription>
              升级会重启服务器上的 agent；有在途任务时将被拒绝（force=false）。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (upgradeTarget) void act(upgradeTarget, "runner/upgrade", { force: false });
                setUpgradeTarget(null);
              }}
            >
              升级
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={!!deleteTarget} onOpenChange={(v) => !v && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除 {deleteTarget?.name} 的注册？</AlertDialogTitle>
            <AlertDialogDescription>
              将移除服务器条目、GPU 清单和 SSH 凭据。有训练或环境历史的服务器会被拒绝删除（审计链约束）。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-white hover:bg-destructive/90"
              disabled={busy === `${deleteTarget?.id}:delete`}
              onClick={() => void confirmDelete()}
            >
              删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
        </>
      )}
    </>
  );
}
