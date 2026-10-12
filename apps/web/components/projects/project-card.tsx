"use client";

import Link from "next/link";
import {
  FileText, FlaskConical, FolderKanban, Link2, MoreHorizontal, Pencil, RefreshCw,
  Server as ServerIcon, Trash2,
} from "lucide-react";
import type { Project, StageKey, StageState } from "@/lib/api";
import { timeAgo } from "@/lib/time";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "~/vendor/ui/components/ui/dropdown-menu";

const STAGE_LABEL: Record<StageKey, string> = { setup: "立项", baseline: "Baseline", research: "研究" };
const DOT: Record<StageState, string> = {
  done: "bg-success", active: "bg-brand", attention: "bg-warning", pending: "bg-muted-foreground/30",
};

export function ProjectCard({ project, onEdit, onSync, onDelete }: {
  project: Project;
  onEdit: () => void;
  onSync: () => void;
  onDelete: () => void;
}) {
  const canSync = !!project.workdir && (!!project.localWorkspace || !!project.githubUrl);
  return (
    <div className="flex flex-col rounded-md border-[0.5px] border-surface-border bg-surface p-4 shadow-[var(--surface-shadow)] transition-colors hover:border-primary/40">
      <div className="flex items-center gap-2">
        <FolderKanban className="size-4 shrink-0 text-muted-foreground" />
        <h3 className="min-w-0 flex-1 truncate text-body font-medium">{project.name}</h3>
        {!project.workdir ? (
          <span className="rounded-full bg-warning/10 px-1.5 py-0.5 text-micro font-medium text-warning">待完善</span>
        ) : null}
      </div>
      <div className="mt-2 flex items-center gap-1.5 text-caption text-muted-foreground">
        <ServerIcon className="size-3 shrink-0" />
        <span className="truncate">
          {project.server.name}{project.server.transport_mode === "ssh" ? " · SSH" : " · 本机"}
        </span>
      </div>
      <p className="mt-1 truncate font-mono text-micro text-muted-foreground" title={project.workdir}>
        {project.workdir || "未设置工作区(编辑补全)"}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-micro">
        {project.githubUrl ? (
          <span className="inline-flex items-center gap-1 rounded-full bg-muted/60 px-1.5 py-0.5 text-muted-foreground"
                title={project.githubUrl}>
            <Link2 className="size-3" />GitHub
          </span>
        ) : null}
        <span className={`inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 ${
          project.papersCount > 0 ? "bg-info/10 text-info" : "bg-muted/60 text-muted-foreground/60"}`}>
          <FileText className="size-3" />论文 {project.papersCount}
        </span>
      </div>
      <div className="mt-3 flex items-center gap-3 border-t border-surface-border pt-2 text-micro text-muted-foreground">
        {(Object.keys(STAGE_LABEL) as StageKey[]).map((key) => (
          <span key={key} className="inline-flex items-center gap-1" title={project.stages[key].summary}>
            <span className={`size-1.5 rounded-full ${DOT[project.stages[key].state]}`} />
            {STAGE_LABEL[key]}
          </span>
        ))}
        <span className="ml-auto">{timeAgo(project.updated_at)}</span>
      </div>
      <div className="mt-3 flex items-center gap-2">
        <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-caption" onClick={onEdit}>
          <Pencil className="size-3" />编辑
        </Button>
        {project.workdir ? (
          <Link href={`/baseline?project=${project.id}`}>
            <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-caption">
              <FlaskConical className="size-3" />Baseline
            </Button>
          </Link>
        ) : null}
        <DropdownMenu>
          <DropdownMenuTrigger render={
            <Button variant="ghost" size="icon-sm" aria-label="更多操作" className="ml-auto">
              <MoreHorizontal className="size-3.5" />
            </Button>} />
          <DropdownMenuContent align="end">
            <DropdownMenuItem disabled={!canSync} onClick={onSync}>
              <RefreshCw className="size-3.5" />同步代码
            </DropdownMenuItem>
            <DropdownMenuItem className="text-destructive" onClick={onDelete}>
              <Trash2 className="size-3.5" />删除项目
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}
