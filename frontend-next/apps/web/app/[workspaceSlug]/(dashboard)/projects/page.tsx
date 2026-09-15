"use client";

/**
 * 科研项目管理（Projects，Work 分组）。
 *
 * 新建项目 = 选择本地代码工作区（BASE 来源）+ 可选论文；创建后项目
 * 进入看板「准备」列，工作台内确认原始代码库信息，通过后进入方案评审。
 * Phase 1 走 fixture（/api/projects 内存态）；Phase 2 对接 sidecar
 * POST /api/v1/projects（base_source_kind=local + base_source_ref）与
 * papers 域，文件选择器由 Tauri 原生提供（v0.3 Phase F）。
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderKanban } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { toast } from "sonner";

import { Badge } from "@multica/ui/components/ui/badge";
import { Button } from "@multica/ui/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@multica/ui/components/ui/dialog";
import { Input } from "@multica/ui/components/ui/input";
import { Label } from "@multica/ui/components/ui/label";
import { PageHeader } from "@multica/views/layout/page-header";

interface ProjectView {
  id: string;
  identifier: string;
  title: string;
  stage: string;
  priority: string;
  direction: string;
  workspacePath: string;
  paper: string | null;
  codebase: { files: number; languages: string; entry: string; readmeKb: number } | null;
  createdAt: string;
  updatedAt: string;
}

/** 与 fixture 六阶段目录（issue-statuses）同色。 */
const STAGE_STYLE: Record<string, { label: string; color: string }> = {
  prepare: { label: "准备", color: "#94a3b8" },
  review: { label: "方案评审", color: "#8b5cf6" },
  baseline: { label: "基线验证", color: "#0ea5e9" },
  iterate: { label: "自动迭代", color: "#f59e0b" },
  confirm: { label: "最终确认", color: "#ec4899" },
  reported: { label: "已完成", color: "#10b981" },
};

const EMPTY_FORM = { name: "", direction: "", workspacePath: "", paper: "" };

function stageBadge(stage: string) {
  const s = STAGE_STYLE[stage] ?? { label: stage, color: "#94a3b8" };
  return (
    <Badge variant="outline" className="gap-1.5">
      <span className="size-1.5 rounded-full" style={{ backgroundColor: s.color }} />
      {s.label}
    </Badge>
  );
}

async function fixtureApi<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const e = (await r.json().catch(() => ({}))) as { error?: string };
    throw new Error(typeof e.error === "string" ? e.error : `HTTP ${r.status}`);
  }
  return r.json() as Promise<T>;
}

export default function ProjectsPage() {
  const qc = useQueryClient();
  const pathname = usePathname();
  // 工作台链接跟随当前 workspace slug（fixture 模式为 autolab）
  const issueHref = (id: string) => `/${pathname.split("/")[1] ?? "autolab"}/issues/${id}`;
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);

  const { data, isPending, isError, error } = useQuery({
    queryKey: ["autolab", "fixture-projects"],
    queryFn: () => fixtureApi<{ projects: ProjectView[] }>("GET", "/api/projects"),
  });

  const create = useMutation({
    mutationFn: (input: typeof EMPTY_FORM) =>
      fixtureApi<ProjectView>("POST", "/api/projects", input),
    onSuccess: (p) => {
      toast.success(`项目 ${p.identifier} 已创建`, {
        description: "已加入看板「准备」列；进入工作台确认原始代码库信息。",
      });
      setOpen(false);
      setForm(EMPTY_FORM);
      void qc.invalidateQueries({ queryKey: ["autolab", "fixture-projects"] });
      // Multica QueryProvider 用 staleTime: Infinity，真实产品靠 WS 失效通知
      // 刷新看板；fixture 模式没有 WS，创建后主动失效全量查询（内存数据源，
      // 代价为零）。Phase 2 接 sidecar WS 后移除。
      void qc.invalidateQueries();
    },
    onError: (e: Error) => {
      toast.error("创建失败", { description: e.message.slice(0, 300) });
    },
  });

  const submit = () => {
    if (!form.name.trim()) {
      toast.error("项目名称必填");
      return;
    }
    if (!form.workspacePath.trim()) {
      toast.error("本地代码工作区路径必填");
      return;
    }
    create.mutate(form);
  };

  const projects = data?.projects ?? [];

  return (
    <>
      <PageHeader>
        <FolderKanban className="size-4 text-muted-foreground" />
        <h1 className="text-body font-medium">科研项目</h1>
        {projects.length > 0 ? (
          <span className="font-mono text-caption tabular-nums text-muted-foreground">
            {projects.length}
          </span>
        ) : null}
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          新建项目（本地工作区作为 BASE · 论文可选）→ 准备 → 方案评审
        </p>
        <div className="ml-auto">
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger render={<Button size="sm">＋ 新建项目</Button>} />
            <DialogContent className="sm:max-w-lg">
              <DialogHeader>
                <DialogTitle>新建科研项目</DialogTitle>
                <DialogDescription>
                  选择本地代码工作区作为迭代优化的 BASE；论文可选（登记后进入方案评审的对照材料）。
                </DialogDescription>
              </DialogHeader>
              <div className="grid gap-3">
                <div className="grid gap-1.5">
                  <Label>项目名称 *</Label>
                  <Input
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder="mynet-clinic · 自适应网络"
                  />
                </div>
                <div className="grid gap-1.5">
                  <Label>研究方向（一句话）</Label>
                  <Input
                    value={form.direction}
                    onChange={(e) => setForm({ ...form, direction: e.target.value })}
                    placeholder="在 clinic 数据集上复现并改进自适应训练"
                  />
                </div>
                <div className="grid gap-1.5">
                  <Label>本地代码工作区 *</Label>
                  <Input
                    value={form.workspacePath}
                    onChange={(e) => setForm({ ...form, workspacePath: e.target.value })}
                    placeholder="/Users/you/work/mynet"
                    className="font-mono text-sm"
                  />
                  <p className="text-caption text-muted-foreground">
                    创建后进入「准备」：确认代码库信息无误即过准备门。原生目录选择器随 Tauri 壳提供。
                  </p>
                </div>
                <div className="grid gap-1.5">
                  <Label>论文（可选）</Label>
                  <Input
                    value={form.paper}
                    onChange={(e) => setForm({ ...form, paper: e.target.value })}
                    placeholder="arXiv 链接或标题"
                  />
                </div>
              </div>
              <DialogFooter>
                <Button onClick={submit} disabled={create.isPending}>创建</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </PageHeader>

      <div className="p-4 md:px-6">
        {isPending && <p className="text-sm text-muted-foreground">加载项目列表…</p>}
        {isError && (
          <p className="text-sm text-destructive">
            加载失败：{error instanceof Error ? error.message : String(error)}
          </p>
        )}
        {!isPending && projects.length === 0 && (
          <p className="text-sm text-muted-foreground">
            尚无科研项目。新建一个：本地工作区作为 BASE，论文可选。
          </p>
        )}
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {projects.map((p) => (
            <Link
              key={p.id}
              href={issueHref(p.id)}
              className="group space-y-3 rounded-lg border p-4 transition-colors hover:bg-muted/40"
            >
              <div className="flex items-center gap-2">
                {stageBadge(p.stage)}
                <span className="font-mono text-caption text-muted-foreground">{p.identifier}</span>
                <span className="ml-auto font-mono text-caption text-muted-foreground">
                  {new Date(p.createdAt).toLocaleDateString("zh-CN")}
                </span>
              </div>
              <div>
                <p className="text-body font-medium group-hover:underline">{p.title}</p>
                {p.direction && (
                  <p className="mt-1 line-clamp-2 text-caption text-muted-foreground">{p.direction}</p>
                )}
              </div>
              <div className="space-y-1 border-t pt-2 font-mono text-caption text-muted-foreground">
                <p className="truncate">{p.workspacePath || "—"}</p>
                {p.codebase && (
                  <p>
                    {p.codebase.files} 文件 · {p.codebase.entry} · README {p.codebase.readmeKb} KB
                  </p>
                )}
              </div>
              {p.paper && (
                <p className="line-clamp-1 text-caption text-muted-foreground">📄 {p.paper}</p>
              )}
            </Link>
          ))}
        </div>
      </div>
    </>
  );
}
