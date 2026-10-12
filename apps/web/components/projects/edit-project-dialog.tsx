"use client";

/** 补全项目:缺工作区时给目录或 GitHub 链接;本机代码目录(同步来源);追加论文。 */

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { FolderOpen, Loader2 } from "lucide-react";
import { fetchPapers, updateProject, uploadPaper, type Project } from "@/lib/api";
import { FolderBrowserDialog } from "@/components/folder-browser";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import { Label } from "~/vendor/ui/components/ui/label";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "~/vendor/ui/components/ui/dialog";

export function EditProjectDialog({ project, onClose }: { project: Project | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [githubUrl, setGithubUrl] = useState("");
  const [workdir, setWorkdir] = useState("");
  const [localWorkspace, setLocalWorkspace] = useState("");
  const [paper, setPaper] = useState<File | null>(null);
  const [saving, setSaving] = useState(false);
  const [picker, setPicker] = useState<null | "workdir" | "local">(null);

  const { data: papers = [] } = useQuery({
    queryKey: ["papers", project?.id], queryFn: () => fetchPapers(project!.id),
    enabled: !!project?.workdir,
  });

  const save = async () => {
    if (!project) return;
    const patch: { github_url?: string; workdir?: string; local_workspace?: string } = {};
    if (githubUrl.trim() && githubUrl.trim() !== (project.githubUrl ?? "")) patch.github_url = githubUrl.trim();
    if (!project.workdir && workdir.trim()) patch.workdir = workdir.trim();
    if (localWorkspace.trim() && localWorkspace.trim() !== (project.localWorkspace ?? "")) {
      patch.local_workspace = localWorkspace.trim();
    }
    setSaving(true);
    try {
      if (Object.keys(patch).length) await updateProject(project.id, patch);
      if (paper) await uploadPaper(project.id, paper);
      toast.success(Object.keys(patch).length || paper ? "已保存" : "没有需要保存的更改");
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["papers", project.id] });
      onClose();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Dialog open={!!project} onOpenChange={(next) => (next ? null : onClose())}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>编辑 · {project?.name}</DialogTitle>
            <DialogDescription>补全工作区、设置本机代码目录(同步来源)、追加论文。</DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <div className="grid gap-1.5">
              <Label>GitHub 链接{project?.githubUrl ? "(已配置)" : "(没有工作区时会克隆)"}</Label>
              <Input defaultValue={project?.githubUrl ?? ""} spellCheck={false}
                     onChange={(e) => setGithubUrl(e.target.value)} placeholder="https://github.com/user/repo" />
            </div>
            {project?.workdir ? (
              <div className="grid gap-1.5">
                <Label>工作区</Label>
                <Input value={project.workdir} readOnly className="font-mono text-caption" />
              </div>
            ) : (
              <div className="grid gap-1.5">
                <Label>服务器工作区(绝对路径)</Label>
                <div className="flex gap-2">
                  <Input value={workdir} onChange={(e) => setWorkdir(e.target.value)} spellCheck={false} />
                  <Button variant="secondary" size="icon" title="浏览目录" onClick={() => setPicker("workdir")}>
                    <FolderOpen className="size-4" />
                  </Button>
                </div>
              </div>
            )}
            <div className="grid gap-1.5">
              <Label>本机代码目录(可选:你电脑上的路径,「同步代码」从这里打包上传)</Label>
              <div className="flex gap-2">
                <Input defaultValue={project?.localWorkspace ?? ""} spellCheck={false}
                       onChange={(e) => setLocalWorkspace(e.target.value)} />
                <Button variant="secondary" size="icon" title="浏览本机目录" onClick={() => setPicker("local")}>
                  <FolderOpen className="size-4" />
                </Button>
              </div>
            </div>
            <div className="grid gap-1.5">
              <Label>论文 PDF{papers.length ? `(已有:${papers.join("、")})` : ""}</Label>
              <Input type="file" accept="application/pdf" className="text-caption"
                     disabled={!project?.workdir}
                     onChange={(e) => setPaper(e.target.files?.[0] ?? null)} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={onClose}>取消</Button>
            <Button size="sm" disabled={saving} onClick={() => void save()} className="gap-1.5">
              {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <FolderBrowserDialog
        open={picker !== null} onOpenChange={(v) => (v ? null : setPicker(null))}
        source={picker === "local" ? { kind: "local" } : { kind: "server", serverId: project?.server.id ?? "" }}
        title={picker === "local" ? "选择本机代码目录" : "选择服务器工作区"}
        onPick={(path) => {
          if (picker === "local") setLocalWorkspace(path);
          else setWorkdir(path);
          setPicker(null);
        }} />
    </>
  );
}
