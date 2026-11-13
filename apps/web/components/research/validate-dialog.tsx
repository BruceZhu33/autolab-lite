"use client";

/** 全量验证:研究出提升后的人工决策动作——由 0-Token Python 确定性状态机检出干净冠军 worktree,
 * 在选定数据集上用独立留存 seed 按基线口径重跑(不改任何代码),逐数据集出均值结论;确认新基线仍是 owner 手动动作。 */

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { BadgeCheck, Database, Loader2 } from "lucide-react";
import { fetchProjectDatasets, startValidation, type Project } from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "~/vendor/ui/components/ui/dialog";

const FIELD = "w-full rounded-md border-[0.5px] border-surface-border bg-background px-2 text-caption";

export function ValidateLaunch({ project, sourceRunId, label }: {
  project: Project; sourceRunId?: string; label?: string;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [seeds, setSeeds] = useState("3");
  const [seedStart, setSeedStart] = useState("101");
  const [picked, setPicked] = useState<string[]>([]);
  const [starting, setStarting] = useState(false);
  const { data } = useQuery({
    queryKey: ["datasets", project.id],
    queryFn: () => fetchProjectDatasets(project.id),
    enabled: open,
  });
  const confirmed = (data?.datasets ?? []).filter((d) => d.state === "confirmed");

  const toggle = (name: string) =>
    setPicked((prev) => prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]);

  const launch = async () => {
    setStarting(true);
    try {
      const parsedSeedStart = seedStart.trim() === "" ? 101 : Number(seedStart);
      if (!Number.isInteger(parsedSeedStart) || parsedSeedStart < 0 || parsedSeedStart > 1_000_000) {
        toast.error("留存种子起始值需为 0..1000000 之间的整数");
        return;
      }
      const payload: Parameters<typeof startValidation>[1] & { seed_start?: number } = {
        datasets: picked.length ? picked : undefined,
        seeds: Number(seeds) || 3,
        seed_start: parsedSeedStart,
        source_run_id: sourceRunId,
      };
      await startValidation(project.id, payload);
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
            <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
              每数据集 seed 数
              <input value={seeds} onChange={(e) => setSeeds(e.target.value)} placeholder="3"
                     className={`${FIELD} h-7 w-14`} />
              (1-10;判定按均值,seed 少了会记「证据不足」)
            </label>
            <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
              留存种子起始值
              <input value={seedStart} onChange={(e) => setSeedStart(e.target.value)} placeholder="101"
                     className={`${FIELD} h-7 w-20 font-mono`} />
              (默认 101,独立于探索期种子以防偶然高分偏差)
            </label>
          </div>
          <DialogFooter>
            <Button size="sm" className="gap-1" disabled={starting} onClick={() => void launch()}>
              {starting ? <Loader2 className="size-3.5 animate-spin" /> : <BadgeCheck className="size-3.5" />}
              启动验证
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
