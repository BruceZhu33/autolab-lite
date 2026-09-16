"use client";

/**
 * 科研项目管理（Projects，Work 分组）——准备阶段的工作现场。
 *
 * 对齐口径 2026-09-15：看板「准备」卡片点击进入本页并选中该项目；
 * 双栏 = 左项目列表 + 右选中项目详情（代码库核对 / 补充材料 / 提审 /
 * 内嵌时间线）。创建项目（本地工作区 BASE + 可选论文）后项目进入
 * 看板「准备」列。
 * Phase 1 走 fixture（/api/projects 内存态）；Phase 2 对接 sidecar
 * POST /api/v1/projects（base_source_kind=local + base_source_ref）与
 * papers 域，文件选择器由 Tauri 原生提供（v0.3 Phase F）。
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderKanban } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
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
import { ProjectTimeline } from "@/features/autolab/project-timeline";
import { stageColor, stageLabel } from "@/features/autolab/stage";
import { getServer, listServers } from "@autolab/research/sidecar/servers";

interface ProjectView {
  id: string;
  identifier: string;
  title: string;
  stage: string;
  priority: string;
  sourceKind: "local" | "github";
  direction: string;
  workspacePath: string;
  paper: string | null;
  codebase: { files: number; languages: string; entry: string; readmeKb: number } | null;
  createdAt: string;
  updatedAt: string;
}

const EMPTY_FORM = {
  name: "",
  direction: "",
  sourceKind: "local" as "local" | "github",
  workspacePath: "",
  paper: "",
  // 真实立项（sidecar 模式）扩展字段；fixture 模式忽略
  dataPath: "",
  gpuIds: [] as string[],
  budgetHours: "10",
  trainCommand: "python train.py",
  evalCommand: "python eval.py",
  condaEnv: "",
};

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

function StageBadge({ stage }: { stage: string }) {
  return (
    <Badge variant="outline" className="gap-1.5">
      <span className="size-1.5 rounded-full" style={{ backgroundColor: stageColor(stage) }} />
      {stageLabel(stage)}
    </Badge>
  );
}

/** GPU 卡池勾选（真实立项：读取 sidecar 服务器注册表）。 */
function GpuPoolPicker({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const { data: servers } = useQuery({
    queryKey: ["autolab", "servers"],
    queryFn: async () => {
      const list = await listServers();
      return Promise.all(list.map((s) => getServer(s.id)));
    },
    staleTime: 30_000,
  });
  const gpus =
    servers?.flatMap((s) =>
      (s.gpus ?? []).map((g) => ({ id: g.id, label: `${s.name} · #${g.gpu_index} ${g.model_name}` })),
    ) ?? [];
  if (gpus.length === 0) {
    return (
      <p className="text-caption text-muted-foreground">
        无已注册 GPU（先在 GPU 服务器页注册）。
      </p>
    );
  }
  return (
    <div className="grid max-h-36 gap-1 overflow-y-auto rounded-md border p-2">
      {gpus.map((g) => {
        const on = selected.includes(g.id);
        return (
          <label key={g.id} className="flex cursor-pointer items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={on}
              onChange={() =>
                onChange(on ? selected.filter((x) => x !== g.id) : [...selected, g.id])
              }
            />
            <span className="truncate font-mono text-caption">{g.label}</span>
          </label>
        );
      })}
    </div>
  );
}

export default function ProjectsPage() {
  const qc = useQueryClient();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const wsSlug = pathname.split("/")[1] ?? "autolab";
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);

  const { data, isPending, isError, error } = useQuery({
    queryKey: ["autolab", "fixture-projects"],
    queryFn: () => fixtureApi<{ projects: ProjectView[] }>("GET", "/api/projects"),
  });

  const projects = useMemo(() => data?.projects ?? [], [data]);

  // 看板卡片路由带 ?project={id}；本地选择优先，列表变化后兜底选第一个
  const fromUrl = searchParams.get("project");
  useEffect(() => {
    if (fromUrl && projects.some((p) => p.id === fromUrl)) setSelectedId(fromUrl);
  }, [fromUrl, projects]);
  useEffect(() => {
    if (!fromUrl && projects.length > 0 && (selectedId === null || !projects.some((p) => p.id === selectedId))) {
      setSelectedId(projects[0]?.id ?? null);
    }
  }, [projects, fromUrl, selectedId]);
  const selected = projects.find((p) => p.id === selectedId) ?? null;

  const { data: cfg } = useQuery({
    queryKey: ["autolab", "config"],
    queryFn: () => fetch("/api/autolab-config", { cache: "no-store" }).then((r) => r.json() as Promise<{ data_source?: string }>),
    staleTime: Infinity,
  });
  const realMode = cfg?.data_source === "sidecar";

  const create = useMutation({
    mutationFn: (input: typeof EMPTY_FORM) =>
      fixtureApi<ProjectView>("POST", "/api/projects", input),
    onSuccess: (p) => {
      toast.success(`项目 ${p.identifier} 已创建`, {
        description: "已加入看板「准备」列；在右侧核对代码库信息后开始基线验证。",
      });
      setOpen(false);
      setForm(EMPTY_FORM);
      setSelectedId(p.id);
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
      toast.error(form.sourceKind === "github" ? "GitHub 仓库链接必填" : "本地代码工作区路径必填");
      return;
    }
    if (realMode) {
      // 真实立项（sidecar）必需字段；fixture 演示模式不强制
      if (!form.dataPath.trim()) {
        toast.error("数据集目录必填（真实立项：登记数据清单与副本）");
        return;
      }
      if (form.gpuIds.length === 0) {
        toast.error("GPU 卡池至少选择一块（真实立项绑定执行机）");
        return;
      }
    }
    create.mutate(form);
  };

  /** 准备门：确认代码库信息 → 进入基线验证（Phase 4 改接 sidecar availableActions）。 */
  const submitReview = async () => {
    if (!selected) return;
    try {
      await fixtureApi("PUT", `/api/issues/${selected.id}`, { status: "baseline" });
      toast.success(`${selected.identifier} 已提交基线验证`, {
        description: "BASE（原始代码）派发后，验收通过即可带着基线数字进方案评审。",
      });
      void qc.invalidateQueries();
    } catch (e) {
      toast.error("提审失败", {
        description: (e instanceof Error ? e.message : String(e)).slice(0, 300),
      });
    }
  };

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
          新建项目（本地工作区作为 BASE · 论文可选）→ 核对代码库 → 基线验证
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
                  <Label>BASE 来源 *</Label>
                  <div className="flex gap-1.5">
                    <Button size="sm" variant={form.sourceKind === "local" ? "default" : "outline"}
                            onClick={() => setForm({ ...form, sourceKind: "local" })}>📁 本地工作区</Button>
                    <Button size="sm" variant={form.sourceKind === "github" ? "default" : "outline"}
                            onClick={() => setForm({ ...form, sourceKind: "github" })}>🐙 GitHub</Button>
                  </div>
                  <Input
                    value={form.workspacePath}
                    onChange={(e) => setForm({ ...form, workspacePath: e.target.value })}
                    placeholder={form.sourceKind === "local" ? "/Users/you/work/mynet" : "https://github.com/you/mynet"}
                    className="font-mono text-sm"
                  />
                  <p className="text-caption text-muted-foreground">
                    {form.sourceKind === "local"
                      ? "创建后进入「准备」：确认代码库信息后开始基线验证。原生目录选择器随 Tauri 壳提供。"
                      : "GitHub 来源在基线导入时走 git clone（sidecar deviation #8 后置，当前 fixture 先行）。"}
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
                {/* 真实立项字段（sidecar 模式生效；fixture 忽略） */}
                <div className="grid gap-1.5">
                  <Label>数据集目录 *（真实立项）</Label>
                  <Input
                    value={form.dataPath}
                    onChange={(e) => setForm({ ...form, dataPath: e.target.value })}
                    placeholder="/Users/you/work/mynet/data"
                    className="font-mono text-sm"
                  />
                </div>
                <div className="grid gap-1.5">
                  <Label>GPU 卡池 *（真实立项）</Label>
                  <GpuPoolPicker
                    selected={form.gpuIds}
                    onChange={(gpuIds) => setForm({ ...form, gpuIds })}
                  />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div className="grid gap-1.5">
                    <Label>预算（GPU·h）</Label>
                    <Input
                      value={form.budgetHours}
                      onChange={(e) => setForm({ ...form, budgetHours: e.target.value })}
                      inputMode="numeric"
                    />
                  </div>
                  <div className="grid gap-1.5">
                    <Label>conda 环境（可选）</Label>
                    <Input
                      value={form.condaEnv}
                      onChange={(e) => setForm({ ...form, condaEnv: e.target.value })}
                      placeholder="复用服务器现成环境（deviation #14）"
                    />
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div className="grid gap-1.5">
                    <Label>训练命令</Label>
                    <Input
                      value={form.trainCommand}
                      onChange={(e) => setForm({ ...form, trainCommand: e.target.value })}
                      className="font-mono text-sm"
                    />
                  </div>
                  <div className="grid gap-1.5">
                    <Label>评测命令</Label>
                    <Input
                      value={form.evalCommand}
                      onChange={(e) => setForm({ ...form, evalCommand: e.target.value })}
                      className="font-mono text-sm"
                    />
                  </div>
                </div>
              </div>
              <DialogFooter>
                <Button onClick={submit} disabled={create.isPending}>创建</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </PageHeader>

      <div className="flex min-h-0 flex-1 gap-4 p-4 md:px-6">
        {/* 左栏：项目列表 */}
        <div className="hidden w-72 shrink-0 space-y-1.5 overflow-y-auto md:block">
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
          {projects.map((p) => (
            <button
              key={p.id}
              onClick={() => setSelectedId(p.id)}
              className={`flex w-full flex-col gap-1 rounded-lg border p-3 text-left transition-colors ${
                p.id === selectedId ? "border-primary/60 bg-muted/50" : "hover:bg-muted/40"
              }`}
            >
              <span className="flex items-center gap-2">
                <span className="size-1.5 shrink-0 rounded-full" style={{ backgroundColor: stageColor(p.stage) }} />
                <span className="font-mono text-caption text-muted-foreground">{p.identifier}</span>
                <span className="ml-auto text-caption text-muted-foreground">{stageLabel(p.stage)}</span>
              </span>
              <span className="truncate text-sm font-medium">{p.title}</span>
            </button>
          ))}
        </div>

        {/* 右栏：选中项目详情（准备阶段工作现场） */}
        {!selected ? (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-sm text-muted-foreground">选择左侧项目查看详情</p>
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto lg:flex-row">
            <div className="flex min-w-0 flex-1 flex-col gap-4">
              {/* 标题区 */}
              <div className="flex flex-wrap items-center gap-3">
                <StageBadge stage={selected.stage} />
                <span className="font-mono text-caption text-muted-foreground">{selected.identifier}</span>
                <h2 className="text-body font-medium">{selected.title}</h2>
                {selected.stage === "baseline" && (
                  <Link
                    href={`/${wsSlug}/baseline/${selected.id}`}
                    className="ml-auto text-caption text-primary hover:underline"
                  >
                    前往基线验证 →
                  </Link>
                )}
                {selected.stage === "review" && (
                  <Link
                    href={`/${wsSlug}/review/${selected.id}`}
                    className="ml-auto text-caption text-primary hover:underline"
                  >
                    前往评审页 →
                  </Link>
                )}
              </div>
              {selected.direction && (
                <p className="text-caption text-muted-foreground">{selected.direction}</p>
              )}

              {/* 代码库登记（准备门核对材料） */}
              <div className="rounded-lg border">
                <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
                  原始代码库登记（准备门）
                </p>
                <div className="space-y-1.5 px-4 py-3 font-mono text-sm">
                  <p>{selected.sourceKind === "github" ? "仓库" : "工作区"}：<span className="text-foreground">{selected.workspacePath || "—"}</span></p>
                  {selected.codebase && (
                    <p>
                      {selected.codebase.files} 文件 · {selected.codebase.languages} · 入口{" "}
                      {selected.codebase.entry} · README {selected.codebase.readmeKb} KB
                    </p>
                  )}
                  <p className="font-sans">
                    论文：<span className="text-foreground">{selected.paper ?? "未登记（可选）"}</span>
                  </p>
                </div>
                {selected.stage === "prepare" && (
                  <div className="flex items-center gap-3 border-t px-4 py-2.5">
                    <p className="min-w-0 flex-1 truncate text-caption text-muted-foreground">
                      核对无误后开始基线验证（跑原始代码建立 BASE）；需要变更请在时间线补充说明。
                    </p>
                    <Button size="sm" onClick={() => void submitReview()}>
                      确认代码库信息，开始基线验证
                    </Button>
                  </div>
                )}
              </div>
            </div>

            {/* 内嵌时间线：补充材料 / 与 Agent 讨论 */}
            <div className="flex min-h-64 min-w-0 flex-col rounded-lg border lg:w-96 lg:shrink-0">
              <p className="border-b px-4 py-2.5 text-caption font-medium text-muted-foreground">
                项目时间线（评论 / Agent 汇报）
              </p>
              <div className="flex min-h-0 flex-1 flex-col p-4">
                <ProjectTimeline projectId={selected.id} />
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
