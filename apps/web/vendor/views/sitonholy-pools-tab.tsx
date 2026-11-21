"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Cpu, HardDriveDownload, Layers, Plus, RefreshCw, ShieldCheck } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "~/vendor/ui/components/ui/badge";
import { Button } from "~/vendor/ui/components/ui/button";
import { Progress } from "~/vendor/ui/components/ui/progress";
import {
  sitonholyBuildBundle,
  sitonholyPoolSpecs,
  sitonholyStatus,
  type SitonholyPool,
  type SitonholySpec,
  type SitonholyStatus,
} from "~/vendor/views/gpu-sitonholy";

function formatSpecLine(spec: SitonholySpec, index: number): string {
  const name = String(spec.specificationName || spec.name || `规格 #${index + 1}`);
  const parts: string[] = [name];
  if (spec.gpuAmount != null) parts.push(`${spec.gpuAmount} GPU`);
  if (spec.cpuCoreNumber != null) parts.push(`${spec.cpuCoreNumber} CPU`);
  if (spec.memory != null) parts.push(`${spec.memory} GB 内存`);
  if (spec.hardDisk != null) parts.push(`${spec.hardDisk} GB 磁盘`);
  return parts.join(" · ");
}

export function SitonholyPoolsTab({
  pools,
  fetchingPools,
  status,
  onRefreshPools,
  onUsePoolToCreate,
}: {
  pools: SitonholyPool[];
  fetchingPools: boolean;
  status: SitonholyStatus | undefined;
  onRefreshPools: () => void;
  onUsePoolToCreate: (poolId: string) => void;
}) {
  const [a6000Only, setA6000Only] = useState(false);
  const [onlyAvailable, setOnlyAvailable] = useState(false);
  const [showClusterAll, setShowClusterAll] = useState(false);
  const [inspectPool, setInspectPool] = useState<SitonholyPool | null>(null);

  const [verifying, setVerifying] = useState(false);
  const [verifyResult, setVerifyResult] = useState<SitonholyStatus | null>(null);
  const [bundleBusy, setBundleBusy] = useState(false);
  const [bundleResult, setBundleResult] = useState<{ path: string; size_bytes: number } | null>(null);

  const { data: specsData, isFetching: fetchingSpecs } = useQuery({
    queryKey: ["sitonholy-pool-specs", inspectPool?.pool_id],
    queryFn: () => sitonholyPoolSpecs(inspectPool!.pool_id),
    enabled: Boolean(inspectPool?.pool_id),
    staleTime: 60_000,
    retry: false,
  });

  const filtered = pools.filter((p) => {
    if (a6000Only && !p.is_a6000) return false;
    if (onlyAvailable && p.available <= 0) return false;
    return true;
  });

  const myPools = filtered.filter((p) => p.user_visible !== false);
  const clusterOnlyPools = filtered.filter((p) => p.user_visible === false);

  const handleVerifySession = async () => {
    setVerifying(true);
    try {
      const res = await sitonholyStatus(true);
      setVerifyResult(res);
      if (res.valid) {
        toast.success("思腾合力会话校验通过");
      } else {
        toast.error("会话校验未通过", { description: res.error || "请重新登录" });
      }
    } catch (e) {
      toast.error("会话校验失败", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setVerifying(false);
    }
  };

  const handleBuildBundle = async () => {
    setBundleBusy(true);
    try {
      const res = await sitonholyBuildBundle(true);
      setBundleResult(res);
      toast.success("Linux Runner 离线安装包已刷新", {
        description: `${(res.size_bytes / (1024 * 1024)).toFixed(2)} MB`,
      });
    } catch (e) {
      toast.error("构建离线包失败", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setBundleBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="rounded-lg border bg-muted/20 p-3 text-caption text-muted-foreground">
        思腾合力后台接口会返回两类池：<strong>「我的可用池」</strong>（你在「提交作业」页面实际可选用的池，如{" "}
        <span className="font-mono">A6000(公用)</span>）以及{" "}
        <strong>「全集群节点物理池」</strong>（仅用于观察节点余卡负载，部分池可能没有提交权限）。这里默认把两类拆开展示，避免混成一堆看不懂。
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-card p-3">
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <label className="flex cursor-pointer items-center gap-1.5">
            <input
              type="checkbox"
              checked={a6000Only}
              onChange={(e) => setA6000Only(e.target.checked)}
            />
            <span>仅看 A6000</span>
          </label>
          <label className="flex cursor-pointer items-center gap-1.5">
            <input
              type="checkbox"
              checked={onlyAvailable}
              onChange={(e) => setOnlyAvailable(e.target.checked)}
            />
            <span>只看有空闲卡</span>
          </label>
          <label className="flex cursor-pointer items-center gap-1.5">
            <input
              type="checkbox"
              checked={showClusterAll}
              onChange={(e) => setShowClusterAll(e.target.checked)}
            />
            <span>展开全集群节点池 ({clusterOnlyPools.length})</span>
          </label>
        </div>
        <Button size="sm" variant="outline" disabled={fetchingPools} onClick={onRefreshPools}>
          <RefreshCw className={`mr-1 size-3.5 ${fetchingPools ? "animate-spin" : ""}`} />
          刷新资源池
        </Button>
      </div>

      <div className="space-y-2">
        <h3 className="text-sm font-semibold">
          我的可提交资源池 ({myPools.length})
        </h3>
        {myPools.length === 0 ? (
          <div className="rounded-lg border border-dashed p-4 text-caption text-muted-foreground">
            未在普通用户可见列表里匹配到资源池（可取消上方过滤条件，或勾选「展开全集群节点池」检查）。
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
            {myPools.map((p) => {
              const pct = p.total > 0 ? Math.min(100, Math.round((p.available / p.total) * 100)) : 0;
              return (
                <div key={p.pool_id} className="flex flex-col justify-between gap-3 rounded-lg border bg-card p-4">
                  <div className="space-y-2">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate font-medium">{p.pool_name || p.pool_id}</div>
                        <div className="font-mono text-caption text-muted-foreground">
                          ID: {p.pool_id} {p.host ? `· ${p.host}` : ""}
                        </div>
                      </div>
                      <div className="flex shrink-0 gap-1">
                        {p.is_a6000 && <Badge variant="secondary">A6000</Badge>}
                        <Badge variant="default">可提交</Badge>
                      </div>
                    </div>
                    <div className="space-y-1">
                      <div className="flex items-center justify-between text-caption">
                        <span>
                          空闲 <strong className="font-mono text-foreground">{p.available}</strong> /{" "}
                          <span className="font-mono">{p.total}</span> 卡
                        </span>
                        <span className="text-muted-foreground">排队：{p.queue_num}</span>
                      </div>
                      <Progress value={pct} />
                    </div>
                  </div>

                  <div className="flex items-center justify-between gap-2 pt-1">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setInspectPool(p)}
                    >
                      <Layers className="mr-1 size-3.5" />
                      查看规格
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => onUsePoolToCreate(p.pool_id)}
                    >
                      <Plus className="mr-1 size-3.5" />
                      用此池创建
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {showClusterAll && (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold">
            全集群底层节点池参考 ({clusterOnlyPools.length})
          </h3>
          {clusterOnlyPools.length === 0 ? (
            <p className="text-caption text-muted-foreground">当前筛选条件下无额外节点池。</p>
          ) : (
            <div className="overflow-x-auto rounded-lg border bg-card">
              <table className="w-full text-left text-sm">
                <thead className="border-b bg-muted/40 text-caption text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 font-medium">池名称</th>
                    <th className="px-3 py-2 font-medium">型号标记</th>
                    <th className="px-3 py-2 font-medium">节点主机</th>
                    <th className="px-3 py-2 font-medium">可用 / 总卡数</th>
                    <th className="px-3 py-2 font-medium">排队数</th>
                    <th className="px-3 py-2 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {clusterOnlyPools.map((p) => (
                    <tr key={p.pool_id} className="hover:bg-muted/20">
                      <td className="px-3 py-2">
                        <div className="font-medium">{p.pool_name || p.pool_id}</div>
                        <div className="font-mono text-caption text-muted-foreground">{p.pool_id}</div>
                      </td>
                      <td className="px-3 py-2">
                        {p.is_a6000 ? <Badge variant="secondary">A6000</Badge> : <Badge variant="outline">其他</Badge>}
                      </td>
                      <td className="px-3 py-2 font-mono text-caption">{p.host || "—"}</td>
                      <td className="px-3 py-2 font-mono">
                        {p.available} / {p.total}
                      </td>
                      <td className="px-3 py-2 font-mono">{p.queue_num}</td>
                      <td className="px-3 py-2 text-right">
                        <div className="flex justify-end gap-1.5">
                          <Button size="sm" variant="ghost" onClick={() => setInspectPool(p)}>
                            规格
                          </Button>
                          <Button size="sm" variant="outline" onClick={() => onUsePoolToCreate(p.pool_id)}>
                            选用
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {inspectPool && (
        <div className="rounded-lg border bg-card p-4">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 font-medium">
              <Cpu className="size-4 text-primary" />
              <span>资源池规格明细：{inspectPool.pool_name || inspectPool.pool_id}</span>
            </div>
            <Button size="sm" variant="ghost" onClick={() => setInspectPool(null)}>
              收起
            </Button>
          </div>
          {fetchingSpecs ? (
            <p className="mt-2 text-caption text-muted-foreground">正在拉取该池可用规格…</p>
          ) : (specsData?.specs ?? []).length === 0 ? (
            <p className="mt-2 text-caption text-muted-foreground">该资源池未返回任何可用规格。</p>
          ) : (
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {(specsData?.specs ?? []).map((spec, idx) => (
                <div
                  key={String(spec.specificationId || spec.id || idx)}
                  className="rounded-md border bg-muted/20 px-3 py-2 text-caption"
                >
                  <div className="font-medium text-foreground">{formatSpecLine(spec, idx)}</div>
                  <div className="font-mono text-muted-foreground">
                    spec_id: {String(spec.specificationId || spec.id || "—")}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-2 rounded-lg border bg-card p-4">
          <div className="flex items-center justify-between">
            <span className="flex items-center gap-1.5 text-sm font-medium">
              <ShieldCheck className="size-4 text-primary" />
              平台会话凭据诊断
            </span>
            <Button size="sm" variant="outline" disabled={verifying} onClick={() => void handleVerifySession()}>
              {verifying ? "校验中…" : "实时校验会话 (verify)"}
            </Button>
          </div>
          <div className="space-y-1 text-caption text-muted-foreground">
            <div>用户：{status?.nick_name || status?.username || "—"} (ID: {status?.user_id || "—"})</div>
            <div>地址：{status?.base_url || "http://10.12.54.221"}</div>
            <div>
              脱敏凭据：Token <span className="font-mono">{status?.token_preview || "无"}</span> · Cookie{" "}
              <span className="font-mono">{status?.session_preview || "无"}</span>
            </div>
            {verifyResult && (
              <div className={verifyResult.valid ? "text-emerald-600" : "text-destructive"}>
                实时核验：{verifyResult.valid ? "会话有效" : `失效 (${verifyResult.error || "未知"})`}
              </div>
            )}
          </div>
        </div>

        <div className="space-y-2 rounded-lg border bg-card p-4">
          <div className="flex items-center justify-between">
            <span className="flex items-center gap-1.5 text-sm font-medium">
              <HardDriveDownload className="size-4 text-primary" />
              控制端离线 Runner 打包缓存
            </span>
            <Button size="sm" variant="outline" disabled={bundleBusy} onClick={() => void handleBuildBundle()}>
              {bundleBusy ? "打包中…" : "重新构建离线包"}
            </Button>
          </div>
          <p className="text-caption text-muted-foreground">
            接入新容器时由 Control 宿主机将 <span className="font-mono">autolab-runner</span> 与依赖离线推入容器，无需容器访问 PyPI。
          </p>
          {bundleResult && (
            <div className="rounded bg-muted/40 p-2 font-mono text-caption">
              <div>路径: {bundleResult.path}</div>
              <div>大小: {(bundleResult.size_bytes / (1024 * 1024)).toFixed(2)} MB</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
