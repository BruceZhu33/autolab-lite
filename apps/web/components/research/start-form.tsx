"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Lightbulb, Loader2, PlayCircle } from "lucide-react";
import { startResearch, type Project, type ResearchTier } from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";

const TIERS: Array<{ key: ResearchTier; title: string; badge: string; desc: string }> = [
  { key: "light", title: "轻量 · 人机协作", badge: "≤3 轮",
    desc: "每轮结束停下给你看报告,你回复后才进下一轮" },
  { key: "medium", title: "中量", badge: "≤5 轮", desc: "自动连续迭代,出现提升或轮数用完即停" },
  { key: "heavy", title: "重量 · 过夜", badge: "不限轮",
    desc: "达到显著阈值(Δ≥0.01)才停,GPU 小时兜底" },
];

const FIELD = "w-full rounded-md border-[0.5px] border-surface-border bg-background px-2 text-caption";

export function ResearchStartForm({ project }: { project: Project }) {
  const queryClient = useQueryClient();
  const [tier, setTier] = useState<ResearchTier>("light");
  const [idea, setIdea] = useState("");
  const [reference, setReference] = useState("");
  const [gpuHours, setGpuHours] = useState("");
  const [network, setNetwork] = useState(false);
  const [starting, setStarting] = useState(false);

  const start = async () => {
    setStarting(true);
    try {
      await startResearch(project.id, {
        tier, idea, reference, network,
        gpu_hours: tier === "heavy" && gpuHours ? Number(gpuHours) : undefined,
      });
      setIdea("");
      void queryClient.invalidateQueries({ queryKey: ["run-latest", project.id] });
    } catch (err) {
      toast.error(`启动失败:${(err as Error).message}`);
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="space-y-2 rounded-xl border-[0.5px] border-surface-border bg-card p-3">
      <p className="text-caption font-semibold">
        <Lightbulb className="mr-1 inline size-3.5 align-[-2px] text-brand" />开始研究
      </p>
      <textarea value={idea} onChange={(e) => setIdea(e.target.value)} rows={3}
                placeholder="你的想法(可选):想验证什么、往哪个方向改、哪些不要碰…"
                className={`${FIELD} py-1.5`} />
      <input value={reference} onChange={(e) => setReference(e.target.value)}
             placeholder="参考代码库(可选):GitHub URL 或本机路径;论文在项目页上传"
             className={`${FIELD} h-8`} />
      <div className="grid gap-2 md:grid-cols-3">
        {TIERS.map((t) => (
          <button key={t.key} type="button" onClick={() => setTier(t.key)}
                  className={`rounded-lg border-[0.5px] p-2 text-left ${tier === t.key
                    ? "border-brand bg-brand/5" : "border-surface-border hover:bg-muted/40"}`}>
            <p className="text-caption font-medium">{t.title}
              <span className="ml-1.5 rounded-full bg-muted/60 px-1.5 text-micro text-muted-foreground">{t.badge}</span>
            </p>
            <p className="mt-0.5 text-micro text-muted-foreground">{t.desc}</p>
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        {tier === "heavy" ? (
          <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
            GPU 小时上限
            <input value={gpuHours} onChange={(e) => setGpuHours(e.target.value)} placeholder="24"
                   className={`${FIELD} h-7 w-16`} />
          </label>
        ) : null}
        <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
          <input type="checkbox" checked={network} onChange={(e) => setNetwork(e.target.checked)} />
          服务器可访问外网
        </label>
        <Button size="sm" className="ml-auto gap-1" disabled={starting} onClick={() => void start()}>
          {starting ? <Loader2 className="size-3.5 animate-spin" /> : <PlayCircle className="size-3.5" />}
          启动
        </Button>
      </div>
    </div>
  );
}
