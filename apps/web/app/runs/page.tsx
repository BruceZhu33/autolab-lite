"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/shell";
import { fetchJobs, type JobView } from "@/lib/api";

const STATUS_TONE: Record<string, string> = {
  SUCCEEDED: "text-[var(--success)]",
  FAILED: "text-[var(--destructive)]",
  TIMED_OUT: "text-[var(--destructive)]",
  RUNNING: "text-[var(--info)]",
  QUEUED: "text-[var(--muted-foreground)]",
  CANCELLING: "text-[var(--warning)]",
};

export default function RunsPage() {
  const [jobs, setJobs] = useState<JobView[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = () =>
      fetchJobs()
        .then((data) => alive && (setJobs(data), setError(null)))
        .catch((e: Error) => alive && setError(e.message));
    tick();
    const timer = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  return (
    <div>
      <PageHeader title="Runs" sub="Every job with its runtime facts" />
      {error ? <p className="text-[var(--destructive)]">{error}</p> : null}
      <div className="autolab-card overflow-hidden">
        <table className="w-full text-left text-[12px]">
          <thead className="border-b border-[var(--surface-border)] text-[11px] uppercase tracking-wide text-[var(--muted-foreground)]">
            <tr>
              <th className="px-3 py-2">Job</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Exit</th>
              <th className="px-3 py-2">Started</th>
              <th className="px-3 py-2">Finished</th>
            </tr>
          </thead>
          <tbody>
            {(jobs ?? []).map((job) => (
              <tr key={job.id} className="border-b border-[var(--surface-border)] last:border-0">
                <td className="px-3 py-1.5 font-mono text-[11px]">
                  <Link href={`/runs/${job.id}`} className="underline-offset-2 hover:underline">
                    {job.id.slice(0, 22)}…
                  </Link>
                </td>
                <td className={`px-3 py-1.5 font-medium ${STATUS_TONE[job.status] ?? ""}`}>
                  {job.status}
                </td>
                <td className="px-3 py-1.5">{job.exit_code ?? "—"}</td>
                <td className="px-3 py-1.5">{job.started_at?.slice(11, 19) ?? "—"}</td>
                <td className="px-3 py-1.5">{job.finished_at?.slice(11, 19) ?? "—"}</td>
              </tr>
            ))}
            {jobs && jobs.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-[var(--muted-foreground)]">
                  no jobs yet
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}
