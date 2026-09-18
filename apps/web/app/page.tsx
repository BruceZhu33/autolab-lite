"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/shell";
import { approvePlan, fetchBoard, type BoardCardView } from "@/lib/api";

const COLUMNS: Array<{ key: BoardCardView["column"]; label: string }> = [
  { key: "needs_plan", label: "Needs plan" },
  { key: "queued", label: "Queued" },
  { key: "running", label: "Running" },
  { key: "needs_attention", label: "Needs attention" },
  { key: "recently_completed", label: "Recently completed" },
];

const TONE_CLASS: Record<string, string> = {
  neutral: "text-[var(--muted-foreground)]",
  info: "text-[var(--info)]",
  warning: "text-[var(--warning)]",
  danger: "text-[var(--destructive)]",
};

export default function BoardPage() {
  const [cards, setCards] = useState<BoardCardView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [approveNote, setApproveNote] = useState<string | null>(null);

  async function approve(card: BoardCardView) {
    const typed = window.prompt(
      `批准 ${card.title} 的 plan ${card.planId.slice(0, 18)}…\n粘贴完整 spec hash 以确认：\n${card.specHash}`,
    );
    if (!typed) return;
    try {
      const result = await approvePlan(card.planId, typed.trim());
      setApproveNote(`approved: ${result.status} (${result.jobs.length} jobs)`);
    } catch (e) {
      setApproveNote(`approve failed: ${(e as Error).message}`);
    }
  }

  useEffect(() => {
    let alive = true;
    const tick = () =>
      fetchBoard()
        .then((data) => alive && (setCards(data), setError(null)))
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
      <PageHeader title="Board" sub="Plan/job projection — columns are derived, never stored" />
      {approveNote ? (
        <p className="autolab-card mb-3 p-2 text-[12px] text-[var(--muted-foreground)]">{approveNote}</p>
      ) : null}
      {error ? (
        <p className="autolab-card p-3 text-[var(--destructive)]">{error}</p>
      ) : cards === null ? (
        <p className="text-xs text-[var(--muted-foreground)]">loading…</p>
      ) : (
        <div className="flex gap-3 overflow-x-auto pb-2">
          {COLUMNS.map((column) => {
            const items = cards.filter((c) => c.column === column.key);
            return (
              <section key={column.key} className="w-72 shrink-0">
                <h2 className="mb-2 flex items-center gap-2 text-xs font-medium text-[var(--muted-foreground)]">
                  {column.label}
                  <span className="autolab-chip">{items.length}</span>
                </h2>
                <div className="space-y-2">
                  {items.map((card) => (
                    <article key={card.projectId} className="autolab-card p-3">
                      <div className="flex items-baseline justify-between gap-2">
                        <h3 className="truncate text-[13px] font-medium">{card.title}</h3>
                      </div>
                      <p className="mt-0.5 line-clamp-2 text-[11px] text-[var(--muted-foreground)]">
                        {card.summary}
                      </p>
                      <div className="mt-2 flex flex-wrap gap-1">
                        {card.badges.map((badge) => (
                          <span key={badge.label} className={`autolab-chip ${TONE_CLASS[badge.tone]}`}>
                            {badge.label}
                          </span>
                        ))}
                        {card.latestMetric ? (
                          <span className="autolab-chip text-[var(--success)]">
                            {card.latestMetric.name}: {card.latestMetric.value}
                          </span>
                        ) : null}
                      </div>
                      {card.column === "needs_plan" ? (
                        <button
                          type="button"
                          onClick={() => approve(card)}
                          className="mt-2 rounded-md border border-[var(--surface-border)] px-2 py-1 text-[11px] font-medium hover:bg-[var(--surface-hover)]"
                        >
                          批准（粘贴 spec hash）
                        </button>
                      ) : null}
                      <p className="mt-2 text-[11px] text-[var(--muted-foreground)]">
                        jobs {card.jobs.done}/{card.jobs.total}
                        {card.jobs.running ? ` · ${card.jobs.running} running` : ""}
                        {card.jobs.failed ? ` · ${card.jobs.failed} failed` : ""}
                      </p>
                    </article>
                  ))}
                  {items.length === 0 ? (
                    <div className="rounded-[10px] border border-dashed border-[var(--surface-border)] p-3 text-center text-[11px] text-[var(--muted-foreground)]">
                      —
                    </div>
                  ) : null}
                </div>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
