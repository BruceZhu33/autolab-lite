"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Activity, AlertTriangle, CheckCircle2, ChevronDown, CircleDashed, CircleDot,
  CloudUpload, FileUp, FlaskConical, FolderKanban, HardDrive, Loader2, Package,
  Play, RefreshCw, SearchCode, Sparkles, Terminal,
} from "lucide-react";
import {
  analyzeWorkspace, buildEnvironment, checkCodePresence, confirmBaselinePass,
  fetchBaselineState, fixBaselineRun, fetchProjects,
  fetchServerEnvironments,
  pollBaselineRun, pollEnvBuild, provideAsset, runBaselineCheck,
  saveBaselineConfig, startBaselineRun, syncCode, uploadAsset,
  type BaselineCheck, type BaselineConfig, type ProjectListItem,
  type ServerEnvironment,
} from "@/lib/api";
import { LiveSessionCard } from "@/components/live-session-card";
import { EmptyState } from "@/components/empty-state";
import { Button } from "~/vendor/ui/components/ui/button";
import { Progress } from "~/vendor/ui/components/ui/progress";
import { Input } from "~/vendor/ui/components/ui/input";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "@/vendor/views/layout/page-header";
import { timeAgo } from "@/vendor/views/issues/time";

const CHECK_ICON: Record<BaselineCheck["status"], typeof CheckCircle2> = {
  ok: CheckCircle2,
  warn: AlertTriangle,
  fail: AlertTriangle,
};
const CHECK_COLOR: Record<BaselineCheck["status"], string> = {
  ok: "text-success",
  warn: "text-warning",
  fail: "text-destructive",
};

type Step = "code" | "analyze" | "env" | "check" | "run" | "done";

const STEP_ORDER: Step[] = ["code", "analyze", "env", "check", "run"];

function StepHeader({ index, title, state }: {
  index: number; title: string; state: "done" | "active" | "pending" | "failed";
}) {
  return (
    <div className="flex items-center gap-2">
      <span className={`flex size-5 shrink-0 items-center justify-center rounded-full text-micro font-semibold ${
        state === "done" ? "bg-success/15 text-success"
        : state === "active" ? "bg-brand/15 text-brand"
        : state === "failed" ? "bg-destructive/15 text-destructive"
        : "bg-muted/60 text-muted-foreground"}`}>
        {state === "done" ? <CheckCircle2 className="size-3.5" />
          : state === "failed" ? <AlertTriangle className="size-3.5" />
          : state === "active" ? <Loader2 className="size-3.5 animate-spin" /> : index}
      </span>
      <h3 className="text-caption font-semibold text-foreground">{title}</h3>
    </div>
  );
}

/** 串行步骤节:Baseline 是严格串行流程(代码→环境→体检→跑通),一次只
 * 展开当前步骤;已完成折叠为一行摘要,可点标题回看,未到的折叠为待办。 */
function StepSection({ index, title, state, summary, open, onToggle, children }: {
  index: number; title: string;
  state: "done" | "active" | "pending" | "failed";
  summary?: string; open: boolean; onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <section className={`rounded-xl border-[0.5px] transition-colors ${
      state === "active" ? "border-brand/40 bg-brand/[0.03]"
      : state === "failed" ? "border-warning/40 bg-warning/[0.03]"
      : "border-surface-border bg-surface"}`}>
      <button type="button" onClick={onToggle}
              className="flex w-full items-center gap-2.5 rounded-xl px-3.5 py-2.5 text-left transition-colors hover:bg-surface-hover/60">
        <StepHeader index={index} title={title} state={state} />
        {!open && summary ? (
          <span className="min-w-0 flex-1 truncate text-caption text-muted-foreground" title={summary}>
            {summary}
          </span>
        ) : (
          <span className="flex-1" />
        )}
        <ChevronDown className={`size-3.5 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open ? (
        <div className="space-y-2 border-t border-surface-border/60 px-3.5 py-3">{children}</div>
      ) : null}
    </section>
  );
}

/** 服务器已有训练环境选择器:探测(含 torch/CUDA)→ 点选保存为 conda_env。 */
function EnvPicker({ project, currentEnv }: { project: ProjectListItem; currentEnv: string }) {
  const queryClient = useQueryClient();
  const [envs, setEnvs] = useState<ServerEnvironment[] | null>(null);
  const [busy, setBusy] = useState(false);

  const probe = async () => {
    setBusy(true);
    try {
      const found = await fetchServerEnvironments(project.server.id, true);
      setEnvs(found);
      if (found.length === 0) toast.info("服务器上没有发现 conda 环境");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const pick = async (name: string) => {
    setBusy(true);
    try {
      await saveBaselineConfig(project.id, { conda_env: name });
      toast.success(`训练环境已设为 ${name}`, { description: "Baseline 运行与迭代计划都会用它激活" });
      void queryClient.invalidateQueries({ queryKey: ["baseline", project.id] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border-[0.5px] border-surface-border p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <Package className="size-3.5 text-muted-foreground" />
        <span className="text-micro font-medium text-muted-foreground">服务器已有环境</span>
        {currentEnv ? (
          <span className="rounded-full bg-info/10 px-2 py-0.5 font-mono text-micro text-info">
            当前:{currentEnv}
          </span>
        ) : (
          <span className="text-micro text-muted-foreground">当前:系统 python3(未选环境)</span>
        )}
        <Button variant="outline" size="sm" className="ml-auto h-6 gap-1 px-2 text-micro"
                disabled={busy} onClick={() => void probe()}>
          {busy ? <Loader2 className="size-3 animate-spin" /> : <RefreshCw className="size-3" />}
          {envs === null ? "探测(含 torch/CUDA)" : "重新探测"}
        </Button>
      </div>
      {envs !== null && envs.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {envs.map((env) => (
            <button key={env.name} type="button" disabled={busy}
                    onClick={() => void pick(env.name)}
                    title={env.cuda ? `torch ${env.torch} · CUDA ${env.cuda}` : env.torch ? `torch ${env.torch}` : "未装 torch"}
                    className={`rounded-full px-2.5 py-1 text-micro font-medium transition-colors ${
                      env.name === currentEnv
                        ? "bg-accent text-accent-foreground"
                        : "bg-muted/50 text-muted-foreground hover:bg-muted"}`}>
              <span className="font-mono">{env.name}</span>
              {env.python ? <span className="ml-1 opacity-70">py{env.python}</span> : null}
              {env.torch ? <span className="ml-1 text-success/90">torch{env.torch.split("+")[0]}</span> : null}
              {env.cuda ? <span className="ml-1 opacity-70">cu{env.cuda}</span> : null}
            </button>
          ))}
        </div>
      ) : null}
      {envs !== null && envs.length === 0 ? (
        <p className="mt-1.5 text-micro text-muted-foreground">
          没有可选环境:可让 Agent 按依赖文件构建,或先在服务器上 conda create。
        </p>
      ) : null}
    </div>
  );
}

/** 训练进度面板:epoch 进度条 + 批次/ETA/速度 + 逐轮指标表。
 * 指标列从日志动态发现(训练脚本打印什么就展示什么,不同实验自动不同);
 * 原始日志折叠在下方,需要时再展开。 */
function TrainingProgressPanel({ progress, rawLog }: {
  progress: NonNullable<BaselineRunLike["progress"]>;
  rawLog: string;
}) {
  const { epoch, total_epochs: total, batch, total_batches: totalB,
          batch_label: label, eta_seconds: eta, speed,
          overall_progress: overall, metric_keys: keys, history } = progress;
  const pct = overall != null ? Math.round(overall * 100)
    : total && epoch ? Math.round(((epoch - 1) / total) * 100) : null;
  const recent = history.filter((h) => h.metrics && Object.keys(h.metrics).length > 0)
    .slice(-8).reverse();
  const etaText = eta == null ? null
    : eta >= 3600 ? `${Math.floor(eta / 3600)}h${Math.floor((eta % 3600) / 60)}m`
    : eta >= 60 ? `${Math.floor(eta / 60)}m${eta % 60}s` : `${eta}s`;

  return (
    <div className="rounded-lg border-[0.5px] border-brand/25 bg-brand/[0.03] p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-caption font-semibold text-brand">
          {epoch != null ? `Epoch ${epoch}${total ? ` / ${total}` : ""}` : "训练中"}
        </span>
        {batch != null && totalB ? (
          <span className="font-mono text-caption tabular-nums text-muted-foreground">
            {label ? `${label} ` : ""}{batch}/{totalB}{speed ? ` · ${speed}` : ""}
            {etaText ? ` · 本轮剩余 ${etaText}` : ""}
          </span>
        ) : null}
        {pct != null ? (
          <span className="ml-auto font-mono text-caption tabular-nums text-brand">{pct}%</span>
        ) : null}
      </div>
      <Progress className="mt-2 h-1.5" value={pct ?? 0} />
      {recent.length > 0 ? (
        <div className="mt-2.5 overflow-x-auto">
          <table className="w-full text-micro">
            <thead>
              <tr className="text-muted-foreground">
                <th className="px-1.5 py-1 text-left font-normal">Epoch</th>
                {keys.map((k) => (
                  <th key={k} className="px-1.5 py-1 text-right font-normal">{k}</th>
                ))}
              </tr>
            </thead>
            <tbody className="font-mono tabular-nums">
              {recent.map((h, i) => (
                <tr key={h.epoch} className={i === 0 ? "text-foreground" : "text-muted-foreground"}>
                  <td className="px-1.5 py-1 text-left">{h.epoch}</td>
                  {keys.map((k) => (
                    <td key={k} className="px-1.5 py-1 text-right">
                      {h.metrics[k] !== undefined
                        ? (h.metrics[k] >= 100 ? h.metrics[k].toFixed(0)
                          : h.metrics[k].toFixed(4))
                        : "—"}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <details className="mt-2">
        <summary className="cursor-pointer text-micro text-muted-foreground hover:text-foreground">
          原始日志(实时)
        </summary>
        <pre className="mt-1.5 max-h-40 overflow-auto rounded-lg bg-muted/50 p-2 font-mono text-micro leading-relaxed">
          {rawLog || "(暂无)"}
        </pre>
      </details>
    </div>
  );
}

type BaselineRunLike = { progress?: {
  epoch: number | null; total_epochs: number | null;
  batch?: number | null; total_batches?: number | null;
  batch_label?: string | null; eta_seconds?: number | null; speed?: string | null;
  overall_progress?: number;
  metric_keys: string[];
  history: { epoch: number; metrics: Record<string, number> }[];
} | null };

/** 数据/权重供给对话框:① 服务器已有目录 → 符号链接(大数据集不搬家);
 * ② 上传 zip → 解包(小数据集/预训练权重)。目标为工作区内相对路径。 */
function AssetSupplyDialog({ project, initialTarget, onClose }: {
  project: ProjectListItem; initialTarget: string; onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [target, setTarget] = useState(initialTarget);
  const [serverPath, setServerPath] = useState("");
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [busy, setBusy] = useState<"link" | "upload" | null>(null);

  const run = async (mode: "link" | "upload") => {
    if (!target.trim()) {
      toast.error("先填目标路径(工作区内相对路径,如 data/Kvasir)");
      return;
    }
    setBusy(mode);
    try {
      if (mode === "link") {
        const r = await provideAsset(project.id, target.trim(), serverPath.trim());
        toast.success(`已供给 ${r.target}`, {
          description: `符号链接 ← ${r.source};重跑体检确认就位`,
          duration: 8000,
        });
      } else {
        const r = await uploadAsset(project.id, target.trim(), zipFile!);
        toast.success(`已解包到 ${r.target}`, {
          description: "重跑体检确认就位",
          duration: 8000,
        });
      }
      void queryClient.invalidateQueries({ queryKey: ["baseline", project.id] });
      onClose();
    } catch (e) {
      toast.error("供给失败", { description: (e as Error).message, duration: 8000 });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="rounded-lg border-[0.5px] border-surface-border bg-surface p-3 shadow-lg">
      <div className="flex items-center gap-2">
        <HardDrive className="size-4 text-muted-foreground" />
        <span className="text-body font-medium">供给数据 / 权重</span>
        <Button variant="ghost" size="sm" className="ml-auto h-6 px-2 text-micro" onClick={onClose}>
          ×
        </Button>
      </div>
      <div className="mt-2 grid gap-2">
        <label className="grid gap-1">
          <span className="text-micro text-muted-foreground">
            目标路径(工作区内相对路径;数据集填清单里的数据路径,权重如 pretrained/)
          </span>
          <Input className="h-7 font-mono text-micro" value={target}
                 placeholder="data/Kvasir" spellCheck={false}
                 onChange={(e) => setTarget(e.target.value)} />
        </label>
        <div className="rounded-md border-[0.5px] border-surface-border p-2">
          <p className="text-micro font-medium text-muted-foreground">
            ① 服务器已有目录(推荐,大数据集不搬家,建符号链接)
          </p>
          <div className="mt-1.5 flex gap-1.5">
            <Input className="h-7 min-w-0 flex-1 font-mono text-micro"
                   value={serverPath} placeholder="/data/datasets/Kvasir"
                   spellCheck={false}
                   onChange={(e) => setServerPath(e.target.value)} />
            <Button size="sm" className="h-7 gap-1 px-2 text-micro"
                    disabled={busy !== null || !serverPath.trim()}
                    onClick={() => void run("link")}>
              {busy === "link" ? <Loader2 className="size-3 animate-spin" /> : <HardDrive className="size-3" />}
              链接
            </Button>
          </div>
        </div>
        <div className="rounded-md border-[0.5px] border-surface-border p-2">
          <p className="text-micro font-medium text-muted-foreground">
            ② 上传 zip 解包(小数据集 / 预训练权重,≤2 GiB)
          </p>
          <div className="mt-1.5 flex gap-1.5">
            <Input type="file" accept=".zip" className="h-7 text-micro"
                   onChange={(e) => setZipFile(e.target.files?.[0] ?? null)} />
            <Button size="sm" className="h-7 gap-1 px-2 text-micro"
                    disabled={busy !== null || !zipFile}
                    onClick={() => void run("upload")}>
              {busy === "upload" ? <Loader2 className="size-3 animate-spin" /> : <FileUp className="size-3" />}
              上传
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

/** 体检缺口的行内供给:目标路径已知(清单声明的数据路径),用户只需给
 * 来源(服务器已有目录 或 zip),补完自动复核——不用再理解 scp/rsync。 */
function DatasetSupplyInline({ project, target, label, onSupplied }: {
  project: ProjectListItem; target: string; label: string; onSupplied: () => void;
}) {
  const [serverPath, setServerPath] = useState("");
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [busy, setBusy] = useState<"link" | "upload" | null>(null);

  const run = async (mode: "link" | "upload") => {
    setBusy(mode);
    try {
      if (mode === "link") {
        const r = await provideAsset(project.id, target, serverPath.trim());
        toast.success(`${label} 已就位`, { description: `${r.target} ← ${r.source}`, duration: 8000 });
      } else {
        // 供给卡的目标可能是空壳目录(有标注 CSV)→ merge 合并进目录
        const r = await uploadAsset(project.id, target, zipFile!, true);
        toast.success(`${label} 已解包就位`, {
          description: `${r.target}(同名覆盖,已有标注保留)`, duration: 8000,
        });
      }
      onSupplied();
    } catch (e) {
      toast.error(`${label} 供给失败`, {
        description: (e as Error).message, duration: 8000,
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="rounded-lg border border-warning/30 bg-warning/5 p-2.5">
      <p className="text-caption font-medium text-warning">
        {label} 缺失 → 归位到 <span className="font-mono">{target}</span>
      </p>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        <Input className="h-7 min-w-0 flex-1 font-mono text-micro"
               value={serverPath} placeholder="服务器已有目录的绝对路径(推荐,秒链)"
               spellCheck={false}
               onChange={(e) => setServerPath(e.target.value)} />
        <Button size="sm" className="h-7 gap-1 px-2 text-micro"
                disabled={busy !== null || !serverPath.trim()}
                onClick={() => void run("link")}>
          {busy === "link" ? <Loader2 className="size-3 animate-spin" /> : <HardDrive className="size-3" />}
          链接
        </Button>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        <Input type="file" accept=".zip" className="h-7 min-w-0 flex-1 text-micro"
               onChange={(e) => setZipFile(e.target.files?.[0] ?? null)} />
        <Button size="sm" variant="outline" className="h-7 gap-1 px-2 text-micro"
                disabled={busy !== null || !zipFile}
                onClick={() => void run("upload")}>
          {busy === "upload" ? <Loader2 className="size-3 animate-spin" /> : <FileUp className="size-3" />}
          上传 zip
        </Button>
      </div>
    </div>
  );
}

/** 联合基线数据集清单编辑器:每行 名称/命令/数据路径,保存后为 Baseline
 * 真相源(跑=按清单顺序;确认=全部跑通;迭代可选池=确认时的快照)。 */
function DatasetsEditor({ project, config, disabled, onSaved }: {
  project: ProjectListItem; config: BaselineConfig; disabled?: boolean;
  onSaved?: () => void;
}) {
  const queryClient = useQueryClient();
  const seed = config.datasets && config.datasets.length > 0
    ? config.datasets
    : [{ name: config.dataset_path.split("/").filter(Boolean).pop() || "default",
        command: config.command, dataset_path: config.dataset_path }];
  const [rows, setRows] = useState(seed.map((d) => ({ ...d })));
  const [busy, setBusy] = useState(false);
  const [supplyTarget, setSupplyTarget] = useState<string | null>(null);
  const dirty = JSON.stringify(rows) !== JSON.stringify(seed);

  const save = async () => {
    setBusy(true);
    try {
      await saveBaselineConfig(project.id, { datasets: rows });
      toast.success(`已保存 ${rows.length} 个数据集`, {
        description: "Agent 正在逐个检测数据集是否在服务器上就位…",
      });
      void queryClient.invalidateQueries({ queryKey: ["baseline", project.id] });
      onSaved?.();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border-[0.5px] border-surface-border p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-micro font-medium text-muted-foreground">
          数据集清单(联合基线)
        </span>
        <span className="text-micro text-muted-foreground">
          {rows.length} 个 · 每个 = 一条训练命令 + 数据路径
        </span>
        <div className="ml-auto flex gap-1.5">
          <Button variant="outline" size="sm" className="h-6 gap-1 px-2 text-micro"
                  disabled={busy || disabled}
                  onClick={() => setSupplyTarget("")}>
            <FileUp className="size-3" />
            供给数据/权重
          </Button>
          <Button variant="outline" size="sm" className="h-6 px-2 text-micro"
                  disabled={busy || disabled}
                  onClick={() => setRows([...rows, { name: "", command: "", dataset_path: "" }])}>
            加数据集
          </Button>
          <Button size="sm" className="h-6 px-2 text-micro"
                  disabled={busy || disabled || !dirty || rows.some((r) => !r.name.trim() || !r.command.trim())}
                  onClick={() => void save()}>
            {busy ? <Loader2 className="size-3 animate-spin" /> : null}
            保存清单
          </Button>
        </div>
      </div>
      <div className="mt-2 space-y-1.5">
        {rows.map((row, i) => (
          <div key={i} className="flex items-center gap-1.5">
            <Input className="h-7 w-28 shrink-0 font-mono text-micro"
                   value={row.name} placeholder="名称(Kvasir)"
                   spellCheck={false}
                   onChange={(e) => setRows(rows.map((r, j) =>
                     j === i ? { ...r, name: e.target.value } : r))} />
            <Input className="h-7 min-w-0 flex-1 font-mono text-micro"
                   value={row.command} placeholder="python train.py --config-file configs/Kvasir.yaml"
                   spellCheck={false}
                   onChange={(e) => setRows(rows.map((r, j) =>
                     j === i ? { ...r, command: e.target.value } : r))} />
            <Input className="h-7 w-36 shrink-0 font-mono text-micro"
                   value={row.dataset_path} placeholder="data/Kvasir"
                   spellCheck={false}
                   onChange={(e) => setRows(rows.map((r, j) =>
                     j === i ? { ...r, dataset_path: e.target.value } : r))} />
            <Button variant="ghost" size="sm" className="size-7 shrink-0 p-0 text-muted-foreground"
                    disabled={busy || disabled || rows.length <= 1}
                    title="移除该数据集" onClick={() => setRows(rows.filter((r, j) => j !== i))}>
              ×
            </Button>
            <Button variant="ghost" size="sm"
                    className="size-7 shrink-0 p-0 text-muted-foreground"
                    disabled={busy || disabled || !row.dataset_path.trim()}
                    title={row.dataset_path.trim()
                      ? `把数据供给到 ${row.dataset_path}(服务器已有目录链接或上传 zip)`
                      : "先填该行的数据路径"}
                    onClick={() => setSupplyTarget(row.dataset_path.trim())}>
              <FileUp className="size-3" />
            </Button>
          </div>
        ))}
      </div>
      {supplyTarget !== null && (
        <div className="mt-2">
          <AssetSupplyDialog project={project} initialTarget={supplyTarget}
                             onClose={() => setSupplyTarget(null)} />
        </div>
      )}
      <p className="mt-1.5 text-micro text-muted-foreground">
        加 BUSI:一行一个数据集(命令里的 --config-file 换成对应 yaml);确认通过后,自动迭代就能勾选这些数据集。
        缺数据/权重用「供给」:服务器已有目录链接即就位,或上传 zip。
      </p>
    </div>
  );
}

function BaselinePanel({ project }: { project: ProjectListItem }) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [stepState, setStepState] = useState<Partial<Record<Step, "done" | "active" | "pending" | "failed">>>({});
  const [presence, setPresence] = useState<{ present: boolean; reason: string; files: string[] } | null>(null);
  const [sources, setSources] = useState<{ githubUrl: string | null; localWorkspace: string | null }>({ githubUrl: null, localWorkspace: null });
  const [syncDetail, setSyncDetail] = useState<string | null>(null);
  const [envPlan, setEnvPlan] = useState<{ command: string; detail: string } | null>(null);
  const [envPid, setEnvPid] = useState<number | null>(null);
  const [gap, setGap] = useState<{ kind: "dataset" | "command"; hint: string } | null>(null);
  const [gapValue, setGapValue] = useState("");
  const autoDetected = useRef(false);

  const { data: state, refetch } = useQuery({
    queryKey: ["baseline", project.id],
    queryFn: () => fetchBaselineState(project.id),
    refetchInterval: (query) =>
      query.state.data?.run.status === "running" ? 2000 : false,
  });
  const { data: envState } = useQuery({
    queryKey: ["baseline-env", project.id],
    queryFn: () => pollEnvBuild(project.id),
    refetchInterval: (query) => (envPid ?? 0) > 0 ? 2000 : false,
    enabled: !!envPid,
  });

  // Agent 自动监测:数据集清单为空时,扫工作区真实数据目录自动填入(每挂载一次)
  useEffect(() => {
    if (autoDetected.current || !state) return;
    if ((state.config.datasets?.length ?? 0) > 0) return;
    autoDetected.current = true;
    void (async () => {
      try {
        const analysis = await analyzeWorkspace(project.id);
        const found = analysis.applied.datasets ?? [];
        if (found.length > 0) {
          toast.success(`Agent 检测到 ${found.length} 个数据集,已自动填入清单`, {
            description: found.map((d) => d.name).join("、") +
              "(可在此增删改,百分比变体加一行即可)",
          });
          void refetch();
        }
      } catch { /* 检测失败静默:不打扰,清单仍可手填 */ }
    })();
  }, [state]);

  const refreshAll = () => {
    void refetch();
    void queryClient.invalidateQueries({ queryKey: ["projects"] });
    void queryClient.invalidateQueries({ queryKey: ["board"] });
  };

  const fail = (step: Step, message: string) => {
    setStepState((s) => ({ ...s, [step]: "failed" }));
    toast.error(message);
  };

  const doCodeCheck = async (): Promise<{ present: boolean; sources: { githubUrl: string | null; localWorkspace: string | null } }> => {
    const result = await checkCodePresence(project.id);
    setPresence(result.presence);
    setSources(result.sources);
    return { present: result.presence.present, sources: result.sources };
  };

  const doCodeSync = async (): Promise<boolean> => {
    const result = await syncCode(project.id);
    setSyncDetail(`${result.source}:${result.detail}`);
    return (await doCodeCheck()).present;
  };

  const doEnvBuildAndWait = async (): Promise<boolean> => {
    const result = await buildEnvironment(project.id);
    setEnvPid(result.pid);
    // 轮询到结束
    for (let i = 0; i < 150; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const st = await pollEnvBuild(project.id);
      if (st.status === "finished") {
        return st.exit_code === 0;
      }
    }
    return false;
  };

  const [fixSummary, setFixSummary] = useState<string | null>(null);

  /** 修复 Agent:读失败日志→最小补丁→自动重跑(补丁有备份与语法门)。 */
  const agentFix = async () => {
    setBusy("fix");
    setFixSummary(null);
    toast.info("修复 Agent 正在读失败日志定位问题…", {
      description: "诊断 → 打补丁(备份+语法检查)→ 自动重跑,约 1-3 分钟", duration: 6000,
    });
    try {
      const result = await fixBaselineRun(project.id);
      setFixSummary(`已修:${result.summary}(改动 ${result.changes.length} 处,重跑 pid ${result.rerunPid})`);
      toast.success("补丁已应用,Baseline 已重跑", { description: result.summary });
      void refetch();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const doCheck = async (): Promise<{ ok: boolean; datasetMissing: boolean }> => {
    const result = await runBaselineCheck(project.id);
    void refetch();
    const datasetFail = result.checks.some(
      (c) => (c.key === "dataset" || c.key.startsWith("dataset:")) && c.status === "fail");
    const hardFail = result.checks.some((c) => c.status === "fail");
    return { ok: !hardFail, datasetMissing: datasetFail };
  };

  /** Agent 一键 Baseline:就位 → 分析(内部配置) → 构建环境 → 数据集检查 → 跑。 */
  const runAuto = async () => {
    setBusy("auto");
    setGap(null);
    setStepState({});
    try {
      // ① 代码就位
      setStepState((s) => ({ ...s, code: "active" }));
      const code = await doCodeCheck();
      let present = code.present;
      if (!present && (code.sources.githubUrl || code.sources.localWorkspace)) {
        toast.info("服务器上没有代码,自动同步…");
        present = await doCodeSync();
      }
      if (!present) {
        fail("code", "代码未就位:请在项目编辑里补 GitHub 链接或本地工作区路径,再点一键 Baseline");
        return;
      }
      setStepState((s) => ({ ...s, code: "done" }));

      // ② Agent 分析(内部配置:命令/数据集/环境/指标文件,自动推断)
      setStepState((s) => ({ ...s, analyze: "active" }));
      const analysis = await analyzeWorkspace(project.id);
      setEnvPlan((await Promise.resolve(analysis)).envPlan);
      if (!analysis.applied.command) {
        setStepState((s) => ({ ...s, analyze: "failed" }));
        setGap({ kind: "command", hint: "Agent 读了代码/README 也没认出训练入口,告诉你它怎么跑:" });
        toast.error("无法确定训练命令");
        return;
      }
      setStepState((s) => ({ ...s, analyze: "done" }));
      setEnvPlan(analysis.envPlan);

      // ③ 环境构建(有依赖文件才构建;失败亮日志)
      if (analysis.envPlan.command) {
        setStepState((s) => ({ ...s, env: "active" }));
        toast.info(`环境构建中:${analysis.envPlan.detail}`);
        const built = await doEnvBuildAndWait();
        if (!built) {
          fail("env", "环境构建失败:看下方日志,按提示在服务器上处理(缺系统库/网络/权限)后重试");
          return;
        }
        setStepState((s) => ({ ...s, env: "done" }));
      } else {
        setStepState((s) => ({ ...s, env: "done" }));
      }

      // ④ 数据集与环境检查
      setStepState((s) => ({ ...s, check: "active" }));
      const check = await doCheck();
      if (check.datasetMissing) {
        setStepState((s) => ({ ...s, check: "failed" }));
        setGap({ kind: "dataset", hint: "服务器上没找到数据集:把数据集放上去后填路径,或直接告诉我路径:" });
        toast.error("数据集缺失");
        return;
      }
      if (!check.ok) {
        fail("check", "环境检查未通过:见下方清单,红色项需要处理");
        return;
      }
      setStepState((s) => ({ ...s, check: "done" }));

      // ⑤ 跑(联合基线:只排队体检就绪的,未就绪的自动跳过)
      setStepState((s) => ({ ...s, run: "active" }));
      const run = await startBaselineRun(project.id);
      const skippedNote = (run as { skipped?: string[] }).skipped?.length
        ? `;跳过未就绪 ${((run as { skipped?: string[] }).skipped ?? []).length} 个(补齐后追加)`
        : "";
      toast.success(
        `Agent 已在 ${project.server.name} 上起跑 ${run.dataset}(PID ${run.pid})`, {
          description: run.queue.length > 0
            ? `联合基线队列:${[run.dataset, ...run.queue].join(" → ")}${skippedNote}`
            : `结果和日志在下方实时刷新;跑通且有指标即可确认通过${skippedNote}`,
        });
      void refetch();
      refreshAll();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const saveGap = async () => {
    if (!gap || !gapValue.trim()) return;
    try {
      await saveBaselineConfig(project.id,
        gap.kind === "dataset"
          ? { dataset_path: gapValue.trim() }
          : { command: gapValue.trim() });
      toast.success("已补填,继续点「Agent 一键 Baseline」");
      setGap(null);
      setGapValue("");
      void refetch();
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  const run = state?.run;
  const running = run?.status === "running";
  const confirmedNames = Object.keys(state?.confirmed ?? {});
  const pendingCount = (state?.datasets ?? []).filter((d) => !d.ok).length;
  /** 已确认过基线,但清单里有新跑通、尚未纳入可选池的数据集。 */
  const pendingAdopt = (state?.datasets ?? []).filter(
    (d) => d.ok && !confirmedNames.includes(d.name));
  const [nowTs, setNowTs] = useState(Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNowTs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  const elapsedText = (() => {
    const started = run?.started_at ? Date.parse(run.started_at) : NaN;
    if (!running || Number.isNaN(started)) return "—";
    const secs = Math.max(0, Math.floor((nowTs - started) / 1000));
    return secs >= 3600 ? `${Math.floor(secs / 3600)}h${Math.floor((secs % 3600) / 60)}m`
      : secs >= 60 ? `${Math.floor(secs / 60)}m${secs % 60}s` : `${secs}s`;
  })();
  const passed = !!state?.passedAt || !!run?.passed;
  const confirmable = !!run?.confirmable && !passed;

  /** 人工确认门:跑通+有指标后,由用户确认"以此为基线"。 */
  const confirmPass = async () => {
    setBusy("pass");
    try {
      const result = await confirmBaselinePass(project.id);
      toast.success(result.replayed ? "此前已确认通过" : "Baseline 已确认通过,进入方向页", {
        description: result.replayed ? undefined
          : "各数据集指标已快照为基线;自动迭代将在这些数据集上展开",
      });
      void queryClient.invalidateQueries({ queryKey: ["baseline", project.id] });
      void queryClient.invalidateQueries({ queryKey: ["board"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  /** 只重跑某一个数据集(修复/换配置后验证)。 */
  const rerunDataset = async (dataset: string) => {
    setBusy(`rerun:${dataset}`);
    try {
      const started = await startBaselineRun(project.id, dataset);
      toast.success(`已在 ${project.server.name} 上重跑 ${dataset}(PID ${started.pid})`);
      void refetch();
      refreshAll();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const checks = state?.checks ?? [];
  const codeReady = presence?.present;
  const envDone = envState?.status === "finished" && envState?.exit_code === 0;
  const isLocal = project.server.transport_mode !== "ssh";
  const anyActive = busy === "auto" || running;

  const stepStatus = (step: Step): "done" | "active" | "pending" | "failed" =>
    stepState[step]
    ?? (step === "code" && codeReady === true ? "done"
      : step === "code" && codeReady === false ? "failed"
      : step === "env" && envDone ? "done"
      : step === "run" && passed ? "done"
      : step === "run" && running ? "active"
      : "pending");

  // 串行展开:默认只展开第一个未完成步骤(失败强制展开);点击标题可覆盖
  const [openSteps, setOpenSteps] = useState<Partial<Record<Step, boolean>>>({});
  const stepOpen = (s: Step): boolean => {
    if (s in openSteps) return !!openSteps[s];
    if (stepStatus(s) === "failed") return true;
    const firstOpen = STEP_ORDER.find((x) => stepStatus(x) !== "done");
    return s === firstOpen;
  };
  const toggleStep = (s: Step) =>
    setOpenSteps((m) => ({ ...m, [s]: !(s in m ? m[s] : stepOpen(s)) }));

  // 体检缺口 → 行内供给行(后端检查项自带 supply:缺什么、归位到哪,补完
  // 自动复核)。同一物理目录被多个清单条目引用时(如 Kvasir/10%/25% 变体)
  // 按目标路径去重,一张卡补一个目录。
  const failedSupplies = checks.flatMap((c) =>
    (c.supply ?? []).map((s) => ({ key: `${c.key}:${s.target}`, ...s })))
    .filter((s, i, arr) => arr.findIndex((x) => x.target === s.target) === i);

  return (
    <div className="space-y-3">
      <div className="rounded-xl border-[0.5px] border-surface-border bg-surface shadow-[var(--surface-shadow)]">
        {/* 头部:项目名 + 一键按钮 + 通过状态 */}
        <div className="flex flex-wrap items-center gap-2 border-b border-surface-border px-4 py-3">
          <FlaskConical className="size-4 text-muted-foreground" />
          <h2 className="text-body font-semibold">{project.name}</h2>
          <span className="text-caption text-muted-foreground">
            {project.server.name} · {project.workdir}
          </span>
          <span className="ml-auto inline-flex items-center gap-1.5">
            {passed ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-success/10 px-2 py-0.5 text-caption font-medium text-success">
                <CheckCircle2 className="size-3.5" />
                Baseline 已通过{state?.passedAt ? ` · ${timeAgo(state.passedAt)}` : ""}
              </span>
            ) : running ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-warning/10 px-2 py-0.5 text-caption font-medium text-warning">
                <Loader2 className="size-3.5 animate-spin" />
                运行中 · PID {run?.pid}
              </span>
            ) : (
              <Button size="sm" className="gap-1.5" disabled={anyActive} onClick={() => void runAuto()}>
                {busy === "auto"
                  ? <Loader2 className="size-3.5 animate-spin" />
                  : <Sparkles className="size-3.5" />}
                {busy === "auto" ? "Agent 执行中…" : "Agent 一键 Baseline"}
              </Button>
            )}
          </span>
        </div>

        <div className="max-w-3xl space-y-2.5 p-4">
            {/* ① 代码就位 */}
            <StepSection index={1} title="代码就位" state={stepStatus("code")}
                         summary={presence?.present ? "已就位" : (presence?.reason || "检查服务器上有没有代码")}
                         open={stepOpen("code")} onToggle={() => toggleStep("code")}>
              {presence ? (
                presence.present ? (
                  <p className="text-caption text-success">
                    已就位:{presence.files.slice(0, 6).join("、")}{presence.files.length > 6 ? "…" : ""}
                  </p>
                ) : (
                  <div className="rounded-lg border border-warning/30 bg-warning/5 p-2.5 text-caption">
                    <p className="font-medium text-warning">未就位:{presence.reason}</p>
                    <p className="mt-0.5 text-muted-foreground">
                      一键 Baseline 会自动同步(GitHub 或本地工作区);
                      都没配就去项目编辑补一个来源。
                    </p>
                    {!isLocal ? (
                      <Button size="sm" variant="outline" className="mt-1.5 h-7 gap-1 px-2 text-caption"
                              disabled={busy !== null} onClick={() => void doCodeSync()}>
                        <CloudUpload className="size-3" />
                        手动同步
                      </Button>
                    ) : null}
                  </div>
                )
              ) : (
                <p className="text-caption text-muted-foreground">
                  {isLocal ? "本机服务器:代码已在执行位置" : "检查服务器上有没有代码;没有自动同步"}
                </p>
              )}
              {syncDetail ? <p className="text-micro text-muted-foreground">上次同步:{syncDetail}</p> : null}
            </StepSection>

            {/* ② 环境构建 */}
            <StepSection index={2} title="构建 / 选择环境" state={stepStatus("env")}
                         summary={envDone ? "环境就绪" : (envPlan?.detail || "Agent 分析依赖后自动构建")}
                         open={stepOpen("env")} onToggle={() => toggleStep("env")}>
              {envPlan ? (
                <div className="rounded-lg bg-muted/40 p-2.5">
                  <p className="text-caption text-muted-foreground">{envPlan.detail}</p>
                  {envPlan.command ? (
                    <pre className="mt-1 overflow-x-auto font-mono text-micro text-muted-foreground">{envPlan.command}</pre>
                  ) : (
                    <p className="mt-1 text-caption text-warning">无依赖文件:跳过构建(需要特定环境时 Agent 会在检查环节发现)</p>
                  )}
                </div>
              ) : (
                <p className="text-caption text-muted-foreground">Agent 分析依赖后自动构建(requirements/environment.yml)</p>
              )}
              {envState && envState.status !== "idle" && envState.log_tail ? (
                <pre className="max-h-28 overflow-y-auto rounded-lg bg-muted/50 p-2 font-mono text-micro leading-relaxed">
{envState.log_tail}
                </pre>
              ) : null}
              {envState?.status === "finished" && envState.exit_code !== null ? (
                <p className={`text-caption ${envState.exit_code === 0 ? "text-success" : "text-destructive"}`}>
                  构建退出码 {envState.exit_code}
                  {envState.exit_code !== 0 ? " —— 按日志提示在服务器上处理(缺系统库/网络/权限)后重试" : ""}
                </p>
              ) : null}
              <EnvPicker project={project} currentEnv={state?.config.conda_env ?? ""} />
            </StepSection>

            {/* ③ 数据集与环境检查 */}
            <StepSection index={3} title="数据集与环境检查" state={stepStatus("check")}
                         summary={checks.length > 0
                           ? (() => {
                               const okN = checks.filter((c) => c.status === "ok").length;
                               const warnN = checks.filter((c) => c.status === "warn").length;
                               const failN = checks.filter((c) => c.status === "fail").length;
                               return `${okN} 通过${warnN ? ` · ${warnN} 待补` : ""}${failN ? ` · ${failN} 阻塞` : ""}`;
                             })()
                           : "Agent 复核数据集就位与运行环境"}
                         open={stepOpen("check")} onToggle={() => toggleStep("check")}>
              {failedSupplies.length > 0 ? (
                <div className="space-y-2">
                  <p className="text-caption text-muted-foreground">
                    待补材料 {failedSupplies.length} 项(渐进式基线:不阻塞已就绪的数据集,
                    起跑会自动跳过未就绪的)。下面直接补,系统自动归位并复核:
                  </p>
                  {failedSupplies.map((s) => (
                    <DatasetSupplyInline key={s.key} project={project}
                                         target={s.target} label={s.label}
                                         onSupplied={() => void doCheck()} />
                  ))}
                </div>
              ) : null}
              {checks.length > 0 ? (
                <div className="divide-y divide-surface-border/50">
                  {checks.map((check) => (
                    <div key={check.key} className="flex items-center gap-2 py-1.5">
                      {(() => {
                        const Icon = CHECK_ICON[check.status];
                        return <Icon className={`size-3.5 shrink-0 ${CHECK_COLOR[check.status]}`} />;
                      })()}
                      <span className="w-32 shrink-0 text-caption font-medium">{check.label}</span>
                      <span className={`min-w-0 flex-1 truncate text-caption ${
                        check.status === "fail" ? "text-destructive" : "text-muted-foreground"
                      }`} title={check.detail}>
                        {check.detail}
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-caption text-muted-foreground">数据集在服务器上就位即可通过;缺了 Agent 会停下来找你</p>
              )}
              <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-caption"
                      disabled={busy !== null} onClick={() => void doCheck()}>
                <SearchCode className="size-3" />
                重新检查
              </Button>
              {state?.config.command ? (
                <div className="pt-1">
                  <DatasetsEditor project={project} config={state.config}
                                  disabled={busy !== null || running}
                                  onSaved={() => void doCheck()} />
                </div>
              ) : null}
            </StepSection>

            {/* ④ 跑通基线 */}
            <StepSection index={4} title="跑通基线(Agent)"
                         summary={passed ? "已通过" : running ? "训练进行中"
                           : (state?.datasets?.length ?? 0) > 0
                             ? `${state!.datasets.filter((d) => d.ok).length}/${state!.datasets.length} 跑通`
                             : "≥1 个数据集跑通且有指标即可确认"}
                         state={running ? "active"
                                : run?.status === "finished" && run.exit_code === 0 ? "done"
                                : run?.status === "finished" ? "failed"
                                : stepStatus("run")}
                         open={stepOpen("run")} onToggle={() => toggleStep("run")}>
              {(state?.datasets?.length ?? 0) > 0 ? (
                <div className="mt-2 rounded-lg border-[0.5px] border-surface-border p-2.5">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-micro font-medium text-muted-foreground">数据集进度</span>
                    {running && run?.queue && run.queue.length > 0 ? (
                      <span className="text-micro text-muted-foreground">
                        队列:{[run.dataset, ...run.queue].join(" → ")}
                      </span>
                    ) : null}
                    <span className="ml-auto text-micro text-muted-foreground">
                      {state!.datasets.filter((d) => d.ok).length}/{state!.datasets.length} 跑通
                    </span>
                  </div>
                  <div className="mt-1.5 space-y-1">
                    {state!.datasets.map((d) => (
                      <div key={d.name} className="flex items-center gap-2 text-caption">
                        {d.active ? (
                          <Loader2 className="size-3 shrink-0 animate-spin text-brand" />
                        ) : d.ok ? (
                          <CheckCircle2 className="size-3 shrink-0 text-success" />
                        ) : d.status === "finished" ? (
                          <AlertTriangle className="size-3 shrink-0 text-destructive" />
                        ) : (
                          <CircleDot className="size-3 shrink-0 text-muted-foreground" />
                        )}
                        <span className="w-20 shrink-0 truncate font-mono font-medium" title={d.name}>
                          {d.name}
                        </span>
                        {d.seed != null ? (
                          <span className="shrink-0 rounded bg-muted/60 px-1 py-0.5 font-mono text-micro text-muted-foreground"
                                title="随机种子(来自训练日志/命令行)">
                            seed {d.seed}
                          </span>
                        ) : null}
                        {d.metrics && Object.keys(d.metrics).length > 0 ? (
                          <span className="min-w-0 flex-1 truncate font-mono text-success">
                            {Object.entries(d.metrics).map(([k, v]) => `${k}=${String(v)}`).join(" ")}
                          </span>
                        ) : (
                          <span className="min-w-0 flex-1 truncate text-muted-foreground">
                            {d.active ? "训练中…" : d.status === "finished"
                              ? `退出码 ${d.exitCode ?? "?"}` : "待跑"}
                          </span>
                        )}
                        {!running ? (
                          <button type="button"
                                  className="shrink-0 rounded-md border border-brand/40 bg-brand/5 px-2 py-0.5 text-caption font-medium text-brand transition-colors hover:bg-brand/15 disabled:opacity-40"
                                  disabled={busy !== null}
                                  onClick={() => void rerunDataset(d.name)}>
                            {d.ok || d.status === "finished" ? "重跑" : "跑"}
                          </button>
                        ) : null}
                      </div>
                    ))}
                  </div>
                  {passed && pendingAdopt.length > 0 ? (
                    <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border-[0.5px] border-success/30 bg-success/5 px-2.5 py-2">
                      <span className="text-caption text-muted-foreground">
                        {pendingAdopt.map((d) => d.name).join("、")} 跑通且拿到指标——
                        纳入基线后自动迭代就能选它
                      </span>
                      <Button size="sm" className="ml-auto h-7 gap-1 px-2.5 text-caption"
                              disabled={busy !== null} onClick={() => void confirmPass()}>
                        {busy === "pass" ? <Loader2 className="size-3 animate-spin" /> : <CheckCircle2 className="size-3" />}
                        {busy === "pass" ? "纳入中…" : `纳入基线(${pendingAdopt.length})`}
                      </Button>
                    </div>
                  ) : null}
                </div>
              ) : null}
              {running ? (
                <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border-[0.5px] border-brand/30 bg-brand/5 px-3 py-2">
                  <Loader2 className="size-4 shrink-0 animate-spin text-brand" />
                  <span className="text-caption font-semibold text-brand">训练进行中</span>
                  <span className="font-mono text-caption tabular-nums text-muted-foreground">
                    已运行 {elapsedText}
                  </span>
                  <span className="text-micro text-muted-foreground">
                    (GPU 服务器上真实训练;日志尾部下方 2s 自动刷新)
                  </span>
                </div>
              ) : null}
              <div className="mt-2 flex items-center gap-2">
                <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-caption"
                        disabled={busy !== null || running} onClick={() => void runAuto()}>
                  {busy === "auto" ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
                  {running ? "运行中…"
                    : (pendingCount) > 0
                      ? `▶ 跑通基线(${pendingCount} 个数据集待跑,按清单顺序)`
                      : run?.status === "finished" ? "重跑基线" : "跑基线"}
                </Button>
                <span className="text-micro text-muted-foreground">
                  ≥1 个数据集 跑通且有指标 即可「确认通过」;其余后续补跑,追加纳入
                </span>
              </div>
              <p className="mt-1.5 text-caption text-muted-foreground">
                命令/数据集清单/指标文件由 Agent 从代码里定,可在此改;确认通过后迭代只能选这些数据集。
              </p>
            </StepSection>

            {gap ? (
              <section className="rounded-lg border border-warning/40 bg-warning/5 p-3">
                <p className="text-caption font-medium text-warning">{gap.hint}</p>
                <div className="mt-2 flex gap-2">
                  <Input
                    value={gapValue}
                    onChange={(e) => setGapValue(e.target.value)}
                    placeholder={gap.kind === "dataset" ? "/home/serveruser/datasets/medseg" : "python3 train.py --seed 1"}
                    spellCheck={false}
                  />
                  <Button size="sm" onClick={() => void saveGap()}>补填</Button>
                </div>
              </section>
            ) : null}

            {run && run.status !== "idle" ? (
              <>
                {running && (run.progress || run.progress_tail) ? (
                  <section>
                    {run.progress ? (
                      <TrainingProgressPanel progress={run.progress}
                                             rawLog={run.progress_tail || ""} />
                    ) : (
                      <details className="rounded-lg border-[0.5px] border-brand/25 bg-muted/40">
                        <summary className="cursor-pointer px-2.5 py-1.5 text-caption font-semibold text-brand">
                          <Activity className="mr-1 inline size-3.5" />
                          训练进度(实时,未识别出结构化进度)
                        </summary>
                        <pre className="max-h-48 overflow-auto px-2.5 pb-2.5 font-mono text-micro leading-relaxed">
{run.progress_tail}
                        </pre>
                      </details>
                    )}
                  </section>
                ) : null}
                {run.status === "finished" ? (
                  <section className="space-y-1.5">
                    <h3 className="text-caption font-semibold text-muted-foreground">结果</h3>
                    <p className="text-caption">
                      退出码:
                      <span className={`ml-1 font-mono font-semibold ${
                        run.exit_code === 0 ? "text-success" : "text-destructive"}`}>
                        {run.exit_code ?? "未知(进程异常终止)"}
                      </span>
                    </p>
                    {run.metrics && Object.keys(run.metrics).length > 0 ? (
                      <div className="rounded-lg border-[0.5px] border-surface-border p-2.5">
                        <p className="mb-1 text-micro font-medium text-muted-foreground">
                          基准指标
                          {(run as { metrics_source?: string }).metrics_source === "log"
                            ? "(取自训练日志最后一轮——脚本未写指标文件)" : ""}
                        </p>
                        <div className="flex flex-wrap gap-2">
                          {Object.entries(run.metrics).map(([name, value]) => (
                            <span key={name} className="rounded-full bg-info/10 px-2 py-0.5 font-mono text-caption text-info">
                              {name}: {String(value)}
                            </span>
                          ))}
                        </div>
                        {passed ? (
                          <p className="mt-1.5 flex items-center gap-1 text-micro font-medium text-success">
                            <CheckCircle2 className="size-3" />
                            Baseline 通过:{state?.datasets.length ?? 1} 个数据集全部跑通并取得基准指标
                          </p>
                        ) : confirmable ? (
                          <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border-[0.5px] border-success/30 bg-success/5 px-2.5 py-2">
                            <span className="text-caption text-muted-foreground">
                              已跑通的数据集将纳入基线池——确认后提炼方向,迭代先在池内数据集上展开;其余数据集补跑后追加纳入
                            </span>
                            <Button size="sm" className="ml-auto h-7 gap-1 px-2.5 text-caption"
                                    disabled={busy !== null} onClick={() => void confirmPass()}>
                              {busy === "pass" ? <Loader2 className="size-3 animate-spin" /> : <CheckCircle2 className="size-3" />}
                              {busy === "pass" ? "确认中…" : "确认通过 Baseline"}
                            </Button>
                          </div>
                        ) : null}
                      </div>
                    ) : run.exit_code === 0 ? (
                      <p className="flex items-center gap-1 text-caption text-warning">
                        <AlertTriangle className="size-3.5" />
                        跑通但没拿到指标:训练脚本既没写指标文件,日志里也没解析出指标。
                        需要在训练命令后追加把指标写到 $AUTOLAB_OUTPUT_DIR/metrics.json
                        的逻辑(可让实现 Agent 改),或在日志里打印「指标名: 数值」格式的结果行
                      </p>
                    ) : (
                      <div className="space-y-2">
                        <p className="flex items-center gap-1 text-caption text-destructive">
                          <AlertTriangle className="size-3.5" />
                          运行失败{run.dataset && run.dataset !== "default" ? `(数据集 ${run.dataset})` : ""}:
                          可让修复 Agent 读日志打最小补丁并自动重跑
                        </p>
                        {(run.fix_attempts ?? 0) > 0 && run.diagnosis ? (
                          <div className="rounded-lg border border-warning/40 bg-warning/5 p-2.5">
                            <p className="text-caption font-semibold text-warning">
                              自动修复已尝试 {run.fix_attempts} 次仍未解决 —— 需要你手动处理。
                              诊断:{run.diagnosis.title}
                            </p>
                            <ol className="mt-1.5 list-decimal space-y-0.5 pl-5 text-caption text-muted-foreground">
                              {run.diagnosis.steps.map((step) => (
                                <li key={step}>{step}</li>
                              ))}
                            </ol>
                          </div>
                        ) : run.diagnosis ? (
                          <div className="rounded-lg border-[0.5px] border-surface-border bg-muted/40 p-2.5">
                            <p className="text-caption font-medium text-foreground">
                              失败诊断:{run.diagnosis.title}
                            </p>
                            <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-caption text-muted-foreground">
                              {run.diagnosis.steps.map((step) => (
                                <li key={step}>{step}</li>
                              ))}
                            </ol>
                          </div>
                        ) : null}
                        <div className="flex flex-wrap items-center gap-2">
                          <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-caption"
                                  disabled={busy !== null || running}
                                  onClick={() => void agentFix()}>
                            {busy === "fix" ? <Loader2 className="size-3 animate-spin" /> : <Sparkles className="size-3" />}
                            {busy === "fix" ? "Agent 修复中…" : "Agent 修复并重跑"}
                          </Button>
                          {fixSummary ? (
                            <span className="min-w-0 flex-1 text-micro text-muted-foreground [overflow-wrap:anywhere]">
                              {fixSummary}
                            </span>
                          ) : null}
                        </div>
                        {/* 修复会话实时面板:看 Agent 读日志/定位/打补丁,可注入引导/停止 */}
                        <LiveSessionCard projectId={project.id} />
                      </div>
                    )}
                  </section>
                ) : null}
              </>
            ) : (
              <div className="flex h-full min-h-40 items-center justify-center rounded-lg border border-dashed border-surface-border">
                <p className="flex items-center gap-1.5 text-caption text-muted-foreground">
                  <CircleDashed className="size-4" />
                  一键 Baseline:就位 → 构建 → 查数据集 → 跑
                </p>
              </div>
            )}
            <p className="text-micro text-muted-foreground">
              <button type="button" className="inline-flex items-center gap-1 underline hover:text-foreground"
                      onClick={refreshAll}>
                <RefreshCw className="size-3" />
                刷新状态
              </button>
            </p>
        </div>
      </div>
    </div>
  );
}

export default function BaselinePage() {
  const { data: projects = [] } = useQuery({
    queryKey: ["projects"],
    queryFn: fetchProjects,
    refetchInterval: 8000,
  });
  const requested = projects.filter((p) => p.baselineRequestedAt);
  const [selectedId, setSelectedId] = useState<string>("");
  // 看板卡片跳转带 ?project= 预选(client-only 读取,不参与 SSR)
  useEffect(() => {
    const from = new URLSearchParams(window.location.search).get("project");
    if (from) setSelectedId(from);
  }, []);
  const selected = requested.find((p) => p.id === selectedId) ?? requested[0];

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <FlaskConical className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">Baseline</h1>
        {requested.length > 0 ? (
          <span className="font-mono text-caption tabular-nums text-muted-foreground">
            {requested.length}
          </span>
        ) : null}
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          一键:代码就位 → 环境构建 → 数据集检查 → 跑通拿指标
        </p>
      </PageHeader>

      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${PAGE_RAIL} space-y-3 py-4`}>
          {requested.length === 0 ? (
            <div className="flex min-h-[60vh] flex-col items-center justify-center text-center">
              <EmptyState
                icon={FlaskConical}
                title="还没有 Baseline 申请"
                description="「提 Baseline」按钮在项目页:补全代码 / 数据集信息后一键提交,项目会出现在这里。"
                action={
                  <Link href="/projects">
                    <Button variant="outline" size="sm" className="gap-1.5">
                      <FolderKanban className="size-3.5" />
                      去项目页提 Baseline
                    </Button>
                  </Link>
                }
              />
            </div>
          ) : (
            <>
              {requested.length > 1 ? (
                <div className="flex flex-wrap gap-2">
                  {requested.map((p) => (
                    <button
                      key={p.id}
                      type="button"
                      onClick={() => setSelectedId(p.id)}
                      className={`rounded-full px-3 py-1 text-caption font-medium transition-colors ${
                        p.id === selected?.id
                          ? "bg-accent text-accent-foreground"
                          : "bg-muted/50 text-muted-foreground hover:bg-muted"
                      }`}
                    >
                      {p.name}
                    </button>
                  ))}
                </div>
              ) : null}
              {selected ? <BaselinePanel key={selected.id} project={selected} /> : null}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
