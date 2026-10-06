"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { KeyRound, RefreshCw, ServerOff } from "lucide-react";
import { Button } from "~/vendor/ui/components/ui/button";
import { apiUrl } from "@/lib/api";

/**
 * 统一 API 错误态:区分「未授权」与「控制面不可达」,给中文文案与明确的
 * 下一步(去 Settings / 重试),不再把 `Failed to fetch` 原文直接甩给用户,
 * 也不在 header 和 body 各显示一遍。
 *
 * 判定依据 lib/api.ts:401/403 抛 Error("未授权…");浏览器网络层失败抛
 * TypeError("Failed to fetch")。
 */
function isAuthError(error: unknown): boolean {
  return error instanceof Error && error.message.includes("未授权");
}

function isNetworkError(error: unknown): boolean {
  if (error instanceof TypeError) return true;
  return error instanceof Error && /fetch/i.test(error.message);
}

export function ApiError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const auth = isAuthError(error);
  const network = isNetworkError(error);

  const title = auth ? "未授权" : network ? "无法连接 Control API" : "加载失败";
  const desc: ReactNode = auth ? (
    <>token 缺失或已失效。到 Settings 粘贴 owner token 后重试。</>
  ) : network ? (
    <>
      请确认 autolab-control 正在监听{" "}
      <code className="rounded bg-muted px-1 font-mono text-caption">{apiUrl}</code>
      (本地 demo 用 <code className="font-mono text-caption">scripts/demo-web.sh</code> 启动)。
    </>
  ) : (
    <>{error instanceof Error ? error.message : String(error)}</>
  );
  const Icon = auth ? KeyRound : ServerOff;

  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <div className="w-full max-w-md rounded-xl border border-destructive/30 bg-destructive/5 p-5">
        <div className="flex items-center gap-2.5">
          <Icon className="size-4 shrink-0 text-destructive" />
          <h2 className="text-body font-semibold text-destructive">{title}</h2>
        </div>
        <p className="mt-2 text-caption leading-relaxed text-muted-foreground">{desc}</p>
        <div className="mt-4 flex items-center gap-2">
          {auth ? (
            <Link href="/settings">
              <Button size="sm">
                <KeyRound className="size-3.5" />
                去 Settings 设置 token
              </Button>
            </Link>
          ) : null}
          {onRetry ? (
            <Button variant="outline" size="sm" onClick={onRetry}>
              <RefreshCw className="size-3.5" />
              重试
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
