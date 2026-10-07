"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowUp, Folder, FolderOpen, Home, Loader2 } from "lucide-react";
import { browseFs, browseServerFs } from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "~/vendor/ui/components/ui/dialog";

/**
 * 文件夹选择器(共享):本机目录(local)或服务器工作区(server)两种来源。
 * 从 Chat 页的选择器提炼——项目页选本地工作区 / GPU 服务器上的远端
 * 工作区,都不该让 owner 手抄路径。
 */
export function FolderBrowserDialog({
  open, onOpenChange, initialPath, onPick, source, title, description, quickPicks,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  initialPath?: string;
  onPick: (path: string) => void;
  /** local = 本机(chat fs browse);server = 指定服务器的 SFTP/本机列目录。 */
  source: { kind: "local" } | { kind: "server"; serverId: string };
  title?: string;
  description?: string;
  /** 快捷入口(如常用项目工作区);label 展示,path 跳转。 */
  quickPicks?: Array<{ label: string; path: string }>;
}) {
  const [current, setCurrent] = useState(initialPath || "");
  const [manual, setManual] = useState("");
  const enabled = open && !(source.kind === "server" && !source.serverId);
  const browse = useQuery({
    queryKey: ["folder-browser", source.kind, (source as { serverId?: string }).serverId ?? "", current, open],
    queryFn: () => source.kind === "server"
      ? browseServerFs(source.serverId, current)
      : browseFs(current),
    enabled,
    retry: false,
  });
  useEffect(() => {
    if (open) {
      setCurrent(initialPath || "");
      setManual("");
    }
  }, [open, initialPath]);

  const crumbs = (browse.data?.path ?? current).split("/").filter(Boolean);
  const go = (path: string) => {
    setManual("");
    setCurrent(path);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{title ?? "选择工作区"}</DialogTitle>
          <DialogDescription>
            {description ?? "浏览目录,选定文件夹。"}
          </DialogDescription>
        </DialogHeader>

        {/* 快捷入口:家目录 + 调用方给的常用路径 */}
        <div className="flex flex-wrap gap-1.5">
          <button type="button" onClick={() => go("")}
                  className="inline-flex items-center gap-1 rounded-full bg-muted/60 px-2 py-1
                             text-micro text-muted-foreground hover:bg-muted">
            <Home className="size-3" /> 家目录
          </button>
          {(quickPicks ?? []).slice(0, 6).map((pick) => (
            <button key={pick.path} type="button" onClick={() => go(pick.path)}
                    title={pick.path}
                    className="inline-flex max-w-48 items-center gap-1 rounded-full bg-brand/10
                               px-2 py-1 text-micro text-brand hover:bg-brand/20">
              <FolderOpen className="size-3 shrink-0" />
              <span className="truncate">{pick.label}</span>
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
