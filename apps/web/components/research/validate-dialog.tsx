"use client";

/** 全量验证:研究出提升后的人工决策动作——由 0-Token Python 确定性状态机检出干净冠军 worktree,
 * 在选定数据集上用独立留存 seed 按基线口径重跑(不改任何代码),逐数据集出均值结论;确认新基线仍是 owner 手动动作。 */

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { BadgeCheck, Database, Loader2 } from "lucide-react";
import { fetchProjectDatasets, startValidation, type Project } from "@/lib/api";
import { parseStrictFloat, parseStrictInt } from "@/lib/validation";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "~/vendor/ui/components/ui/dialog";

const FIELD = "w-full rounded-md border-[0.5px] border-surface-border bg-background px-2 text-caption";

function ValidationField({
  label, value, onChange, placeholder, hint, error, widthClass,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  placeholder: string;
  hint: string;
  error: string | null;
  widthClass: string;
}) {
  return (
    <div>
      <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
        {label}
        <input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          aria-invalid={error !== null}
          className={`${FIELD} h-7 ${widthClass} ${error !== null ? "border-destructive" : ""}`}
        />
        {hint}
      </label>
      {error !== null ? (
        <p role="alert" className="mt-0.5 text-micro text-destructive">{error}</p>
      ) : null}
    </div>
  );
}

export function ValidateLaunch({ project, sourceRunId, label }: {
  project: Project; sourceRunId?: string; label?: string;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [seeds, setSeeds] = useState("3");
  const [seedStart, setSeedStart] = useState("101");
  const [gpuHours, setGpuHours] = useState("");
  const [regressTol, setRegressTol] = useState("0.005");
  const [picked, setPicked] = useState<string[]>([]);
  const [starting, setStarting] = useState(false);
  const { data } = useQuery({
    queryKey: ["datasets", project.id],
    queryFn: () => fetchProjectDatasets(project.id),
    enabled: open,
  });
  const confirmed = (data?.datasets ?? []).filter((d) => d.state === "confirmed");

  const seedsParsed = parseStrictInt(seeds, { label: "每数据集 seed 数", min: 1, max: 10 });
  const seedStartParsed = parseStrictInt(seedStart, { label: "留存种子起始值", min: 0, max: 1_000_000 });
  const gpuHoursParsed = parseStrictFloat(gpuHours, { label: "GPU 小时上限", min: 0, optional: true });
  const regressTolParsed = parseStrictFloat(regressTol, {
    label: "退化容忍度", min: 0, max: 1, optional: true,
  });
  const validationError =
    (!seedsParsed.ok && seedsParsed.error)
    || (!seedStartParsed.ok && seedStartParsed.error)
    || (!gpuHoursParsed.ok && gpuHoursParsed.error)
    || (!regressTolParsed.ok && regressTolParsed.error)
    || null;

  const toggle = (name: string) =>
    setPicked((prev) => prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]);

  const launch = async () => {
    if (!seedsParsed.ok || !seedStartParsed.ok || !gpuHoursParsed.ok || !regressTolParsed.ok) {
      toast.error(validationError ?? "参数格式有误");
      return;
    }
    setStarting(true);
    try {
      await startValidation(project.id, {
        datasets: picked.length ? picked : undefined,
        seeds: seedsParsed.value,
        seed_start: seedStartParsed.value,
        gpu_hours: gpuHoursParsed.value,
        regress_tol: regressTolParsed.value,
        source_run_id: sourceRunId,
      });
      setOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["run-latest", project.id] });
      void queryClient.invalidateQueries({ queryKey: ["project-runs", project.id] });
    } catch (err) {
      toast.error(`启动验证失败:${(err as Error).message}`);
    } finally {
      setStarting(false);
    }
  };

  return (
    <>
      <Button size="sm" variant="outline" className="gap-1" onClick={() => setOpen(true)}>
        <BadgeCheck className="size-3.5" />
        {label ?? "全量验证"}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>全量验证 · 0-Token 确定性复核</DialogTitle>
          </DialogHeader>
          <p className="text-caption text-muted-foreground">
            由纯 Python 确定性重放控制器(0 LLM Token、无需配置模型 API)自动从冠军快照检出干净
            worktree,按各数据集基线命令口径使用独立留存种子一次性并行派发复核(不改任何代码),
            逐数据集给出均值与置信区间结论。确认晋级新基线仍由你手动操作。
          </p>
          {confirmed.length > 1 ? (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="flex items-center gap-1 text-micro text-muted-foreground">
                <Database className="size-3" />验证数据集(不选=全部):
              </span>
              {confirmed.map((d) => (
                <label key={d.name} className="flex items-center gap-1 text-caption">
                  <input type="checkbox" checked={picked.includes(d.name)} onChange={() => toggle(d.name)} />
                  {d.name}
                </label>
              ))}
            </div>
          ) : null}
          <div className="space-y-2">
            <ValidationField
              label="每数据集 seed 数"
              value={seeds}
              onChange={setSeeds}
              placeholder="3"
              hint="(1-10;判定按均值,seed 少了会记「证据不足」)"
              error={seedsParsed.ok ? null : seedsParsed.error}
              widthClass="w-14"
            />
            <ValidationField
              label="留存种子起始值"
              value={seedStart}
              onChange={setSeedStart}
              placeholder="101"
              hint="(0-1000000,默认 101,独立于探索期种子)"
              error={seedStartParsed.ok ? null : seedStartParsed.error}
              widthClass="w-20 font-mono"
            />
            <ValidationField
              label="退化容忍度"
              value={regressTol}
              onChange={setRegressTol}
              placeholder="0.005"
              hint="(0.0-1.0,默认 0.005;设为 0 表示零容忍)"
              error={regressTolParsed.ok ? null : regressTolParsed.error}
              widthClass="w-20 font-mono"
            />
            <ValidationField
              label="GPU 小时上限"
              value={gpuHours}
              onChange={setGpuHours}
              placeholder="不限"
              hint="(可选,≥ 0)"
              error={gpuHoursParsed.ok ? null : gpuHoursParsed.error}
              widthClass="w-20 font-mono"
            />
          </div>
          <DialogFooter>
            <Button
              size="sm"
              className="gap-1"
              disabled={starting || validationError !== null}
              onClick={() => void launch()}
            >
              {starting ? <Loader2 className="size-3.5 animate-spin" /> : <BadgeCheck className="size-3.5" />}
              启动验证
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
