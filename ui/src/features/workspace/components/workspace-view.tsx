'use client';

/**
 * Phase A · Project Workspace 三段式（方案 v0.2.1 §5）。
 * 头部条 + Workflow Rail + （Conversation Timeline | Context Panel）+ composer。
 * fixture 驱动；composer 只作用于本地状态。
 */

import Link from 'next/link';
import { useMemo, useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { toast } from 'sonner';
import {
  IconAlertTriangle,
  IconChevronRight,
  IconCircleCheck,
  IconFileText,
  IconFlask,
  IconGauge,
  IconLock,
  IconBook2,
  IconProgress,
  IconSend,
  IconSparkles
} from '@tabler/icons-react';
import {
  AGENT_STATUS_LABEL,
  STAGES,
  type TimelineItem,
  type WorkspaceFixture
} from '../fixtures';
import { AgentWorkingBadge, TimelineRow } from './timeline-items';

/* ---------------- Workflow Rail ---------------- */

function WorkflowRail({ rail }: { rail: WorkspaceFixture['rail'] }) {
  return (
    <nav aria-label='科研流程' className='flex shrink-0 items-center gap-1 overflow-x-auto'>
      {STAGES.map((stage, i) => {
        const node = rail.find((r) => r.stage === stage.key);
        const state = node?.state ?? 'future';
        return (
          <div key={stage.key} className='flex shrink-0 items-center gap-1'>
            {i > 0 ? <IconChevronRight className='text-muted-foreground/40 size-3.5 shrink-0' /> : null}
            <button
              type='button'
              title={node?.note ?? '未进入'}
              onClick={() =>
                toast(
                  state === 'current'
                    ? '已在工作现场'
                    : state === 'done'
                      ? '只读回放（Phase B 接真实历史）'
                      : '进入条件与缺口（Phase B 接 available-actions）',
                  { description: node?.note ?? '该阶段尚未进入' }
                )
              }
              className={
                'flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs transition-colors ' +
                (state === 'current'
                  ? 'bg-primary text-primary-foreground font-medium'
                  : state === 'done'
                    ? 'text-emerald-600 hover:bg-muted dark:text-emerald-400'
                    : 'text-muted-foreground/60 hover:bg-muted/50')
              }
            >
              {state === 'done' ? (
                <IconCircleCheck className='size-3.5 shrink-0' />
              ) : state === 'current' ? (
                <IconProgress className='size-3.5 shrink-0 animate-spin [animation-duration:3s]' />
              ) : (
                <IconLock className='size-3 shrink-0' />
              )}
              {stage.label}
            </button>
          </div>
        );
      })}
    </nav>
  );
}

/* ---------------- Context Panel ---------------- */

function ContextPanel({ context }: { context: WorkspaceFixture['context'] }) {
  return (
    <aside className='flex flex-col gap-3'>
      <section className='rounded-lg border p-3'>
        <h3 className='flex items-center gap-1.5 text-xs font-semibold'>
          <IconFileText className='size-3.5' />
          当前方案
        </h3>
        <p className='text-muted-foreground mt-1.5 text-xs leading-relaxed whitespace-pre-line'>
          {context.plan}
        </p>
      </section>

      <section className='rounded-lg border p-3'>
        <h3 className='flex items-center gap-1.5 text-xs font-semibold'>
          <IconBook2 className='size-3.5' />
          论文
        </h3>
        <ul className='mt-1.5 space-y-1.5'>
          {context.papers.map((p) => (
            <li key={p.title} className='text-xs'>
              <button
                type='button'
                className='hover:underline text-left font-medium'
                onClick={() => toast('Phase B：接论文详情（arXiv 抓取已有）')}
              >
                {p.title}
              </button>
              <p className='text-muted-foreground text-[11px]'>{p.note}</p>
            </li>
          ))}
        </ul>
      </section>

      <section className='rounded-lg border p-3'>
        <h3 className='flex items-center gap-1.5 text-xs font-semibold'>
          <IconFlask className='size-3.5' />
          当前实验
        </h3>
        <p className='text-muted-foreground mt-1.5 text-xs leading-relaxed whitespace-pre-line'>
          {context.experiment}
        </p>
      </section>

      <section className='rounded-lg border p-3'>
        <h3 className='flex items-center gap-1.5 text-xs font-semibold'>
          <IconGauge className='size-3.5' />
          资源
        </h3>
        <div className='text-muted-foreground mt-1.5 space-y-0.5 text-xs'>
          <p>{context.resources.gpu}</p>
          <p className='tabular-nums'>预算 {context.resources.budget}</p>
        </div>
      </section>

      <section className='rounded-lg border p-3'>
        <h3 className='flex items-center gap-1.5 text-xs font-semibold'>
          <IconLock className='size-3.5' />
          产物
        </h3>
        <ul className='text-muted-foreground mt-1.5 space-y-1 text-[11px]'>
          {context.artifacts.map((a) => (
            <li key={a} className='truncate'>
              · {a}
            </li>
          ))}
        </ul>
      </section>
    </aside>
  );
}

/* ---------------- Composer ---------------- */

function Composer({
  onSend,
  disabledReason
}: {
  onSend: (text: string) => void;
  disabledReason?: string;
}) {
  const [value, setValue] = useState('');
  return (
    <div className='bg-background/90 sticky bottom-0 border-t p-2.5 backdrop-blur'>
      <div className='flex items-center gap-1.5 pb-1.5'>
        <Badge variant='outline' className='gap-1 text-[10px]'>
          <IconSparkles className='size-3' />
          @AutoLab
        </Badge>
        <span className='text-muted-foreground/60 text-[11px]'>
          {disabledReason ?? '评论给 Agent：提问、补充信息或下达指示（Phase A 仅本地演示）'}
        </span>
      </div>
      <div className='flex gap-2'>
        <Textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder='例如：这轮的裁剪阈值为什么选 12？/ 同意，继续下一轮 / 把 K 学习的爆炸历史写进上下文'
          className='min-h-[38px] resize-none text-sm'
          rows={2}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && value.trim()) {
              onSend(value.trim());
              setValue('');
            }
          }}
        />
        <Button
          size='sm'
          className='h-9 shrink-0 self-end'
          disabled={!value.trim()}
          onClick={() => {
            if (value.trim()) {
              onSend(value.trim());
              setValue('');
            }
          }}
        >
          <IconSend className='size-4' />
          发送
        </Button>
      </div>
    </div>
  );
}

/* ---------------- 主视图 ---------------- */

export default function WorkspaceView({ fixture }: { fixture: WorkspaceFixture }) {
  const { header, rail, context, alerts } = fixture;
  const [localMessages, setLocalMessages] = useState<TimelineItem[]>([]);
  const timeline = useMemo(
    () => [...fixture.timeline, ...localMessages],
    [fixture.timeline, localMessages]
  );

  return (
    <div className='flex h-[calc(100dvh-3.5rem)] flex-col gap-3 p-4'>
      {/* 头部条 */}
      <div className='flex shrink-0 flex-wrap items-center justify-between gap-2'>
        <div className='flex min-w-0 items-center gap-3'>
          <Link href='/dashboard/board' className='text-muted-foreground hover:text-foreground text-xs'>
            ← 看板
          </Link>
          <h1 className='truncate text-lg font-semibold tracking-tight'>{header.title}</h1>
          <Badge variant='secondary' className='shrink-0'>
            {header.stageLabel} · {header.stageProgress}
          </Badge>
        </div>
        <div className='flex items-center gap-4'>
          <div className='hidden items-center gap-4 text-xs sm:flex'>
            <AgentWorkingBadge note={header.agentNote} />
            <span className='text-muted-foreground'>
              {header.compute} · 预算 <span className='tabular-nums'>{header.budget}</span>
            </span>
          </div>
          <Button size='sm' className='h-7 text-xs' onClick={() => toast('Phase B：接真实主操作')}>
            {header.primaryAction}
          </Button>
        </div>
      </div>

      {/* 首屏"需要你"告警条 */}
      {alerts.length > 0 ? (
        <div className='shrink-0 space-y-2'>
          {alerts.map((a) => (
            <div key={a.title} className='rounded-lg border border-destructive/40 bg-destructive/5 p-3'>
              <div className='text-destructive flex items-center gap-1.5 text-sm font-semibold'>
                <IconAlertTriangle className='size-4 shrink-0' />
                {a.title}
              </div>
              <p className='text-muted-foreground mt-1 text-xs leading-relaxed'>{a.reason}</p>
              <div className='mt-2 flex flex-wrap gap-1.5'>
                {a.actions.map((act) => (
                  <Button
                    key={act}
                    size='sm'
                    variant={act.includes('推荐') ? 'default' : 'outline'}
                    className='h-7 text-xs'
                    onClick={() => toast('Phase A 占位：恢复入口待接 sidecar 命令', { description: act })}
                  >
                    {act}
                  </Button>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {/* Workflow Rail */}
      <div className='bg-muted/30 shrink-0 rounded-lg border px-2 py-1.5'>
        <WorkflowRail rail={rail} />
      </div>

      {/* 主区：Timeline | Context */}
      <div className='grid min-h-0 flex-1 gap-3 lg:grid-cols-[minmax(0,1fr)_300px]'>
        <div className='flex min-h-0 flex-col rounded-lg border'>
          <div className='text-muted-foreground flex shrink-0 items-center justify-between border-b px-3 py-2 text-xs'>
            <span className='font-medium'>对话与活动</span>
            <span className='text-muted-foreground/60'>
              Agent 状态：{AGENT_STATUS_LABEL[header.agentStatus]}
            </span>
          </div>
          <div className='min-h-0 flex-1 space-y-3 overflow-y-auto p-3'>
            {timeline.map((item, i) => (
              <TimelineRow key={i} item={item} />
            ))}
          </div>
          <Composer
            onSend={(text) => {
              setLocalMessages((prev) => [
                ...prev,
                { kind: 'user', ts: '刚刚', content: text },
                {
                  kind: 'system',
                  ts: '刚刚',
                  event: 'Phase A：消息仅写入本地演示状态（Phase C 起持久化并创建 Agent Job）'
                }
              ]);
            }}
          />
        </div>

        <div className='min-h-0 overflow-y-auto lg:block'>
          <ContextPanel context={context} />
        </div>
      </div>
    </div>
  );
}
