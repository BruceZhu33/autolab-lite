"use client";

/** 数据集状态板:按数据集投影 跑通(已确认/待确认) / 在跑 / 失败 / 待补充。
 * 待补充项内联供给——服务器已有目录直接指路(链接)或上传 zip;
 * 供给完成后自动给等待中的 baseline 会话回话,agent 无需人再通知。 */

import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckCircle2, CircleAlert, Database, FolderSymlink, Loader2, Upload } from "lucide-react";
import {
  fetchProjectDatasets, sendRunMessage, type DatasetState, type Project,
} from "@/lib/api";
import { linkAsset, uploadAsset } from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";

const FIELD = "rounded-md border-[0.5px] border-surface-border bg-background px-2 text-caption";

function MetricChips({ metrics }: { metrics?: Record<string, number> }) {
  const entries = Object.entries(metrics ?? {});
  if (!entries.length) return null;
  return (
    <span className="ml-1 inline-flex flex-wrap gap-1">
      {entries.map(([k, v]) => (
        <span key={k} className="rounded-full bg-muted/60 px-1.5 text-micro tabular-nums">
          {k} {typeof v === "number" ? v.toFixed(4) : String(v)}
        </span>
      ))}
    </span>
  );
}

function SupplyRow({ project, ds }: { project: Project; ds: DatasetState }) {
  const queryClient = useQueryClient();
  const [serverPath, setServerPath] = useState("");
  const [busy, setBusy] = useState<"link" | "upload" | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const done = async (how: string) => {
    toast.success(`已供给 ${ds.name}(${how})`);
    void queryClient.invalidateQueries({ queryKey: ["datasets", project.id] });
    if (ds.runId) {
      try {
        await sendRunMessage(
          ds.runId,
          `已供给数据集 ${ds.name}:${how},目标位置 ${ds.targetPath}(链接或解包已就位)。请确认能读到后继续。`);
      } catch (err) {
        toast.error(`通知 Agent 失败:${(err as Error).message}(可手动在会话里回话)`);
      }
    }
  };

  const link = async () => {
    if (!serverPath.trim()) return toast.error("先填服务器上数据集目录的绝对路径");
    setBusy("link");
    try {
      await linkAsset(project.id, ds.targetPath || "", serverPath.trim());
      await done(`链接 ${serverPath.trim()}`);
    } catch (err) {
      toast.error(`链接失败:${(err as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  const upload = async (file: File) => {
    setBusy("upload");
    try {
      await uploadAsset(project.id, ds.targetPath || "", file);
      await done(`上传 ${file.name}`);
    } catch (err) {
      toast.error(`上传失败:${(err as Error).message}`);
    } finally {
      setBusy(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
      <input value={serverPath} onChange={(e) => setServerPath(e.target.value)}
             placeholder="服务器上已有目录的绝对路径,如 /home/xx/datasets/BUSI"
             className={`${FIELD} h-7 min-w-52 flex-1`} />
      <Button size="sm" variant="outline" className="gap-1 px-2" disabled={busy !== null}
              onClick={() => void link()}>
        {busy === "link" ? <Loader2 className="size-3 animate-spin" /> : <FolderSymlink className="size-3.5" />}
        指路链接
      </Button>
      <input ref={fileInput} type="file" accept=".zip" className="hidden"
             onChange={(e) => e.target.files?.[0] && void upload(e.target.files[0])} />
      <Button size="sm" variant="outline" className="gap-1 px-2" disabled={busy !== null}
              onClick={() => fileInput.current?.click()}>
        {busy === "upload" ? <Loader2 className="size-3 animate-spin" /> : <Upload className="size-3.5" />}
        上传 zip
      </Button>
    </div>
  );
}

const STATE_STYLE: Record<DatasetState["state"], string> = {
  confirmed: "border-success/30 bg-success/5",
  ran: "border-success/20 bg-card",
  running: "border-brand/30 bg-brand/5",
  failed: "border-destructive/30 bg-destructive/5",
  pending_supply: "border-warning/40 bg-warning/5",
};

export function DatasetBoard({ project }: { project: Project }) {
  const { data } = useQuery({
    queryKey: ["datasets", project.id],
    queryFn: () => fetchProjectDatasets(project.id),
    refetchInterval: 4000,
  });
  const datasets = data?.datasets ?? [];
  if (!datasets.length) return null;
  const order = { pending_supply: 0, running: 1, failed: 2, ran: 3, confirmed: 4 } as const;

  return (
    <section className="rounded-xl border-[0.5px] border-surface-border bg-card p-3">
      <p className="text-caption font-semibold">
        <Database className="mr-1 inline size-3.5 align-[-2px] text-muted-foreground" />
        数据集状态
        <span className="ml-2 font-normal text-micro text-muted-foreground">
          跑通要你确认;待补充的直接在这里给(指路或上传),给完 Agent 自动继续
        </span>
      </p>
      <ul className="mt-2 space-y-1.5">
        {[...datasets].sort((a, b) => order[a.state] - order[b.state]).map((ds) => (
          <li key={ds.name}
              className={`rounded-lg border-[0.5px] px-2.5 py-1.5 ${STATE_STYLE[ds.state]}`}>
            <div className="flex flex-wrap items-center text-caption">
              <span className="font-medium">{ds.name}</span>
              {ds.state === "confirmed"
                ? <CheckCircle2 className="ml-1.5 size-3.5 text-success" /> : null}
              {ds.state === "pending_supply"
                ? <CircleAlert className="ml-1.5 size-3.5 text-warning" /> : null}
              <span className="ml-1.5 rounded-full bg-muted/60 px-1.5 text-micro">
                {ds.state === "confirmed" ? "已确认基线"
                  : ds.state === "ran" ? "已跑通待确认"
                  : ds.state === "running" ? `在跑(${ds.jobStatus})`
                  : ds.state === "failed" ? `失败(${ds.jobStatus})`
                  : "待你补充"}
              </span>
              <MetricChips metrics={ds.metrics} />
              {ds.jobId
                ? <code className="ml-auto text-micro text-muted-foreground">{ds.jobId}</code> : null}
            </div>
            {ds.command ? (
              <p className="mt-0.5 truncate text-micro text-muted-foreground" title={ds.command}>
                <code className="rounded bg-muted/50 px-1">{ds.command}</code>
              </p>
            ) : null}
            {ds.state === "pending_supply" ? (
              <>
                {ds.why
                  ? <p className="mt-0.5 text-micro text-muted-foreground">需要它:{ds.why}</p> : null}
                <p className="mt-0.5 text-micro">
                  应放到:<code className="rounded bg-muted/50 px-1">{ds.targetPath || "(agent 未给出路径)"}</code>
                </p>
                <SupplyRow project={project} ds={ds} />
              </>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
