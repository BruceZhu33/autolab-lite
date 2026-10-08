"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Activity, AlertTriangle, CheckCircle2, ChevronDown, CircleDashed, CircleDot,
  CloudUpload, FileUp, FlaskConical, FolderKanban, HardDrive, Loader2, Package,
  Play, RefreshCw, SearchCode, Sparkles, Terminal, Wand2,
} from "lucide-react";
import {
  askBaselineAdvisor, buildEnvironment, checkCodePresence, confirmBaselinePass,
  proposeBaselineIntake, setBaselineStep,
  fetchBaselineState, fixBaselineRun, fetchProjects,
  fetchServerEnvironments,
  pollBaselineRun, pollEnvBuild, provideAsset, runBaselineCheck,
  saveBaselineConfig, startBaselineRun, syncCode, uploadAsset,
  type BaselineCheck, type BaselineConfig, type BaselineIntakeProposal,
  type ProjectListItem, type ServerEnvironment,
} from "@/lib/api";
import { LiveSessionCard } from "@/components/live-session-card";
import { EmptyState } from "@/components/empty-state";
import { FolderBrowserDialog } from "@/components/folder-browser";
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

/** D-030 六步固定接入流程:确定性校验 + 提议确认,agent 只提议不自主执行。 */
type Step = "code" | "assets" | "env" | "intake" | "run" | "confirm";

const STEP_ORDER: Step[] = ["code", "assets", "env", "intake", "run", "confirm"];

const STEP_SHORT: Record<Step, string> = {
  code: "代码", assets: "资料", env: "环境", intake: "口径", run: "跑通", confirm: "确认",
};

const STEP_TITLES: Record<Step, string> = {
  code: "代码就位", assets: "资料就位", env: "环境就位",
  intake: "入口与口径", run: "跑通基线", confirm: "基线确认",
};

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

/** D-031 每步 set 门的状态条:set = owner 显式确认这一步、解锁下一步;
 * unset = 回退(补数据集/改环境后重新 set)。事实校验不通过时 Set 不可点,
 * 原因直接写在按钮旁边——不满足就回步骤内容里补。 */
function WorkflowSetBar({ step, isSet, orderOk, valid, invalidReason, busy,
                          onSet, onUnset }: {
  step: "code" | "assets" | "env" | "intake";
  isSet: boolean; orderOk: boolean; valid: boolean; invalidReason?: string;
  busy: boolean; onSet: () => void; onUnset: () => void;
}) {
  if (isSet) {
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-lg border-[0.5px] border-success/30 bg-success/5 px-2.5 py-1.5">
        <CheckCircle2 className="size-3.5 shrink-0 text-success" />
        <span className="text-caption font-medium text-success">
          已 set(这一步锁定)
        </span>
        <span className="min-w-0 flex-1 text-micro text-muted-foreground">
          {step === "assets"
            ? "要补充数据集:取消 set → 上传/链接 → 重新 set,跑阶段会把清单里的都跑一遍"
            : step === "intake"
              ? "要改口径:取消 set(会连带动后续),编辑清单后重新 set"
              : "要回退修改:取消 set 后再改"}
        </span>
        <Button variant="outline" size="sm"
                className="h-6 px-2 text-micro text-muted-foreground"
                disabled={busy} onClick={onUnset}>
          取消 set
        </Button>
      </div>
    );
  }
  if (!orderOk) {
    return (
      <p className="text-micro text-muted-foreground/70">
        按顺序走:先 set 上一步,这一步才解锁
      </p>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" className="h-7 gap-1 px-2.5 text-caption"
              disabled={busy || !valid} onClick={onSet}>
        <CheckCircle2 className="size-3.5" />
        Set · 确认这一步,进入下一步
      </Button>
      {!valid ? (
        <span className="text-micro text-warning">
          {invalidReason || "本步条件还没满足"}
        </span>
      ) : null}
    </div>
  );
}

/** 串行步骤节:Baseline 是严格串行流程,一次只展开当前步骤;已完成折叠为
 * 一行摘要,可点标题回看,未到的折叠为待办。locked:一键执行/前序未 set——
 * 标题箭头冻结;dim:前序未 set 的内容步整体降透明度(路线图可见但不可动)。 */
function StepSection({ index, title, state, summary, open, onToggle, locked, dim, children }: {
  index: number; title: string;
  state: "done" | "active" | "pending" | "failed";
  summary?: string; open: boolean; onToggle: () => void;
  locked?: boolean; dim?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section className={`rounded-xl border-[0.5px] transition-colors ${
      state === "active" ? "border-brand/40 bg-brand/[0.03]"
      : state === "failed" ? "border-warning/40 bg-warning/[0.03]"
      : "border-surface-border bg-surface"} ${dim ? "opacity-55" : ""}`}>
      <button type="button" onClick={onToggle} disabled={locked}
              title={locked ? "一键执行中或前序步骤未 set,暂时锁定" : undefined}
              className={`flex w-full items-center gap-2.5 rounded-xl px-3.5 py-2.5 text-left transition-colors ${
                locked ? "cursor-not-allowed" : "hover:bg-surface-hover/60"}`}>
        <StepHeader index={index} title={title} state={state} />
        {!open && summary ? (
          <span className="min-w-0 flex-1 truncate text-caption text-muted-foreground" title={summary}>
            {summary}
          </span>
        ) : (
          <span className="flex-1" />
        )}
        <ChevronDown className={`size-3.5 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-180" : ""} ${locked ? "opacity-40" : ""}`} />
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
  const [browsing, setBrowsing] = useState(false);

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
            <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-micro"
                    disabled={busy !== null}
                    title="在服务器目录树里选(不用手抄路径)"
                    onClick={() => setBrowsing(true)}>
              <FolderKanban className="size-3" />
              浏览
            </Button>
            <Button size="sm" className="h-7 gap-1 px-2 text-micro"
                    disabled={busy !== null || !serverPath.trim()}
                    onClick={() => void run("link")}>
              {busy === "link" ? <Loader2 className="size-3 animate-spin" /> : <HardDrive className="size-3" />}
              链接
            </Button>
          </div>
          <FolderBrowserDialog
            open={browsing} onOpenChange={setBrowsing}
            source={{ kind: "server", serverId: project.server.id }}
            title="选服务器上的数据/权重目录"
            onPick={(p) => { setServerPath(p); setBrowsing(false); }} />
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
  const [browsing, setBrowsing] = useState(false);

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
        <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-micro"
                disabled={busy !== null}
                title="在服务器目录树里选(不用手抄路径)"
                onClick={() => setBrowsing(true)}>
          <FolderKanban className="size-3" />
          浏览
        </Button>
        <Button size="sm" className="h-7 gap-1 px-2 text-micro"
                disabled={busy !== null || !serverPath.trim()}
                onClick={() => void run("link")}>
          {busy === "link" ? <Loader2 className="size-3 animate-spin" /> : <HardDrive className="size-3" />}
          链接
        </Button>
      </div>
      <FolderBrowserDialog
        open={browsing} onOpenChange={setBrowsing}
        source={{ kind: "server", serverId: project.server.id }}
        title={`选 ${label} 的服务器目录`}
        onPick={(p) => { setServerPath(p); setBrowsing(false); }} />
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

/** D-030 入口与口径提案表单:分析 Agent 的只读提案在此变成 owner 的
 * 编辑稿——命令/环境/指标文件/口径清单逐项可改,「采用并保存」才落库。 */
function IntakeProposalForm({ project, proposal, disabled, onAdopted, onDiscard }: {
  project: ProjectListItem;
  proposal: BaselineIntakeProposal;
  disabled?: boolean;
  onAdopted: () => void;
  onDiscard: () => void;
}) {
  const queryClient = useQueryClient();
  const p = proposal.proposal;
  const [command, setCommand] = useState(p.command);
  const [condaEnv, setCondaEnv] = useState(p.conda_env);
  const [metricFile, setMetricFile] = useState(p.metric_file);
  const [rows, setRows] = useState(p.datasets.map((d) => ({ ...d })));
  const [busy, setBusy] = useState(false);
  const rowsValid = rows.length > 0
    && rows.every((r) => r.name.trim() && r.command.trim());

  const adopt = async () => {
    setBusy(true);
    try {
      await saveBaselineConfig(project.id, {
        datasets: rows, command, conda_env: condaEnv, metric_file: metricFile,
      });
      toast.success(`已采用并保存:${rows.length} 条口径(入口与口径已确认)`, {
        description: "清单即 Baseline 真相源;缺的数据集到「资料就位」补,补齐后起跑",
        duration: 8000,
      });
      void queryClient.invalidateQueries({ queryKey: ["baseline", project.id] });
      onAdopted();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-2 rounded-lg border-[0.5px] border-brand/30 bg-brand/[0.03] p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <Wand2 className="size-3.5 text-brand" />
        <span className="text-caption font-semibold text-brand">
          Agent 提案(未落库——检查每行后采用)
        </span>
        {proposal.fallback ? (
          <span className="rounded-full bg-warning/10 px-2 py-0.5 text-micro font-medium text-warning">
            规则法提案(分析会话不可用)
          </span>
        ) : null}
        <span className="ml-auto flex gap-1.5">
          <Button variant="outline" size="sm" className="h-7 px-2 text-micro"
                  disabled={busy || disabled} onClick={onDiscard}>
            放弃
          </Button>
          <Button size="sm" className="h-7 gap-1 px-2.5 text-micro"
                  disabled={busy || disabled || !rowsValid || !command.trim()}
                  onClick={() => void adopt()}>
            {busy ? <Loader2 className="size-3 animate-spin" /> : <CheckCircle2 className="size-3" />}
            采用并保存
          </Button>
        </span>
      </div>
      <div className="mt-2 grid gap-1.5 sm:grid-cols-3">
        <label className="grid gap-1">
          <span className="text-micro text-muted-foreground">默认命令(清单首行会镜像)</span>
          <Input className="h-7 font-mono text-micro" value={command} spellCheck={false}
                 onChange={(e) => setCommand(e.target.value)} />
        </label>
        <label className="grid gap-1">
          <span className="text-micro text-muted-foreground">conda 环境(建议,可改选)</span>
          <Input className="h-7 font-mono text-micro" value={condaEnv} spellCheck={false}
                 placeholder="未定"
                 onChange={(e) => setCondaEnv(e.target.value)} />
        </label>
        <label className="grid gap-1">
          <span className="text-micro text-muted-foreground">指标文件</span>
          <Input className="h-7 font-mono text-micro" value={metricFile} spellCheck={false}
                 onChange={(e) => setMetricFile(e.target.value)} />
        </label>
      </div>
      <div className="mt-2 space-y-1.5">
        {rows.map((row, i) => (
          <div key={i} className="flex items-center gap-1.5">
            <Input className="h-7 w-28 shrink-0 font-mono text-micro"
                   value={row.name} placeholder="名称(Kvasir-10)" spellCheck={false}
                   onChange={(e) => setRows(rows.map((r, j) =>
                     j === i ? { ...r, name: e.target.value } : r))} />
            <Input className="h-7 min-w-0 flex-1 font-mono text-micro"
                   value={row.command} placeholder="训练命令(3ep 冒烟档)" spellCheck={false}
                   onChange={(e) => setRows(rows.map((r, j) =>
                     j === i ? { ...r, command: e.target.value } : r))} />
            <Input className="h-7 w-36 shrink-0 font-mono text-micro"
                   value={row.dataset_path} placeholder="data/Kvasir" spellCheck={false}
                   onChange={(e) => setRows(rows.map((r, j) =>
                     j === i ? { ...r, dataset_path: e.target.value } : r))} />
            <Button variant="ghost" size="sm" className="size-7 shrink-0 p-0 text-muted-foreground"
                    disabled={busy || disabled || rows.length <= 1}
                    title="移除该行" onClick={() => setRows(rows.filter((r, j) => j !== i))}>
              ×
            </Button>
          </div>
        ))}
        <Button variant="outline" size="sm" className="h-7 px-2 text-micro"
                disabled={busy || disabled}
                onClick={() => setRows([...rows, { name: "", command: "", dataset_path: "" }])}>
          加一行
        </Button>
      </div>
      {Object.keys(proposal.evidence).length > 0 ? (
        <details className="mt-1.5">
          <summary className="cursor-pointer text-micro text-muted-foreground">
            提案出处(evidence)
          </summary>
          <ul className="mt-1 space-y-0.5">
            {Object.entries(proposal.evidence).map(([k, v]) => (
              <li key={k} className="text-micro text-muted-foreground">
                <span className="font-medium">{k}</span>:{String(v).slice(0, 160)}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

function BaselinePanel({ project, onLockChange }: {
  project: ProjectListItem;
  /** 一键执行期间向上通报锁定(页级项目切换tabs同步禁用) */
  onLockChange?: (locked: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [autoSeconds, setAutoSeconds] = useState(0);
  const [stepState, setStepState] = useState<Partial<Record<Step, "done" | "active" | "pending" | "failed">>>({});
  const [presence, setPresence] = useState<{ present: boolean; reason: string; files: string[] } | null>(null);
  const [sources, setSources] = useState<{ githubUrl: string | null; localWorkspace: string | null }>({ githubUrl: null, localWorkspace: null });
  const [syncDetail, setSyncDetail] = useState<string | null>(null);
  const [envPlan, setEnvPlan] = useState<{ command: string; detail: string } | null>(null);
  const [envPid, setEnvPid] = useState<number | null>(null);
  const [gap, setGap] = useState<{ kind: "dataset" | "command"; hint: string } | null>(null);
  const [gapValue, setGapValue] = useState("");
  // 步骤折叠覆盖(声明在此:一键走流程的 stopAt 要用;stepOpen/toggleStep 在下方)
  const [openSteps, setOpenSteps] = useState<Partial<Record<Step, boolean>>>({});

  // 一键执行中:计时(配置中的"活着"信号)+ 通知页级锁定
  const autoBusy = busy === "auto";
  useEffect(() => {
    onLockChange?.(autoBusy);
  }, [autoBusy, onLockChange]);
  useEffect(() => {
    if (!autoBusy) { setAutoSeconds(0); return; }
    const timer = setInterval(() => setAutoSeconds((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, [autoBusy]);

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

  /** D-030 提议:分析 Agent(只读)读代码 → 入口/口径清单(3ep 冒烟档)提案。
   * 只返回提案不落库;owner 在「入口与口径」步骤编辑后采用保存。 */
  const [proposal, setProposal] = useState<BaselineIntakeProposal | null>(null);
  const [proposing, setProposing] = useState(false);
  const propose = async (): Promise<boolean> => {
    setProposing(true);
    setProposal(null);
    try {
      const p = await proposeBaselineIntake(project.id);
      setProposal(p);
      toast.success(`提议完成:${p.proposal.datasets.length} 条口径(带 3ep 冒烟档)`, {
        description: (p.fallback ? "规则法提案(分析会话不可用):" : "检查每条命令与路径后点「采用并保存」;")
          + Object.entries(p.evidence).filter(([k]) => k !== "analyzer_session")
            .slice(0, 2).map(([k, v]) => `${k}: ${String(v).slice(0, 60)}`).join(";"),
        duration: 10000,
      });
      return true;
    } catch (e) {
      toast.error(`提议失败:${(e as Error).message}`, {
        description: "可在「入口与口径」步骤手工填清单,或补齐资料后再试",
      });
      return false;
    } finally {
      setProposing(false);
    }
  };

  /** 流程助手(D-030):单发 LLM「缺什么/放哪/下一步」;条件变化即失效。 */
  const [advice, setAdvice] = useState<string | null>(null);
  const [advising, setAdvising] = useState(false);
  const askAdvice = async () => {
    setAdvising(true);
    try {
      const r = await askBaselineAdvisor(project.id);
      setAdvice(r.advice);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setAdvising(false);
    }
  };
  useEffect(() => { setAdvice(null); }, [state?.checks, state?.config]);

  /** 一键走流程(D-030/031):按六步固定流程顺序推进——确定性步骤自动
   * Set(服务端逐项校验,不满足即停),遇缺口/待确认就停在对应步骤;
   * 提议结果永远等 owner 编辑确认。 */
  const runAuto = async () => {
    setBusy("auto");
    setGap(null);
    setStepState({});
    const stopAt = (step: Step, why: string) => {
      setStepState((s) => ({ ...s, [step]: "active" }));
      setOpenSteps((m) => ({ ...m, [step]: true }));
      toast.info(`停在「${STEP_TITLES[step]}」:${why}`, { duration: 6000 });
    };
    try {
      // ① 代码就位 → set(服务端校验代码在位)
      setStepState((s) => ({ ...s, code: "active" }));
      if (!wf.code) {
        const code = await doCodeCheck();
        let present = code.present;
        if (!present && (code.sources.githubUrl || code.sources.localWorkspace)) {
          toast.info("服务器上没有代码,自动同步…");
          present = await doCodeSync();
        }
        if (!present || !(await autoSet("code"))) {
          fail("code", "代码未就位:请在项目编辑里补 GitHub 链接或本地工作区路径");
          return;
        }
      }
      setStepState((s) => ({ ...s, code: "done" }));

      // ② 资料就位 → set(服务端重跑体检,缺资料即未就位)
      setStepState((s) => ({ ...s, assets: "active" }));
      if (!wf.assets) {
        const fresh = await runBaselineCheck(project.id);
        void refetch();
        const fails = fresh.checks.filter((c) => c.status === "fail");
        if (fails.length > 0) {
          setStepState((s) => ({ ...s, assets: "failed" }));
          stopAt("assets", `体检 ${fails.length} 项未过(缺资料就在这步链接/上传补齐)`);
          return;
        }
        if (!(await autoSet("assets"))) {
          stopAt("assets", "体检未全绿,补齐资料后重试");
          return;
        }
      }
      setStepState((s) => ({ ...s, assets: "done" }));

      // ③ 环境就位 → set(已选环境);没选且口径未确认 → 先提议(提案带环境建议)
      setStepState((s) => ({ ...s, env: "active" }));
      if (!wf.env) {
        if (!state?.config.conda_env && !intakeConfirmed) {
          setStepState((s) => ({ ...s, intake: "active" }));
          const ok = await propose();
          stopAt("intake", ok
            ? "Agent 已提议入口与口径清单——编辑后「采用并保存」(环境建议一并保存),再回来一键走流程"
            : "提议会话不可用——手工填清单后保存,或补齐资料后重试");
          return;
        }
        if (!state?.config.conda_env) {
          stopAt("env", "还没选 conda 环境(点选已有环境,或先在④采用提案)");
          return;
        }
        if (!(await autoSet("env"))) return;
      }
      setStepState((s) => ({ ...s, env: "done" }));

      // ④ 入口与口径 → set(清单已确认)
      setStepState((s) => ({ ...s, intake: "active" }));
      if (!wf.intake) {
        if (!intakeConfirmed) {
          const ok = await propose();
          stopAt("intake", ok
            ? "提案已出——检查/编辑后「采用并保存」,再一键走流程继续"
            : "提议会话不可用——手工填清单后保存");
          return;
        }
        if (!(await autoSet("intake"))) return;
      }
      setStepState((s) => ({ ...s, intake: "done" }));

      // ⑤ 起跑:清单里的数据集按顺序都跑一遍(未就绪自动跳过,补齐后追加)
      setStepState((s) => ({ ...s, run: "active" }));
      const run = await startBaselineRun(project.id);
      const skippedNote = (run as { skipped?: string[] }).skipped?.length
        ? `;跳过未就绪 ${((run as { skipped?: string[] }).skipped ?? []).length} 个(补齐后追加)`
        : "";
      toast.success(
        `已在 ${project.server.name} 上起跑 ${run.dataset}(PID ${run.pid})`, {
          description: run.queue.length > 0
            ? `按清单全跑:${[run.dataset, ...run.queue].join(" → ")}${skippedNote}`
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

  /** 六步状态全部派生(D-030:确定性校验,无持久化 workflow 状态)。
   * 一键走流程的 stepState 覆盖优先(执行中的实时步)。 */
  const supplyGapCount = checks.filter((c) =>
    c.status === "fail" && (c.supply?.length ?? 0) > 0).length;
  const hardFailCount = checks.filter((c) =>
    c.status === "fail" && !(c.supply?.length ?? 0)).length;
  const intakeConfirmed = !!state?.config.onboarded
    && (state?.config.datasets?.length ?? 0) > 0;
  const anyOk = (state?.datasets ?? []).some((d) => d.ok);

  // ---------------------------------------------------------- D-031 set 门
  const CONTENT_STEPS = ["code", "assets", "env", "intake"] as const;
  type ContentStep = (typeof CONTENT_STEPS)[number];
  const wf = state?.config.workflow
    ?? { code: null, assets: null, env: null, intake: null };
  const firstUnsetIdx = CONTENT_STEPS.findIndex((s) => !wf[s]);
  const allSet = firstUnsetIdx === -1;
  /** 内容步的顺序锁:前序未 set 时本步不可交互(仍可见,路线图不藏)。 */
  const orderLocked = (s: ContentStep): boolean =>
    CONTENT_STEPS.indexOf(s) > (allSet ? CONTENT_STEPS.length : firstUnsetIdx);
  const stepValidity = (s: ContentStep): { valid: boolean; invalidReason?: string } => {
    if (s === "code") return codeReady === true
      ? { valid: true } : { valid: false, invalidReason: "代码未就位" };
    if (s === "assets") return checks.length === 0
      ? { valid: false, invalidReason: "先跑一次体检" }
      : hardFailCount + supplyGapCount > 0
        ? { valid: false, invalidReason: `体检有 ${hardFailCount + supplyGapCount} 项未过(缺资料即未就位)` }
        : { valid: true };
    if (s === "env") return state?.config.conda_env
      ? { valid: true } : { valid: false, invalidReason: "还没选 conda 环境" };
    return intakeConfirmed ? { valid: true }
      : { valid: false, invalidReason: "口径清单未确认(提议/手填后保存)" };
  };
  const doSetStep = async (action: "set" | "unset", step: ContentStep) => {
    setBusy(`wf-${step}`);
    try {
      await setBaselineStep(project.id, action, step);
      toast.success(action === "set"
        ? `「${STEP_TITLES[step]}」已 set`
        : "已取消 set:补充/修改后重新 set 再前进");
      void refetch();
      if (action === "unset") {
        setOpenSteps((m) => ({ ...m, [step]: true }));
      }
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  /** runAuto 内部用:静默 set 一步(失败返回 false 并 toast)。 */
  const autoSet = async (step: ContentStep): Promise<boolean> => {
    try {
      await setBaselineStep(project.id, "set", step);
      return true;
    } catch (e) {
      toast.info(`「${STEP_TITLES[step]}」set 未通过:${(e as Error).message}`, {
        duration: 8000,
      });
      return false;
    }
  };
  const stepStatus = (step: Step): "done" | "active" | "pending" | "failed" => {
    if (stepState[step]) return stepState[step];
    // set 门的四步:已 set 即 done(owner 确认优先于派生)
    if ((CONTENT_STEPS as readonly string[]).includes(step)
        && wf[step as ContentStep]) {
      return "done";
    }
    return (
      step === "code" && codeReady === true ? "done"
      : step === "code" && codeReady === false ? "failed"
      : step === "assets"
        ? (checks.length === 0 ? "pending"
          : hardFailCount > 0 ? "failed"
          : supplyGapCount > 0 ? "active" : "done")
      : step === "env" && state?.config.conda_env ? "done"
      : step === "intake" && intakeConfirmed ? "done"
      : step === "intake" && (proposing || proposal) ? "active"
      : step === "run" && passed ? "done"
      : step === "run" && running ? "active"
      : step === "run" && anyOk ? "done"
      : step === "run" && run?.status === "finished" ? "failed"
      : step === "confirm" && passed ? "done"
      : step === "confirm" && (passed || anyOk) ? "active"
      : "pending");
  };

  // 串行展开:默认只展开第一个未完成步骤(失败强制展开);点击标题可覆盖
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
                {busy === "auto" ? `走流程中… ${autoSeconds}s` : "一键走流程"}
              </Button>
            )}
          </span>
        </div>

        {/* 流程助手条(D-030):确定性六步点列 + 缺口计数 + Agent 单发建议。
            流程是写死的,agent 只做总揽——告诉你缺什么、放哪、下一步点什么。 */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-surface-border px-4 py-2.5">
          {STEP_ORDER.map((s, i) => {
            const st = stepStatus(s);
            return (
              <span key={s} className="flex items-center gap-1">
                <span className={`size-2 shrink-0 rounded-full ${
                  st === "done" ? "bg-success"
                  : st === "active" ? "bg-brand animate-pulse"
                  : st === "failed" ? "bg-destructive" : "bg-muted-foreground/30"}`} />
                <span className={`text-micro ${st === "pending" ? "text-muted-foreground/60" : "font-medium text-foreground"}`}>
                  {i + 1}·{STEP_SHORT[s]}
                </span>
              </span>
            );
          })}
          <span className="text-micro text-muted-foreground">
            {supplyGapCount > 0 ? `资料缺 ${supplyGapCount}` : ""}
            {supplyGapCount > 0 && !state?.config.conda_env ? " · " : ""}
            {!state?.config.conda_env ? "环境未选" : ""}
            {!state?.config.conda_env && !intakeConfirmed ? " · " : ""}
            {!intakeConfirmed ? "口径未确认" : ""}
            {supplyGapCount === 0 && state?.config.conda_env && intakeConfirmed
              ? "前置就绪,可起跑" : ""}
          </span>
          <span className="ml-auto flex items-center gap-1.5">
            {advising || advice ? (
              <span className="max-w-md truncate rounded-lg border-[0.5px] border-brand/25 bg-brand/5 px-2 py-1 text-micro text-foreground"
                    title={advice ?? ""}>
                <Wand2 className="mr-1 inline size-3 text-brand" />
                {advising ? "Agent 分析中…" : advice}
              </span>
            ) : null}
            <Button variant="outline" size="sm" className="h-6 gap-1 px-2 text-micro"
                    disabled={advising || busy !== null} onClick={() => void askAdvice()}>
              {advising ? <Loader2 className="size-3 animate-spin" /> : <Wand2 className="size-3" />}
              Agent 分析下一步
            </Button>
          </span>
        </div>

        <div className="max-w-3xl space-y-2.5 p-4">
            {/* ① 代码就位 */}
            <StepSection index={1} title="代码就位" state={stepStatus("code")}
                         locked={autoBusy} dim={orderLocked("code")}
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
                      一键走流程会自动同步(GitHub 或本地工作区);
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
              <WorkflowSetBar step="code" isSet={!!wf.code} orderOk={!orderLocked("code")}
                              {...stepValidity("code")} busy={busy !== null}
                              onSet={() => void doSetStep("set", "code")}
                              onUnset={() => void doSetStep("unset", "code")} />
            </StepSection>

            {/* ② 资料就位(D-030 资料槽):体检派生的确定性缺口——数据集/权重/
                声明路径;每槽 = 链接服务器目录 或 上传 zip,补完自动复核。 */}
            <StepSection index={2} title="资料就位" state={stepStatus("assets")}
                         locked={autoBusy} dim={orderLocked("assets")}
                         summary={checks.length === 0
                           ? "跑一次体检看缺什么(代码/数据/权重/环境)"
                           : (() => {
                               const gapsN = checks.filter((c) =>
                                 c.status === "fail" && (c.supply?.length ?? 0) > 0).length;
                               const hardN = checks.filter((c) =>
                                 c.status === "fail" && !(c.supply?.length ?? 0)).length;
                               const okN = checks.filter((c) => c.status === "ok").length;
                               return gapsN > 0
                                 ? `缺资料 ${gapsN} 项(链接或上传即就位)`
                                 : hardN > 0
                                   ? `${okN} 通过 · ${hardN} 项阻塞`
                                   : `${okN} 项通过,资料齐`;
                             })()}
                         open={stepOpen("assets")} onToggle={() => toggleStep("assets")}>
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
                <p className="text-caption text-muted-foreground">
                  体检扫描服务器工作区:数据集/权重在位即可通过,缺了会在这里列出归位路径
                </p>
              )}
              <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-caption"
                      disabled={busy !== null} onClick={() => void doCheck()}>
                <SearchCode className="size-3" />
                重新体检
              </Button>
              <WorkflowSetBar step="assets" isSet={!!wf.assets} orderOk={!orderLocked("assets")}
                              {...stepValidity("assets")} busy={busy !== null}
                              onSet={() => void doSetStep("set", "assets")}
                              onUnset={() => void doSetStep("unset", "assets")} />
            </StepSection>

            {/* ③ 环境就位:点选服务器已有环境(确定性);没有合适的在服务器上建好再探测 */}
            <StepSection index={3} title="环境就位" state={stepStatus("env")}
                         locked={autoBusy} dim={orderLocked("env")}
                         summary={state?.config.conda_env
                           ? `已选 ${state.config.conda_env}`
                           : "点选服务器已有 conda 环境(含 torch/CUDA 探测)"}
                         open={stepOpen("env")} onToggle={() => toggleStep("env")}>
              {envPlan ? (
                <div className="rounded-lg bg-muted/40 p-2.5">
                  <p className="text-caption text-muted-foreground">{envPlan.detail}</p>
                  {envPlan.command ? (
                    <pre className="mt-1 overflow-x-auto font-mono text-micro text-muted-foreground">{envPlan.command}</pre>
                  ) : (
                    <p className="mt-1 text-caption text-warning">无依赖文件:跳过构建(需要特定环境时在服务器上 conda env create 后重探测)</p>
                  )}
                </div>
              ) : (
                <p className="text-caption text-muted-foreground">
                  从服务器已有环境里点选(提案若给了建议会预填);没有合适的环境,在服务器上
                  用 requirements/environment.yml 建好后重探测。
                </p>
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
              <WorkflowSetBar step="env" isSet={!!wf.env} orderOk={!orderLocked("env")}
                              {...stepValidity("env")} busy={busy !== null}
                              onSet={() => void doSetStep("set", "env")}
                              onUnset={() => void doSetStep("unset", "env")} />
            </StepSection>

            {/* ④ 入口与口径(D-030 提议制):分析 Agent(只读)读代码 → 提案
                (入口命令/口径清单 3ep 冒烟档/环境建议/证据)→ owner 编辑 →
                采用保存。agent 只提议,永远不直接落库。 */}
            <StepSection index={4} title="入口与口径(提议制)" state={stepStatus("intake")}
                         locked={autoBusy || proposing} dim={orderLocked("intake")}
                         summary={proposing
                           ? `Agent 读代码提议中 · ${autoSeconds}s(时间线见本步内)`
                           : proposal
                             ? "已有提案待你检查/编辑后采用"
                             : state?.config.onboarded
                               ? `已确认 · ${(state.config.datasets?.length ?? 0)} 条口径`
                               : "让 Agent 读代码提议入口命令与口径清单,或手工填写"}
                         open={stepOpen("intake")} onToggle={() => toggleStep("intake")}>
              {proposing ? (
                <div className="mb-2 flex flex-wrap items-center gap-2 rounded-lg border-[0.5px] border-brand/30 bg-brand/5 px-2.5 py-2">
                  <Loader2 className="size-3.5 shrink-0 animate-spin text-brand" />
                  <span className="text-caption font-semibold text-brand">
                    提议中 · {autoSeconds}s
                  </span>
                  <span className="text-micro text-muted-foreground">
                    分析 Agent 只读工作区(README/configs/入口 argparse/数据目录),
                    产出提案后你编辑确认——期间界面锁定,可看下方时间线或注入引导
                  </span>
                </div>
              ) : null}
              {proposing || proposal ? <LiveSessionCard projectId={project.id} /> : null}
              {proposal ? (
                <IntakeProposalForm project={project} proposal={proposal}
                                    disabled={busy !== null}
                                    onAdopted={() => {
                                      setProposal(null);
                                      void refetch();
                                      void doCheck();
                                    }}
                                    onDiscard={() => setProposal(null)} />
              ) : !state?.config.onboarded ? (
                <div className="flex flex-wrap items-center gap-2">
                  <Button size="sm" className="h-7 gap-1 px-2.5 text-caption"
                          disabled={busy !== null || proposing}
                          onClick={() => void propose()}>
                    {proposing ? <Loader2 className="size-3 animate-spin" /> : <Wand2 className="size-3.5" />}
                    让 Agent 读代码提议
                  </Button>
                  <span className="text-micro text-muted-foreground">
                    提案含入口命令、数据集×档位清单(3ep 冒烟档)、环境建议与出处;
                    也可直接手工编辑清单(下方)再保存。
                  </span>
                </div>
              ) : null}
              {state?.config.command || state?.config.onboarded ? (
                <div className="pt-1">
                  {state?.config.onboarded ? (
                    <span className="mb-1.5 inline-flex items-center gap-1 rounded-full bg-success/10 px-2 py-0.5 text-micro font-medium text-success">
                      <CheckCircle2 className="size-3" />
                      口径已确认(保存清单即确认;改动后重新保存)
                    </span>
                  ) : null}
                  <DatasetsEditor project={project} config={state.config}
                                  disabled={busy !== null || running}
                                  onSaved={() => void doCheck()} />
                  {state?.config.onboarded ? (
                    <Button variant="outline" size="sm" className="mt-1.5 h-7 gap-1 px-2 text-micro"
                            disabled={busy !== null || proposing}
                            onClick={() => void propose()}>
                      <Wand2 className="size-3" />
                      重新提议(覆盖下方提案,不改已保存清单)
                    </Button>
                  ) : null}
                </div>
              ) : null}
              <WorkflowSetBar step="intake" isSet={!!wf.intake} orderOk={!orderLocked("intake")}
                              {...stepValidity("intake")} busy={busy !== null}
                              onSet={() => void doSetStep("set", "intake")}
                              onUnset={() => void doSetStep("unset", "intake")} />
            </StepSection>

            {/* ⑤ 跑通基线:四步全 set 后解锁;清单里的数据集按顺序都跑一遍 */}
            <StepSection index={5} title="跑通基线" locked={autoBusy || !allSet}
                         dim={!allSet}
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
                    <p className="mt-2 text-caption text-muted-foreground">
                      {pendingAdopt.map((d) => d.name).join("、")} 新跑通——到「基线确认」步骤追加纳入
                    </p>
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

            {/* ⑥ 基线确认(人工确认门):≥1 数据集跑通有指标即可确认;确认即
                定口径——快照指标、停剩余队列,迭代只能选确认池里的数据集。 */}
            <StepSection index={6} title="基线确认" state={stepStatus("confirm")} locked={autoBusy}
                         summary={passed
                           ? `已确认 · ${timeAgo(state?.passedAt ?? "")} 起`
                           : confirmable
                             ? "已可确认(跑通且有指标)"
                             : "≥1 个数据集跑通且有指标后在此确认"}
                         open={stepOpen("confirm")} onToggle={() => toggleStep("confirm")}>
              {passed ? (
                <div className="flex flex-wrap items-center gap-2 rounded-lg border-[0.5px] border-success/30 bg-success/5 px-2.5 py-2">
                  <CheckCircle2 className="size-4 shrink-0 text-success" />
                  <span className="text-caption text-success">
                    Baseline 已确认:基线口径已定,方向页可提炼方向、迭代页可选池内数据集
                  </span>
                </div>
              ) : confirmable ? (
                <div className="flex flex-wrap items-center gap-2 rounded-lg border-[0.5px] border-success/30 bg-success/5 px-2.5 py-2">
                  <span className="text-caption text-muted-foreground">
                    已跑通的数据集将纳入基线池——确认后提炼方向,迭代先在池内数据集上展开;
                    其余数据集补跑后追加纳入。确认会停掉仍在跑的队列(确认即定口径)。
                  </span>
                  <Button size="sm" className="ml-auto h-7 gap-1 px-2.5 text-caption"
                          disabled={busy !== null} onClick={() => void confirmPass()}>
                    {busy === "pass" ? <Loader2 className="size-3 animate-spin" /> : <CheckCircle2 className="size-3" />}
                    {busy === "pass" ? "确认中…" : "确认通过 Baseline"}
                  </Button>
                </div>
              ) : pendingAdopt.length > 0 ? (
                <div className="flex flex-wrap items-center gap-2 rounded-lg border-[0.5px] border-success/30 bg-success/5 px-2.5 py-2">
                  <span className="text-caption text-muted-foreground">
                    {pendingAdopt.map((d) => d.name).join("、")} 跑通且拿到指标——
                    纳入基线后自动迭代就能选它
                  </span>
                  <Button size="sm" className="ml-auto h-7 gap-1 px-2.5 text-caption"
                          disabled={busy !== null} onClick={() => void confirmPass()}>
                    {busy === "pass" ? <Loader2 className="size-3 animate-spin" /> : <CheckCircle2 className="size-3" />}
                    {busy === "pass" ? "纳入中…" : `追加纳入(${pendingAdopt.length})`}
                  </Button>
                </div>
              ) : (
                <p className="text-caption text-muted-foreground">
                  冒烟跑通且拿到指标后,这里确认;确认 = 定基线口径(指标快照 + 停队列)。
                </p>
              )}
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
                          <p className="mt-1.5 text-caption text-muted-foreground">
                            已可确认——到「基线确认」步骤确认通过
                          </p>
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
  /** 面板一键执行中:项目 tabs 禁用(防切走后界面与流程脱节) */
  const [panelLocked, setPanelLocked] = useState(false);
  const onLockChange = useCallback((locked: boolean) => setPanelLocked(locked), []);
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
                      disabled={panelLocked && p.id !== selected?.id}
                      title={panelLocked && p.id !== selected?.id
                        ? "Agent 一键执行中,暂时不能切项目" : undefined}
                      onClick={() => setSelectedId(p.id)}
                      className={`rounded-full px-3 py-1 text-caption font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
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
              {selected ? (
                <BaselinePanel key={selected.id} project={selected} onLockChange={onLockChange} />
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
