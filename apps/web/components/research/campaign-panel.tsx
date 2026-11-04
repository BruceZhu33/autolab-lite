"use client";

/** 并行研究分支 Campaign 面板 (Phase C3a/C3b):
 * 展示源项目上的多方向并行 git worktree 分叉实验进度、Python 选出的冠军分支,
 * 并支持一键将冠军代码 merge 回源项目 + 采纳冠军实测均值为新基线 (Promote)。 */

import { useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle, CheckCircle2, ExternalLink, GitBranch, GitMerge,
  Loader2, ShieldCheck, Square, Trophy,
} from "lucide-react";
import {
  fetchCampaign, fetchProjectCampaigns, promoteCampaign, stopCampaign,
  type CampaignBranch, type CampaignStatus, type Project, type ResearchCampaign,
} from "@/lib/api";
import { Badge } from "~/vendor/ui/components/ui/badge";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
  AlertDialogTrigger,
} from "~/vendor/ui/components/ui/alert-dialog";

const CAMPAIGN_STATUS_LABEL: Record<CampaignStatus, { label: string; cls: string }> = {
  running: { label: "并行探索中", cls: "bg-brand/10 text-brand" },
  done: { label: "已收束", cls: "bg-success/10 text-success" },
  stopped: { label: "已停止", cls: "bg-muted/60 text-muted-foreground" },
  failed: { label: "失败", cls: "bg-destructive/10 text-destructive" },
};

const RUN_STATUS_LABEL: Record<string, { label: string; cls: string }> = {
  running: { label: "工作中", cls: "bg-brand/10 text-brand" },
  waiting_human: { label: "等人回复", cls: "bg-warning/15 text-warning" },
  waiting_jobs: { label: "等 GPU", cls: "bg-info/10 text-info" },
  done: { label: "完成", cls: "bg-success/10 text-success" },
  failed: { label: "失败", cls: "bg-destructive/10 text-destructive" },
  stopped: { label: "已停止", cls: "bg-muted/60 text-muted-foreground" },
};

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  return `${iso.slice(5, 10)} ${iso.slice(11, 16)}`;
}

function fmtGain(v?: number | null): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(4)}`;
}

function BranchCard({ branch, isWinner }: { branch: CampaignBranch; isWinner: boolean }) {
  const status = RUN_STATUS_LABEL[branch.runStatus ?? ""] ?? {
    label: branch.runStatus ?? "未知",
    cls: "bg-muted/60 text-muted-foreground",
  };
  const champion = branch.result?.champion;
  const gain = branch.bestGain ?? champion?.meanGain ?? null;
  const rounds = branch.result?.rounds;
  const succeeded = branch.runStatus === "done" && branch.result?.success === true;

  return (
    <div
      className={`flex flex-col justify-between rounded-xl border-[0.5px] p-3 transition-colors ${
        isWinner
          ? "border-success/50 bg-success/5 ring-1 ring-success/20"
          : "border-surface-border bg-background/60"
      }`}
    >
      <div className="space-y-1.5">
        <div className="flex flex-wrap items-center gap-1.5">
          {isWinner ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-success/15 px-2 py-0.5 text-micro font-semibold text-success">
              <Trophy className="size-3" />冠军分支
            </span>
          ) : null}
          <span className="truncate text-caption font-semibold">
            {branch.name ?? `分支 ${(branch.index ?? 0) + 1}`}
          </span>
          <span className={`ml-auto rounded-full px-2 py-0.5 text-micro font-medium ${status.cls}`}>
            {status.label}
          </span>
        </div>

        {branch.idea ? (
          <p className="line-clamp-2 text-caption text-foreground/85" title={branch.idea}>
            {branch.idea}
          </p>
        ) : null}

        {branch.fork ? (
          <p className="truncate font-mono text-micro text-muted-foreground" title={branch.fork}>
            <GitBranch className="mr-1 inline size-3 align-[-2px]" />
            {branch.fork}
          </p>
        ) : null}
      </div>

      <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2 border-t border-surface-border/60 pt-2 text-micro">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-muted-foreground">
            增益:{" "}
            <span className={`font-mono font-semibold tabular-nums ${
              succeeded && gain != null && gain > 0 ? "text-success" : "text-foreground"
            }`}>
              {fmtGain(gain)}
            </span>
          </span>
          {rounds != null ? (
            <span className="text-muted-foreground">· {rounds} 轮</span>
          ) : null}
          {champion?.dataset ? (
            <span className="rounded bg-muted/60 px-1.5 py-0.5 font-mono text-muted-foreground">
              {champion.dataset} ({champion.n} seed)
            </span>
          ) : null}
        </div>
        {branch.projectId ? (
          <Link
            href={`/research?project=${branch.projectId}`}
            className="inline-flex items-center gap-0.5 text-brand hover:underline"
          >
            查看分支会话
            <ExternalLink className="size-3" />
          </Link>
        ) : null}
      </div>
    </div>
  );
}

function PromoteAction({ campaign, project }: { campaign: ResearchCampaign; project: Project }) {
  const qc = useQueryClient();
  const [promoting, setPromoting] = useState(false);
  const winner = campaign.winner;
  if (!winner) return null;

  const promote = async () => {
    setPromoting(true);
    try {
      await promoteCampaign(campaign.id);
      toast.success(`已将冠军分支「${winner.name ?? winner.projectId}」回灌至 ${project.name}`);
      void qc.invalidateQueries({ queryKey: ["campaigns", project.id] });
      void qc.invalidateQueries({ queryKey: ["campaign", campaign.id] });
      void qc.invalidateQueries({ queryKey: ["datasets", project.id] });
      void qc.invalidateQueries({ queryKey: ["projects"] });
    } catch (err) {
      toast.error(`冠军回灌失败:${(err as Error).message}`);
    } finally {
      setPromoting(false);
    }
  };

  return (
    <AlertDialog>
      <AlertDialogTrigger render={
        <Button size="sm" className="gap-1" disabled={promoting} />
      }>
        {promoting ? <Loader2 className="size-3.5 animate-spin" /> : <GitMerge className="size-3.5" />}
        回灌冠军到源项目
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>回灌冠军分支到「{project.name}」?</AlertDialogTitle>
          <AlertDialogDescription>
            系统将在源项目工作区执行 <code className="rounded bg-muted px-1 font-mono">git merge --no-ff</code> 合并冠军分支{" "}
            <span className="font-medium text-foreground">{winner.name ?? winner.runId}</span>（均值增益{" "}
            <span className="font-mono">{fmtGain(winner.gain)}</span>），并将冠军已验证的多 seed 均值采纳为源项目新基线（旧基线自动归入历史）。
            若源工作区有未提交改动或合并冲突，会自动安全中止并保持源工作区原样。
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>取消</AlertDialogCancel>
          <AlertDialogAction onClick={() => void promote()}>确认合并并采纳基线</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

export function CampaignPanel({ project }: { project: Project }) {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);

  const listQuery = useQuery({
    queryKey: ["campaigns", project.id],
    queryFn: () => fetchProjectCampaigns(project.id),
    refetchInterval: 5000,
  });

  const campaigns = listQuery.data?.campaigns ?? [];
  const activeId = selectedId && campaigns.some((c) => c.id === selectedId)
    ? selectedId
    : campaigns[0]?.id ?? null;

  const detailQuery = useQuery({
    queryKey: ["campaign", activeId ?? ""],
    queryFn: () => fetchCampaign(activeId!),
    enabled: !!activeId,
    refetchInterval: (query) =>
      (query.state.data?.campaign.status ?? "running") === "running" ? 4000 : false,
  });

  if (campaigns.length === 0) return null;

  const campaign = detailQuery.data?.campaign ?? campaigns[0];
  const st = CAMPAIGN_STATUS_LABEL[campaign.status] ?? CAMPAIGN_STATUS_LABEL.stopped;
  const promotion = campaign.result?.promotion;
  const winnerRunId = campaign.winner?.runId ?? null;
  const canPromote =
    campaign.status === "done" && !!campaign.winner && !promotion;

  const stop = async () => {
    setStopping(true);
    try {
      await stopCampaign(campaign.id);
      toast.success("已请求停止并行分支 Campaign");
      void qc.invalidateQueries({ queryKey: ["campaigns", project.id] });
      void qc.invalidateQueries({ queryKey: ["campaign", campaign.id] });
    } catch (err) {
      toast.error(`停止失败:${(err as Error).message}`);
    } finally {
      setStopping(false);
    }
  };

  return (
    <div className="space-y-2.5 rounded-xl border-[0.5px] border-surface-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <GitBranch className="size-4 text-brand" />
        <span className="text-caption font-semibold">并行研究分支 (Campaign)</span>
        <span className={`rounded-full px-2 py-0.5 text-micro font-medium ${st.cls}`}>
          {st.label}
        </span>
        {campaign.status === "running" ? (
          <Loader2 className="size-3.5 animate-spin text-brand" />
        ) : null}
        {campaign.config?.tier ? (
          <Badge variant="outline" className="text-micro">{campaign.config.tier}</Badge>
        ) : null}
        {campaign.config?.critic ? (
          <Badge variant="ghost" className="bg-info/10 text-micro text-info">
            <ShieldCheck className="mr-0.5 inline size-3" />Critic
          </Badge>
        ) : null}
        {campaign.config?.reviewer ? (
          <Badge variant="ghost" className="bg-info/10 text-micro text-info">
            <ShieldCheck className="mr-0.5 inline size-3" />Reviewer
          </Badge>
        ) : null}

        {campaigns.length > 1 ? (
          <select
            value={campaign.id}
            onChange={(e) => setSelectedId(e.target.value)}
            className="ml-auto h-7 rounded-md border-[0.5px] border-surface-border bg-background px-2 font-mono text-micro"
          >
            {campaigns.map((c, idx) => (
              <option key={c.id} value={c.id}>
                #{campaigns.length - idx} · {c.id.slice(-8)} · {CAMPAIGN_STATUS_LABEL[c.status]?.label ?? c.status} ({fmtTime(c.createdAt)})
              </option>
            ))}
          </select>
        ) : (
          <span className="ml-auto font-mono text-micro text-muted-foreground">
            {campaign.id.slice(-8)} · {fmtTime(campaign.createdAt)}
          </span>
        )}

        {campaign.status === "running" ? (
          <Button
            size="sm"
            variant="outline"
            className="h-7 gap-1 px-2 text-micro"
            disabled={stopping}
            onClick={() => void stop()}
          >
            {stopping ? <Loader2 className="size-3 animate-spin" /> : <Square className="size-3" />}
            停止全部分支
          </Button>
        ) : null}
      </div>

      {campaign.result?.error ? (
        <p className="rounded-lg border-[0.5px] border-destructive/30 bg-destructive/5 px-2.5 py-1.5 text-micro text-destructive">
          <AlertTriangle className="mr-1 inline size-3 align-[-2px]" />
          {campaign.result.error}
        </p>
      ) : null}

      <div className="grid gap-2 md:grid-cols-3">
        {campaign.branches.map((b, idx) => (
          <BranchCard
            key={b.runId || idx}
            branch={b}
            isWinner={!!winnerRunId && b.runId === winnerRunId}
          />
        ))}
      </div>

      {canPromote && campaign.winner ? (
        <div className="flex flex-wrap items-center gap-2 rounded-xl border-[0.5px] border-success/40 bg-success/5 p-2.5">
          <Trophy className="size-4 text-success" />
          <p className="min-w-0 flex-1 text-caption">
            Python 已按验收指标选出冠军分支{" "}
            <span className="font-semibold">{campaign.winner.name ?? campaign.winner.projectId}</span>
            （均值增益 <span className="font-mono font-semibold text-success">{fmtGain(campaign.winner.gain)}</span>）。
            是否将冠军代码合并回源项目并采纳为新基线？
          </p>
          <PromoteAction campaign={campaign} project={project} />
        </div>
      ) : null}

      {promotion ? (
        <div className="flex flex-wrap items-center gap-2 rounded-xl border-[0.5px] border-success/40 bg-success/5 p-2.5 text-caption">
          <CheckCircle2 className="size-4 text-success" />
          <span>
            已回灌至源项目（分支 <code className="font-mono text-micro">{promotion.winnerBranch}</code> → commit{" "}
            <code className="font-mono text-micro">{promotion.sourceCommit.slice(0, 8)}</code>，{fmtTime(promotion.at)}）
          </span>
        </div>
      ) : null}

      {campaign.status === "done" && !campaign.winner && !campaign.result?.error ? (
        <p className="text-micro text-muted-foreground">
          所有并行分支已结束，本次没有分支产生超越基线的已验收提升。
        </p>
      ) : null}
    </div>
  );
}
