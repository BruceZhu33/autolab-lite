"use client";

/** 全量验证:研究出提升后的人工决策动作——把冠军改动在选定数据集上按基线口径
 * 多 seed 重跑(会话不改代码),逐数据集出均值结论;确认新基线仍是 owner 手动动作。 */

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
      await startValidation(project.id, {
        datasets: picked.length ? picked : undefined,
        seeds: Number(seeds) || 3,
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
            <DialogTitle>全量验证</DialogTitle>
          </DialogHeader>
          <p className="text-caption text-muted-foreground">
            把最近一次成功研究会话的冠军改动,按各数据集的基线命令口径多 seed 原样重跑
            (验证会话不改任何代码),逐数据集给出均值结论。确认新基线仍由你在 Baseline 页手动操作。
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
          <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
            每数据集 seed 数
            <input value={seeds} onChange={(e) => setSeeds(e.target.value)} placeholder="3"
                   className={`${FIELD} h-7 w-14`} />
            (1-5;判定按均值,seed 少了会记「证据不足」)
          </label>
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
