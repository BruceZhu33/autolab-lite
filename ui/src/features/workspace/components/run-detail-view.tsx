'use client';

/**
 * Phase A · Experiment Run detail（日志 / 指标 / diff / 产物 四个标签页）。
 */

import Link from 'next/link';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { toast } from 'sonner';
import type { RunFixture } from '../fixtures';

function DiffView({ diff }: { diff: string }) {
  if (diff.startsWith('(')) {
    return <p className='text-muted-foreground p-4 text-xs'>{diff}</p>;
  }
  return (
    <pre className='overflow-x-auto p-4 font-mono text-xs leading-relaxed'>
      {diff.split('\n').map((line, i) => (
        <div
          key={i}
          className={
            line.startsWith('+')
              ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
              : line.startsWith('-')
                ? 'bg-destructive/10 text-destructive'
                : 'text-muted-foreground'
          }
        >
          <span className='inline-block w-8 shrink-0 select-none text-right pr-2 opacity-50'>
            {i + 1}
          </span>
          {line || ' '}
        </div>
      ))}
    </pre>
  );
}

/** 轻量指标条形（不引图表库，Phase A 够用且无依赖风险）。 */
function MetricBars({ series }: { series: RunFixture['metricSeries'] }) {
  const max = Math.max(...series.map((s) => s.value));
  const min = Math.min(...series.map((s) => s.value)) * 0.95;
  return (
    <div className='flex h-40 items-end gap-6 p-4'>
      {series.map((s) => (
        <div key={s.round} className='flex h-full flex-1 flex-col items-center justify-end gap-1.5'>
          <span className='text-xs font-medium tabular-nums'>{s.value.toFixed(3)}</span>
          <div
            className='bg-primary/80 w-full max-w-16 rounded-t'
            style={{ height: `${Math.max(4, ((s.value - min) / (max - min || 1)) * 100)}%` }}
          />
          <span className='text-muted-foreground text-[11px]'>{s.round}</span>
        </div>
      ))}
    </div>
  );
}

export default function RunDetailView({ run }: { run: RunFixture }) {
  return (
    <div className='mx-auto flex max-w-4xl flex-col gap-3 p-4'>
      <div className='flex flex-wrap items-center justify-between gap-2'>
        <div className='flex min-w-0 items-center gap-3'>
          <Link
            href={`/dashboard/workspace/${run.project}`}
            className='text-muted-foreground hover:text-foreground text-xs'
          >
            ← {run.project}
          </Link>
          <h1 className='truncate text-lg font-semibold tracking-tight'>{run.label}</h1>
          <Badge variant={run.state === 'FAILED' ? 'destructive' : 'secondary'}>{run.state}</Badge>
        </div>
        <Button size='sm' variant='outline' className='h-7 text-xs' onClick={() => toast('Phase A 占位')}>
          导出产物
        </Button>
      </div>

      <Tabs defaultValue='logs' className='min-h-0 flex-1'>
        <TabsList>
          <TabsTrigger value='logs'>日志</TabsTrigger>
          <TabsTrigger value='metrics'>指标</TabsTrigger>
          <TabsTrigger value='diff'>代码变更</TabsTrigger>
          <TabsTrigger value='artifacts'>产物</TabsTrigger>
        </TabsList>

        <TabsContent value='logs'>
          <div className='rounded-lg border bg-zinc-950 p-3 font-mono text-xs leading-relaxed text-zinc-300 dark:dark:bg-zinc-900'>
            {run.logTail.map((l, i) => (
              <div key={i} className='whitespace-pre-wrap'>
                {l}
              </div>
            ))}
            <div className='mt-1 animate-pulse text-zinc-500'>▌</div>
          </div>
        </TabsContent>

        <TabsContent value='metrics'>
          <div className='overflow-hidden rounded-lg border'>
            <table className='w-full text-xs'>
              <thead className='bg-muted/50 text-muted-foreground'>
                <tr>
                  <th className='px-3 py-2 text-left font-medium'>指标</th>
                  <th className='px-3 py-2 text-right font-medium'>本轮</th>
                  <th className='px-3 py-2 text-right font-medium'>BASE</th>
                </tr>
              </thead>
              <tbody>
                {run.metricsTable.map((m) => (
                  <tr key={m.name} className='border-t'>
                    <td className='px-3 py-2'>{m.name}</td>
                    <td className='px-3 py-2 text-right font-medium tabular-nums'>{m.value}</td>
                    <td className='text-muted-foreground px-3 py-2 text-right tabular-nums'>
                      {m.base ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className='mt-3 rounded-lg border'>
            <MetricBars series={run.metricSeries} />
          </div>
        </TabsContent>

        <TabsContent value='diff'>
          <div className='overflow-hidden rounded-lg border'>
            <DiffView diff={run.diff} />
          </div>
        </TabsContent>

        <TabsContent value='artifacts'>
          <ul className='divide-y rounded-lg border'>
            {run.artifacts.map((a) => (
              <li key={a.name} className='flex items-center justify-between gap-3 px-3 py-2.5 text-xs'>
                <span className='font-mono font-medium'>{a.name}</span>
                <span className='text-muted-foreground truncate'>{a.note}</span>
                <Button
                  size='sm'
                  variant='ghost'
                  className='h-6 shrink-0 px-2 text-[11px]'
                  onClick={() => toast('Phase B：接产物下载')}
                >
                  下载
                </Button>
              </li>
            ))}
          </ul>
        </TabsContent>
      </Tabs>
    </div>
  );
}
