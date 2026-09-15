'use client';

/**
 * Phase A · Timeline item 渲染（方案 v0.2.1 §5.2）。
 * 八种条目：用户评论 / Agent 更新 / Agent 问题 / 动作提案 / 系统事件（读时投影，
 * Phase A 以 fixture 模拟）/ Run 卡片 / 错误阻塞 / 人工裁决。
 */

import Link from 'next/link';
import { useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { toast } from 'sonner';
import {
  IconAlertTriangle,
  IconChevronDown,
  IconCircleCheck,
  IconClock,
  IconFlask,
  IconGavel,
  IconProgress,
  IconTerminal2,
  IconUser
} from '@tabler/icons-react';
import type { TimelineItem } from '../fixtures';

function Ts({ ts }: { ts: string }) {
  return (
    <span className='text-muted-foreground/70 shrink-0 font-mono text-[11px] tabular-nums'>
      {ts}
    </span>
  );
}

function AgentAvatar({ name }: { name: string }) {
  return (
    <span className='bg-primary/10 text-primary flex size-6 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold'>
      {name.slice(0, 2)}
    </span>
  );
}

function ProposalCard({ item }: { item: Extract<TimelineItem, { kind: 'proposal' }> }) {
  const [state, setState] = useState<'proposed' | 'approved' | 'rejected'>('proposed');

  return (
    <div className='rounded-lg border border-amber-500/40 bg-amber-500/5 p-3'>
      <div className='flex items-center justify-between gap-2'>
        <div className='flex items-center gap-2'>
          <AgentAvatar name={item.agent} />
          <span className='text-xs font-medium'>动作提案 · {item.agent}</span>
        </div>
        <Ts ts={item.ts} />
      </div>
      <p className='mt-2 text-sm font-medium'>{item.title}</p>
      <p className='text-muted-foreground mt-1 text-xs leading-relaxed'>{item.rationale}</p>

      <div className='bg-background/60 mt-2 grid gap-x-4 gap-y-1 rounded-md p-2 text-[11px] sm:grid-cols-2'>
        {item.impact.budget ? (
          <div>
            <span className='text-muted-foreground'>预算影响：</span>
            <span className='font-medium'>{item.impact.budget}</span>
          </div>
        ) : null}
        {item.impact.command ? (
          <div>
            <span className='text-muted-foreground'>命令入口：</span>
            <code className='font-mono'>{item.impact.command}</code>
          </div>
        ) : null}
        {item.impact.risk ? (
          <div className='sm:col-span-2'>
            <span className='text-muted-foreground'>风险：</span>
            {item.impact.risk}
          </div>
        ) : null}
      </div>

      {state === 'proposed' ? (
        <div className='mt-2.5 flex gap-2'>
          <Button
            size='sm'
            className='h-7 text-xs'
            onClick={() => {
              setState('approved');
              toast('Phase A 演示：已本地标记批准', {
                description: '真实链路（Phase D）将经 sidecar guard 与 revision 校验后执行'
              });
            }}
          >
            批准
          </Button>
          <Button
            size='sm'
            variant='outline'
            className='h-7 text-xs'
            onClick={() => {
              setState('rejected');
              toast('Phase A 演示：已本地标记拒绝');
            }}
          >
            拒绝
          </Button>
        </div>
      ) : (
        <div className='mt-2.5'>
          <Badge variant={state === 'approved' ? 'default' : 'secondary'} className='gap-1'>
            <IconCircleCheck className='size-3' />
            {state === 'approved' ? '已批准（本地演示）' : '已拒绝（本地演示）'}
          </Badge>
        </div>
      )}
    </div>
  );
}

function RunCard({ item }: { item: Extract<TimelineItem, { kind: 'run' }> }) {
  const failed = item.state === 'FAILED';
  return (
    <Link
      href={`/dashboard/workspace/${item.runId === 'run-r1-failed' ? 'mynet-clinic' : 'mynet-clinic-v3'}/runs/${item.runId}`}
      className='hover:bg-muted/40 block rounded-lg border p-3 transition-colors'
    >
      <div className='flex items-center justify-between gap-2'>
        <div className='flex items-center gap-2'>
          <IconFlask className={failed ? 'text-destructive size-4 shrink-0' : 'text-muted-foreground size-4 shrink-0'} />
          <span className='text-xs font-medium'>{item.label}</span>
          <Badge variant={failed ? 'destructive' : 'secondary'} className='h-4 px-1.5 text-[10px]'>
            {item.state}
          </Badge>
        </div>
        <Ts ts={item.ts} />
      </div>
      {(item.metrics || item.note) && (
        <p className='text-muted-foreground mt-1.5 pl-6 text-xs'>
          {item.metrics ? <span className='font-medium text-foreground'>{item.metrics}</span> : null}
          {item.metrics && item.note ? ' · ' : ''}
          {item.note}
        </p>
      )}
    </Link>
  );
}

function SystemEventRow({ item }: { item: Extract<TimelineItem, { kind: 'system' }> }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className='group rounded-md border border-dashed px-2.5 py-1.5 text-xs'
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary className='text-muted-foreground flex cursor-pointer list-none items-center gap-2 [&::-webkit-details-marker]:hidden'>
        <IconChevronDown
          className={'size-3 shrink-0 transition-transform ' + (open ? 'rotate-0' : '-rotate-90')}
        />
        <IconTerminal2 className='size-3 shrink-0' />
        <span className='truncate'>{item.event}</span>
        <span className='ml-auto'>
          <Ts ts={item.ts} />
        </span>
      </summary>
      {item.detail ? (
        <p className='text-muted-foreground/80 mt-1.5 pl-5 font-mono text-[11px] leading-relaxed'>
          {item.detail}
        </p>
      ) : (
        <p className='text-muted-foreground/60 mt-1.5 pl-5 text-[11px]'>
          技术详情（state_events 投影 · Phase B 接真实数据）
        </p>
      )}
    </details>
  );
}

function BlockerCard({ item }: { item: Extract<TimelineItem, { kind: 'blocker' }> }) {
  return (
    <div className='rounded-lg border border-destructive/40 bg-destructive/5 p-3'>
      <div className='text-destructive flex items-center gap-1.5 text-xs font-semibold'>
        <IconAlertTriangle className='size-4 shrink-0' />
        {item.title}
      </div>
      <p className='text-muted-foreground mt-1.5 text-xs leading-relaxed'>{item.reason}</p>
      <div className='mt-2 flex flex-wrap gap-1.5'>
        {item.actions.map((a) => (
          <Button
            key={a}
            size='sm'
            variant={a.includes('推荐') ? 'default' : 'outline'}
            className='h-6 px-2 text-[11px]'
            onClick={() => toast('Phase A 占位：恢复入口待接 sidecar 命令', { description: a })}
          >
            {a}
          </Button>
        ))}
      </div>
    </div>
  );
}

export function TimelineRow({ item }: { item: TimelineItem }) {
  switch (item.kind) {
    case 'user':
      return (
        <div className='flex gap-2.5'>
          <span className='bg-muted text-muted-foreground flex size-6 shrink-0 items-center justify-center rounded-full'>
            <IconUser className='size-3.5' />
          </span>
          <div className='min-w-0 flex-1'>
            <div className='flex items-baseline justify-between gap-2'>
              <span className='text-xs font-semibold'>你</span>
              <Ts ts={item.ts} />
            </div>
            <p className='bg-primary/5 mt-1 rounded-lg rounded-tl-none px-3 py-2 text-sm leading-relaxed'>
              {item.content}
            </p>
          </div>
        </div>
      );

    case 'agent':
      return (
        <div className='flex gap-2.5'>
          <AgentAvatar name={item.agent} />
          <div className='min-w-0 flex-1'>
            <div className='flex items-baseline justify-between gap-2'>
              <span className='flex items-center gap-1.5 text-xs font-semibold'>
                {item.agent}
                <span className='text-muted-foreground/70 font-mono text-[10px] font-normal'>
                  {item.jobState}
                </span>
              </span>
              <Ts ts={item.ts} />
            </div>
            <p className='text-muted-foreground mt-1 rounded-lg border px-3 py-2 text-sm leading-relaxed'>
              {item.content}
            </p>
          </div>
        </div>
      );

    case 'question':
      return (
        <div className='flex gap-2.5'>
          <AgentAvatar name={item.agent} />
          <div className='min-w-0 flex-1'>
            <div className='flex items-baseline justify-between gap-2'>
              <span className='text-xs font-semibold'>
                {item.agent} · 提问
                <IconGavel className='text-amber-500 ml-1 inline size-3.5' />
              </span>
              <Ts ts={item.ts} />
            </div>
            <p className='mt-1 rounded-lg border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-sm leading-relaxed'>
              {item.content}
            </p>
            <Button
              size='sm'
              variant='outline'
              className='mt-1.5 h-6 px-2 text-[11px]'
              onClick={() =>
                toast('Phase A 演示：聚焦评论框回复', {
                  description: 'Phase C 起 Agent 将在回复后创建新 job'
                })
              }
            >
              回复
            </Button>
          </div>
        </div>
      );

    case 'proposal':
      return <ProposalCard item={item} />;

    case 'system':
      return <SystemEventRow item={item} />;

    case 'run':
      return <RunCard item={item} />;

    case 'blocker':
      return <BlockerCard item={item} />;

    case 'decision':
      return (
        <div className='flex items-center gap-2.5 rounded-md border px-2.5 py-1.5 text-xs'>
          <IconGavel className='text-muted-foreground size-3.5 shrink-0' />
          <span className='text-muted-foreground'>
            <span className='text-foreground font-medium'>{item.who}</span> 裁决：{item.what}
          </span>
          <span className='ml-auto'>
            <Ts ts={item.ts} />
          </span>
        </div>
      );

    default:
      return null;
  }
}

export function AgentWorkingBadge({ note }: { note: string }) {
  return (
    <span className='flex items-center gap-1.5 text-xs text-emerald-600 dark:text-emerald-400'>
      <IconProgress className='size-3.5 animate-spin [animation-duration:3s]' />
      {note}
    </span>
  );
}

export { IconClock };
