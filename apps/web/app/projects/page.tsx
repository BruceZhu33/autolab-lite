"use client";

/** 项目(立项):登记、补全、同步代码、删除。 */

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { FolderKanban, FolderPlus } from "lucide-react";
import { deleteProject, fetchProjects, syncProjectCode, type Project } from "@/lib/api";
import { ApiError } from "@/components/api-error";
import { EmptyState } from "@/components/empty-state";
import { EditProjectDialog } from "@/components/projects/edit-project-dialog";
import { NewProjectDialog } from "@/components/projects/new-project-dialog";
import { ProjectCard } from "@/components/projects/project-card";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "~/vendor/ui/components/ui/alert-dialog";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "~/vendor/views/layout/page-header";

export default function ProjectsPage() {
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Project | null>(null);
  const [deleting, setDeleting] = useState<Project | null>(null);
  const { data: projects = [], isLoading, error } = useQuery({
    queryKey: ["projects"], queryFn: fetchProjects, refetchInterval: 5000,
  });
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["projects"] });
    void queryClient.invalidateQueries({ queryKey: ["board"] });
  };

  const sync = async (project: Project) => {
    const pending = toast.loading(`正在同步 ${project.name} 的代码…`);
    try {
      const result = await syncProjectCode(project.id);
      toast.success(result.detail, { id: pending });
    } catch (err) {
      toast.error((err as Error).message, { id: pending });
    }
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    try {
      await deleteProject(deleting.id);
      toast.success(`已删除 ${deleting.name} 的登记`, { description: "服务器上的代码/数据目录保持原样" });
      refresh();
    } catch (err) {
      toast.error(`删除 ${deleting.name} 失败`, { description: (err as Error).message, duration: 8000 });
    } finally {
      setDeleting(null);
    }
  };

  const newButton = (
    <Button size="sm" className="gap-1.5" onClick={() => setCreating(true)}>
      <FolderPlus className="size-3.5" />立项
    </Button>
  );

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <FolderKanban className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">项目</h1>
        <p className="ml-2 hidden truncate text-caption text-muted-foreground md:block">
          立项:选服务器,放好代码与论文
        </p>
        <div className="ml-auto">{newButton}</div>
      </PageHeader>
      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${PAGE_RAIL} py-4`}>
          {error ? (
            <ApiError error={error} onRetry={refresh} />
          ) : isLoading ? (
            <p className="text-body text-muted-foreground">加载项目…</p>
          ) : projects.length === 0 ? (
            <EmptyState icon={FolderKanban} title="还没有项目"
                        description="选一台 GPU 服务器,把代码放上去(已有目录 / GitHub / zip),再到 Baseline 页让 Agent 跑通基线。"
                        action={newButton} />
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {projects.map((p) => (
                <ProjectCard key={p.id} project={p} onEdit={() => setEditing(p)}
                             onSync={() => void sync(p)} onDelete={() => setDeleting(p)} />
              ))}
            </div>
          )}
        </div>
      </div>
      <AlertDialog open={!!deleting} onOpenChange={(v) => !v && setDeleting(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除 {deleting?.name} 的登记?</AlertDialogTitle>
            <AlertDialogDescription>
              有实验历史的项目会被拒绝删除(保留审计链);服务器上的代码与数据目录不会被删除。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction className="bg-destructive text-white hover:bg-destructive/90"
                               onClick={() => void confirmDelete()}>删除</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <NewProjectDialog open={creating} onClose={() => setCreating(false)} />
      <EditProjectDialog project={editing} onClose={() => setEditing(null)} />
    </div>
  );
}
