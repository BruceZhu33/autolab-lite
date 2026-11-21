"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Cloud,
  Cpu,
  HardDrive,
  LogIn,
  LogOut,
  Plus,
  RefreshCw,
  Server,
  ShieldAlert,
  ShieldCheck,
  Terminal,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import { whoami } from "~/lib/api/system";
import { Badge } from "~/vendor/ui/components/ui/badge";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import { Label } from "~/vendor/ui/components/ui/label";
import { Progress } from "~/vendor/ui/components/ui/progress";
import { PageHeader } from "~/vendor/views/layout/page-header";
import {
  sitonholyCreateInstance,
  sitonholyImages,
  sitonholyInstances,
  sitonholyLogout,
  sitonholyPools,
  sitonholyPoolSpecs,
  sitonholyStatus,
  sitonholyVolumes,
  type SitonholyBridgeMode,
  type SitonholyImage,
  type SitonholySpec,
  type SitonholyVolume,
} from "~/vendor/views/gpu-sitonholy";
import { SitonholyAutoAcquireCard } from "~/vendor/views/sitonholy-auto-acquire-card";
import { SitonholyInstancesTab } from "~/vendor/views/sitonholy-instances-tab";
import { SitonholyLoginDialog } from "~/vendor/views/sitonholy-panel";
import { SitonholyPoolsTab } from "~/vendor/views/sitonholy-pools-tab";

type ActiveTab = "create" | "instances" | "pools";

interface ExtraPortRow {
  container_port: string;
  host_port: string;
  auto_port: boolean;
  env_name: string;
}

interface ExtraEnvRow {
  key: string;
  value: string;
}

interface CustomVolumeRow {
  storagePath: string;
  mountPath: string;
  perm: string;
  selected: boolean;
  label: string;
}

function normalizeVolumePerm(raw: unknown): "rw" | "ro" {
  const s = String(raw ?? "").trim().toLowerCase();
  if (s === "1" || s === "ro") return "ro";
  return "rw";
}

function imageValue(img: SitonholyImage): string {
  const full = String(img.fullPath ?? "").trim();
  if (full) return full;
  const direct = String(img.imageName ?? "").trim();
  if (direct) return direct;
  const repo = String(
    img.repository ?? img.repositoriesName ?? img.depositoryName ?? img.artifactName ?? img.name ?? "",
  ).trim();
  const tag = String(img.tag ?? img.tagName ?? "").trim();
  if (repo && tag) return `${repo}:${tag}`;
  return repo;
}

function specLabel(spec: SitonholySpec, idx: number): string {
  const name = String(spec.specificationName || spec.name || `规格 #${idx + 1}`);
  const bits: string[] = [name];
  if (spec.gpuAmount != null) bits.push(`${spec.gpuAmount} GPU`);
  if (spec.cpuCoreNumber != null) bits.push(`${spec.cpuCoreNumber} CPU`);
  if (spec.memory != null) bits.push(`${spec.memory}GB 内存`);
  if (spec.hardDisk != null) bits.push(`${spec.hardDisk}GB 磁盘`);
  return bits.join(" · ");
}

export default function SitonholyConsolePage() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<ActiveTab>("instances");
  const [loginOpen, setLoginOpen] = useState(false);
  const [autoOpenTaskId, setAutoOpenTaskId] = useState<string | null>(null);

  const me = useQuery({
    queryKey: ["whoami"],
    queryFn: whoami,
    retry: false,
    staleTime: 60_000,
  });
  const isAdmin = me.data?.kind === "owner";

  const {
    data: status,
    isLoading: loadingStatus,
    refetch: refetchStatus,
  } = useQuery({
    queryKey: ["sitonholy-status"],
    queryFn: () => sitonholyStatus(false),
    enabled: isAdmin,
    staleTime: 30_000,
    retry: false,
  });

  const loggedIn = isAdmin && Boolean(status?.logged_in);

  const {
    data: poolsData,
    isFetching: fetchingPools,
    refetch: refetchPools,
  } = useQuery({
    queryKey: ["sitonholy-pools-all"],
    queryFn: () => sitonholyPools(false),
    enabled: isAdmin && loggedIn,
    staleTime: 30_000,
    retry: false,
  });

  const { data: imagesData, isFetching: fetchingImages } = useQuery({
    queryKey: ["sitonholy-images"],
    queryFn: () => sitonholyImages("", 100),
    enabled: isAdmin && loggedIn,
    staleTime: 60_000,
    retry: false,
  });

  const { data: volumesData } = useQuery({
    queryKey: ["sitonholy-volumes"],
    queryFn: sitonholyVolumes,
    enabled: isAdmin && loggedIn,
    staleTime: 60_000,
    retry: false,
  });

  const [instNameFilter, setInstNameFilter] = useState("");
  const [instPoolFilter, setInstPoolFilter] = useState<string | null>(null);
  const [instStatusFilter, setInstStatusFilter] = useState<number | null>(null);

  const filterPoolName = instPoolFilter
    ? (poolsData?.pools ?? []).find((p) => p.pool_id === instPoolFilter)?.pool_name
    : undefined;

  const {
    data: instancesData,
    isFetching: fetchingInstances,
    refetch: refetchInstances,
  } = useQuery({
    queryKey: ["sitonholy-instances-page", instNameFilter, instPoolFilter, instStatusFilter],
    queryFn: () =>
      sitonholyInstances({
        page_size: 50,
        name: instNameFilter,
        pool_id: instPoolFilter,
        pool_name: filterPoolName,
        status: instStatusFilter,
      }),
    enabled: isAdmin && loggedIn,
    staleTime: 20_000,
    retry: false,
  });

  // 申请算力表单状态（默认展示可用公用池、开启自动接入与空闲保活）
  const [showClusterNodes, setShowClusterNodes] = useState(false);
  const [poolId, setPoolId] = useState("");
  const [specId, setSpecId] = useState("");
  const [gpus, setGpus] = useState("1");
  const [imageSearch, setImageSearch] = useState("");
  const [imageName, setImageName] = useState("");
  const [runCmd, setRunCmd] = useState("sleep infinity");

  const [useDefaultVolumes, setUseDefaultVolumes] = useState(true);
  const [customVolumeEdits, setCustomVolumeEdits] = useState<Record<string, Partial<CustomVolumeRow>>>({});

  const [showAdvanced, setShowAdvanced] = useState(false);
  const [vscodePort, setVscodePort] = useState("16871");
  const [vscodeAutoPort, setVscodeAutoPort] = useState(true);
  const [vscodeHostPort, setVscodeHostPort] = useState("");
  const [vscodePassword, setVscodePassword] = useState("");
  const [extraPorts, setExtraPorts] = useState<ExtraPortRow[]>([]);
  const [extraEnv, setExtraEnv] = useState<ExtraEnvRow[]>([]);

  const [taskName, setTaskName] = useState("");
  const [waitRunning, setWaitRunning] = useState(true);
  const [autoRegister, setAutoRegister] = useState(true);
  const [enableKeepalive, setEnableKeepalive] = useState(true);
  const [serverName, setServerName] = useState("");
  const [mode, setMode] = useState<SitonholyBridgeMode>("auto");

  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createReceipt, setCreateReceipt] = useState<{
    taskId: string;
    serverName?: string;
    transport?: string;
    gpuProbe?: string;
  } | null>(null);

  const allPools = poolsData?.pools ?? [];
  const myVisiblePools = allPools.filter((p) => p.user_visible !== false);
  const myA6000 = myVisiblePools.filter((p) => p.is_a6000);
  const defaultPool =
    myA6000.find((p) => (p.available || 0) > 0) ??
    myVisiblePools.find((p) => (p.available || 0) > 0) ??
    myA6000[0] ??
    myVisiblePools[0] ??
    null;
  const effectivePoolId = poolId || defaultPool?.pool_id || "";

  const availMyGpus = myVisiblePools.reduce((sum, p) => sum + (p.available || 0), 0);
  const totalMyGpus = myVisiblePools.reduce((sum, p) => sum + (p.total || 0), 0);

  const selectablePools = showClusterNodes ? allPools : myVisiblePools;
  const selectedPool = allPools.find((p) => p.pool_id === effectivePoolId) ?? null;

  const specsQuery = useQuery({
    queryKey: ["sitonholy-specs", effectivePoolId],
    queryFn: () => sitonholyPoolSpecs(effectivePoolId),
    enabled: isAdmin && loggedIn && Boolean(effectivePoolId),
    staleTime: 60_000,
    retry: false,
  });

  const allImageOptions = Array.from(
    new Set((imagesData?.images ?? []).map(imageValue).filter(Boolean)),
  );
  const filteredImages = imageSearch.trim()
    ? allImageOptions.filter((img) =>
        img.toLowerCase().includes(imageSearch.trim().toLowerCase()),
      )
    : allImageOptions;

  const volumeRows: CustomVolumeRow[] = (volumesData?.volumes ?? [])
    .map((vol: SitonholyVolume, idx: number) => {
      const sp = String(vol.fullPath ?? "").trim();
      if (!sp) return null;
      const ov = customVolumeEdits[sp] ?? {};
      const defaultMount =
        sp.startsWith("/sitonholy/nfs/") || !sp.startsWith("/root/")
          ? `/root/data${idx + 1}`
          : sp;
      return {
        storagePath: sp,
        mountPath: ov.mountPath ?? defaultMount,
        perm: ov.perm ?? normalizeVolumePerm(vol.typeFlag),
        selected: ov.selected ?? true,
        label: String(vol.volumeName || vol.name || sp),
      };
    })
    .filter((v): v is CustomVolumeRow => v !== null);

  const instances = instancesData?.instances ?? [];
  const runningCount = instances.filter((i) => String(i.status ?? "") === "1").length;
  const attachedCount = instances.filter((i) => Boolean(i.registered_server)).length;

  const invalidateServers = () => {
    void qc.invalidateQueries({ queryKey: ["autolab", "servers"] });
    void qc.invalidateQueries({ queryKey: ["gpu-servers"] });
  };

  const refreshAll = async () => {
    await refetchStatus();
    if (loggedIn) {
      await Promise.all([refetchPools(), refetchInstances()]);
    }
    invalidateServers();
  };

  const handleLogout = async () => {
    try {
      await sitonholyLogout();
      toast.info("已退出思腾合力会话");
      await qc.invalidateQueries({ queryKey: ["sitonholy-status"] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const handleSelectPool = (nextPoolId: string) => {
    setPoolId(nextPoolId);
    setSpecId("");
  };

  const submitCreate = async () => {
    setCreateError(null);
    if (!imageName.trim()) {
      setCreateError("请先从镜像列表选择或输入容器镜像名");
      return;
    }
    const gpuCount = Math.max(1, Math.min(8, Number(gpus) || 1));
    const parsedVscodePort = Math.max(1, Math.min(65535, Number(vscodePort) || 16871));
    const parsedHostPort =
      !vscodeAutoPort && vscodeHostPort.trim()
        ? Number(vscodeHostPort.trim())
        : null;
    if (
      parsedHostPort !== null &&
      (!Number.isInteger(parsedHostPort) || parsedHostPort < 10000 || parsedHostPort > 20000)
    ) {
      setCreateError("思腾合力宿主机映射端口必须在 10000 ~ 20000 之间");
      return;
    }

    const parsedExtraPorts = extraPorts
      .filter((r) => r.container_port.trim())
      .map((r) => ({
        container_port: Number(r.container_port),
        host_port: !r.auto_port && r.host_port.trim() ? Number(r.host_port) : null,
        auto_port: r.auto_port,
        env_name: r.env_name.trim() || "OTHER",
      }));

    const parsedExtraEnv: Record<string, string> = {};
    for (const row of extraEnv) {
      if (row.key.trim()) {
        parsedExtraEnv[row.key.trim()] = row.value;
      }
    }

    const mountVolumes = useDefaultVolumes
      ? null
      : volumeRows
          .filter((v) => v.selected && v.mountPath.trim())
          .map((v) => ({
            storagePath: v.storagePath,
            mountPath: v.mountPath.trim(),
            perm: v.perm,
          }));

    setCreateBusy(true);
    try {
      const res = await sitonholyCreateInstance({
        task_name: taskName.trim() || undefined,
        image_name: imageName.trim(),
        pool_id: effectivePoolId || null,
        spec_id: specId || null,
        gpus: gpuCount,
        run_cmd: runCmd.trim() || "sleep infinity",
        vscode_port: parsedVscodePort,
        vscode_password: vscodePassword.trim() || null,
        vscode_host_port: parsedHostPort,
        auto_port: vscodeAutoPort,
        extra_ports: parsedExtraPorts.length > 0 ? parsedExtraPorts : null,
        mount_volumes: mountVolumes,
        extra_env: Object.keys(parsedExtraEnv).length > 0 ? parsedExtraEnv : null,
        wait_running: waitRunning || autoRegister || enableKeepalive,
        auto_register: autoRegister,
        enable_keepalive: enableKeepalive,
        server_name: serverName.trim() || null,
        mode,
      });

      const tid = String(res.created?.task_id ?? "");
      const srv = res.registered?.server;
      setCreateReceipt({
        taskId: tid,
        serverName: srv?.name,
        transport: res.registered?.transport,
        gpuProbe: res.registered?.gpu_probe,
      });
      setTaskName("");
      setServerName("");
      toast.success(
        srv?.name
          ? `A6000 已创建并接入 AutoLab：${srv.name}`
          : "思腾合力实例已提交创建",
      );
      await Promise.all([refetchInstances(), refetchPools()]);
      invalidateServers();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setCreateError(msg);
      toast.error("创建或接入失败", { description: msg });
    } finally {
      setCreateBusy(false);
    }
  };

  return (
    <>
      <PageHeader>
        <Link href="/gpus">
          <Button size="sm" variant="ghost" className="mr-1 px-2">
            <ArrowLeft className="mr-1 size-3.5" />
            GPU 服务器
          </Button>
        </Link>
        <Cloud className="size-4 text-primary" />
        <h1 className="text-title-sm font-semibold tracking-tight">思腾合力云算力管理</h1>
        {isAdmin && (
          <>
            {loadingStatus ? (
              <Badge variant="outline">检查登录…</Badge>
            ) : loggedIn ? (
              <Badge variant="secondary">
                {status?.nick_name || status?.username} @ 10.12.54.221
              </Badge>
            ) : (
              <Badge variant="outline">未登录</Badge>
            )}
            <div className="ml-auto flex items-center gap-2">
              {loggedIn ? (
                <>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={fetchingPools || fetchingInstances}
                    onClick={() => void refreshAll()}
                  >
                    <RefreshCw
                      className={`mr-1 size-3.5 ${fetchingPools || fetchingInstances ? "animate-spin" : ""}`}
                    />
                    刷新
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => void handleLogout()}>
                    <LogOut className="mr-1 size-3.5" />
                    退出
                  </Button>
                </>
              ) : (
                <Button size="sm" onClick={() => setLoginOpen(true)}>
                  <LogIn className="mr-1 size-3.5" />
                  登录思腾合力
                </Button>
              )}
            </div>
          </>
        )}
      </PageHeader>

      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-4 pb-8 pt-2">
        {me.isLoading ? (
          <div className="mx-auto mt-10 w-full max-w-md rounded-xl border bg-card p-6 text-center text-sm text-muted-foreground">
            正在验证管理员权限…
          </div>
        ) : me.isError ? (
          <div className="mx-auto mt-10 w-full max-w-md space-y-3 rounded-xl border border-destructive/40 bg-card p-6 text-sm">
            <div className="font-semibold">无法验证当前账号权限</div>
            <p className="text-muted-foreground">
              {me.error instanceof Error ? me.error.message : String(me.error)}
            </p>
            <div className="flex gap-2">
              <Link href="/settings">
                <Button size="sm">前往 Settings 登录</Button>
              </Link>
              <Link href="/gpus">
                <Button size="sm" variant="outline">返回 GPU 服务器</Button>
              </Link>
            </div>
          </div>
        ) : !isAdmin ? (
          <div className="mx-auto mt-10 w-full max-w-lg space-y-4 rounded-xl border bg-card p-6">
            <div className="flex items-center gap-2.5 text-base font-semibold">
              <ShieldAlert className="size-5 text-amber-500" />
              <span>仅管理员可申请与管理思腾合力算力</span>
            </div>
            <p className="text-sm text-muted-foreground">
              当前账号为普通成员视角。您可以在「GPU 服务器」页面实时查看已接入的训练服务器与显卡空闲状态；如需新申请云端算力卡，请联系管理员操作。
            </p>
            <div>
              <Link href="/gpus">
                <Button>
                  <ArrowLeft className="mr-1.5 size-4" />
                  返回 GPU 服务器总览
                </Button>
              </Link>
            </div>
          </div>
        ) : !loggedIn ? (
          <div className="mx-auto mt-8 w-full max-w-lg space-y-4 rounded-xl border bg-card p-6">
            <div className="flex items-center gap-2 text-base font-semibold">
              <Cloud className="size-5 text-primary" />
              <span>登录校内思腾合力平台（10.12.54.221）</span>
            </div>
            <p className="text-sm text-muted-foreground">
              管理员登录后可直接在此申请 GPU 实例、打开内置 Web 终端，并将实例自动接入「GPU 服务器」供团队开展训练。凭据仅保存在控制节点本地文件中。
            </p>
            <div className="flex flex-wrap gap-2 pt-1">
              <Button onClick={() => setLoginOpen(true)}>
                <LogIn className="mr-1.5 size-4" />
                立即登录思腾合力
              </Button>
              <Link href="/gpus">
                <Button variant="outline">返回 GPU 服务器</Button>
              </Link>
            </div>
          </div>
        ) : (
          <>
            {/* 顶部导航与精简配额状态 */}
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-card px-4 py-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  variant={tab === "instances" ? "default" : "outline"}
                  onClick={() => setTab("instances")}
                >
                  <Terminal className="mr-1.5 size-3.5" />
                  实例与终端 ({instances.length})
                </Button>
                <Button
                  size="sm"
                  variant={tab === "create" ? "default" : "outline"}
                  onClick={() => setTab("create")}
                >
                  <Plus className="mr-1.5 size-3.5" />
                  申请新算力
                </Button>
                <Button
                  size="sm"
                  variant={tab === "pools" ? "default" : "ghost"}
                  onClick={() => setTab("pools")}
                >
                  <Cpu className="mr-1.5 size-3.5" />
                  算力池详情
                </Button>
              </div>

              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-muted-foreground">
                <span>
                  公用池可用：
                  <strong className="ml-1 font-mono text-foreground">
                    {myVisiblePools.length > 0 ? `${availMyGpus}/${totalMyGpus} 卡` : "暂无显式可用池"}
                  </strong>
                </span>
                <span>
                  运行中：<strong className="font-mono text-foreground">{runningCount}</strong>
                </span>
                <span>
                  已接入 AutoLab：<strong className="font-mono text-foreground">{attachedCount}</strong>
                </span>
                <Link href="/gpus" className="inline-flex items-center text-primary hover:underline">
                  <Server className="mr-1 size-3" />
                  GPU 服务器总览
                </Link>
              </div>
            </div>

            <SitonholyAutoAcquireCard
              loggedIn={loggedIn}
              imageOptions={allImageOptions}
              onMutated={refreshAll}
            />

            {tab === "create" && (
              <div className="grid gap-4 lg:grid-cols-12">
                {/* 左侧：1-2-3 极简向导 + 可折叠高级配置 */}
                <div className="space-y-4 rounded-xl border bg-card p-5 lg:col-span-7">
                  {/* Step 1: 算力池与卡数 */}
                  <div className="space-y-3">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2 font-medium">
                        <span className="flex size-5 items-center justify-center rounded-full bg-primary text-xs font-semibold text-primary-foreground">
                          1
                        </span>
                        <span>选择算力池与 GPU 卡数</span>
                      </div>
                      <span className="text-caption text-muted-foreground">
                        仅展示当前账号有权限的算力池
                      </span>
                    </div>

                    {selectablePools.length === 0 ? (
                      <div className="rounded-md border border-dashed p-3 text-caption text-muted-foreground">
                        当前账号可见池为空（提交时将自动匹配平台可用 A6000 资源池）。
                      </div>
                    ) : (
                      <div className="grid gap-2 sm:grid-cols-2">
                        {selectablePools.map((p) => {
                          const selected = effectivePoolId === p.pool_id;
                          const pct =
                            p.total > 0 ? Math.min(100, Math.round((p.available / p.total) * 100)) : 0;
                          return (
                            <button
                              key={p.pool_id}
                              type="button"
                              onClick={() => handleSelectPool(p.pool_id)}
                              className={`rounded-lg border p-3 text-left transition ${
                                selected
                                  ? "border-primary bg-primary/5 ring-1 ring-primary"
                                  : "hover:bg-muted/40"
                              }`}
                            >
                              <div className="flex items-center justify-between gap-1.5">
                                <span className="truncate text-sm font-medium">
                                  {p.pool_name || p.pool_id}
                                </span>
                                {p.queue_num > 0 && (
                                  <Badge variant="outline" className="text-[11px]">
                                    排队 {p.queue_num}
                                  </Badge>
                                )}
                              </div>
                              <div className="mt-1.5 flex items-center justify-between text-caption">
                                <span>
                                  空闲 <strong className="font-mono text-foreground">{p.available}</strong> / {p.total} 卡
                                </span>
                                <span className="font-mono text-muted-foreground">{pct}%</span>
                              </div>
                              <Progress className="mt-1.5 h-1.5" value={pct} />
                            </button>
                          );
                        })}
                      </div>
                    )}

                    <div className="flex flex-wrap items-center gap-2 pt-1">
                      <Label className="mr-1 text-caption text-muted-foreground">申请卡数：</Label>
                      {[1, 2, 4, 8].map((n) => (
                        <Button
                          key={n}
                          type="button"
                          size="sm"
                          variant={String(gpus) === String(n) ? "default" : "outline"}
                          className="h-8 px-3 font-mono"
                          onClick={() => setGpus(String(n))}
                        >
                          {n} 卡
                        </Button>
                      ))}
                      <Input
                        type="number"
                        min={1}
                        max={8}
                        className="h-8 w-20 font-mono"
                        value={gpus}
                        onChange={(e) => setGpus(e.target.value)}
                      />
                    </div>
                    {selectedPool && Number(gpus) > selectedPool.available && (
                      <p className="text-caption text-amber-600">
                        注意：所选池当前空闲 {selectedPool.available} 卡，申请 {gpus} 卡可能需要排队。
                      </p>
                    )}
                  </div>

                  <div className="border-t pt-4 space-y-3">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2 font-medium">
                        <span className="flex size-5 items-center justify-center rounded-full bg-primary text-xs font-semibold text-primary-foreground">
                          2
                        </span>
                        <span>选择运行镜像</span>
                      </div>
                      <span className="text-caption text-muted-foreground">
                        {fetchingImages ? "加载镜像列表…" : `共 ${allImageOptions.length} 个可用镜像`}
                      </span>
                    </div>

                    <div className="grid gap-2">
                      <Input
                        placeholder="搜索镜像（如 pytorch / cuda / medclip）…"
                        value={imageSearch}
                        onChange={(e) => setImageSearch(e.target.value)}
                      />
                      {filteredImages.length > 0 ? (
                        <select
                          className="h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                          value={filteredImages.includes(imageName) ? imageName : ""}
                          onChange={(e) => setImageName(e.target.value)}
                        >
                          <option value="">-- 请选择镜像 ({filteredImages.length}) --</option>
                          {filteredImages.map((img) => (
                            <option key={img} value={img}>
                              {img}
                            </option>
                          ))}
                        </select>
                      ) : (
                        !fetchingImages && (
                          <p className="text-caption text-muted-foreground">
                            当前筛选无匹配镜像，可在下方手动输入完整镜像名。
                          </p>
                        )
                      )}
                      <Input
                        className="font-mono text-xs"
                        value={imageName}
                        onChange={(e) => setImageName(e.target.value)}
                        placeholder="或手动粘贴镜像地址（直接选现有 PyTorch/CUDA 镜像即可，无需自制）"
                      />
                    </div>
                  </div>

                  <div className="border-t pt-4 space-y-3">
                    <div className="flex items-center gap-2 font-medium">
                      <span className="flex size-5 items-center justify-center rounded-full bg-primary text-xs font-semibold text-primary-foreground">
                        3
                      </span>
                      <span>实例名称与自动化策略</span>
                    </div>

                    <div className="grid gap-1.5">
                      <Label className="text-caption text-muted-foreground">实例名称（可选，留空自动命名）</Label>
                      <Input
                        value={taskName}
                        onChange={(e) => setTaskName(e.target.value)}
                        placeholder="例如 autolab-a6000-01"
                      />
                    </div>

                    <div className="space-y-2 rounded-lg border bg-muted/20 p-3 text-sm">
                      <label className="flex cursor-pointer items-start gap-2.5">
                        <input
                          type="checkbox"
                          className="mt-0.5"
                          checked={autoRegister}
                          onChange={(e) => {
                            const next = e.target.checked;
                            setAutoRegister(next);
                            if (next) setWaitRunning(true);
                          }}
                        />
                        <span>
                          <span className="font-medium">启动后自动接入 AutoLab「GPU 服务器」</span>
                          <span className="block text-caption text-muted-foreground">
                            自动注入离线 Runner 并接入调度，团队即可在项目中使用这批显卡
                          </span>
                        </span>
                      </label>
                      <label className="flex cursor-pointer items-start gap-2.5">
                        <input
                          type="checkbox"
                          className="mt-0.5"
                          checked={enableKeepalive}
                          onChange={(e) => {
                            const next = e.target.checked;
                            setEnableKeepalive(next);
                            if (next) setWaitRunning(true);
                          }}
                        />
                        <span>
                          <span className="font-medium">默认开启 GPU 空闲保活（防回收）</span>
                          <span className="block text-caption text-muted-foreground">
                            防止显卡空闲超时被平台收回；检测到真实训练任务时自动让路
                          </span>
                        </span>
                      </label>
                    </div>
                  </div>

                  {/* 单一可折叠高级配置面板（保持全部平台能力不丢失） */}
                  <div className="border-t pt-3">
                    <button
                      type="button"
                      className="flex w-full items-center justify-between text-left text-caption font-medium text-muted-foreground hover:text-foreground"
                      onClick={() => setShowAdvanced((v) => !v)}
                    >
                      <span>高级配置（硬件规格 / 存储挂载 / 启动命令 / 端口 / 网桥模式）</span>
                      {showAdvanced ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
                    </button>

                    {showAdvanced && (
                      <div className="mt-4 space-y-4 rounded-lg border bg-muted/10 p-4">
                        {/* 规格与节点视图 */}
                        <div className="grid gap-3 sm:grid-cols-2">
                          <div className="grid gap-1.5">
                            <Label>硬件资源规格</Label>
                            <select
                              className="h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm disabled:opacity-60"
                              disabled={!effectivePoolId || specsQuery.isFetching}
                              value={specId}
                              onChange={(e) => setSpecId(e.target.value)}
                            >
                              <option value="">
                                {!effectivePoolId
                                  ? "自动采用池内默认规格"
                                  : specsQuery.isFetching
                                    ? "拉取规格中…"
                                    : "自动选择第一条可用规格"}
                              </option>
                              {(specsQuery.data?.specs ?? []).map((s, idx) => {
                                const sid = String(s.specificationId || s.id || "");
                                return (
                                  <option key={sid || idx} value={sid}>
                                    {specLabel(s, idx)}
                                  </option>
                                );
                              })}
                            </select>
                          </div>
                          <div className="flex items-end pb-1.5">
                            <label className="flex cursor-pointer items-center gap-1.5 text-caption text-muted-foreground">
                              <input
                                type="checkbox"
                                checked={showClusterNodes}
                                onChange={(e) => setShowClusterNodes(e.target.checked)}
                              />
                              <span>在算力池中显示底层单节点（仅排障）</span>
                            </label>
                          </div>
                        </div>

                        {/* 启动命令 */}
                        <div className="grid gap-1.5">
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <Label>容器启动命令 (run_cmd)</Label>
                            <div className="flex gap-1.5">
                              <Button
                                type="button"
                                size="sm"
                                variant="outline"
                                onClick={() => setRunCmd("sleep infinity")}
                              >
                                sleep infinity
                              </Button>
                              <Button
                                type="button"
                                size="sm"
                                variant="outline"
                                onClick={() => setRunCmd("if [ -f /root/scm.sh ]; then bash /root/scm.sh; fi; sleep infinity")}
                              >
                                带 /root/scm.sh 启动
                              </Button>
                            </div>
                          </div>
                          <Input
                            className="font-mono text-xs"
                            value={runCmd}
                            onChange={(e) => setRunCmd(e.target.value)}
                          />
                        </div>

                        {/* 存储卷 */}
                        <div className="space-y-2">
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <div className="flex items-center gap-1.5 text-sm font-medium">
                              <HardDrive className="size-3.5 text-primary" />
                              <span>持久化存储卷（{volumeRows.length} 个个人卷）</span>
                            </div>
                            <label className="flex cursor-pointer items-center gap-1.5 text-caption">
                              <input
                                type="checkbox"
                                checked={useDefaultVolumes}
                                onChange={(e) => setUseDefaultVolumes(e.target.checked)}
                              />
                              <span>自动挂载全部个人卷</span>
                            </label>
                          </div>

                          {volumeRows.length === 0 ? (
                            <p className="text-caption text-muted-foreground">
                              当前账号下未查到存储卷记录（平台创建时仍会沿用默认目录规则）。
                            </p>
                          ) : useDefaultVolumes ? (
                            <div className="flex flex-wrap gap-1.5">
                              {volumeRows.map((v) => (
                                <Badge key={v.storagePath} variant="outline" className="font-mono">
                                  {v.storagePath} → {v.mountPath} ({v.perm === "ro" ? "只读" : "读写"})
                                </Badge>
                              ))}
                            </div>
                          ) : (
                            <div className="space-y-2">
                              {volumeRows.map((v) => (
                                <div
                                  key={v.storagePath}
                                  className="flex flex-wrap items-center gap-2 rounded border bg-background p-2 text-xs"
                                >
                                  <input
                                    type="checkbox"
                                    checked={v.selected}
                                    onChange={(e) =>
                                      setCustomVolumeEdits((prev) => ({
                                        ...prev,
                                        [v.storagePath]: { ...prev[v.storagePath], selected: e.target.checked },
                                      }))
                                    }
                                  />
                                  <span className="min-w-0 flex-1 truncate font-mono">{v.storagePath}</span>
                                  <span>→</span>
                                  <Input
                                    className="h-7 w-44 font-mono text-xs"
                                    value={v.mountPath}
                                    onChange={(e) =>
                                      setCustomVolumeEdits((prev) => ({
                                        ...prev,
                                        [v.storagePath]: { ...prev[v.storagePath], mountPath: e.target.value },
                                      }))
                                    }
                                  />
                                  <select
                                    className="h-7 rounded border border-input bg-transparent px-1.5"
                                    value={v.perm}
                                    onChange={(e) =>
                                      setCustomVolumeEdits((prev) => ({
                                        ...prev,
                                        [v.storagePath]: {
                                          ...prev[v.storagePath],
                                          perm: normalizeVolumePerm(e.target.value),
                                        },
                                      }))
                                    }
                                  >
                                    <option value="rw">读写</option>
                                    <option value="ro">只读</option>
                                  </select>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>

                        {/* 等待与接入参数 */}
                        <div className="grid gap-3 sm:grid-cols-2">
                          <div className="grid gap-1">
                            <Label>AutoLab 服务器别名（可选）</Label>
                            <Input
                              value={serverName}
                              onChange={(e) => setServerName(e.target.value)}
                              placeholder="留空默认使用实例名"
                            />
                          </div>
                          <div className="grid gap-1">
                            <Label>网桥通道模式 (`mode`)</Label>
                            <select
                              className="h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                              value={mode}
                              onChange={(e) => setMode(e.target.value as SitonholyBridgeMode)}
                            >
                              <option value="auto">auto（自动探测/网关穿透）</option>
                              <option value="gateway">gateway（code-server 网关）</option>
                              <option value="direct">direct（宿主机直连）</option>
                            </select>
                          </div>
                        </div>

                        <label className="flex cursor-pointer items-center gap-2 text-caption">
                          <input
                            type="checkbox"
                            checked={waitRunning}
                            onChange={(e) => {
                              const next = e.target.checked;
                              setWaitRunning(next);
                              if (!next) {
                                setAutoRegister(false);
                                setEnableKeepalive(false);
                              }
                            }}
                          />
                          <span>等待容器就绪后再返回 (`wait_running`)</span>
                        </label>

                        {/* VSCode 端口与环境变量 */}
                        <div className="grid gap-3 sm:grid-cols-3">
                          <div className="grid gap-1">
                            <Label>VSCode 容器端口</Label>
                            <Input value={vscodePort} onChange={(e) => setVscodePort(e.target.value)} />
                          </div>
                          <div className="grid gap-1">
                            <Label>VSCode 宿主机端口</Label>
                            <div className="flex items-center gap-2">
                              <label className="flex shrink-0 cursor-pointer items-center gap-1 text-caption">
                                <input
                                  type="checkbox"
                                  checked={vscodeAutoPort}
                                  onChange={(e) => setVscodeAutoPort(e.target.checked)}
                                />
                                <span>自动</span>
                              </label>
                              {!vscodeAutoPort && (
                                <Input
                                  placeholder="10000~20000"
                                  value={vscodeHostPort}
                                  onChange={(e) => setVscodeHostPort(e.target.value)}
                                />
                              )}
                            </div>
                          </div>
                          <div className="grid gap-1">
                            <Label>VSCode 密码（留空随机）</Label>
                            <Input
                              type="password"
                              autoComplete="new-password"
                              placeholder="留空自动生成"
                              value={vscodePassword}
                              onChange={(e) => setVscodePassword(e.target.value)}
                            />
                          </div>
                        </div>

                        <div className="space-y-2">
                          <div className="flex items-center justify-between">
                            <Label>额外端口映射 (extra_ports)</Label>
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              onClick={() =>
                                setExtraPorts((prev) => [
                                  ...prev,
                                  { container_port: "", host_port: "", auto_port: true, env_name: "OTHER" },
                                ])
                              }
                            >
                              + 添加端口
                            </Button>
                          </div>
                          {extraPorts.map((row, idx) => (
                            <div key={idx} className="grid grid-cols-12 items-center gap-2">
                              <Input
                                className="col-span-3 h-8 text-xs"
                                placeholder="容器端口"
                                value={row.container_port}
                                onChange={(e) =>
                                  setExtraPorts((prev) =>
                                    prev.map((r, i) =>
                                      i === idx ? { ...r, container_port: e.target.value } : r,
                                    ),
                                  )
                                }
                              />
                              <Input
                                className="col-span-3 h-8 text-xs"
                                placeholder="宿主(10000~20000)"
                                disabled={row.auto_port}
                                value={row.host_port}
                                onChange={(e) =>
                                  setExtraPorts((prev) =>
                                    prev.map((r, i) =>
                                      i === idx ? { ...r, host_port: e.target.value } : r,
                                    ),
                                  )
                                }
                              />
                              <label className="col-span-2 flex items-center gap-1 text-caption">
                                <input
                                  type="checkbox"
                                  checked={row.auto_port}
                                  onChange={(e) =>
                                    setExtraPorts((prev) =>
                                      prev.map((r, i) =>
                                        i === idx ? { ...r, auto_port: e.target.checked } : r,
                                      ),
                                    )
                                  }
                                />
                                自动
                              </label>
                              <Input
                                className="col-span-3 h-8 text-xs"
                                placeholder="环境变量名"
                                value={row.env_name}
                                onChange={(e) =>
                                  setExtraPorts((prev) =>
                                    prev.map((r, i) =>
                                      i === idx ? { ...r, env_name: e.target.value } : r,
                                    ),
                                  )
                                }
                              />
                              <Button
                                type="button"
                                size="sm"
                                variant="ghost"
                                className="col-span-1 px-1"
                                onClick={() => setExtraPorts((prev) => prev.filter((_, i) => i !== idx))}
                              >
                                <Trash2 className="size-3.5" />
                              </Button>
                            </div>
                          ))}
                        </div>

                        <div className="space-y-2">
                          <div className="flex items-center justify-between">
                            <Label>自定义环境变量 (extra_env)</Label>
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              onClick={() => setExtraEnv((prev) => [...prev, { key: "", value: "" }])}
                            >
                              + 添加变量
                            </Button>
                          </div>
                          {extraEnv.map((row, idx) => (
                            <div key={idx} className="grid grid-cols-12 items-center gap-2">
                              <Input
                                className="col-span-5 h-8 font-mono text-xs"
                                placeholder="变量名 KEY"
                                value={row.key}
                                onChange={(e) =>
                                  setExtraEnv((prev) =>
                                    prev.map((r, i) => (i === idx ? { ...r, key: e.target.value } : r)),
                                  )
                                }
                              />
                              <Input
                                className="col-span-6 h-8 font-mono text-xs"
                                placeholder="变量值 VALUE"
                                value={row.value}
                                onChange={(e) =>
                                  setExtraEnv((prev) =>
                                    prev.map((r, i) => (i === idx ? { ...r, value: e.target.value } : r)),
                                  )
                                }
                              />
                              <Button
                                type="button"
                                size="sm"
                                variant="ghost"
                                className="col-span-1 px-1"
                                onClick={() => setExtraEnv((prev) => prev.filter((_, i) => i !== idx))}
                              >
                                <Trash2 className="size-3.5" />
                              </Button>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </div>

                {/* 右侧：清晰摘要与一键申请 */}
                <div className="space-y-4 lg:col-span-5">
                  <div className="space-y-4 rounded-xl border bg-card p-5">
                    <div className="flex items-center gap-1.5 font-medium">
                      <ShieldCheck className="size-4 text-primary" />
                      <span>配置确认</span>
                    </div>

                    <div className="space-y-2 rounded-lg border bg-muted/20 p-3.5 text-caption">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-muted-foreground">算力池</span>
                        <span className="font-medium">
                          {selectedPool ? `${selectedPool.pool_name} (余 ${selectedPool.available} 卡)` : "自动匹配可用池"}
                        </span>
                      </div>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-muted-foreground">申请数量</span>
                        <span className="font-mono font-medium">{gpus || 1} 张 GPU</span>
                      </div>
                      <div className="flex items-center justify-between gap-2">
                        <span className="shrink-0 text-muted-foreground">运行镜像</span>
                        <span className="truncate font-mono">{imageName || "（请先选择镜像）"}</span>
                      </div>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-muted-foreground">自动接入 AutoLab</span>
                        <span>{autoRegister ? "开启" : "仅创建容器"}</span>
                      </div>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-muted-foreground">空闲防回收保活</span>
                        <span>{enableKeepalive ? "开启" : "关闭"}</span>
                      </div>
                      <div className="border-t pt-2 text-muted-foreground">
                        已自动挂载个人持久化卷；环境包修改后可在实例「更多 → 保存容器为镜像」推回仓库。
                      </div>
                    </div>

                    {createError && (
                      <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-caption text-destructive">
                        {createError}
                      </div>
                    )}

                    <Button
                      className="w-full"
                      size="lg"
                      disabled={createBusy}
                      onClick={() => void submitCreate()}
                    >
                      {createBusy
                        ? "创建并接入中（通常需要 20 ~ 90 秒）…"
                        : autoRegister
                          ? "申请实例并自动接入 AutoLab"
                          : "提交创建实例"}
                    </Button>
                  </div>

                  {createReceipt && (
                    <div className="space-y-3 rounded-xl border border-emerald-500/40 bg-emerald-500/5 p-4 text-sm">
                      <div className="flex items-center gap-2 font-semibold text-emerald-700 dark:text-emerald-400">
                        <CheckCircle2 className="size-4" />
                        <span>实例创建完成</span>
                      </div>
                      <div className="space-y-1 text-caption">
                        <div className="font-mono">Task ID：{createReceipt.taskId || "—"}</div>
                        {createReceipt.serverName && (
                          <div>
                            已接入 AutoLab 服务器：<strong>{createReceipt.serverName}</strong>（{createReceipt.gpuProbe}）
                          </div>
                        )}
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          size="sm"
                          onClick={() => {
                            if (createReceipt.taskId) setAutoOpenTaskId(createReceipt.taskId);
                            setTab("instances");
                          }}
                        >
                          <Terminal className="mr-1 size-3.5" />
                          打开 Web 终端
                        </Button>
                        <Link href="/gpus">
                          <Button size="sm" variant="outline">
                            查看 GPU 服务器
                          </Button>
                        </Link>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )}

            {tab === "instances" && (
              <SitonholyInstancesTab
                instances={instances}
                usablePools={myVisiblePools}
                fetchingInstances={fetchingInstances}
                nameFilter={instNameFilter}
                poolFilter={instPoolFilter}
                statusFilter={instStatusFilter}
                autoOpenTaskId={autoOpenTaskId}
                onClearAutoOpenTaskId={() => setAutoOpenTaskId(null)}
                onApplySearch={(next) => {
                  setInstNameFilter(next.name);
                  setInstPoolFilter(next.poolId);
                  setInstStatusFilter(next.status);
                }}
                onRefresh={() => refetchInstances().then(() => undefined)}
                onSwitchToCreate={() => setTab("create")}
                onServersChanged={invalidateServers}
              />
            )}

            {tab === "pools" && (
              <SitonholyPoolsTab
                pools={allPools}
                fetchingPools={fetchingPools}
                status={status}
                onRefreshPools={() => void refetchPools()}
                onUsePoolToCreate={(pid) => {
                  handleSelectPool(pid);
                  setShowClusterNodes(allPools.some((p) => p.pool_id === pid && p.user_visible === false));
                  setTab("create");
                }}
              />
            )}
          </>
        )}
      </div>

      <SitonholyLoginDialog
        open={loginOpen}
        onOpenChange={setLoginOpen}
        onSuccess={() => {
          void refreshAll();
        }}
      />
    </>
  );
}
