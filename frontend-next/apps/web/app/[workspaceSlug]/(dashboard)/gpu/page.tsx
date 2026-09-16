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
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, RefreshCw, Server } from "lucide-react";
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
  DialogTrigger,
} from "@multica/ui/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@multica/ui/components/ui/alert-dialog";
import { Input } from "@multica/ui/components/ui/input";
import { Label } from "@multica/ui/components/ui/label";
import { Progress } from "@multica/ui/components/ui/progress";
import { PageHeader } from "@multica/views/layout/page-header";
import {
  agentVersion,
  deleteServer,
  describeOpResult,
  getServerTelemetry,
  listServerDetails,
  registerServer,
  serverOp,
  type GpuTelemetry,
  type ServerDetail,
  type ServerOp,
} from "@autolab/research/sidecar/servers";
import { SidecarApiError } from "@autolab/research/sidecar/client";

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
  agent_port: "7800",
  agent_token: "",
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
  if (pct >= 85) return "text-amber-600";
  return "text-foreground";
}

function ServerCard({
  s,
  busy,
  act,
  setUpgradeTarget,
  setDeleteTarget,
}: {
  s: ServerDetail;
  busy: string | null;
  act: (s: ServerDetail, op: ServerOp, body?: unknown) => Promise<void>;
  setUpgradeTarget: (s: ServerDetail) => void;
  setDeleteTarget: (s: ServerDetail) => void;
}) {
  const qc = useQueryClient();
  const { data: tel, isPending, isFetching, isError, error, refetch } = useQuery({
    queryKey: ["autolab", "telemetry", s.id],
    queryFn: () => getServerTelemetry(s.id),
    staleTime: 30_000,
    retry: false,
  });

  const telFor = (idx: number): GpuTelemetry | undefined =>
    tel?.gpus.find((g) => g.gpu_index === idx);

  return (
    <div className="space-y-3 rounded-lg border p-4">
      <div className="flex flex-wrap items-center gap-3">
        <Badge variant="outline" className="gap-1.5">
          <span className={`size-1.5 rounded-full ${STATUS_DOT[s.status] ?? "bg-muted"}`} />
          {s.name}
        </Badge>
        <span className="font-mono text-xs text-muted-foreground">{transportLabel(s)}</span>
        <span className="ml-auto flex flex-wrap gap-2">
          <Button size="sm" variant="outline" disabled={isFetching}
                  onClick={() => void refetch().then(() => qc.invalidateQueries({ queryKey: ["autolab", "telemetry", s.id] }))}>
            <Activity className="size-3.5" />
            {isFetching ? "遥测中…" : "刷新遥测"}
          </Button>
          <Button size="sm" variant="outline" disabled={busy === `${s.id}:connectivity-test`}
                  onClick={() => void act(s, "connectivity-test")}>连通性测试</Button>
          <Button size="sm" variant="outline" disabled={!s.agent_port || busy === `${s.id}:agent/probe`}
                  onClick={() => void act(s, "agent/probe")}>agent 探活</Button>
          <Button size="sm" variant="outline" disabled={busy === `${s.id}:agent/install`}
                  onClick={() => void act(s, "agent/install")}>部署 / 修复 Agent</Button>
          <Button size="sm" variant="ghost" disabled={busy === `${s.id}:agent/upgrade`}
                  onClick={() => setUpgradeTarget(s)}>升级</Button>
          <Button size="sm" variant="ghost" className="text-destructive hover:text-destructive"
                  disabled={busy === `${s.id}:delete`}
                  onClick={() => setDeleteTarget(s)}>删除</Button>
        </span>
      </div>

      {s.gpus.length > 0 && (
        <div className="overflow-hidden rounded-md border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-caption text-muted-foreground">
              <tr>
                <th className="px-3 py-1.5 text-left font-normal">#</th>
                <th className="px-3 py-1.5 text-left font-normal">型号</th>
                <th className="px-3 py-1.5 text-left font-normal">显存占用</th>
                <th className="px-3 py-1.5 text-left font-normal">利用率</th>
                <th className="px-3 py-1.5 text-left font-normal">状态</th>
              </tr>
            </thead>
            <tbody>
              {s.gpus.map((g) => {
                const t = telFor(g.gpu_index);
                const usedPct = t && t.memory_total_mb > 0
                  ? Math.round((t.memory_used_mb / t.memory_total_mb) * 100) : null;
                return (
                  <tr key={g.id} className="border-t">
                    <td className="px-3 py-1.5 font-mono text-xs">{g.gpu_index}</td>
                    <td className="px-3 py-1.5">{g.model_name}</td>
                    <td className="px-3 py-1.5">
                      {t ? (
                        <span className="flex items-center gap-2">
                          <span className={`font-mono text-xs tabular-nums ${memColor(usedPct ?? 0)}`}>
                            {fmtMem(t.memory_used_mb)} / {fmtMem(t.memory_total_mb)}
                          </span>
                          <Progress className="h-1 w-20" value={usedPct ?? 0} />
                        </span>
                      ) : (
                        <span className="font-mono text-xs text-muted-foreground">
                          — / {fmtMem(g.vram_mb)}
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-1.5 font-mono text-xs tabular-nums">
                      {t ? `${t.utilization_pct}%${t.temperature_c ? ` · ${t.temperature_c}°C` : ""}` : "—"}
                    </td>
                    <td className="px-3 py-1.5">
                      <Badge variant={g.status === "IDLE" ? "secondary" : "default"}>{g.status}</Badge>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* 磁盘占用（df -BM 真实块设备） */}
      {tel?.disks && tel.disks.length > 0 && (
        <div className="space-y-1">
          {tel.disks.map((d) => (
            <div key={d.device} className="flex items-center gap-2 font-mono text-caption text-muted-foreground">
              <span className="w-24 truncate" title={d.device}>{d.mount}</span>
              <span className="tabular-nums">{fmtMem(d.used_mb)} / {fmtMem(d.total_mb)}</span>
              <span className={`w-9 text-right tabular-nums ${d.use_pct >= 95 ? "text-destructive" : d.use_pct >= 85 ? "text-amber-600" : ""}`}>
                {d.use_pct}%
              </span>
              <Progress className="h-1 w-28" value={d.use_pct} />
            </div>
          ))}
        </div>
      )}

      {isPending && (
        <p className="flex items-center gap-1.5 text-caption text-muted-foreground">
          <RefreshCw className="size-3 animate-spin" /> 遥测获取中…
        </p>
      )}
      {isError && (
        <p className="text-caption text-amber-600">
          遥测不可用（{error instanceof SidecarApiError ? error.message : String(error)}）——
          SSH 不通时登记数据仍可用，可点「连通性测试」排查。
        </p>
      )}
    </div>
  );
}

export default function GpuPage() {
  const qc = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [upgradeTarget, setUpgradeTarget] = useState<ServerDetail | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ServerDetail | null>(null);

  const { data: servers, isPending, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["autolab", "servers"],
    queryFn: listServerDetails,
    refetchInterval: 5000,
  });

  const act = async (s: ServerDetail, op: ServerOp, body?: unknown) => {
    setBusy(`${s.id}:${op}`);
    const OP_LABEL: Record<ServerOp, string> = {
      "connectivity-test": "连通性测试",
      "agent/probe": "agent 探活",
      "agent/install": "部署 Agent",
      "agent/upgrade": "升级 Agent",
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
      const msg =
        e instanceof SidecarApiError ? `${e.code}: ${e.message}` : String(e);
      toast.error(`${s.name} · ${OP_LABEL[op]}失败`, { description: msg.slice(0, 300) });
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
        });
      } else {
        const msg = e instanceof SidecarApiError ? `${e.code}: ${e.message}` : String(e);
        toast.error(`删除 ${s.name} 失败`, { description: msg.slice(0, 300) });
      }
    } finally {
      setBusy(null);
      setDeleteTarget(null);
    }
  };

  const submit = async () => {
    try {
      await registerServer({
        name: form.name,
        host: form.host,
        port: Number(form.port) || 22,
        username: form.username,
        auth_kind: form.auth_kind,
        secret: form.secret || null,
        transport_mode: form.transport_mode,
        agent_port: form.agent_port ? Number(form.agent_port) : null,
        agent_token: form.agent_token || null,
      });
      toast.success(`已注册 ${form.name}`);
      setOpen(false);
      setForm(EMPTY_FORM);
      await qc.invalidateQueries({ queryKey: ["autolab", "servers"] });
    } catch (e) {
      const msg =
        e instanceof SidecarApiError ? `${e.code}: ${e.message}` : String(e);
      toast.error("注册失败", { description: msg.slice(0, 300) });
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
        <h1 className="text-body font-medium">GPU 服务器</h1>
        {servers && servers.length > 0 ? (
          <span className="font-mono text-caption tabular-nums text-muted-foreground">
            {servers.length}
          </span>
        ) : null}
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          注册 GPU 服务器 · 连通性测试 · 实时遥测 · gpu-agent 部署与升级
        </p>
        <div className="ml-auto">
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger render={<Button size="sm">＋ 注册服务器</Button>} />
            <DialogContent className="sm:max-w-lg">
              <DialogHeader>
                <DialogTitle>注册服务器</DialogTitle>
                <DialogDescription>
                  生产：SSH + 服务器上先启动 gpu-agent（--token）。开发：local 模式直连本机 agent。
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
                      <option value="key">密钥（私钥路径）</option>
                      <option value="password">密码</option>
                    </select>
                  </div>
                  <div className="grid gap-1.5">
                    <Label>凭据（密钥路径 / 密码）</Label>
                    <Input value={form.secret} onChange={(e) => setForm({ ...form, secret: e.target.value })} placeholder="~/.ssh/id_ed25519" />
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div className="grid gap-1.5">
                    <Label>agent 端口</Label>
                    <Input value={form.agent_port} onChange={(e) => setForm({ ...form, agent_port: e.target.value })} />
                  </div>
                  <div className="grid gap-1.5">
                    <Label>agent token</Label>
                    <Input value={form.agent_token} onChange={(e) => setForm({ ...form, agent_token: e.target.value })} />
                  </div>
                </div>
              </div>
              <DialogFooter>
                <Button onClick={submit}>注册</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </PageHeader>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4 md:px-6">
          {isPending && <p className="text-sm text-muted-foreground">加载服务器列表…</p>}

          {isError && (
            <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm">
              <p className="font-medium">无法连接 AutoLab sidecar</p>
              <p className="mt-1 text-muted-foreground">
                {error instanceof Error ? error.message : String(error)}
              </p>
              <p className="mt-1 text-muted-foreground">
                请确认 sidecar 在运行（默认 http://127.0.0.1:39876，可用 scripts/dev_local.sh 启动）。
              </p>
              <Button size="sm" variant="outline" className="mt-2" onClick={() => void refetch()}>
                重试
              </Button>
            </div>
          )}

          {servers?.length === 0 && (
            <p className="text-sm text-muted-foreground">
              尚无服务器。注册一台并完成连通性测试后即可立项。
            </p>
          )}

          {servers?.map((s) => (
            <ServerCard
              key={s.id}
              s={s}
              busy={busy}
              act={act}
              setUpgradeTarget={setUpgradeTarget}
              setDeleteTarget={setDeleteTarget}
            />
          ))}
      </div>

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
                if (upgradeTarget) void act(upgradeTarget, "agent/upgrade", { force: false });
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
              onClick={() => void confirmDelete()}
            >
              删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
