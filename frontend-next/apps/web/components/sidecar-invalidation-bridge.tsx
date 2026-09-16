"use client";
/**
 * sidecar 失效通知桥（v0.3 §6：WS 只发失效通知，REST/SQLite 保持权威）。
 * sidecar 数据源模式下连 sidecar 事件 WS，任何事件帧（状态迁移/attempt/
 * env_setup/review…）去抖后失效全部 React Query——看板与工作台随之自动
 * 刷新，无需手动 F5。fixture 模式不连接（无失效语义）。
 */
import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { getSidecarConfig } from "@autolab/research/sidecar/config";
import { connectSidecarEvents } from "@autolab/research/sidecar/ws";

export function SidecarInvalidationBridge() {
  const qc = useQueryClient();
  useEffect(() => {
    let dispose: (() => void) | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;
    void (async () => {
      try {
        const cfg = await getSidecarConfig();
        if (disposed || cfg.data_source !== "sidecar") return;
        dispose = connectSidecarEvents(() => {
          if (timer) clearTimeout(timer);
          // 训练期事件成串——去抖合并为一次全量失效
          timer = setTimeout(() => {
            void qc.invalidateQueries();
          }, 400);
        });
      } catch {
        // 配置不可达：保持手动刷新
      }
    })();
    return () => {
      disposed = true;
      dispose?.();
      if (timer) clearTimeout(timer);
    };
  }, [qc]);
  return null;
}
