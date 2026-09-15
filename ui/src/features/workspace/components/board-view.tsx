'use client';

/**
 * Phase A · Project Board（方案 v0.2.1 §4）。
 * 六列固定阶段，卡片位置由项目真实状态决定，不提供跨列拖拽（§4.3：
 * 阶段切换只能来自合法状态迁移动作）。fixture 驱动，不接 sidecar。
 */

import Link from 'next/link';
import { Badge } from '@/components/ui/badge';
import { buttonVariants } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import {
  IconActivity,
  IconAlertTriangle,
  IconArrowNarrowUp,
  IconCircleCheck,
  IconMessage2,
  IconPlayerPause,
  IconProgress,
  type TablerIcon
} from '@tabler/icons-react';
import {
  AGENT_STATUS_LABEL,
  BOARD_PROJECTS,
  STAGES,
  type AgentStatus,
  type BoardProject,
  type StageKey
} from '../fixtures';

const AGENT_ICON: Record<AgentStatus, TablerIcon> = {
  idle: IconCircleCheck,
  working: IconProgress,
  'waiting-reply': IconMessage2,
  'waiting-approval': IconPlayerPause,
  failed: IconAlertTriangle
};

function agentChipTone(status: AgentStatus): string {
  switch (status) {
    case 'working':
      return 'text-emerald-600 dark:text-emerald-400';
    case 'waiting-reply':
    case 'waiting-approval':
      return 'text-amber-600 dark:text-amber-400';
    case 'failed':
      return 'text-destructive';
    default:
      return 'text-muted-foreground';
  }
}

function ProjectCard({ project }: { project: BoardProject }) {
  const AgentIcon = AGENT_ICON[project.agent];
  const blocked = project.blockers.length > 0;
  const wsHref = `/dashboard/workspace/${project.id}`;

  return (
    <Card
      className={
        'group/card gap-0 py-0 transition-shadow hover:shadow-md ' +
        (blocked ? 'border-destructive/40' : '')
      }
    >
      <CardContent className='flex flex-col gap-2 p-3'>
        <div className='flex items-start justify-between gap-2'>
          <Link
            href={wsHref}
            className='truncate text-sm font-semibold hover:underline'
            title={project.name}
          >
            {project.name}
          </Link>
                   {project.unread ? (
            <Badge variant='secondary' className='gap-1 px-1.5 tabular-nums'>
              <IconMessage2 className='size-3' />
              {project.unread}
            </Badge>
          ) : null}
        </div>

        <p className='text-muted-foreground line-clamp-2 text-xs leading-relaxed'>{project.goal}</p>

        <div className='flex items-center gap-1.5 text-xs'>
          <span className={agentChipTone(project.agent) + ' flex items-center gap-1'}>
            <AgentIcon className={project.agent === 'working' ? 'size-3.5 animate-spin [animation-duration:3s]' : 'size-3.5'} />
            {AGENT_STATUS_LABEL[project.agent]}
          </span>
          <span className='text-muted-foreground/60'>·</span>
          <span className='text-muted-foreground truncate'>{project.stageProgress}</span>
        </div>

        {project.bestMetric ? (
          <div className='flex items-center gap-2 text-xs'>
            <span className='font-medium tabular-nums'>
              {project.bestMetric.name} {project.bestMetric.value}
            </span>
            {project.bestMetric.deltaVsBase ? (
              <span className='flex items-center gap-0.5 text-emerald-600 tabular-nums dark:text-emerald-400'>
                <IconArrowNarrowUp className='size-3.5' />
                {project.bestMetric.deltaVsBase}
              </span>
            ) : null}
          </div>
        ) : null}

        {(project.compute || project.budget) && (
          <div className='text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px]'>
            {project.compute ? <span>{project.compute}</span> : null}
            {project.budget ? <span className='tabular-nums'>预算 {project.budget}</span> : null}
          </div>
        )}

        {blocked ? (
          <div className='rounded-md border border-destructive/30 bg-destructive/5 px-2 py-1.5'>
            <div className='text-destructive flex items-center gap-1 text-xs font-medium'>
              <IconAlertTriangle className='size-3.5 shrink-0' />
              需要你 · {project.blockers.length}
            </div>
            <p className='text-muted-foreground mt-0.5 line-clamp-2 text-[11px] leading-relaxed'>
              {project.blockers[0].title.replace('需要你：', '')}
            </p>
          </div>
        ) : null}

        <div className='mt-0.5 flex items-center justify-between gap-2'>
          <span className='text-muted-foreground/70 flex items-center gap-1 text-[11px]'>
            <IconActivity className='size-3' />
            {project.lastActivity}
          </span>
        </div>

        <Link
          href={wsHref}
          className={buttonVariants({
            variant: blocked ? 'default' : 'outline',
            size: 'sm',
            className: 'h-7 w-full justify-center text-xs'
          })}
        >
          {project.primaryAction}
        </Link>
      </CardContent>
    </Card>
  );
}

export default function BoardView() {
  const byStage = (key: StageKey) => BOARD_PROJECTS.filter((p) => p.stage === key);
  const totalNeedYou = BOARD_PROJECTS.reduce((n, p) => n + p.blockers.length, 0);

  return (
    <div className='flex h-[calc(100dvh-3.5rem)] flex-col gap-3 p-4'>
      <div className='flex shrink-0 flex-wrap items-center justify-between gap-2'>
        <div>
          <h1 className='text-lg font-semibold tracking-tight'>项目看板</h1>
          <p className='text-muted-foreground text-xs'>
            {BOARD_PROJECTS.length} 个项目 · 阶段位置由科研状态机决定，卡片不可跨列拖动
          </p>
        </div>
        {totalNeedYou > 0 ? (
          <Badge variant='destructive' className='gap-1'>
            <IconAlertTriangle className='size-3.5' />
            {totalNeedYou} 项需要你
          </Badge>
        ) : null}
      </div>

      <div className='min-h-0 flex-1 overflow-x-auto'>
        <div className='flex h-full min-w-[1080px] gap-3'>
          {STAGES.map((stage) => {
            const projects = byStage(stage.key);
            const needYou = projects.reduce((n, p) => n + p.blockers.length, 0);
            return (
              <section key={stage.key} className='flex h-full min-w-0 flex-1 flex-col gap-2'>
                <div className='bg-muted/50 flex h-8 shrink-0 items-center justify-between rounded-md px-2.5'>
                  <span className='text-xs font-medium'>{stage.label}</span>
                  <span className='text-muted-foreground flex items-center gap-1.5 text-[11px] tabular-nums'>
                    {projects.length} 项
                    {needYou > 0 ? (
                      <span className='text-destructive' title={`${needYou} 项阻塞等待人工处理`}>
                        · {needYou} 需处理
                      </span>
                    ) : null}
                  </span>
                </div>
                <div className='flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto pr-0.5'>
                  {projects.map((p) => (
                    <ProjectCard key={p.id} project={p} />
                  ))}
                  {projects.length === 0 ? (
                    <div className='text-muted-foreground/50 flex flex-1 items-center justify-center rounded-md border border-dashed text-[11px]'>
                      暂无项目
                    </div>
                  ) : null}
                </div>
              </section>
            );
          })}
        </div>
      </div>
    </div>
  );
}
