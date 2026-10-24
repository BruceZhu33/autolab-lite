"use client";

/** 研究日志:每轮的假设 / 改动 / 分析,以及 Python 对照基线的判定;
 * 结论自动回流知识库的轮次带「已回流」徽标,可一键去知识对话提问。 */

import Link from "next/link";
import { BookDown } from "lucide-react";
import type { ResearchRound } from "@/lib/api";

const fmt = (v: number | null | undefined) => (v == null ? "-" : v.toFixed(4));

function RoundItem({ round, projectTitle }: { round: ResearchRound; projectTitle: string }) {
  const ev = round.evaluation;
  return (
    <details className="rounded-md border-[0.5px] border-surface-border px-2 py-1">
      <summary className="cursor-pointer text-caption">
        <span className="font-medium">第 {round.roundNo} 轮</span>
        <span className={`ml-2 rounded-full px-1.5 text-micro ${ev.anyImproved
          ? "bg-success/10 text-success" : "bg-muted/60 text-muted-foreground"}`}>
          {ev.anyImproved ? "有提升" : "未提升"}
          {ev.bestGain != null ? ` ${ev.bestGain >= 0 ? "+" : ""}${ev.bestGain.toFixed(4)}` : ""}
        </span>
        <span className="ml-2 text-muted-foreground">{round.hypothesis}</span>
      </summary>
      <div className="mt-1 space-y-1 text-caption">
        <p><span className="text-muted-foreground">改动:</span>{round.changes}</p>
        <p><span className="text-muted-foreground">分析:</span>{round.analysis}</p>
        {round.nextIdea ? <p><span className="text-muted-foreground">下一步:</span>{round.nextIdea}</p> : null}
        {round.kbReflow ? (
          round.kbReflow.ok ? (
            <p className="flex flex-wrap items-center gap-2 text-micro">
              <span className="inline-flex items-center gap-1 rounded-full bg-info/10 px-1.5 py-0.5 text-info">
                <BookDown className="size-3" />已回流知识库
              </span>
              <Link className="text-brand underline underline-offset-2"
                    href={`/kb?q=${encodeURIComponent(`${projectTitle} 第 ${round.roundNo} 轮的实验结论是什么?`)}`}>
                去知识对话问
              </Link>
            </p>
          ) : (
            <p className="text-micro text-warning">
              回流知识库失败:{round.kbReflow.error ?? "未知原因"}(不影响研究)
            </p>
          )
        ) : null}
        <table className="w-full text-micro">
          <tbody>
            {ev.rows.map((row) => (
              <tr key={row.jobId}>
                <td className="pr-2">{row.name}</td>
                <td className="pr-2 text-muted-foreground">{row.dataset ?? "-"}</td>
                <td className="pr-2">{row.status}</td>
                <td className="font-mono tabular-nums">
                  {row.metric}={fmt(row.value)}(基线 {fmt(row.baseline)})
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {ev.warnings.map((w) => <p key={w} className="text-micro text-warning">⚠ {w}</p>)}
      </div>
    </details>
  );
}

export function ResearchJournal({ rounds, projectTitle }: {
  rounds: ResearchRound[]; projectTitle: string;
}) {
  if (!rounds.length) return null;
  return (
    <div className="space-y-1.5 rounded-xl border-[0.5px] border-surface-border bg-card p-3">
      <p className="text-caption font-semibold">研究日志</p>
      {rounds.map((r) => <RoundItem key={r.id} round={r} projectTitle={projectTitle} />)}
    </div>
  );
}
