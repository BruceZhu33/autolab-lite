"use client";

import { use, useEffect, useRef, useState } from "react";
import { PageHeader } from "@/components/shell";
import { fetchJobDetail, fetchJobLogs } from "@/lib/api";

export default function RunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof fetchJobDetail>> | null>(null);
  const [error, setError] = useState<string | null>(null);
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
        if (alive) setError((e as Error).message);
      }
    };
    tick();
    const timer = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [id]);

  if (error) return <p className="text-[var(--destructive)]">{error}</p>;
  if (!detail) return <p className="text-xs text-[var(--muted-foreground)]">loading…</p>;
  const job = detail.job as Record<string, string | number | null>;

  return (
    <div className="space-y-4">
      <PageHeader
        title={`Run ${String(job.id).slice(0, 22)}…`}
        sub={`${job.status} · observation ${detail.observation}${job.failure_summary ? ` · ${job.failure_summary}` : ""}`}
      />
      <section className="autolab-card p-3">
        <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-[var(--muted-foreground)]">
          Facts
        </h2>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-[12px] sm:grid-cols-3">
          <Fact label="dispatch" value={String(job.dispatch_id ?? "—")} />
          <Fact label="exit code" value={String(job.exit_code ?? "—")} />
          <Fact label="started" value={String(job.started_at ?? "—")} />
          <Fact label="finished" value={String(job.finished_at ?? "—")} />
        </dl>
      </section>
      <section className="autolab-card p-3">
        <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-[var(--muted-foreground)]">
          Artifacts
        </h2>
        <ul className="space-y-1 text-[12px]">
          {detail.artifacts.map((artifact) => (
            <li key={artifact.id} className="flex flex-wrap items-baseline gap-2">
              <span className="autolab-chip">{artifact.kind}</span>
              <span className="font-mono text-[11px]">{artifact.remote_path}</span>
              <span className="font-mono text-[11px] text-[var(--muted-foreground)]">
                sha256 {artifact.sha256.slice(0, 12)}…
              </span>
              {"metric_values" in artifact.metadata
                ? JSON.stringify(artifact.metadata.metric_values)
                : null}
            </li>
          ))}
          {detail.artifacts.length === 0 ? (
            <li className="text-[var(--muted-foreground)]">none</li>
          ) : null}
        </ul>
      </section>
      <section className="autolab-card p-3">
        <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-[var(--muted-foreground)]">
          Logs (incremental)
        </h2>
        <pre className="max-h-72 overflow-auto rounded-md bg-[var(--surface-hover)] p-2 font-mono text-[11px] leading-5">
          {logs || "—"}
        </pre>
      </section>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2">
      <dt className="w-20 shrink-0 text-[var(--muted-foreground)]">{label}</dt>
      <dd className="truncate font-mono text-[11px]">{value}</dd>
    </div>
  );
}
