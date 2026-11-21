"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bot,
  ChevronDown,
  ChevronUp,
  Play,
  RefreshCw,
  Save,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "~/vendor/ui/components/ui/badge";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import { Label } from "~/vendor/ui/components/ui/label";
import {
  sitonholyAutoAcquireGet,
  sitonholyAutoAcquireRunOnce,
  sitonholyAutoAcquireUpdate,
  type SitonholyAutoAcquireTarget,
} from "~/vendor/views/gpu-sitonholy";

export function SitonholyAutoAcquireCard({
  loggedIn,
  imageOptions,
  onMutated,
}: {
  loggedIn: boolean;
  imageOptions: string[];
  onMutated: () => Promise<void> | void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [runningOnce, setRunningOnce] = useState(false);

  const { data, isFetching, refetch } = useQuery({
    queryKey: ["sitonholy-auto-acquire"],
    queryFn: () => sitonholyAutoAcquireGet(true),
    enabled: loggedIn,
    refetchInterval: (q) => (q.state.data?.config?.enabled ? 12_000 : false),
    retry: false,
  });

  const [enabled, setEnabled] = useState(false);
  const [pollSec, setPollSec] = useState("20");
  const [imageName, setImageName] = useState("");
  const [autoRegister, setAutoRegister] = useState(true);
  const [attachKeepalive, setAttachKeepalive] = useState(true);
  const [autoRenew, setAutoRenew] = useState(true);
  const [renewBeforeHours, setRenewBeforeHours] = useState("6");
  const [renewTightMinutes, setRenewTightMinutes] = useState("15");
  const [renewSaveImage, setRenewSaveImage] = useState(true);
  const [autoSnapshotOnChange, setAutoSnapshotOnChange] = useState(true);
  const [autoSnapshotMinHours, setAutoSnapshotMinHours] = useState("12");
  const [autoSnapshotKeepCount, setAutoSnapshotKeepCount] = useState("2");
  const [targets, setTargets] = useState<SitonholyAutoAcquireTarget[]>([]);
  const [initialized, setInitialized] = useState(false);

  useEffect(() => {
    if (!data?.config) return;
    if (!initialized || !expanded) {
      setEnabled(Boolean(data.config.enabled));
      setPollSec(String(data.config.poll_interval_sec ?? 20));
      setImageName(data.config.image_name || imageOptions[0] || "");
      setAutoRegister(Boolean(data.config.auto_register));
      setAttachKeepalive(Boolean(data.config.attach_keepalive));
      setAutoRenew(data.config.auto_renew ?? true);
      setRenewBeforeHours(String(data.config.renew_before_hours ?? 6));
      setRenewTightMinutes(String(data.config.renew_tight_minutes ?? 15));
      setRenewSaveImage(data.config.renew_save_image ?? true);
      setAutoSnapshotOnChange(data.config.auto_snapshot_on_change ?? true);
      setAutoSnapshotMinHours(String(data.config.auto_snapshot_min_interval_hours ?? 12));
      setAutoSnapshotKeepCount(String(data.config.auto_snapshot_keep_count ?? 2));
      setTargets(data.config.targets ?? []);
      setInitialized(true);
    }
  }, [data, expanded, initialized, imageOptions]);

  const handleToggleEnabled = async () => {
    if (!data?.config) return;
    const nextEnabled = !data.config.enabled;
    setSaving(true);
    try {
      await sitonholyAutoAcquireUpdate({
        ...data.config,
        enabled: nextEnabled,
        image_name: data.config.image_name || imageName || imageOptions[0] || "",
      });
      setEnabled(nextEnabled);
      toast.success(nextEnabled ? "已开启自动抢卡与配额维护" : "已暂停自动抢卡守护");
      await refetch();
      await onMutated();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const clampedSec = Math.max(10, Math.min(300, Number(pollSec) || 20));
      const clampedHours = Math.max(1, Math.min(72, Number(renewBeforeHours) || 6));
      const clampedTight = Math.max(3, Math.min(120, Number(renewTightMinutes) || 15));
      const clampedSnapHours = Math.max(1, Math.min(168, Number(autoSnapshotMinHours) || 12));
      const clampedKeepCount = Math.max(1, Math.min(10, Number(autoSnapshotKeepCount) || 2));
      await sitonholyAutoAcquireUpdate({
        enabled,
        poll_interval_sec: clampedSec,
        image_name: imageName.trim(),
        auto_register: autoRegister,
        attach_keepalive: attachKeepalive,
        auto_renew: autoRenew,
        renew_before_hours: clampedHours,
        renew_tight_minutes: clampedTight,
        renew_save_image: renewSaveImage,
        renew_try_extend: data?.config?.renew_try_extend ?? true,
        auto_snapshot_on_change: autoSnapshotOnChange,
        auto_snapshot_min_interval_hours: clampedSnapHours,
        auto_snapshot_keep_count: clampedKeepCount,
        targets,
      });
      toast.success("自动抢卡配额策略已保存");
      setExpanded(false);
      await refetch();
      await onMutated();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const handleRunOnce = async () => {
    setRunningOnce(true);
    try {
      const res = await sitonholyAutoAcquireRunOnce();
      if (res.created.length > 0) {
        toast.success(`已自动抢占 ${res.created.length} 张卡`, {
          description: res.created.map((c) => `${c.task_name} @ ${c.pool_name}`).join("；"),
        });
      } else if (res.registered.length > 0) {
        toast.success(`已自动接入 ${res.registered.length} 台新实例`, {
          description: res.registered.map((r) => r.server_name).join("；"),
        });
      } else if (res.error) {
        toast.warning("巡检完成（存在告警）", { description: res.error });
      } else {
        toast.info("巡检完成", { description: "当前公用池暂无新释放余卡或配额已满足。" });
      }
      await refetch();
      await onMutated();
    } catch (e) {
      toast.error("立即巡检失败", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setRunningOnce(false);
    }
  };

  const liveTargets = data?.live?.targets ?? [];
  const events = data?.runtime?.events ?? [];
  const isActive = Boolean(data?.config?.enabled);

  return (
    <div className="rounded-xl border bg-card px-4 py-3 space-y-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Bot className="size-4 text-primary" />
          <span className="text-sm font-semibold">目标配额自动抢卡 & 维护管家</span>
          <Badge variant={isActive ? "default" : "secondary"}>
            {isActive ? `守护中 · 每 ${data?.config?.poll_interval_sec ?? 20}s` : "已暂停"}
          </Badge>
          {isActive && (data?.config?.auto_renew ?? true) && (
            <Badge variant="outline" className="text-[11px]">
              7天到期前 {data?.config?.renew_before_hours ?? 6}h 自动续期接力
            </Badge>
          )}
          {data?.runtime?.last_checked_at && (
            <span className="text-xs text-muted-foreground">
              最近检查：{new Date(data.runtime.last_checked_at).toLocaleTimeString()}
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={runningOnce || !loggedIn}
            onClick={handleRunOnce}
          >
            <Play className="mr-1 size-3.5" />
            {runningOnce ? "检查中…" : "立即抢卡检查"}
          </Button>
          <Button
            size="sm"
            variant={isActive ? "outline" : "default"}
            disabled={saving || !loggedIn}
            onClick={handleToggleEnabled}
          >
            {isActive ? "暂停守护" : "启用自动守护"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? (
              <>
                收起配置 <ChevronUp className="ml-1 size-3.5" />
              </>
            ) : (
              <>
                调整配额 <ChevronDown className="ml-1 size-3.5" />
              </>
            )}
          </Button>
          <Button size="sm" variant="ghost" disabled={isFetching} onClick={() => refetch()}>
            <RefreshCw className={`size-3.5 ${isFetching ? "animate-spin" : ""}`} />
          </Button>
        </div>
      </div>

      {/* 各型号配额总览 */}
      <div className="flex flex-wrap gap-2">
        {liveTargets.map((t) => {
          const ok = t.active_count >= t.desired_count;
          return (
            <div
              key={t.id}
              className="flex flex-wrap items-center gap-2 rounded-lg border bg-muted/25 px-3 py-1.5 text-xs"
            >
              <span className="font-medium">{t.label}</span>
              <span className="font-mono font-semibold text-foreground">
                当前 {t.active_count} / 目标 {t.desired_count} 卡
              </span>
              <Badge variant={ok ? "default" : "outline"} className="text-[11px]">
                {ok ? "已达标" : `缺 ${t.deficit} 卡`}
              </Badge>
              <span className="text-muted-foreground">
                授权池余 {t.pool_free}/{t.pool_total}
              </span>
              {t.instances
                .filter(
                  (inst) =>
                    (inst.unused_time && inst.unused_time !== "无限期") ||
                    inst.auto_snapshot_pending ||
                    inst.env_dirty,
                )
                .map((inst) => {
                  const soon =
                    typeof inst.remaining_sec === "number" && inst.remaining_sec <= 86400;
                  return (
                    <Badge
                      key={inst.task_id}
                      variant="outline"
                      className={`font-mono text-[10px] ${
                        soon ? "border-amber-500/50 text-amber-700 dark:text-amber-400" : "text-muted-foreground"
                      }`}
                    >
                      {inst.task_name}
                      {inst.unused_time && inst.unused_time !== "无限期" ? ` · 剩 ${inst.unused_time}` : ""}
                      {inst.renewal_state === "warming_replacement" ? " · 热备接力中" : ""}
                      {inst.renewal_state === "snapshot_armed_waiting_window" ? " · 已预快照待切换" : ""}
                      {inst.renewal_state === "waiting_idle_busy" ? " · 训练中暂缓" : ""}
                      {inst.auto_snapshot_pending
                        ? " · 自动快照推送中"
                        : inst.env_dirty
                          ? " · 环境已变更待快照"
                          : ""}
                    </Badge>
                  );
                })}
            </div>
          );
        })}
      </div>

      {data?.runtime?.pending_image_promotion?.repository && (
        <div className="rounded-md border border-blue-500/40 bg-blue-500/5 px-3 py-1.5 text-xs text-blue-700 dark:text-blue-300">
          正在等待 Harbor 推送手动保存镜像，就绪后将自动设为自动抢卡基准镜像：
          <span className="ml-1 font-mono font-medium">
            {data.runtime.pending_image_promotion.repository}
          </span>
        </div>
      )}

      {data?.runtime?.last_error && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-1.5 text-xs text-amber-700 dark:text-amber-400">
          {data.runtime.last_error}
        </div>
      )}

      {events[0] && (
        <div className="text-xs text-muted-foreground">
          最新动态：<span className="font-mono">{new Date(events[0].at).toLocaleTimeString()}</span>{" "}
          · {events[0].message}
        </div>
      )}

      {expanded && (
        <div className="space-y-3 border-t pt-3">
          <div className="grid gap-3 md:grid-cols-3">
            {targets.map((rule, idx) => (
              <div key={rule.id} className="space-y-1.5 rounded-lg border bg-muted/15 p-3">
                <div className="flex items-center justify-between text-xs font-medium">
                  <span>{rule.label || rule.gpu_match}</span>
                  <span className="font-mono text-muted-foreground">{rule.gpu_match}</span>
                </div>
                <div className="flex items-center gap-2">
                  <Label className="shrink-0 text-xs">目标保持卡数</Label>
                  <Input
                    type="number"
                    min={0}
                    max={16}
                    value={rule.desired_count}
                    onChange={(e) => {
                      const val = Math.max(0, Math.min(16, Number(e.target.value) || 0));
                      setTargets((prev) =>
                        prev.map((item, i) =>
                          i === idx ? { ...item, desired_count: val } : item,
                        ),
                      );
                    }}
                    className="h-8 font-mono"
                  />
                </div>
              </div>
            ))}

            <div className="space-y-1.5 rounded-lg border bg-muted/15 p-3">
              <Label className="text-xs">自动抢卡默认镜像</Label>
              <Input
                value={imageName}
                onChange={(e) => setImageName(e.target.value)}
                placeholder="192.168.33.224:8888/..."
                list="auto-acquire-image-options"
                className="h-8 font-mono text-xs"
              />
              <datalist id="auto-acquire-image-options">
                {imageOptions.map((img) => (
                  <option key={img} value={img} />
                ))}
              </datalist>
              <div className="flex items-center gap-2 pt-1 text-xs">
                <span>轮询间隔(秒)</span>
                <Input
                  type="number"
                  min={10}
                  max={300}
                  value={pollSec}
                  onChange={(e) => setPollSec(e.target.value)}
                  className="h-7 w-20 font-mono text-xs"
                />
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-4 text-xs">
              <label className="flex cursor-pointer items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                />
                <span>启用后台自动守护</span>
              </label>
              <label className="flex cursor-pointer items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={autoRegister}
                  onChange={(e) => setAutoRegister(e.target.checked)}
                />
                <span>抢到后自动引导 Runner 并接入 AutoLab</span>
              </label>
              <label className="flex cursor-pointer items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={attachKeepalive}
                  onChange={(e) => setAttachKeepalive(e.target.checked)}
                />
                <span>自动开启 IDLE 保活防回收</span>
              </label>
              <label className="flex cursor-pointer items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={autoRenew}
                  onChange={(e) => setAutoRenew(e.target.checked)}
                />
                <span>7天上限前自动续期接力</span>
              </label>
              <label className="flex cursor-pointer items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={renewSaveImage}
                  onChange={(e) => setRenewSaveImage(e.target.checked)}
                />
                <span>接力前自动保存 Harbor 环境镜像</span>
              </label>
              <div className="flex items-center gap-1.5">
                <span>提前触发(h)</span>
                <Input
                  type="number"
                  min={1}
                  max={72}
                  value={renewBeforeHours}
                  onChange={(e) => setRenewBeforeHours(e.target.value)}
                  className="h-7 w-16 font-mono text-xs"
                />
                <span>紧缺临界(m)</span>
                <Input
                  type="number"
                  min={3}
                  max={120}
                  value={renewTightMinutes}
                  onChange={(e) => setRenewTightMinutes(e.target.value)}
                  className="h-7 w-16 font-mono text-xs"
                />
              </div>
              <label className="flex cursor-pointer items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={autoSnapshotOnChange}
                  onChange={(e) => setAutoSnapshotOnChange(e.target.checked)}
                />
                <span>环境变更自动 Harbor 快照</span>
              </label>
              <div className="flex items-center gap-1.5">
                <span>最短间隔(h)</span>
                <Input
                  type="number"
                  min={1}
                  max={168}
                  value={autoSnapshotMinHours}
                  onChange={(e) => setAutoSnapshotMinHours(e.target.value)}
                  className="h-7 w-16 font-mono text-xs"
                />
                <span>保留最新(份)</span>
                <Input
                  type="number"
                  min={1}
                  max={10}
                  value={autoSnapshotKeepCount}
                  onChange={(e) => setAutoSnapshotKeepCount(e.target.value)}
                  className="h-7 w-14 font-mono text-xs"
                />
              </div>
            </div>

            <Button size="sm" disabled={saving} onClick={handleSave}>
              <Save className="mr-1.5 size-3.5" />
              {saving ? "保存中…" : "保存策略"}
            </Button>
          </div>

          <div className="rounded-md bg-muted/30 px-3 py-1.5 text-[11px] text-muted-foreground">
            Anaconda 持久化机制：新建 Conda 环境（/root/data1/.conda/envs）与 Pip 缓存直连 NFS 持久卷即时生效；若改动原基础镜像或系统包，管家将在空闲稳定后自动推送 Harbor 快照并按配额保留最新 {autoSnapshotKeepCount || 2} 份，后续重开/7天续期自动沿用。
          </div>

          {events.length > 0 && (
            <div className="max-h-36 overflow-y-auto rounded-md border bg-muted/20 p-2 text-xs space-y-1">
              {events.slice(0, 12).map((ev, idx) => (
                <div key={`${ev.at}-${idx}`} className="flex gap-2">
                  <span className="shrink-0 font-mono text-muted-foreground">
                    {new Date(ev.at).toLocaleTimeString()}
                  </span>
                  <span>{ev.message}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
