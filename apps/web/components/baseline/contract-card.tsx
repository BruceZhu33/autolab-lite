"use client";

/** 评测指标契约 & 保护路径卡片 (Phase B1 & B2.3):
 * - 固化主指标 primary 与方向 (maximize / minimize)
 * - 配置护栏指标 guardrails (不允许超过 max_regress 退化)
 * - 配置受保护路径 (code_paths / data_paths),自动计算并展示 SHA-256 指纹
 * - 配置平台独立评测命令 (eval.command / conda_env / metric_file / timeout_minutes / gpus_per_job)
 */

import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ChevronDown, ChevronUp, Lock, Loader2, Plus, Save, ShieldCheck, Trash2,
} from "lucide-react";
import {
  fetchMetricContract, updateMetricContract,
  type MetricDirection, type MetricGuardrail, type Project,
} from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";

function parsePathList(raw: string): string[] {
  return raw
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function MetricContractCard({ project }: { project: Project }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["baseline-contract", project.id],
    queryFn: () => fetchMetricContract(project.id),
    enabled: !!project.baselinePassedAt,
  });

  const allMetrics = useMemo(() => {
    if (!data?.metrics) return [];
    const set = new Set<string>();
    for (const list of Object.values(data.metrics)) {
      for (const m of list) set.add(m);
    }
    return Array.from(set);
  }, [data?.metrics]);

  const active = data?.contract ?? null;
  const fallback = data?.default ?? {
    primary: allMetrics[0] ?? "",
    direction: "maximize" as MetricDirection,
    guardrails: [],
    eval: {},
  };

  const [primary, setPrimary] = useState("");
  const [direction, setDirection] = useState<MetricDirection>("maximize");
  const [guardrails, setGuardrails] = useState<MetricGuardrail[]>([]);
  const [codePathsText, setCodePathsText] = useState("");
  const [dataPathsText, setDataPathsText] = useState("");
  const [evalCommand, setEvalCommand] = useState("");
  const [condaEnv, setCondaEnv] = useState("");
  const [metricFile, setMetricFile] = useState("metrics.json");
  const [timeoutMinutes, setTimeoutMinutes] = useState("30");
  const [gpusPerJob, setGpusPerJob] = useState("1");

  useEffect(() => {
    const src = active ?? fallback;
    setPrimary(src.primary || allMetrics[0] || "");
    setDirection(src.direction || "maximize");
    setGuardrails(src.guardrails ? src.guardrails.map((g) => ({ ...g })) : []);
    setCodePathsText((src.eval?.code_paths ?? []).join(", "));
    setDataPathsText((src.eval?.data_paths ?? []).join(", "));
    setEvalCommand(src.eval?.command ?? "");
    setCondaEnv(src.eval?.conda_env ?? "");
    setMetricFile(src.eval?.metric_file || "metrics.json");
    setTimeoutMinutes(String(src.eval?.timeout_minutes ?? 30));
    setGpusPerJob(String(src.eval?.gpus_per_job ?? 1));
  }, [active, fallback.primary, fallback.direction, allMetrics]);

  if (!project.baselinePassedAt) return null;

  const addGuardrail = () => {
    const candidate = allMetrics.find((m) => m !== primary) ?? allMetrics[0] ?? "";
    setGuardrails((prev) => [
      ...prev,
      { metric: candidate, direction: "maximize", max_regress: 0.01 },
    ]);
  };

  const save = async () => {
    if (!primary.trim()) {
      toast.error("请选择主指标 (primary)");
      return;
    }
    setSaving(true);
    try {
      await updateMetricContract(project.id, {
        primary: primary.trim(),
        direction,
        guardrails: guardrails.map((g) => ({
          metric: g.metric,
          direction: g.direction,
          max_regress: Number(g.max_regress) || 0,
        })),
        eval: {
          command: evalCommand.trim(),
          conda_env: condaEnv.trim(),
          metric_file: metricFile.trim() || "metrics.json",
          timeout_minutes: Math.max(1, parseInt(timeoutMinutes, 10) || 30),
          gpus_per_job: Math.max(1, parseInt(gpusPerJob, 10) || 1),
          code_paths: parsePathList(codePathsText),
          data_paths: parsePathList(dataPathsText),
        },
      });
      toast.success("评测指标契约已固化");
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: ["baseline-contract", project.id] });
    } catch (err) {
      toast.error(`保存契约失败:${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-2.5 rounded-xl border-[0.5px] border-surface-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <ShieldCheck className="size-4 text-brand" />
        <span className="text-caption font-semibold">评测指标契约与防篡改保护</span>
        {isLoading ? (
          <Loader2 className="size-3.5 animate-spin text-muted-foreground" />
        ) : active ? (
          <span className="rounded-full bg-success/10 px-2 py-0.5 font-mono text-micro font-medium text-success">
            已固化: {active.primary} ({active.direction === "maximize" ? "越大越好 ↑" : "越小越好 ↓"})
          </span>
        ) : (
          <span className="rounded-full bg-muted/60 px-2 py-0.5 text-micro text-muted-foreground">
            未固化契约 (默认按首个命中基线的指标判定)
          </span>
        )}
        {active?.guardrails?.map((g, i) => (
          <span
            key={`${g.metric}-${i}`}
            className="rounded-full bg-warning/10 px-2 py-0.5 font-mono text-micro text-warning"
          >
            护栏: {g.metric} {g.direction === "maximize" ? "↑" : "↓"} 退化≤{g.max_regress}
          </span>
        ))}
        {active?.eval?.command ? (
          <span
            className="rounded-full bg-info/10 px-2 py-0.5 font-mono text-micro text-info"
            title={`平台独立评测命令: ${active.eval.command}`}
          >
            平台独立评测已启用
          </span>
        ) : null}
        {(active?.eval?.code_paths?.length || active?.eval?.data_paths?.length) ? (
          <span
            className="rounded-full bg-muted/50 px-2 py-0.5 font-mono text-micro text-muted-foreground"
            title={`code_hash=${active.eval?.code_hash || "-"} data_hash=${active.eval?.data_hash || "-"}`}
          >
            <Lock className="mr-1 inline size-2.5 align-[-1px]" />
            保护路径 {(active.eval?.code_paths?.length ?? 0) + (active.eval?.data_paths?.length ?? 0)} 项
            {active.eval?.code_hash ? ` · #${active.eval.code_hash.slice(0, 8)}` : ""}
          </span>
        ) : null}

        <Button
          size="xs"
          variant="outline"
          className="ml-auto gap-1"
          onClick={() => setEditing((v) => !v)}
        >
          {editing ? <ChevronUp className="size-3" /> : <ChevronDown className="size-3" />}
          {editing ? "收起配置" : active ? "修改契约" : "配置指标契约"}
        </Button>
      </div>

      {editing ? (
        <div className="space-y-3 border-t-[0.5px] border-surface-border pt-3 text-micro">
          <div className="grid gap-2.5 sm:grid-cols-2">
            <label className="space-y-1">
              <span className="font-medium text-muted-foreground">主指标 (Primary Metric)</span>
              <select
                value={primary}
                onChange={(e) => setPrimary(e.target.value)}
                className="h-8 w-full rounded-md border-[0.5px] border-surface-border bg-background px-2 font-mono text-caption"
              >
                {allMetrics.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </label>
            <label className="space-y-1">
              <span className="font-medium text-muted-foreground">优化方向 (Direction)</span>
              <select
                value={direction}
                onChange={(e) => setDirection(e.target.value as MetricDirection)}
                className="h-8 w-full rounded-md border-[0.5px] border-surface-border bg-background px-2 text-caption"
              >
                <option value="maximize">maximize (越大越好 ↑, 如 dice / acc / f1)</option>
                <option value="minimize">minimize (越小越好 ↓, 如 loss / mae / fid)</option>
              </select>
            </label>
          </div>

          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <span className="font-medium text-muted-foreground">
                护栏指标 (Guardrails · 违反任一护栏本轮不计提升)
              </span>
              <Button type="button" size="xs" variant="outline" className="gap-1" onClick={addGuardrail}>
                <Plus className="size-3" />添加护栏
              </Button>
            </div>
            {guardrails.length === 0 ? (
              <p className="text-muted-foreground/70">暂未配置护栏指标。</p>
            ) : (
              <div className="space-y-1.5">
                {guardrails.map((g, idx) => (
                  <div key={idx} className="flex flex-wrap items-center gap-1.5">
                    <select
                      value={g.metric}
                      onChange={(e) =>
                        setGuardrails((prev) =>
                          prev.map((item, i) => (i === idx ? { ...item, metric: e.target.value } : item)),
                        )
                      }
                      className="h-7 rounded-md border-[0.5px] border-surface-border bg-background px-2 font-mono text-micro"
                    >
                      {allMetrics.map((m) => (
                        <option key={m} value={m}>{m}</option>
                      ))}
                    </select>
                    <select
                      value={g.direction}
                      onChange={(e) =>
                        setGuardrails((prev) =>
                          prev.map((item, i) =>
                            i === idx ? { ...item, direction: e.target.value as MetricDirection } : item,
                          ),
                        )
                      }
                      className="h-7 rounded-md border-[0.5px] border-surface-border bg-background px-2 text-micro"
                    >
                      <option value="maximize">越大越好 ↑</option>
                      <option value="minimize">越小越好 ↓</option>
                    </select>
                    <span className="text-muted-foreground">最大允许退化 ≤</span>
                    <Input
                      type="number"
                      step="any"
                      min={0}
                      value={g.max_regress}
                      onChange={(e) =>
                        setGuardrails((prev) =>
                          prev.map((item, i) =>
                            i === idx ? { ...item, max_regress: parseFloat(e.target.value) || 0 } : item,
                          ),
                        )
                      }
                      className="h-7 w-24 font-mono text-micro"
                    />
                    <Button
                      type="button"
                      size="xs"
                      variant="ghost"
                      onClick={() => setGuardrails((prev) => prev.filter((_, i) => i !== idx))}
                    >
                      <Trash2 className="size-3 text-muted-foreground" />
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="grid gap-2.5 sm:grid-cols-2">
            <label className="space-y-1">
              <span className="font-medium text-muted-foreground">
                受保护代码路径 (相对工作区, 逗号分隔; 防止 Agent 篡改评测脚本)
              </span>
              <Input
                value={codePathsText}
                onChange={(e) => setCodePathsText(e.target.value)}
                placeholder="例如: eval.py, utils/metrics.py"
                className="h-8 font-mono text-micro"
              />
            </label>
            <label className="space-y-1">
              <span className="font-medium text-muted-foreground">
                受保护数据路径 (相对工作区, 逗号分隔; 锁定测试集 SHA-256)
              </span>
              <Input
                value={dataPathsText}
                onChange={(e) => setDataPathsText(e.target.value)}
                placeholder="例如: data/test, splits/test.txt"
                className="h-8 font-mono text-micro"
              />
            </label>
          </div>

          <div className="space-y-2 rounded-lg border-[0.5px] border-surface-border bg-muted/20 p-2.5">
            <p className="font-medium text-foreground">
              平台独立评测 Job (可选 · 填命令后训练成功自动派发独立评测, 不信训练自报 metrics.json)
            </p>
            <Input
              value={evalCommand}
              onChange={(e) => setEvalCommand(e.target.value)}
              placeholder="留空则使用训练产出的 metrics.json; 填写如: python eval_locked.py --ckpt $AUTOLAB_TRAIN_OUTPUT_DIR/best.pth"
              className="h-8 font-mono text-micro"
            />
            {evalCommand.trim() ? (
              <div className="grid gap-2 sm:grid-cols-4">
                <label className="space-y-0.5">
                  <span className="text-muted-foreground">Conda 环境</span>
                  <Input
                    value={condaEnv}
                    onChange={(e) => setCondaEnv(e.target.value)}
                    placeholder="如 seg"
                    className="h-7 font-mono text-micro"
                  />
                </label>
                <label className="space-y-0.5">
                  <span className="text-muted-foreground">指标文件名</span>
                  <Input
                    value={metricFile}
                    onChange={(e) => setMetricFile(e.target.value)}
                    placeholder="metrics.json"
                    className="h-7 font-mono text-micro"
                  />
                </label>
                <label className="space-y-0.5">
                  <span className="text-muted-foreground">超时 (分钟)</span>
                  <Input
                    type="number"
                    min={1}
                    max={1440}
                    value={timeoutMinutes}
                    onChange={(e) => setTimeoutMinutes(e.target.value)}
                    className="h-7 font-mono text-micro"
                  />
                </label>
                <label className="space-y-0.5">
                  <span className="text-muted-foreground">GPU 数/Job</span>
                  <Input
                    type="number"
                    min={1}
                    max={8}
                    value={gpusPerJob}
                    onChange={(e) => setGpusPerJob(e.target.value)}
                    className="h-7 font-mono text-micro"
                  />
                </label>
              </div>
            ) : null}
          </div>

          <div className="flex items-center justify-end gap-2">
            <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>
              取消
            </Button>
            <Button
              type="button"
              size="sm"
              className="gap-1"
              disabled={saving || !primary}
              onClick={() => void save()}
            >
              {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
              保存并固化契约
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
