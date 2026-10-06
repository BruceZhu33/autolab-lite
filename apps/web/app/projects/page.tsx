"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Bot, CheckCircle2, FileArchive, FileText, FlaskConical, FolderKanban, FolderPlus,
  Link2, Loader2, MoreHorizontal, Pencil, Server as ServerIcon, Trash2,
} from "lucide-react";
import {
  deleteProject, fetchAgents, fetchProjectPapers, fetchProjects, fetchServers,
  patchProject, registerProject, registerProjectZip, requestBaseline,
  uploadProjectPaper, type ProjectListItem,
} from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import { Label } from "~/vendor/ui/components/ui/label";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "~/vendor/ui/components/ui/alert-dialog";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle,
} from "~/vendor/ui/components/ui/dialog";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "~/vendor/ui/components/ui/dropdown-menu";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "@/vendor/views/layout/page-header";
import { EmptyState } from "@/components/empty-state";
import { ApiError } from "@/components/api-error";
import { statusLabelOf } from "@/vendor/views/issues/status";
import { timeAgo } from "@/vendor/views/issues/time";

const STAGE_DOT: Record<string, string> = {
  prepare: "bg-slate-400",
  baseline: "bg-blue-500",
  iterate: "bg-amber-500",
  review: "bg-violet-500",
  confirm: "bg-emerald-500",
  done: "bg-blue-400",
};

function ProjectCard({
  project, onEdit, onBaseline, onDelete,
}: {
  project: ProjectListItem;
  onEdit: (p: ProjectListItem) => void;
  onBaseline: (p: ProjectListItem) => void;
  onDelete: (p: ProjectListItem) => void;
}) {
  const complete = !!project.workdir;
  return (
    <div className="group/card flex flex-col rounded-md border-[0.5px] border-surface-border bg-surface p-4 shadow-[var(--surface-shadow)] transition-colors hover:border-primary/40">
      <div className="flex items-center gap-2">
        <FolderKanban className="size-4 shrink-0 text-muted-foreground" />
        <h3 className="min-w-0 flex-1 truncate text-body font-medium">{project.name}</h3>
        {complete ? (
          <span className="inline-flex shrink-0 items-center gap-1.5 text-caption text-muted-foreground">
            <span className={`size-1.5 rounded-full ${STAGE_DOT[project.stage] ?? "bg-muted-foreground/40"}`} />
            {statusLabelOf(project.stage)}
          </span>
        ) : (
          <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-warning/10 px-1.5 py-0.5 text-micro font-medium text-warning">
            待完善
          </span>
        )}
      </div>
      <div className="mt-2 flex items-center gap-1.5 text-caption text-muted-foreground">
        <ServerIcon className="size-3 shrink-0" />
        <span className="truncate">
          {project.server.name}
          {project.server.transport_mode === "ssh" ? " · SSH" : " · 本机"}
        </span>
      </div>
      <p className="mt-1 truncate font-mono text-micro text-muted-foreground" title={project.workdir}>
        {project.workdir || "未设置工作区(编辑补全)"}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {project.githubUrl ? (
          <span className="inline-flex items-center gap-1 rounded-full bg-muted/60 px-1.5 py-0.5 text-micro text-muted-foreground" title={project.githubUrl}>
            <Link2 className="size-3" />
            GitHub
          </span>
        ) : null}
        <span className={`inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-micro ${project.papersCount > 0 ? "bg-info/10 text-info" : "bg-muted/60 text-muted-foreground/60"}`}>
          <FileText className="size-3" />
          论文 {project.papersCount}
        </span>
        {project.baselineRequestedAt ? (
          <span className="inline-flex items-center gap-1 rounded-full bg-success/10 px-1.5 py-0.5 text-micro font-medium text-success">
            <CheckCircle2 className="size-3" />
            Baseline 已申请
          </span>
        ) : null}
      </div>
      <div className="mt-3 flex items-center justify-between border-t border-surface-border pt-2 text-micro text-muted-foreground">
        <span className="tabular-nums">
          {project.experiments > 0 ? `rev.${project.revision} · ${project.experiments} 次实验` : "尚无实验计划"}
        </span>
        <span>{timeAgo(project.updated_at)}</span>
      </div>
      {project.planId ? (
        <Link
          href={`/plans/${encodeURIComponent(project.planId)}`}
          className="mt-1.5 text-caption text-info hover:underline"
        >
          {project.stage === "prepare" ? "待批准计划 →" : "查看最新计划 →"}
        </Link>
      ) : null}
      <div className="mt-3 flex items-center gap-2">
        <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-caption" onClick={() => onEdit(project)}>
          <Pencil className="size-3" />
          编辑
        </Button>
        <Button
          variant="outline" size="sm" className="h-7 gap-1 px-2 text-caption"
          disabled={!complete}
          title={complete ? undefined : "先补全工作区(GitHub 克隆或本地目录)再提 Baseline"}
          onClick={() => onBaseline(project)}
        >
          <FlaskConical className="size-3" />
          {project.baselineRequestedAt ? "再次申请 Baseline" : "提 Baseline"}
        </Button>
        {/* 删除收进 ⋯ 菜单:与常规操作同权平铺易误触 */}
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button variant="ghost" size="icon-sm" aria-label="更多操作" className="ml-auto">
                <MoreHorizontal className="size-3.5" />
              </Button>
            }
          />
          <DropdownMenuContent align="end">
            <DropdownMenuItem className="text-destructive" onClick={() => onDelete(project)}>
              <Trash2 className="size-3.5" />
              删除项目
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}

function EditProjectDialog({
  project, onClose,
}: { project: ProjectListItem | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [githubUrl, setGithubUrl] = useState("");
  const [workdir, setWorkdir] = useState("");
  const [localWorkspace, setLocalWorkspace] = useState("");
  const [paper, setPaper] = useState<File | null>(null);
  const [saving, setSaving] = useState(false);

  const { data: papers = [] } = useQuery({
    queryKey: ["project-papers", project?.id],
    queryFn: () => fetchProjectPapers(project!.id),
    enabled: !!project,
  });

  const open = !!project;
  const hasWorkdir = !!project?.workdir;
  const hasGithub = !!project?.githubUrl;

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["projects"] });
    void queryClient.invalidateQueries({ queryKey: ["project-papers", project?.id] });
  };

  const save = async () => {
    if (!project) return;
    const patch: { github_url?: string; workdir?: string; local_workspace?: string } = {};
    if (githubUrl.trim() && githubUrl.trim() !== (project.githubUrl ?? "")) {
      patch.github_url = githubUrl.trim();
    }
    if (!hasWorkdir && workdir.trim()) {
      patch.workdir = workdir.trim();
    }
    if (localWorkspace.trim() && localWorkspace.trim() !== (project.localWorkspace ?? "")) {
      patch.local_workspace = localWorkspace.trim();
    }
    if (!hasWorkdir && !workdir.trim() && patch.github_url) {
      // 只补链接且还没有工作区 → 后端会克隆生成工作区
    }
    setSaving(true);
    try {
      if (Object.keys(patch).length > 0) {
        const result = await patchProject(project.id, patch);
        toast.success("项目信息已更新", {
          description: result.research_initialized ? "研究上下文已初始化" : undefined,
        });
      }
      if (paper) {
        try {
          await uploadProjectPaper(project.id, paper);
          toast.success(`论文已上传:${paper.name}`);
        } catch (e) {
          toast.warning(`论文上传失败:${(e as Error).message}`);
        }
      }
      if (Object.keys(patch).length === 0 && !paper) {
        toast.info("没有需要保存的更改");
      }
      refresh();
      onClose();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? null : onClose())}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>编辑 · {project?.name}</DialogTitle>
          <DialogDescription>
            补全缺失信息:{!hasGithub && "GitHub 链接"}
            {!hasGithub && !hasWorkdir ? "、" : ""}
            {!hasWorkdir ? "本地工作区(GitHub 会克隆)" : ""}
            {hasGithub && hasWorkdir ? "论文(可选)" : "、论文(可选)"}。信息确认无误后在卡片上提 Baseline。
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <div className="grid gap-1.5">
            <Label>GitHub 链接{hasGithub ? "(已配置)" : ""}</Label>
            <Input
              defaultValue={project?.githubUrl ?? ""}
              onChange={(e) => setGithubUrl(e.target.value)}
              placeholder={hasGithub ? (project?.githubUrl ?? "") : "https://github.com/user/repo(补链接,无工作区时会克隆)"}
              spellCheck={false}
            />
          </div>
          {hasWorkdir ? (
            <div className="grid gap-1.5">
              <Label>工作区(已配置,只读)</Label>
              <Input value={project.workdir} readOnly className="font-mono text-caption" />
            </div>
          ) : (
            <div className="grid gap-1.5">
              <Label>服务器工作区({project?.server.transport_mode === "ssh" ? "服务器上的绝对路径" : "本机绝对路径"})</Label>
              <Input
                value={workdir}
                onChange={(e) => setWorkdir(e.target.value)}
                placeholder={project?.server.transport_mode === "ssh"
                  ? "/home/serveruser/projects/medseg" : "/Users/juan/projects/medseg"}
                spellCheck={false}
              />
            </div>
          )}
          <div className="grid gap-1.5">
            <Label>本地开发工作区(可选,你 Mac 上的绝对路径;Baseline 同步代码的来源)</Label>
            <Input
              defaultValue={project?.localWorkspace ?? ""}
              onChange={(e) => setLocalWorkspace(e.target.value)}
              placeholder="/Users/juan/projects/medseg"
              spellCheck={false}
            />
          </div>
          <div className="grid gap-1.5">
            <Label>论文 PDF(可选{papers.length > 0 ? `,已有 ${papers.length} 篇` : ""})</Label>
            {papers.length > 0 ? (
              <p className="font-mono text-micro text-muted-foreground">{papers.join(" · ")}</p>
            ) : null}
            <Input
              type="file" accept="application/pdf"
              onChange={(e) => setPaper(e.target.files?.[0] ?? null)}
              className="text-caption"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>取消</Button>
          <Button size="sm" disabled={saving} onClick={() => void save()} className="gap-1.5">
            {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function NewProjectDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [sourceKind, setSourceKind] = useState<"local" | "github" | "zip">("local");
  const [githubUrl, setGithubUrl] = useState("");
  const [serverId, setServerId] = useState("");
  const [workdir, setWorkdir] = useState("");
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [paper, setPaper] = useState<File | null>(null);
  const [initResearch, setInitResearch] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  const { data: servers = [] } = useQuery({
    queryKey: ["servers-for-project"],
    queryFn: fetchServers,
    enabled: open,
  });
  const selected = servers.find((s) => s.id === serverId);

  const submit = async () => {
    if (!name.trim() || !serverId) {
      toast.error("名称与服务器为必填");
      return;
    }
    if (sourceKind === "zip" && !zipFile) {
      toast.error("请选择要上传的 .zip 文件");
      return;
    }
    // 工作目录 / GitHub 链接可后补(编辑对话框补全)
    setSubmitting(true);
    try {
      const result =
        sourceKind === "zip"
          ? await registerProjectZip({
              name: name.trim(),
              server_id: serverId,
              file: zipFile!,
              init_research: initResearch,
            })
          : await registerProject({
              name: name.trim(),
              server_id: serverId,
              workdir: sourceKind === "local" ? workdir.trim() : undefined,
              source: sourceKind === "github" ? { kind: "github", url: githubUrl.trim() } : undefined,
              init_research: initResearch,
            });
      let paperNote = "";
      if (paper) {
        try {
          await uploadProjectPaper(result.project.id, paper);
          paperNote = ",论文已存入 .autolab/papers/";
        } catch (e) {
          toast.warning(`论文上传失败:${(e as Error).message}`);
        }
      }
      toast.success(`已登记 ${result.project.name}`, {
        description: (result.research_initialized
          ? "研究上下文已初始化"
          : "检测到已有 .autolab/,跳过初始化") + paperNote,
      });
      onClose();
      setName(""); setGithubUrl(""); setWorkdir(""); setZipFile(null); setPaper(null);
      void queryClient.invalidateQueries({ queryKey: ["board"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? null : onClose())}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>新建项目</DialogTitle>
          <DialogDescription>
            本地工作区或 GitHub 链接;有论文可一并提交(Agent 评估实验可行性待接入,
            论文先入项目研究上下文)。
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-1.5">
              <Label>项目名称</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="medseg-v2" />
            </div>
            <div className="grid gap-1.5">
              <Label>服务器</Label>
              <select
                className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                value={serverId}
                onChange={(e) => setServerId(e.target.value)}
              >
                <option value="">选择服务器…</option>
                {servers.map((s) => (
                  <option key={s.id} value={s.id}>
                    {`${s.name}(${s.transport_mode === "ssh" ? `SSH · ${s.gpu_slots} GPU` : "本机"})`}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2">
            {(["local", "github", "zip"] as const).map((kind) => (
              <button
                key={kind}
                type="button"
                onClick={() => setSourceKind(kind)}
                className={`flex items-center gap-2 rounded-lg border-[0.5px] px-3 py-2 text-caption font-medium transition-colors ${
                  sourceKind === kind
                    ? "border-primary/50 bg-primary/10 text-foreground"
                    : "border-surface-border bg-surface text-muted-foreground hover:bg-surface-hover"
                }`}
              >
                {kind === "local" ? <FolderKanban className="size-3.5" /> : kind === "github" ? <Link2 className="size-3.5" /> : <FileArchive className="size-3.5" />}
                {kind === "local" ? "本地工作区" : kind === "github" ? "GitHub 链接" : "上传 zip"}
              </button>
            ))}
          </div>
          {sourceKind === "local" ? (
            <div className="grid gap-1.5">
              <Label>项目工作目录({selected?.transport_mode === "ssh" ? "服务器上的绝对路径" : "本机绝对路径"})</Label>
              <Input
                value={workdir}
                onChange={(e) => setWorkdir(e.target.value)}
                placeholder={selected?.transport_mode === "ssh"
                  ? "/home/serveruser/projects/medseg" : "/Users/juan/projects/medseg"}
                spellCheck={false}
              />
            </div>
          ) : sourceKind === "github" ? (
            <div className="grid gap-1.5">
              <Label>GitHub 仓库(克隆到服务器 ~/autolab-projects/)</Label>
              <Input
                value={githubUrl}
                onChange={(e) => setGithubUrl(e.target.value)}
                placeholder="https://github.com/user/repo"
                spellCheck={false}
              />
            </div>
          ) : (
            <div className="grid gap-1.5">
              <Label>项目 zip(解包到服务器 ~/autolab-projects/;服务器连不上 GitHub 时用这个)</Label>
              <Input
                type="file"
                accept=".zip"
                onChange={(e) => setZipFile(e.target.files?.[0] ?? null)}
                className="text-caption"
              />
              {zipFile && (
                <p className="text-micro text-muted-foreground">
                  {zipFile.name} · {(zipFile.size / 1024 / 1024).toFixed(1)} MB
                </p>
              )}
            </div>
          )}
          <div className="grid gap-1.5">
            <Label>论文(可选,PDF,存入项目 .autolab/papers/)</Label>
            <Input
              type="file"
              accept="application/pdf"
              onChange={(e) => setPaper(e.target.files?.[0] ?? null)}
              className="text-caption"
            />
          </div>
          <label className="flex cursor-pointer items-center gap-2 text-caption text-muted-foreground">
            <input
              type="checkbox"
              checked={initResearch}
              onChange={(e) => setInitResearch(e.target.checked)}
              className="size-3.5 accent-[var(--primary)]"
            />
            初始化研究上下文(.autolab/;GitHub 来源在克隆后初始化;已存在则跳过)
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>取消</Button>
          <Button size="sm" disabled={submitting} onClick={() => void submit()} className="gap-1.5">
            {submitting ? <Loader2 className="size-3.5 animate-spin" /> : <FolderPlus className="size-3.5" />}
            登记
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function ProjectsPage() {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<ProjectListItem | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ProjectListItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  const queryClient = useQueryClient();
  const { data: projects = [], isLoading, error } = useQuery({
    queryKey: ["projects"],
    queryFn: fetchProjects,
    refetchInterval: 5000,
  });

  const confirmDelete = async () => {
    const p = deleteTarget;
    if (!p) return;
    setDeleting(true);
    try {
      const r = await deleteProject(p.id);
      toast.success(`已删除 ${r.name || p.name} 的登记`, {
        description: "服务器上的代码/数据目录保持原样",
      });
      setDeleteTarget(null);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["board"] });
    } catch (e) {
      const err = e as Error & { code?: string };
      if (err.code === "HAS_REFERENCES") {
        toast.error(`${p.name} 有训练历史,不可删除`, {
          description: `${err.message}(审计链约束:有运行记录的项目保留)`,
          duration: 8000,
        });
      } else {
        toast.error(`删除 ${p.name} 失败`, {
          description: err.message.slice(0, 300),
          duration: 8000,
        });
      }
    } finally {
      setDeleting(false);
    }
  };

  const onBaseline = async (project: ProjectListItem) => {
    // 再次申请 = 重置通过事实重走(历史运行数据保留);给出确认门防误触
    if (project.baselineRequestedAt
        && !window.confirm(
            "再次申请 Baseline 将重置通过事实:项目回到 Baseline 列重走"
            + "(历史运行数据保留,方案采纳等不受影响)。继续?")) {
      return;
    }
    try {
      const result = await requestBaseline(project.id);
      toast.success(
        result?.reset
          ? `已重置 ${project.name} 的 Baseline:项目回到 Baseline 列`
          : `已为 ${project.name} 提交 Baseline 申请`, {
        description: result?.reset
          ? "到 Baseline 页重跑(就绪项大多直接通过);完成后重新确认通过"
          : "项目进入 Baseline 阶段:Agent 将检查服务器工作区与数据集(缺失会提醒你补),并先跑通一遍基线",
      });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["board"] });
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <FolderKanban className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">项目</h1>
        {projects.length > 0 ? (
          <span className="font-mono text-caption tabular-nums text-muted-foreground">
            {projects.length}
          </span>
        ) : null}
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          本地工作区 / GitHub 链接 · 论文提交 · Agent 评估后进入 Baseline
        </p>
        <div className="ml-auto">
          <Button size="sm" className="gap-1.5" onClick={() => setOpen(true)}>
            <FolderPlus className="size-3.5" />
            新建项目
          </Button>
        </div>
      </PageHeader>

      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${PAGE_RAIL} flex min-h-[70vh] flex-col py-4`}>
          {error ? (
            <ApiError
              error={error}
              onRetry={() => void queryClient.invalidateQueries({ queryKey: ["projects"] })}
            />
          ) : isLoading ? (
            <p className="text-body text-muted-foreground">加载项目…</p>
          ) : projects.length === 0 ? (
            <EmptyState
              icon={FolderKanban}
              title="还没有项目"
              description="登记本地工作区或 GitHub 链接,提交论文;Agent 评估通过后进入 Baseline,在数据集上跑出原模型指标。"
              action={
                <Button size="sm" className="gap-1.5" onClick={() => setOpen(true)}>
                  <FolderPlus className="size-3.5" />
                  新建项目
                </Button>
              }
            />
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {projects.map((project) => (
                <ProjectCard
                  key={project.id}
                  project={project}
                  onEdit={setEditing}
                  onBaseline={(p) => void onBaseline(p)}
                  onDelete={setDeleteTarget}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      <AlertDialog open={!!deleteTarget} onOpenChange={(v) => !v && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除 {deleteTarget?.name} 的登记？</AlertDialogTitle>
            <AlertDialogDescription>
              将移除项目登记及其计划、提案、循环、会话等记录。有训练历史的项目会被拒绝删除
              （审计链约束）；服务器上的代码与数据目录不会被删除。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-white hover:bg-destructive/90"
              disabled={deleting}
              onClick={() => void confirmDelete()}
            >
              删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <NewProjectDialog open={open} onClose={() => setOpen(false)} />
      <EditProjectDialog project={editing} onClose={() => setEditing(null)} />
    </div>
  );
}
