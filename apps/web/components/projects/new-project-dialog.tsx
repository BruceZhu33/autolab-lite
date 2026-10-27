"use client";

/** 立项:选服务器 + 代码来源(服务器上已有目录 / GitHub 克隆 / 上传 zip / 从现有项目分叉)+ 可选论文。 */

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { FileArchive, FolderKanban, FolderOpen, FolderPlus, GitFork, Link2, Loader2 } from "lucide-react";
import {
  createProject, createProjectFromZip, fetchBaselineVersions, fetchProjects,
  fetchServerOptions, forkProject, uploadPaper,
} from "@/lib/api";
import { FolderBrowserDialog } from "@/components/folder-browser";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import { Label } from "~/vendor/ui/components/ui/label";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "~/vendor/ui/components/ui/dialog";

type SourceKind = "dir" | "github" | "zip" | "fork";

const SOURCES: Array<{ kind: SourceKind; label: string; icon: typeof FolderKanban }> = [
  { kind: "dir", label: "服务器上已有目录", icon: FolderKanban },
  { kind: "github", label: "GitHub 克隆", icon: Link2 },
  { kind: "zip", label: "上传 zip", icon: FileArchive },
  { kind: "fork", label: "从现有项目分叉", icon: GitFork },
];

export function NewProjectDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [serverId, setServerId] = useState("");
  const [kind, setKind] = useState<SourceKind>("dir");
  const [workdir, setWorkdir] = useState("");
  const [githubUrl, setGithubUrl] = useState("");
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [paper, setPaper] = useState<File | null>(null);
  const [forkSource, setForkSource] = useState("");
  const [forkVersion, setForkVersion] = useState("oldest");
  const [submitting, setSubmitting] = useState(false);
  const [picking, setPicking] = useState(false);

  const { data: servers = [] } = useQuery({
    queryKey: ["server-options"], queryFn: fetchServerOptions, enabled: open,
  });
  const server = servers.find((s) => s.id === serverId);
  const { data: allProjects = [] } = useQuery({
    queryKey: ["projects"], queryFn: fetchProjects, enabled: open && kind === "fork",
  });
  const forkable = allProjects.filter((p) => p.baselinePassedAt);
  const { data: versionsData } = useQuery({
    queryKey: ["baseline-versions", forkSource],
    queryFn: () => fetchBaselineVersions(forkSource),
    enabled: open && kind === "fork" && !!forkSource,
  });

  const reset = () => {
    setName(""); setWorkdir(""); setGithubUrl(""); setZipFile(null); setPaper(null);
    setForkSource(""); setForkVersion("oldest");
  };

  const submit = async () => {
    if (!name.trim()) {
      toast.error("名称必填");
      return;
    }
    if (kind === "fork") {
      if (!forkSource) {
        toast.error("请选择要分叉的源项目");
        return;
      }
      setSubmitting(true);
      try {
        const { project, code_commit: commit } = await forkProject({
          source_project_id: forkSource, name: name.trim(),
          baseline_version: forkVersion || "oldest",
        });
        toast.success(`已分叉 ${project.name}`, {
          description: `工作区 ${project.workdir}(代码 ${commit.slice(0, 8)});数据/权重与源项目共享,基线已继承,可直接开研究`,
        });
        reset();
        onClose();
        void queryClient.invalidateQueries({ queryKey: ["projects"] });
        void queryClient.invalidateQueries({ queryKey: ["board"] });
      } catch (err) {
        toast.error((err as Error).message);
      } finally {
        setSubmitting(false);
      }
      return;
    }
    if (!serverId) {
      toast.error("名称与服务器为必填");
      return;
    }
    if (kind === "zip" && !zipFile) {
      toast.error("请选择要上传的 .zip 文件");
      return;
    }
    setSubmitting(true);
    try {
      const { project } = kind === "zip"
        ? await createProjectFromZip(name.trim(), serverId, zipFile!)
        : await createProject(kind === "github"
          ? { name: name.trim(), server_id: serverId, source: { kind: "github", url: githubUrl.trim() } }
          : { name: name.trim(), server_id: serverId, source: { kind: "dir", path: workdir.trim() } });
      if (paper) {
        try {
          await uploadPaper(project.id, paper);
        } catch (err) {
          toast.warning(`论文上传失败:${(err as Error).message}`);
        }
      }
      toast.success(`已立项 ${project.name}`, {
        description: project.workdir ? `工作区 ${project.workdir}` : "工作区稍后在编辑里补全",
      });
      reset();
      onClose();
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["board"] });
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Dialog open={open} onOpenChange={(next) => (next ? null : onClose())}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>立项</DialogTitle>
            <DialogDescription>
              {kind === "fork"
                ? "从现有项目的一个基线版本开新方向:代码 worktree 检出(数据/权重共享),基线整份继承,研究记录从零开始。"
                : "把代码放到 GPU 服务器上;论文可一并上传(研究阶段 Agent 会读)。"}
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <div className="grid grid-cols-2 gap-3">
              <div className="grid gap-1.5">
                <Label>项目名称</Label>
                <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="medseg-v2" />
              </div>
              {kind !== "fork" ? (
                <div className="grid gap-1.5">
                  <Label>服务器</Label>
                  <select className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                          value={serverId} onChange={(e) => setServerId(e.target.value)}>
                    <option value="">选择服务器…</option>
                    {servers.map((s) => (
                      <option key={s.id} value={s.id}>
                        {`${s.name}(${s.transport_mode === "ssh" ? `SSH · ${s.gpu_slots} GPU` : "本机"})`}
                      </option>
                    ))}
                  </select>
                </div>
              ) : (
                <div className="grid gap-1.5">
                  <Label>源项目</Label>
                  <select className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                          value={forkSource}
                          onChange={(e) => { setForkSource(e.target.value); setForkVersion("oldest"); }}>
                    <option value="">选择要分叉的项目…</option>
                    {forkable.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </div>
              )}
            </div>
            <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
              {SOURCES.map((s) => (
                <button key={s.kind} type="button" onClick={() => setKind(s.kind)}
                        className={`flex items-center gap-2 rounded-lg border-[0.5px] px-3 py-2 text-caption font-medium transition-colors ${
                          kind === s.kind ? "border-primary/50 bg-primary/10 text-foreground"
                            : "border-surface-border bg-surface text-muted-foreground hover:bg-surface-hover"}`}>
                  <s.icon className="size-3.5 shrink-0" />{s.label}
                </button>
              ))}
            </div>
            {kind === "dir" ? (
              <div className="grid gap-1.5">
                <Label>工作目录({server?.transport_mode === "ssh" ? "服务器上的绝对路径" : "本机绝对路径"})</Label>
                <div className="flex gap-2">
                  <Input value={workdir} onChange={(e) => setWorkdir(e.target.value)} spellCheck={false}
                         placeholder="/home/you/projects/medseg" />
                  <Button variant="secondary" size="icon" disabled={!serverId}
                          title={serverId ? "浏览目录" : "先选择服务器"} onClick={() => setPicking(true)}>
                    <FolderOpen className="size-4" />
                  </Button>
                </div>
              </div>
            ) : kind === "github" ? (
              <div className="grid gap-1.5">
                <Label>GitHub 仓库(克隆到服务器 ~/autolab-projects/)</Label>
                <Input value={githubUrl} onChange={(e) => setGithubUrl(e.target.value)} spellCheck={false}
                       placeholder="https://github.com/user/repo" />
              </div>
            ) : kind === "fork" ? (
              <div className="grid gap-1.5">
                <Label>从哪个基线版本出发</Label>
                <select className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                        value={forkVersion} onChange={(e) => setForkVersion(e.target.value)}
                        disabled={!forkSource}>
                  <option value="oldest">最初的基线(换大方向用)</option>
                  {(versionsData?.versions ?? []).map((v) => (
                    <option key={v.key} value={v.key}>
                      {v.label}{v.passed_at ? ` · ${v.passed_at.slice(0, 10)}` : ""}
                      ({v.datasets.length} 数据集)
                    </option>
                  ))}
                </select>
                <p className="text-micro text-muted-foreground">
                  分叉后源项目原样不动;两个方向的实验在同一个 GPU 队列里自动排队。
                </p>
              </div>
            ) : (
              <div className="grid gap-1.5">
                <Label>项目 zip(解包到服务器 ~/autolab-projects/;服务器连不上 GitHub 时用)</Label>
                <Input type="file" accept=".zip" className="text-caption"
                       onChange={(e) => setZipFile(e.target.files?.[0] ?? null)} />
              </div>
            )}
            {kind !== "fork" ? (
              <div className="grid gap-1.5">
                <Label>论文(可选,PDF)</Label>
                <Input type="file" accept="application/pdf" className="text-caption"
                       onChange={(e) => setPaper(e.target.files?.[0] ?? null)} />
              </div>
            ) : null}
          </div>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={onClose}>取消</Button>
            <Button size="sm" disabled={submitting} onClick={() => void submit()} className="gap-1.5">
              {submitting ? <Loader2 className="size-3.5 animate-spin" /> : null}
              {kind === "fork" ? <GitFork className="size-3.5" /> : <FolderPlus className="size-3.5" />}
              {kind === "fork" ? "分叉" : "立项"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <FolderBrowserDialog open={picking} onOpenChange={setPicking} initialPath={workdir || undefined}
                           source={{ kind: "server", serverId }} title="选择项目工作目录"
                           onPick={(path) => { setWorkdir(path); setPicking(false); }} />
    </>
  );
}
