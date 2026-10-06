"use client";

import { use, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ChevronLeft, FileBox, ScrollText, Terminal } from "lucide-react";
import { fetchJobDetail, fetchJobLogs } from "@/lib/api";
import { ApiError } from "@/components/api-error";
import { Badge } from "~/vendor/ui/components/ui/badge";
import { Button } from "~/vendor/ui/components/ui/button";
import { PageHeader, PAGE_GUTTER } from "~/vendor/views/layout/page-header";

const STATUS_TONE: Record<string, string> = {
  SUCCEEDED: "bg-success/10 text-success",
  FAILED: "bg-destructive/10 text-destructive",
  TIMED_OUT: "bg-destructive/10 text-destructive",
  RUNNING: "bg-info/10 text-info",
  QUEUED: "bg-muted text-muted-foreground",
  CANCELLING: "bg-warning/10 text-warning",
};

const STATUS_LABEL: Record<string, string> = {
  SUCCEEDED: "成功",
  FAILED: "失败",
  TIMED_OUT: "超时",
  RUNNING: "运行中",
  QUEUED: "排队中",
  CANCELLING: "取消中",
};

function fmtDateTime(iso?: string | null): string {
  if (!iso) return "—";
  return `${iso.slice(0, 10)} ${iso.slice(11, 19)}`;
}

function duration(started?: string | null, finished?: string | null): string {
  if (!started || !finished) return "—";
  const ms = Date.parse(finished) - Date.parse(started);
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

export default function RunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof fetchJobDetail>> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [logs, setLogs] = useState("");
  const cursorRef = useRef(0);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const data = await fetchJobDetail(id);
        if (!alive) return;
        setDetail(data);
        setError(null);
        const chunk = await fetchJobLogs(id, cursorRef.current);
        if (!alive) return;
        if (chunk.text) setLogs((prev) => prev + chunk.text);
        cursorRef.current = chunk.cursor;
      } catch (e) {
        if (alive) setError(e);
      }
    };
    tick();
    const timer = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [id]);

  if (error && !detail)
    return (
      <div className="flex h-full flex-col">
        <PageHeader>
          <h1 className="text-title-sm font-semibold tracking-tight">Run</h1>
        </PageHeader>
        <ApiError error={error} />
      </div>
    );
  if (!detail)
    return (
      <div className="flex h-full flex-col">
        <PageHeader>
          <h1 className="text-title-sm font-semibold tracking-tight">Run</h1>
        </PageHeader>
        <p className={`${PAGE_GUTTER} pt-4 text-caption text-muted-foreground`}>加载 Job 详情…</p>
      </div>
    );

  const job = detail.job as Record<string, string | number | null>;
  const status = String(job.status);

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <Link href="/runs" aria-label="返回 Runs">
          <Button variant="ghost" size="icon-sm">
            <ChevronLeft className="size-4" />
          </Button>
        </Link>
        <h1 className="font-mono text-title-sm font-semibold tracking-tight">
          Job #{job.ordinal ?? "—"}
        </h1>
        <Badge variant="ghost" className={STATUS_TONE[status] ?? ""}>
          {STATUS_LABEL[status] ?? status}
        </Badge>
        <span className="hidden truncate font-mono text-caption text-muted-foreground md:inline">
          {String(job.id)}
        </span>
      </PageHeader>

      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className="mx-auto w-full max-w-[900px] space-y-3 py-4">
          <section className="rounded-xl border border-surface-border bg-surface p-4 shadow-[var(--surface-shadow)]">
            <h2 className="mb-2 flex items-center gap-1.5 text-micro font-medium uppercase tracking-wide text-muted-foreground">
              <Terminal className="size-3" />
              运行事实
            </h2>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-caption sm:grid-cols-3">
              <Fact label="状态" value={STATUS_LABEL[status] ?? status} />
              <Fact label="退出码" value={job.exit_code === null ? "—" : String(job.exit_code)} />
              <Fact label="时长" value={duration(job.started_at as string, job.finished_at as string)} />
              <Fact label="开始" value={fmtDateTime(job.started_at as string)} />
              <Fact label="结束" value={fmtDateTime(job.finished_at as string)} />
              <Fact label="dispatch" value={job.dispatch_id ? String(job.dispatch_id) : "—"} />
            </dl>
            {job.failure_summary ? (
              <p className="mt-2 rounded-md bg-destructive/10 px-2 py-1.5 text-caption text-destructive">
                {String(job.failure_summary)}
              </p>
            ) : null}
          </section>

          <section className="rounded-xl border border-surface-border bg-surface p-4 shadow-[var(--surface-shadow)]">
            <h2 className="mb-2 flex items-center gap-1.5 text-micro font-medium uppercase tracking-wide text-muted-foreground">
              <FileBox className="size-3" />
              产物
            </h2>
            <ul className="space-y-1.5">
              {detail.artifacts.map((artifact) => (
                <li key={artifact.id} className="flex flex-wrap items-baseline gap-2 text-caption">
                  <Badge variant="outline">{artifact.kind}</Badge>
                  <span className="font-mono">{artifact.remote_path}</span>
                  <span className="font-mono text-micro text-muted-foreground">
                    sha256 {artifact.sha256.slice(0, 12)}…
                  </span>
                  {"metric_values" in artifact.metadata
                    ? JSON.stringify(artifact.metadata.metric_values)
                    : null}
                </li>
              ))}
              {detail.artifacts.length === 0 ? (
                <li className="text-caption text-muted-foreground">暂无产物</li>
              ) : null}
            </ul>
          </section>

          <section className="rounded-xl border border-surface-border bg-surface p-4 shadow-[var(--surface-shadow)]">
            <h2 className="mb-2 flex items-center gap-1.5 text-micro font-medium uppercase tracking-wide text-muted-foreground">
              <ScrollText className="size-3" />
              日志(增量拉取)
            </h2>
            <pre className="max-h-72 overflow-auto rounded-md bg-muted/50 p-2 font-mono text-micro leading-5">
              {logs || "—"}
            </pre>
          </section>
        </div>
      </div>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex min-w-0 gap-2">
      <dt className="w-14 shrink-0 text-muted-foreground">{label}</dt>
      <dd className="truncate font-mono">{value}</dd>
    </div>
  );
}
