"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/shell";
import { fetchServers, type ServerView } from "@/lib/api";

export default function GpusPage() {
  const [servers, setServers] = useState<ServerView[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = () =>
      fetchServers()
        .then((data) => alive && (setServers(data), setError(null)))
        .catch((e: Error) => alive && setError(e.message));
    tick();
    const timer = setInterval(tick, 3000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  return (
    <div>
      <PageHeader title="GPU Servers" sub="Server cards and telemetry (live probe)" />
      {error ? <p className="text-[var(--destructive)]">{error}</p> : null}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {(servers ?? []).map((server) => {
          const reachable = server.status_hint === "reachable";
          return (
            <article key={server.id} className="autolab-card p-4">
              <div className="flex items-center justify-between">
                <h3 className="text-[13px] font-medium">{server.name}</h3>
                <span
                  className={`autolab-chip ${
                    reachable ? "text-[var(--success)]" : "text-[var(--muted-foreground)]"
                  }`}
                >
                  {server.status_hint ?? "unknown"}
                </span>
              </div>
              <p className="mt-1 text-[11px] text-[var(--muted-foreground)]">
                {server.transport_mode} · {server.gpu_slots} slot(s) · source {server.gpu_slot_source}
              </p>
              <div className="mt-3 space-y-2">
                {server.gpus.length === 0 ? (
                  <p className="text-[11px] text-[var(--muted-foreground)]">
                    no live telemetry (local pseudo slots)
                  </p>
                ) : (
                  server.gpus.map((gpu) => {
                    const usedPct = gpu.memory_total_mb
                      ? Math.round((gpu.memory_used_mb / gpu.memory_total_mb) * 100)
                      : 0;
                    return (
                      <div key={gpu.gpu_index}>
                        <div className="flex justify-between text-[11px]">
                          <span>GPU {gpu.gpu_index}</span>
                          <span>
                            {gpu.memory_used_mb}/{gpu.memory_total_mb} MB · {gpu.utilization_percent}% ·{" "}
                            {gpu.temperature_c}°C
                          </span>
                        </div>
                        <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-[var(--surface-hover)]">
                          <div
                            className="h-full rounded-full bg-[var(--chart-2)]"
                            style={{ width: `${usedPct}%` }}
                          />
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </article>
          );
        })}
        {servers && servers.length === 0 ? (
          <p className="text-xs text-[var(--muted-foreground)]">no servers registered</p>
        ) : null}
      </div>
    </div>
  );
}
