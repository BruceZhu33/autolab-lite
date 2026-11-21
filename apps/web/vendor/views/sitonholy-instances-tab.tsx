"use client";

import { useEffect, useState } from "react";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Code2,
  Copy,
  ExternalLink,
  FileJson,
  HardDriveDownload,
  Link2,
  MoreVertical,
  Pause,
  Plus,
  RefreshCw,
  ScrollText,
  Search,
  Square,
  Terminal,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

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
import { Badge } from "~/vendor/ui/components/ui/badge";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "~/vendor/ui/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "~/vendor/ui/components/ui/dropdown-menu";
import { Input } from "~/vendor/ui/components/ui/input";
import { Label } from "~/vendor/ui/components/ui/label";
import {
  sitonholyDeleteInstance,
  sitonholyImageProjects,
  sitonholyInstanceDetail,
  sitonholyInstanceExec,
  sitonholyInstanceLogs,
  sitonholyKeepaliveStatus,
  sitonholyPauseInstance,
  sitonholyRegisterInstance,
  sitonholySaveImage,
  sitonholySetKeepalive,
  sitonholyStopInstance,
  sitonholyVscodeCredentials,
  type SitonholyBridgeMode,
  type SitonholyInstance,
  type SitonholyInstanceDetail,
  type SitonholyKeepaliveStatus,
  type SitonholyPool,
} from "~/vendor/views/gpu-sitonholy";
import { SitonholyTerminalPane } from "~/vendor/views/sitonholy-terminal";

const STATUS_META: Record<
  string,
  { label: string; variant: "default" | "secondary" | "destructive" | "outline" }
> = {
  "0": { label: "创建中", variant: "secondary" },
  "1": { label: "运行中", variant: "default" },
  "2": { label: "已完成", variant: "outline" },
  "3": { label: "失败", variant: "destructive" },
  "4": { label: "已暂停", variant: "secondary" },
  "5": { label: "已停止", variant: "outline" },
  "6": { label: "排队中", variant: "secondary" },
};

const QUICK_SCRIPTS: Array<{ label: string; script: string }> = [
  { label: "nvidia-smi", script: "nvidia-smi" },
  {
    label: "Conda / Python",
    script: "conda env list 2>/dev/null || true\nwhich python python3; python3 --version",
  },
  { label: "磁盘 / 数据卷", script: "df -h\nls -la /root/data1 2>/dev/null || true" },
  {
    label: "AutoLab Runner 状态",
    script:
      "ps -ef | grep -E 'autolab_runner|bridge.py|sshd' | grep -v grep || true\ncurl -s http://127.0.0.1:7810/healthz 2>/dev/null || true",
  },
];

function formatLogsText(raw: unknown): string {
  if (raw == null) return "（暂无日志返回）";
  if (typeof raw === "string") return raw || "（日志为空）";
  if (typeof raw === "object") {
    const rec = raw as Record<string, unknown>;
    if (typeof rec.data === "string") return rec.data || "（日志为空）";
    if (Array.isArray(rec.data)) return rec.data.map(String).join("\n");
    return JSON.stringify(raw, null, 2);
  }
  return String(raw);
}

export function SitonholyInstancesTab({
  instances,
  usablePools,
  fetchingInstances,
  nameFilter,
  poolFilter,
  statusFilter,
  autoOpenTaskId,
  onClearAutoOpenTaskId,
  onApplySearch,
  onRefresh,
  onSwitchToCreate,
  onServersChanged,
}: {
  instances: SitonholyInstance[];
  usablePools: SitonholyPool[];
  fetchingInstances: boolean;
  nameFilter: string;
  poolFilter: string | null;
  statusFilter: number | null;
  autoOpenTaskId?: string | null;
  onClearAutoOpenTaskId?: () => void;
  onApplySearch: (next: { name: string; poolId: string | null; status: number | null }) => void;
  onRefresh: () => void | Promise<void>;
  onSwitchToCreate: () => void;
  onServersChanged: () => void;
}) {
  const [draftName, setDraftName] = useState(nameFilter);
  const [draftPoolId, setDraftPoolId] = useState<string>(poolFilter ?? "");
  const [draftStatus, setDraftStatus] = useState<string>(
    statusFilter === null ? "" : String(statusFilter),
  );

  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [terminalTarget, setTerminalTarget] = useState<{
    taskId: string;
    taskName: string;
  } | null>(null);

  const [detailTaskId, setDetailTaskId] = useState<string | null>(null);
  const [revealedPwd, setRevealedPwd] = useState<Record<string, string>>({});
  const [attachTarget, setAttachTarget] = useState<SitonholyInstance | null>(null);
  const [attachName, setAttachName] = useState("");
  const [attachMode, setAttachMode] = useState<SitonholyBridgeMode>("auto");
  const [attachKeepalive, setAttachKeepalive] = useState(true);

  const queryClient = useQueryClient();
  const [saveTarget, setSaveTarget] = useState<SitonholyInstance | null>(null);
  const [saveProject, setSaveProject] = useState("");
  const [saveImageName, setSaveImageName] = useState("");
  const [saveTag, setSaveTag] = useState("");
  const [savePromoteDefault, setSavePromoteDefault] = useState(true);

  const [keepaliveTarget, setKeepaliveTarget] = useState<SitonholyInstance | null>(null);
  const [keepaliveInterval, setKeepaliveInterval] = useState("15");
  const [keepaliveMap, setKeepaliveMap] = useState<Record<string, SitonholyKeepaliveStatus>>({});

  const [logsTaskId, setLogsTaskId] = useState<string | null>(null);

  const [execTarget, setExecTarget] = useState<SitonholyInstance | null>(null);
  const [execScript, setExecScript] = useState("nvidia-smi");
  const [execTimeout, setExecTimeout] = useState("60");
  const [execResult, setExecResult] = useState<{ exit_code: number; output: string } | null>(null);

  const [pauseTarget, setPauseTarget] = useState<SitonholyInstance | null>(null);
  const [stopTarget, setStopTarget] = useState<SitonholyInstance | null>(null);
  const [stopUnregister, setStopUnregister] = useState(true);
  const [deleteTarget, setDeleteTarget] = useState<SitonholyInstance | null>(null);

  // 预取前 5 个运行中容器详情，确保 VSCode 直达链接与原生终端链接无需二次等待
  const runningIds = instances
    .filter((i) => String(i.status ?? "") === "1")
    .map((i) => String(i.id ?? ""))
    .filter(Boolean)
    .slice(0, 5);

  const runningDetailQueries = useQueries({
    queries: runningIds.map((tid) => ({
      queryKey: ["sitonholy-instance-detail", tid],
      queryFn: () => sitonholyInstanceDetail(tid),
      staleTime: 60_000,
      retry: false,
    })),
  });

  const detailMap = new Map<string, SitonholyInstanceDetail>();
  runningIds.forEach((tid, idx) => {
    const data = runningDetailQueries[idx]?.data;
    if (data) detailMap.set(tid, data);
  });

  useEffect(() => {
    if (!autoOpenTaskId) return;
    const match = instances.find((i) => String(i.id ?? "") === autoOpenTaskId);
    if (match && String(match.status ?? "") === "1") {
      setTerminalTarget({
        taskId: autoOpenTaskId,
        taskName: String(match.taskName || autoOpenTaskId.slice(0, 12)),
      });
      onClearAutoOpenTaskId?.();
    }
  }, [autoOpenTaskId, instances, onClearAutoOpenTaskId]);

  const detailQuery = useQuery({
    queryKey: ["sitonholy-instance-detail", detailTaskId],
    queryFn: () => sitonholyInstanceDetail(detailTaskId!),
    enabled: Boolean(detailTaskId),
    retry: false,
  });

  const logsQuery = useQuery({
    queryKey: ["sitonholy-instance-logs", logsTaskId],
    queryFn: () => sitonholyInstanceLogs(logsTaskId!),
    enabled: Boolean(logsTaskId),
    retry: false,
  });

  const imageProjectsQuery = useQuery({
    queryKey: ["sitonholy-image-projects"],
    queryFn: sitonholyImageProjects,
    enabled: Boolean(saveTarget),
    retry: false,
  });

  const keepaliveTaskId = keepaliveTarget ? String(keepaliveTarget.id ?? "") : null;
  const keepaliveQuery = useQuery({
    queryKey: ["sitonholy-instance-keepalive", keepaliveTaskId],
    queryFn: async () => {
      const status = await sitonholyKeepaliveStatus(keepaliveTaskId!);
      setKeepaliveMap((prev) => ({ ...prev, [keepaliveTaskId!]: status }));
      return status;
    },
    enabled: Boolean(keepaliveTaskId),
    retry: false,
  });

  useEffect(() => {
    const list = imageProjectsQuery.data?.projects ?? [];
    if (!saveProject && list.length > 0 && list[0]?.projectName) {
      setSaveProject(list[0].projectName);
    }
  }, [imageProjectsQuery.data, saveProject]);

  const triggerSearch = () => {
    onApplySearch({
      name: draftName.trim(),
      poolId: draftPoolId || null,
      status: draftStatus === "" ? null : Number(draftStatus),
    });
  };

  const resetFilters = () => {
    setDraftName("");
    setDraftPoolId("");
    setDraftStatus("");
    onApplySearch({ name: "", poolId: null, status: null });
  };

  const openAttach = (inst: SitonholyInstance) => {
    const fallback = inst.taskName
      ? `sitonholy-${inst.taskName}`
      : `sitonholy-${String(inst.id ?? "").slice(0, 8)}`;
    setAttachTarget(inst);
    setAttachName(fallback);
    setAttachMode("auto");
    setAttachKeepalive(false);
  };

  const submitAttach = async () => {
    const id = String(attachTarget?.id ?? "");
    if (!id) return;
    setBusyKey(`attach:${id}`);
    try {
      const res = await sitonholyRegisterInstance(id, {
        server_name: attachName.trim() || null,
        mode: attachMode,
        enable_keepalive: attachKeepalive,
      });
      setAttachTarget(null);
      toast.success(`已接入 AutoLab 服务器：${res.server.name}`, {
        description: `通道 ${res.transport} · GPU ${res.server.gpu_slots} 卡 · Runner:${res.server.runner_port}`,
      });
      await onRefresh();
      onServersChanged();
    } catch (e) {
      toast.error("接入失败", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusyKey(null);
    }
  };

  const openSaveImageDialog = (inst: SitonholyInstance) => {
    const tid = String(inst.id ?? "");
    const detail = detailMap.get(tid);
    const rawRepo = String(detail?.image_name || "").split("/").pop() || "";
    const baseName = rawRepo.split(":")[0] || String(inst.taskName || "autolab");
    const sanitized = baseName
      .toLowerCase()
      .replace(/[^a-z0-9.]+/g, ".")
      .replace(/^\.+|\.+$/g, "") || "autolab.env";
    const projects = imageProjectsQuery.data?.projects ?? [];
    setSaveTarget(inst);
    if (!saveProject && projects[0]?.projectName) {
      setSaveProject(projects[0].projectName);
    }
    setSaveImageName(sanitized);
    const now = new Date();
    const stamp = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(
      now.getDate(),
    ).padStart(2, "0")}-${String(now.getHours()).padStart(2, "0")}${String(now.getMinutes()).padStart(2, "0")}`;
    setSaveTag(`snap-${stamp}`);
    setSavePromoteDefault(true);
  };

  const submitSaveImage = async () => {
    const id = String(saveTarget?.id ?? "");
    if (!id) return;
    const proj = saveProject.trim();
    const img = saveImageName.trim();
    const tg = saveTag.trim();
    if (!proj || /[\s/:]/.test(proj)) {
      toast.error("请填写有效的 Harbor 项目空间");
      return;
    }
    if (!/^[a-z0-9]+(?:[.][a-z0-9]+)*$/.test(img)) {
      toast.error("镜像名称格式不符：仅支持小写字母、数字和 `.`");
      return;
    }
    if (!/^[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*$/.test(tg)) {
      toast.error("标签格式不符：仅支持字母、数字和 `.` `_` `-`");
      return;
    }
    setBusyKey(`save:${id}`);
    try {
      const res = await sitonholySaveImage(id, {
        project_name: proj,
        image_name: img,
        tag: tg,
        promote_to_auto_acquire: savePromoteDefault,
      });
      setSaveTarget(null);
      void queryClient.invalidateQueries({ queryKey: ["sitonholy-auto-acquire"] });
      toast.success("打包成功，正在推送到镜像仓库", {
        description: res.promote_to_auto_acquire
          ? `目标镜像 ${res.repository}（约 10 分钟完成推送），就绪后将自动设为默认抢卡/续期基准镜像。`
          : `目标镜像 ${res.repository}，预计约 10 分钟完成推送，之后可在「创建 A6000 实例」列表选中。`,
      });
    } catch (e) {
      toast.error("保存镜像失败", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusyKey(null);
    }
  };

  const toggleKeepalive = async (enable: boolean) => {
    const id = String(keepaliveTarget?.id ?? "");
    if (!id) return;
    setBusyKey(`keepalive:${id}`);
    try {
      const interval = Math.max(5, Math.min(120, Number(keepaliveInterval) || 15));
      const status = await sitonholySetKeepalive(id, { enabled: enable, interval_sec: interval });
      setKeepaliveMap((prev) => ({ ...prev, [id]: status }));
      toast.success(enable ? "已开启容器 GPU/CPU 空闲保活" : "已停止容器空闲保活");
      await keepaliveQuery.refetch();
    } catch (e) {
      toast.error(enable ? "开启保活失败" : "停止保活失败", {
        description: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusyKey(null);
    }
  };

  const runExec = async () => {
    const id = String(execTarget?.id ?? "");
    if (!id || !execScript.trim()) return;
    setBusyKey(`exec:${id}`);
    try {
      const res = await sitonholyInstanceExec(id, execScript, Math.max(5, Number(execTimeout) || 60));
      setExecResult({ exit_code: res.exit_code, output: res.output });
    } catch (e) {
      toast.error("远程执行失败", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusyKey(null);
    }
  };

  const confirmPause = async () => {
    const id = String(pauseTarget?.id ?? "");
    setPauseTarget(null);
    if (!id) return;
    setBusyKey(`pause:${id}`);
    try {
      await sitonholyPauseInstance(id);
      if (terminalTarget?.taskId === id) setTerminalTarget(null);
      toast.success("已发送暂停请求（如已建网桥或终端已同步关闭）");
      await onRefresh();
    } catch (e) {
      toast.error("暂停失败", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusyKey(null);
    }
  };

  const confirmStop = async () => {
    const id = String(stopTarget?.id ?? "");
    const unreg = stopUnregister;
    setStopTarget(null);
    if (!id) return;
    setBusyKey(`stop:${id}`);
    try {
      const res = await sitonholyStopInstance(id, unreg);
      if (terminalTarget?.taskId === id) setTerminalTarget(null);
      toast.success("已停止实例并释放显卡", {
        description: res.unregistered
          ? "已清理网关与 AutoLab 服务器注册"
          : res.server_retained_for_history
            ? "存在关联项目或训练记录，已保留服务器并标为离线"
            : undefined,
      });
      await onRefresh();
      onServersChanged();
    } catch (e) {
      toast.error("停止失败", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusyKey(null);
    }
  };

  const confirmDelete = async () => {
    const id = String(deleteTarget?.id ?? "");
    setDeleteTarget(null);
    if (!id) return;
    setBusyKey(`del:${id}`);
    try {
      await sitonholyDeleteInstance(id, true);
      if (terminalTarget?.taskId === id) setTerminalTarget(null);
      toast.success("已删除思腾合力作业记录");
      await onRefresh();
      onServersChanged();
    } catch (e) {
      toast.error("删除失败", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusyKey(null);
    }
  };

  const copyText = (text: string, label: string, silent = false): boolean => {
    if (!text) return false;
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
        void navigator.clipboard.writeText(text).catch(() => {});
        if (!silent) toast.success(`已复制${label}`);
        return true;
      }
    } catch {
      // ignore
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
  };

  const revealVscodePassword = async (tid: string, notify = true) => {
    try {
      const cached = revealedPwd[tid];
      const pwd =
        cached ?? (await sitonholyVscodeCredentials(tid)).vscode_password;
      if (!pwd) {
        if (notify) toast.info("该实例未配置 VSCODE_PASSWD");
        return "";
      }
      setRevealedPwd((prev) => ({ ...prev, [tid]: pwd }));
      copyText(pwd, "VSCode 密码", true);
      if (notify) {
        toast.success(`VSCode 密码：${pwd}`, {
          description: "已尽量自动复制到剪贴板，可直接粘贴到新页面。",
          duration: 15_000,
        });
      }
      return pwd;
    } catch (e) {
      if (notify) {
        toast.error("获取 VSCode 密码失败", {
          description: e instanceof Error ? e.message : String(e),
        });
      }
      return "";
    }
  };

  const openVscodeOrDetail = (tid: string) => {
    void revealVscodePassword(tid, true);
    const prefetched = detailMap.get(tid);
    if (prefetched?.vscode_url) {
      window.open(prefetched.vscode_url, "_blank", "noopener,noreferrer");
      return;
    }
    setDetailTaskId(tid);
  };

  const activeTerminalDetail = terminalTarget ? detailMap.get(terminalTarget.taskId) : undefined;

  return (
    <div className="space-y-4">
      {/* 1) 思腾合力「作业管理」筛选栏 */}
      <div className="flex flex-wrap items-end justify-between gap-3 rounded-lg border bg-card p-3.5">
        <div className="flex flex-wrap items-end gap-3">
          <div className="grid gap-1">
            <Label className="text-caption text-muted-foreground">作业名称</Label>
            <Input
              className="h-8 w-44"
              placeholder="作业名称"
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") triggerSearch();
              }}
            />
          </div>

          <div className="grid gap-1">
            <Label className="text-caption text-muted-foreground">资源池</Label>
            <select
              className="h-8 w-44 rounded-md border border-input bg-transparent px-2 text-sm"
              value={draftPoolId}
              onChange={(e) => setDraftPoolId(e.target.value)}
            >
              <option value="">全部资源池</option>
              {usablePools.map((p) => (
                <option key={p.pool_id} value={p.pool_id}>
                  {p.pool_name || p.pool_id} ({p.available}/{p.total})
                </option>
              ))}
            </select>
          </div>

          <div className="grid gap-1">
            <Label className="text-caption text-muted-foreground">运行状态</Label>
            <select
              className="h-8 w-36 rounded-md border border-input bg-transparent px-2 text-sm"
              value={draftStatus}
              onChange={(e) => setDraftStatus(e.target.value)}
            >
              <option value="">全部状态</option>
              <option value="1">运行中</option>
              <option value="0">创建中</option>
              <option value="6">排队中</option>
              <option value="4">已暂停</option>
              <option value="5">已停止</option>
              <option value="2">已完成</option>
              <option value="3">失败</option>
            </select>
          </div>

          <div className="flex items-center gap-1.5">
            <Button size="sm" onClick={triggerSearch}>
              <Search className="mr-1 size-3.5" />
              搜索
            </Button>
            {(nameFilter || poolFilter || statusFilter !== null) && (
              <Button size="sm" variant="ghost" onClick={resetFilters}>
                重置
              </Button>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Button size="sm" onClick={onSwitchToCreate}>
            <Plus className="mr-1 size-3.5" />
            新增
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={fetchingInstances}
            onClick={() => void onRefresh()}
          >
            <RefreshCw className={`mr-1 size-3.5 ${fetchingInstances ? "animate-spin" : ""}`} />
            刷新
          </Button>
        </div>
      </div>

      {/* 2) 页面内交互式 Web 终端 */}
      {terminalTarget && (
        <SitonholyTerminalPane
          taskId={terminalTarget.taskId}
          taskName={terminalTarget.taskName}
          nativeTerminalUrl={activeTerminalDetail?.terminal_url}
          onClose={() => setTerminalTarget(null)}
        />
      )}

      {/* 3) 思腾合力「作业管理」表格 */}
      {instances.length === 0 ? (
        <div className="rounded-lg border border-dashed p-10 text-center text-sm text-muted-foreground">
          暂无匹配的思腾合力作业记录；你可以点击右上角 <strong>+ 新增</strong> 提交新的 A6000 作业。
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border bg-card">
          <table className="w-full min-w-[920px] text-left text-sm">
            <thead className="border-b bg-muted/40 text-caption font-medium text-muted-foreground">
              <tr>
                <th className="px-3.5 py-2.5">作业名称</th>
                <th className="px-3 py-2.5">用户</th>
                <th className="px-3 py-2.5">资源池 / GPU</th>
                <th className="px-3 py-2.5">创建时间 / 持续</th>
                <th className="px-3 py-2.5">运行状态</th>
                <th className="px-3 py-2.5">AutoLab 接入</th>
                <th className="px-3.5 py-2.5 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {instances.map((inst) => {
                const tid = String(inst.id ?? "");
                const code = String(inst.status ?? "");
                const meta = STATUS_META[code] ?? {
                  label: code || "未知",
                  variant: "outline" as const,
                };
                const isRunning = code === "1";
                const canStop = code === "1" || code === "0" || code === "6" || code === "4";
                const reg = inst.registered_server;
                const detail = detailMap.get(tid);
                const termOpen = terminalTarget?.taskId === tid;
                const displayName = String(inst.taskName || tid.slice(0, 14));

                return (
                  <tr
                    key={tid}
                    className={`transition hover:bg-muted/20 ${termOpen ? "bg-emerald-500/5" : ""}`}
                  >
                    <td className="px-3.5 py-3 align-top">
                      <button
                        type="button"
                        onClick={() => setDetailTaskId(tid)}
                        className="text-left font-medium text-primary hover:underline"
                      >
                        {displayName}
                      </button>
                      <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
                        {tid.slice(0, 16)}…
                      </div>
                    </td>

                    <td className="px-3 py-3 align-top text-caption">
                      {inst.createBy || "—"}
                    </td>

                    <td className="px-3 py-3 align-top">
                      <div className="text-caption font-medium">
                        {inst.poolName || detail?.pool_name || "—"}
                      </div>
                      <div className="mt-0.5 text-[11px] text-muted-foreground">
                        {inst.gpuTotal ?? detail?.gpu_total ?? 1} GPU
                        {detail?.ip_address ? ` · ${detail.ip_address}` : ""}
                      </div>
                    </td>

                    <td className="px-3 py-3 align-top text-caption">
                      <div>{inst.createTime || "—"}</div>
                      {inst.continuedTime && (
                        <div className="mt-0.5 text-[11px] text-muted-foreground">
                          持续：{inst.continuedTime}
                        </div>
                      )}
                      {inst.unusedTime && (
                        <div
                          className={`mt-0.5 text-[11px] font-mono ${
                            typeof inst.remaining_sec === "number" && inst.remaining_sec <= 86400
                              ? "font-medium text-amber-600 dark:text-amber-400"
                              : "text-muted-foreground"
                          }`}
                        >
                          剩余：{inst.unusedTime}
                        </div>
                      )}
                    </td>

                    <td className="px-3 py-3 align-top">
                      <Badge variant={meta.variant}>{meta.label}</Badge>
                    </td>

                    <td className="px-3 py-3 align-top">
                      {reg ? (
                        <div className="space-y-0.5">
                          <Badge variant="default" className="bg-emerald-600 text-white">
                            已接入：{reg.name}
                          </Badge>
                          <div className="font-mono text-[11px] text-muted-foreground">
                            {reg.host}:{reg.port} · R:{reg.runner_port}
                          </div>
                        </div>
                      ) : isRunning ? (
                        <Badge variant="outline" className="text-muted-foreground">
                          未接入
                        </Badge>
                      ) : (
                        <span className="text-caption text-muted-foreground">—</span>
                      )}
                    </td>

                    <td className="px-3.5 py-3 align-top">
                      <div className="flex flex-wrap items-center justify-end gap-1.5">
                        {isRunning && (
                          <Button
                            size="sm"
                            variant={termOpen ? "default" : "outline"}
                            className="h-7 px-2.5 text-xs"
                            onClick={() =>
                              setTerminalTarget(
                                termOpen ? null : { taskId: tid, taskName: displayName },
                              )
                            }
                            title="在当前页面打开交互式 Web 终端"
                          >
                            <Terminal className="mr-1 size-3.5" />
                            {termOpen ? "收起终端" : "终端"}
                          </Button>
                        )}

                        {isRunning &&
                          (detail?.vscode_url ? (
                            <a
                              href={detail.vscode_url}
                              target="_blank"
                              rel="noreferrer"
                              onClick={() => void revealVscodePassword(tid, true)}
                            >
                              <Button
                                size="sm"
                                variant="outline"
                                className="h-7 px-2.5 text-xs"
                                title="打开思腾合力 Web VSCode（同时提示并复制登录密码）"
                              >
                                <Code2 className="mr-1 size-3.5" />
                                VSCode
                              </Button>
                            </a>
                          ) : (
                            <Button
                              size="sm"
                              variant="outline"
                              className="h-7 px-2.5 text-xs"
                              onClick={() => openVscodeOrDetail(tid)}
                            >
                              <Code2 className="mr-1 size-3.5" />
                              VSCode
                            </Button>
                          ))}

                        {isRunning && !reg && (
                          <Button
                            size="sm"
                            className="h-7 px-2.5 text-xs"
                            disabled={busyKey === `attach:${tid}`}
                            onClick={() => openAttach(inst)}
                          >
                            <Link2 className="mr-1 size-3.5" />
                            {busyKey === `attach:${tid}` ? "接入中…" : "接入 AutoLab"}
                          </Button>
                        )}

                        {isRunning && keepaliveMap[tid]?.running && (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 border-emerald-500/40 bg-emerald-500/10 px-2 text-xs text-emerald-700 hover:bg-emerald-500/20 dark:text-emerald-300"
                            onClick={() => setKeepaliveTarget(inst)}
                            title="GPU/CPU 空闲保活已开启，点击管理"
                          >
                            <Activity className="mr-1 size-3" />
                            保活中
                          </Button>
                        )}

                        <DropdownMenu>
                          <DropdownMenuTrigger
                            render={
                              <Button
                                size="sm"
                                variant="ghost"
                                className="h-7 px-2 text-xs"
                                aria-label={`更多操作 ${displayName}`}
                              >
                                <MoreVertical className="mr-0.5 size-3.5" />
                                更多
                              </Button>
                            }
                          />
                          <DropdownMenuContent align="end">
                            {isRunning && (
                              <>
                                <DropdownMenuItem onClick={() => setKeepaliveTarget(inst)}>
                                  <Activity className="mr-2 size-3.5" />
                                  {keepaliveMap[tid]?.running ? "管理空闲保活（运行中）" : "开启空闲保活"}
                                </DropdownMenuItem>
                                <DropdownMenuItem
                                  disabled={busyKey === `save:${tid}`}
                                  onClick={() => openSaveImageDialog(inst)}
                                >
                                  <HardDriveDownload className="mr-2 size-3.5" />
                                  保存容器为镜像
                                </DropdownMenuItem>
                                <DropdownMenuItem
                                  onClick={() => {
                                    setExecTarget(inst);
                                    setExecResult(null);
                                  }}
                                >
                                  <Terminal className="mr-2 size-3.5" />
                                  一次性脚本执行
                                </DropdownMenuItem>
                                <DropdownMenuSeparator />
                              </>
                            )}

                            <DropdownMenuItem onClick={() => setLogsTaskId(tid)}>
                              <ScrollText className="mr-2 size-3.5" />
                              查看运行日志
                            </DropdownMenuItem>
                            <DropdownMenuItem onClick={() => setDetailTaskId(tid)}>
                              <FileJson className="mr-2 size-3.5" />
                              实例详情 / 端口映射
                            </DropdownMenuItem>

                            {(isRunning || canStop || !isRunning) && <DropdownMenuSeparator />}

                            {isRunning && (
                              <DropdownMenuItem
                                disabled={busyKey === `pause:${tid}`}
                                onClick={() => setPauseTarget(inst)}
                              >
                                <Pause className="mr-2 size-3.5" />
                                暂停实例
                              </DropdownMenuItem>
                            )}

                            {canStop && (
                              <DropdownMenuItem
                                variant="destructive"
                                disabled={busyKey === `stop:${tid}`}
                                onClick={() => {
                                  setStopUnregister(true);
                                  setStopTarget(inst);
                                }}
                              >
                                <Square className="mr-2 size-3.5" />
                                停止并释放显卡
                              </DropdownMenuItem>
                            )}

                            {!isRunning && (
                              <DropdownMenuItem
                                variant="destructive"
                                disabled={busyKey === `del:${tid}`}
                                onClick={() => setDeleteTarget(inst)}
                              >
                                <Trash2 className="mr-2 size-3.5" />
                                删除记录
                              </DropdownMenuItem>
                            )}
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* 实例详情与直达链接弹窗 */}
      <Dialog open={!!detailTaskId} onOpenChange={(v) => !v && setDetailTaskId(null)}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>实例详情与直达入口</DialogTitle>
            <DialogDescription className="font-mono">{detailTaskId}</DialogDescription>
          </DialogHeader>
          {detailQuery.isLoading ? (
            <p className="text-sm text-muted-foreground">正在拉取实例详细配置…</p>
          ) : detailQuery.isError ? (
            <p className="text-sm text-destructive">
              {detailQuery.error instanceof Error ? detailQuery.error.message : String(detailQuery.error)}
            </p>
          ) : detailQuery.data ? (
            <div className="space-y-3 text-sm">
              <div className="grid grid-cols-2 gap-2 rounded-md border bg-muted/20 p-3 text-caption sm:grid-cols-3">
                <div>
                  状态：<strong>{detailQuery.data.status_label}</strong>
                </div>
                <div>资源池：{detailQuery.data.pool_name || "—"}</div>
                <div>GPU：{detailQuery.data.gpu_total ?? "—"} 卡</div>
                <div>CPU：{detailQuery.data.cpu_cores ?? "—"} 核</div>
                <div>内存：{detailQuery.data.memory_gb ?? "—"} GB</div>
                <div>硬盘：{detailQuery.data.disk_gb ?? "—"} GB</div>
                <div className="col-span-2 font-mono sm:col-span-3">
                  节点 IP：{detailQuery.data.ip_address || "—"} · 容器：{detailQuery.data.container_id || "—"}
                </div>
                <div className="col-span-2 break-all font-mono sm:col-span-3">
                  镜像：{detailQuery.data.image_name || "—"}
                </div>
              </div>

              {(detailQuery.data.vscode_url || detailQuery.data.terminal_url) && (
                <div className="space-y-1.5 rounded-md border p-3 text-caption">
                  <div className="font-medium text-foreground">平台直达入口</div>
                  {detailQuery.data.vscode_url && (
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="truncate font-mono">{detailQuery.data.vscode_url}</span>
                      <div className="flex gap-1">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => copyText(detailQuery.data!.vscode_url!, "VSCode 链接")}
                        >
                          <Copy className="mr-1 size-3" />
                          复制
                        </Button>
                        <a
                          href={detailQuery.data.vscode_url}
                          target="_blank"
                          rel="noreferrer"
                          onClick={() =>
                            void revealVscodePassword(detailQuery.data!.task_id, true)
                          }
                        >
                          <Button size="sm" variant="outline">
                            <ExternalLink className="mr-1 size-3" />
                            打开 VSCode
                          </Button>
                        </a>
                      </div>
                    </div>
                  )}
                  {detailQuery.data.vscode_url && (
                    <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-1.5">
                      <span className="font-mono text-muted-foreground">
                        VSCode 密码：
                        <strong className="ml-1 text-foreground">
                          {revealedPwd[detailQuery.data.task_id] ?? "••••••••"}
                        </strong>
                      </span>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => void revealVscodePassword(detailQuery.data!.task_id, true)}
                      >
                        <Copy className="mr-1 size-3" />
                        显示并复制密码
                      </Button>
                    </div>
                  )}
                  {detailQuery.data.terminal_url && (
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="truncate font-mono">{detailQuery.data.terminal_url}</span>
                      <a href={detailQuery.data.terminal_url} target="_blank" rel="noreferrer">
                        <Button size="sm" variant="outline">
                          <ExternalLink className="mr-1 size-3" />
                          思腾原生终端
                        </Button>
                      </a>
                    </div>
                  )}
                </div>
              )}

              {detailQuery.data.port_map && Object.keys(detailQuery.data.port_map).length > 0 && (
                <div className="space-y-1">
                  <div className="text-caption font-medium text-muted-foreground">
                    端口映射 (容器 → 宿主机)
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {Object.entries(detailQuery.data.port_map).map(([c, h]) => (
                      <Badge key={c} variant="outline" className="font-mono">
                        {c} → {h}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}

              {detailQuery.data.env_map && Object.keys(detailQuery.data.env_map).length > 0 && (
                <div className="space-y-1">
                  <div className="text-caption font-medium text-muted-foreground">
                    环境变量（敏感值已脱敏）
                  </div>
                  <pre className="max-h-40 overflow-auto rounded bg-muted/40 p-2 font-mono text-xs">
                    {Object.entries(detailQuery.data.env_map)
                      .map(([k, v]) => `${k}=${v}`)
                      .join("\n")}
                  </pre>
                </div>
              )}
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      {/* 接入 AutoLab 弹窗 */}
      <Dialog open={!!attachTarget} onOpenChange={(v) => !v && setAttachTarget(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>将运行中容器接入 AutoLab</DialogTitle>
            <DialogDescription>
              通过 WebSSH 离线安装 <span className="font-mono">autolab-runner</span> 与 SSH 密钥，并注册进 GPU 服务器列表。
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <div className="grid gap-1.5">
              <Label>AutoLab 服务器名称</Label>
              <Input value={attachName} onChange={(e) => setAttachName(e.target.value)} />
            </div>
            <div className="grid gap-1.5">
              <Label>连接通道模式</Label>
              <select
                className="h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                value={attachMode}
                onChange={(e) => setAttachMode(e.target.value as SitonholyBridgeMode)}
              >
                <option value="auto">auto（先探节点直连，不通自动走 code-server 网关）</option>
                <option value="gateway">gateway（强制走 10.12.54.221 code-server 网关）</option>
                <option value="direct">direct（仅使用宿主机映射端口直连）</option>
              </select>
            </div>
            <label className="flex cursor-pointer items-start gap-2 rounded-md border bg-muted/20 p-2.5 text-caption">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={attachKeepalive}
                onChange={(e) => setAttachKeepalive(e.target.checked)}
              />
              <span>
                接入后自动开启 GPU/CPU 空闲保活（防止长期不用被平台收回显卡；检测到训练任务时自动让出算力）
              </span>
            </label>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAttachTarget(null)}>
              取消
            </Button>
            <Button
              disabled={!attachTarget?.id || busyKey === `attach:${attachTarget?.id}`}
              onClick={() => void submitAttach()}
            >
              {busyKey === `attach:${attachTarget?.id}` ? "部署接入中（约数十秒）…" : "确认接入"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 保存容器为镜像（提交 Harbor 仓库）弹窗 */}
      <Dialog open={!!saveTarget} onOpenChange={(v) => !v && setSaveTarget(null)}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>保存容器镜像到 Harbor 仓库</DialogTitle>
            <DialogDescription>
              将当前运行中容器 <span className="font-mono">{saveTarget?.taskName || saveTarget?.id}</span> 的系统盘与 Python/Conda 环境打包推送，重建实例时可直接选用。
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <div className="grid gap-1.5">
              <Label>Harbor 项目空间 (`projectName`)</Label>
              {(imageProjectsQuery.data?.projects?.length ?? 0) > 0 ? (
                <select
                  className="h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                  value={saveProject}
                  onChange={(e) => setSaveProject(e.target.value)}
                >
                  {imageProjectsQuery.data!.projects.map((p) => (
                    <option key={p.projectName} value={p.projectName}>
                      {p.projectName}
                    </option>
                  ))}
                </select>
              ) : (
                <Input
                  value={saveProject}
                  onChange={(e) => setSaveProject(e.target.value)}
                  placeholder={imageProjectsQuery.isLoading ? "正在拉取项目空间…" : "例如 zhengherong"}
                />
              )}
            </div>
            <div className="grid gap-1.5">
              <Label>镜像名称 (`imageName`)</Label>
              <Input
                value={saveImageName}
                onChange={(e) => setSaveImageName(e.target.value)}
                placeholder="仅限小写字母、数字和 `.`（如 autolab.medclip）"
              />
              <p className="text-[11px] text-muted-foreground">
                规则：仅允许小写字符、数字和 `.`，且以字符或数字开头/结尾。
              </p>
            </div>
            <div className="grid gap-1.5">
              <Label>版本标签 (`tag`)</Label>
              <Input
                value={saveTag}
                onChange={(e) => setSaveTag(e.target.value)}
                placeholder="例如 v1 或 snap-20261115"
              />
              <p className="text-[11px] text-muted-foreground">
                规则：支持大小写字母、数字以及 `.` `_` `-`。
              </p>
            </div>
            <label className="flex cursor-pointer items-start gap-2 rounded-md border px-3 py-2 text-xs">
              <input
                type="checkbox"
                checked={savePromoteDefault}
                onChange={(e) => setSavePromoteDefault(e.target.checked)}
                className="mt-0.5"
              />
              <span>
                <span className="font-medium text-foreground">
                  推送完成后自动设为「自动抢卡/续期」默认基准镜像
                </span>
                <span className="mt-0.5 block text-[11px] text-muted-foreground">
                  后台守护每 20 秒检查 Harbor 仓库，一旦推送落地即自动更新基准镜像；此后自动抢卡或 7 天续期均默认基于此新镜像启动。
                </span>
              </span>
            </label>
            <div className="rounded-md border bg-muted/30 p-3 text-caption">
              <div>
                目标仓库路径：
                <span className="ml-1 break-all font-mono font-medium text-foreground">
                  {saveProject.trim() || "<project>"}/{saveImageName.trim() || "<image>"}:{saveTag.trim() || "<tag>"}
                </span>
              </div>
              <div className="mt-1 text-muted-foreground">
                提示：点击确认后平台将执行容器 commit 并推送到 Harbor，大镜像通常需要约 10 分钟推送完成（期间请勿立即停止该容器）。
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSaveTarget(null)}>
              取消
            </Button>
            <Button
              disabled={!saveTarget?.id || busyKey === `save:${saveTarget?.id}`}
              onClick={() => void submitSaveImage()}
            >
              {busyKey === `save:${saveTarget?.id}` ? "正在打包提交…" : "确认保存镜像"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* GPU/CPU 空闲保活管理弹窗 */}
      <Dialog open={!!keepaliveTarget} onOpenChange={(v) => !v && setKeepaliveTarget(null)}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              空闲保活管理：{keepaliveTarget?.taskName || keepaliveTarget?.id}
            </DialogTitle>
            <DialogDescription>
              防止显卡长期处于 0% 空闲状态被平台调度器强制收回。保活仅占极少量显存与瞬时心跳，一旦检测到外部真实训练进程或高 GPU 占用会立即自动让路并释放临时缓存。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            {keepaliveQuery.isLoading ? (
              <p className="text-muted-foreground">正在通过 WebSSH 检查容器保活进程状态…</p>
            ) : keepaliveQuery.isError ? (
              <p className="text-destructive">
                {keepaliveQuery.error instanceof Error
                  ? keepaliveQuery.error.message
                  : String(keepaliveQuery.error)}
              </p>
            ) : keepaliveQuery.data ? (
              <div className="space-y-2 rounded-md border bg-muted/20 p-3 text-caption">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <Badge variant={keepaliveQuery.data.running ? "default" : "outline"}>
                      {keepaliveQuery.data.running ? "运行中" : "未开启"}
                    </Badge>
                    {keepaliveQuery.data.running && keepaliveQuery.data.gpu_yielded && (
                      <Badge variant="secondary">真实任务运行中 · 已自动让路</Badge>
                    )}
                  </div>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 px-2 text-xs"
                    disabled={keepaliveQuery.isFetching}
                    onClick={() => void keepaliveQuery.refetch()}
                  >
                    <RefreshCw
                      className={`mr-1 size-3 ${keepaliveQuery.isFetching ? "animate-spin" : ""}`}
                    />
                    刷新状态
                  </Button>
                </div>
                <div className="grid grid-cols-2 gap-1.5 font-mono">
                  <div>PID: {keepaliveQuery.data.pid ?? "—"}</div>
                  <div>后端模式: {keepaliveQuery.data.mode || "—"}</div>
                  <div>检测 GPU 数: {keepaliveQuery.data.gpu_count ?? "—"}</div>
                  <div>心跳次数: {keepaliveQuery.data.beats ?? 0}</div>
                </div>
                {keepaliveQuery.data.last_beat_at && (
                  <div className="font-mono text-[11px] text-muted-foreground">
                    最近心跳：{keepaliveQuery.data.last_beat_at}
                  </div>
                )}
                {(keepaliveQuery.data.yield_reasons?.length ?? 0) > 0 && (
                  <div className="font-mono text-[11px] text-amber-600 dark:text-amber-400">
                    让路原因：{keepaliveQuery.data.yield_reasons!.join(", ")}
                  </div>
                )}
                {keepaliveQuery.data.note && (
                  <div className="font-mono text-[11px] text-amber-600 dark:text-amber-400">
                    诊断说明：{keepaliveQuery.data.note}
                  </div>
                )}
                {keepaliveQuery.data.log_tail && (
                  <pre className="max-h-28 overflow-auto rounded bg-background/80 p-2 font-mono text-[11px] text-muted-foreground">
                    {keepaliveQuery.data.log_tail}
                  </pre>
                )}
              </div>
            ) : null}

            <div className="flex items-center justify-between gap-3 rounded-md border p-3">
              <div className="grid gap-1">
                <Label className="text-caption">心跳间隔（秒，5 ~ 120）</Label>
                <Input
                  type="number"
                  min={5}
                  max={120}
                  className="h-8 w-28"
                  value={keepaliveInterval}
                  onChange={(e) => setKeepaliveInterval(e.target.value)}
                />
              </div>
              <div className="flex items-center gap-2">
                {keepaliveQuery.data?.running && (
                  <Button
                    variant="outline"
                    disabled={busyKey === `keepalive:${keepaliveTaskId}`}
                    onClick={() => void toggleKeepalive(false)}
                  >
                    停止保活
                  </Button>
                )}
                <Button
                  disabled={!keepaliveTaskId || busyKey === `keepalive:${keepaliveTaskId}`}
                  onClick={() => void toggleKeepalive(true)}
                >
                  {busyKey === `keepalive:${keepaliveTaskId}`
                    ? "执行中…"
                    : keepaliveQuery.data?.running
                      ? "按新间隔重启保活"
                      : "开启空闲保活"}
                </Button>
              </div>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* 运行日志弹窗 */}
      <Dialog open={!!logsTaskId} onOpenChange={(v) => !v && setLogsTaskId(null)}>
        <DialogContent className="max-h-[85vh] sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>容器运行日志</DialogTitle>
            <DialogDescription className="font-mono">{logsTaskId}</DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={logsQuery.isFetching}
              onClick={() => void logsQuery.refetch()}
            >
              <RefreshCw className={`mr-1 size-3.5 ${logsQuery.isFetching ? "animate-spin" : ""}`} />
              刷新日志
            </Button>
            {logsQuery.data && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => copyText(formatLogsText(logsQuery.data?.logs), "日志")}
              >
                <Copy className="mr-1 size-3.5" />
                复制
              </Button>
            )}
          </div>
          {logsQuery.isLoading ? (
            <p className="text-sm text-muted-foreground">正在拉取日志…</p>
          ) : logsQuery.isError ? (
            <p className="text-sm text-destructive">
              {logsQuery.error instanceof Error ? logsQuery.error.message : String(logsQuery.error)}
            </p>
          ) : (
            <pre className="max-h-[55vh] overflow-auto rounded-md bg-zinc-950 p-3 font-mono text-xs text-zinc-100 whitespace-pre-wrap">
              {formatLogsText(logsQuery.data?.logs)}
            </pre>
          )}
        </DialogContent>
      </Dialog>

      {/* 一次性批执行弹窗 */}
      <Dialog open={!!execTarget} onOpenChange={(v) => !v && setExecTarget(null)}>
        <DialogContent className="max-h-[88vh] overflow-y-auto sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>一次性脚本执行：{execTarget?.taskName || execTarget?.id}</DialogTitle>
            <DialogDescription>
              适合非交互式快速巡检并返回结构化 exit_code；需要交互式 Shell 请直接点击表格里的「终端」按钮。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="flex flex-wrap gap-1.5">
              {QUICK_SCRIPTS.map((q) => (
                <Button
                  key={q.label}
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => setExecScript(q.script)}
                >
                  {q.label}
                </Button>
              ))}
            </div>
            <div className="grid gap-1.5">
              <Label>Bash 命令 / 脚本</Label>
              <textarea
                rows={4}
                className="w-full rounded-md border border-input bg-transparent p-2 font-mono text-xs"
                value={execScript}
                onChange={(e) => setExecScript(e.target.value)}
                placeholder="输入要在容器内执行的命令…"
              />
            </div>
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 text-caption">
                <Label>超时 (秒)</Label>
                <Input
                  type="number"
                  min={5}
                  max={600}
                  className="h-8 w-24"
                  value={execTimeout}
                  onChange={(e) => setExecTimeout(e.target.value)}
                />
              </div>
              <Button
                disabled={!execTarget?.id || !execScript.trim() || busyKey === `exec:${execTarget?.id}`}
                onClick={() => void runExec()}
              >
                <Terminal className="mr-1 size-3.5" />
                {busyKey === `exec:${execTarget?.id}` ? "执行中…" : "执行命令"}
              </Button>
            </div>
            {execResult && (
              <div className="space-y-1.5">
                <div className="flex items-center justify-between text-caption">
                  <span>执行输出</span>
                  <Badge variant={execResult.exit_code === 0 ? "default" : "destructive"}>
                    exit_code = {execResult.exit_code}
                  </Badge>
                </div>
                <pre className="max-h-72 overflow-auto rounded-md bg-zinc-950 p-3 font-mono text-xs text-zinc-100 whitespace-pre-wrap">
                  {execResult.output || "（无标准输出）"}
                </pre>
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* 暂停确认 */}
      <AlertDialog open={!!pauseTarget} onOpenChange={(v) => !v && setPauseTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>暂停作业 {pauseTarget?.taskName || pauseTarget?.id}？</AlertDialogTitle>
            <AlertDialogDescription>
              将调用思腾合力暂停接口并关闭当前在线终端与 Control 本地网关桥接（建议确认训练检查点已落盘）。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirmPause()}>确认暂停</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 停止确认 */}
      <AlertDialog open={!!stopTarget} onOpenChange={(v) => !v && setStopTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>停止作业 {stopTarget?.taskName || stopTarget?.id}？</AlertDialogTitle>
            <AlertDialogDescription>
              停止会销毁该容器并释放 GPU 算力（持久化存储卷 `/root/data1` 中的数据不受影响）。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={stopUnregister}
              onChange={(e) => setStopUnregister(e.target.checked)}
            />
            <span>同时清理 AutoLab 服务器注册（有训练历史引用时会自动保留并标为离线）</span>
          </label>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirmStop()}>确认停止</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 删除确认 */}
      <AlertDialog open={!!deleteTarget} onOpenChange={(v) => !v && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除作业记录 {deleteTarget?.taskName || deleteTarget?.id}？</AlertDialogTitle>
            <AlertDialogDescription>
              仅删除思腾合力列表中的该条历史记录及残留网桥绑定。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirmDelete()}>确认删除</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
