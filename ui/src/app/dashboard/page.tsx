'use client';
/**
 * P5 · 迭代监视（M1 最小面）
 * 项目选择 → run/attempt 时间线 + 状态（WS 实时推进）→ 手动触发训练（Round 0 基线）
 * → 日志尾实时滚动 → 紧急停止（三段停止链）→ 预算 C/R 可视。
 */
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { api, wsUrl, ApiError } from '@/lib/autolab/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Progress } from '@/components/ui/progress';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';

interface ProjectRow { id: string; name: string; status: string }
interface RunDetail {
  id: string; round_no: number; role: string; status: string; state_revision: number;
  attempts: {
    id: string; seq: number; dispatch_id: string; status: string; server_id: string;
    state_revision: number; fail_class: string | null;
  }[];
  artifacts: { id: string; kind: string; path: string }[];
}
interface ProjectDetail {
  id: string; name: string; status: string; state_revision: number;
  budget: { limit_s: number; consumed_s: number; reserved_s: number; status: string };
  runs: { id: string; round_no: number; role: string; status: string }[];
}

function MonitorPageInner() {
  const params = useSearchParams();
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [selected, setSelected] = useState<string>(params.get('project') ?? '');
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [runs, setRuns] = useState<RunDetail[]>([]);
  const [logs, setLogs] = useState<Record<string, string[]>>({});
  const [budget, setBudget] = useState('');
  const [cmd, setCmd] = useState('python train.py');
  const [budgetS, setBudgetS] = useState('300');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [live, setLive] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api<ProjectRow[]>('GET', '/api/v1/projects')
      .then((l) => {
        setProjects(l);
        setSelected((cur) => cur || l[0]?.id || '');
      })
      .catch((e) => setNote(String(e)));
  }, []);

  const refresh = useCallback(async () => {
    if (!selected) return;
    try {
      const d = await api<ProjectDetail>('GET', `/api/v1/projects/${selected}`);
      setDetail(d);
      const withAttempts = await Promise.all(
        d.runs.slice(0, 6).map((r) => api<RunDetail>('GET', `/api/v1/runs/${r.id}`))
      );
      setRuns(withAttempts);
      for (const r of withAttempts) {
        const active = r.attempts.find(
          (a) => a.status === 'RUNNING' || a.status === 'STOP_REQUESTED' || a.status === 'STOP_SENT'
        );
        if (active) {
          const lg = await api<{ lines: string[] }>(
            'GET', `/api/v1/attempts/${active.id}/logs?tail=200`
          );
          const lines = Array.isArray(lg.lines) ? lg.lines : String(lg.lines).split('\n');
          setLogs((prev) => ({ ...prev, [active.id]: lines }));
        }
      }
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    }
  }, [selected]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [refresh]);

  // ---- WS 实时事件（sub_seq 单调；收到即轻量刷新）----
  useEffect(() => {
    if (!selected) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout>;
    wsUrl().then((url) => {
      const connect = () => {
        if (closed) return;
        ws = new WebSocket(url);
        ws.onopen = () => {
          setLive(true);
          ws?.send(JSON.stringify({
            type: 'subscribe',
            filters: { project_ids: [selected], include: { log_tail: true } }
          }));
        };
        ws.onmessage = (ev) => {
          try {
            const f = JSON.parse(ev.data);
            if (f.type === 'attempt.log_tail' && f.aggregate?.id) {
              setLogs((prev) => ({
                ...prev,
                [f.aggregate.id]: String(f.payload.text ?? '').split('\n')
              }));
            } else if (f.type?.endsWith('.state_changed') || f.type?.startsWith('budget.')) {
              refresh();
            }
          } catch {
            /* ignore */
          }
        };
        ws.onclose = () => {
          setLive(false);
          if (!closed) retry = setTimeout(connect, 2000);
        };
      };
      connect();
    });
    return () => {
      closed = true;
      clearTimeout(retry);
      ws?.close();
    };
  }, [selected, refresh]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [logs]);

  const trigger = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      const r = await api<{ run_id: string; attempt_id: string; authorized_s: number }>(
        'POST', `/api/v1/projects/${selected}/runs`,
        { role: 'baseline', command: cmd, budget_s: Number(budgetS) || undefined }
      );
      setNote(`已派发：run ${r.run_id.slice(0, 12)}… attempt ${r.attempt_id.slice(0, 12)}… 授权 ${r.authorized_s}s`);
      refresh();
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  };

  const stop = async (attemptId: string) => {
    try {
      const r = await api<{ status: string }>('POST', `/api/v1/attempts/${attemptId}/stop`);
      setNote(`停止链已发起：${r.status}（等待 agent 回执确认，不虚报已停止）`);
    } catch (e) {
      setNote(String(e));
    }
  };

  const activeAttempt = useMemo(
    () => runs.flatMap((r) => r.attempts).find((a) =>
      !['FINISHED', 'FAILED', 'STOP_CONFIRMED', 'CANCELLED'].includes(a.status)),
    [runs]
  );
  const shownLog = activeAttempt ? logs[activeAttempt.id] ?? [] : [];
  const used = detail?.budget.limit_s
    ? Math.round(
        ((detail.budget.consumed_s + detail.budget.reserved_s) / detail.budget.limit_s) * 100
      )
    : 0;

  return (
    <div className="flex flex-1 flex-col gap-4 p-4 md:gap-6 md:p-6">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
          <div className="space-y-1.5">
            <CardTitle>迭代监视（P5）</CardTitle>
            <CardDescription>run/attempt 状态机实时推进 · 日志尾 · 停止链 · 预算账本</CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <select className="rounded border bg-transparent px-2 py-1.5 text-sm"
                    value={selected} onChange={(e) => setSelected(e.target.value)}>
              {projects.length === 0 && <option value="">（先立项）</option>}
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.status}</option>)}
            </select>
            <Badge variant={live ? 'default' : 'secondary'} className="gap-1.5">
              <span className={`size-1.5 rounded-full ${live ? 'bg-emerald-400' : 'bg-muted-foreground/40'}`} />
              WS {live ? '在线' : '离线'}
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-64 flex-1">
              <label className="text-xs text-muted-foreground">训练命令（bash）</label>
              <Input className="font-mono" value={cmd} onChange={(e) => setCmd(e.target.value)} />
            </div>
            <div className="w-28">
              <label className="text-xs text-muted-foreground">授权秒数</label>
              <Input value={budgetS} onChange={(e) => setBudgetS(e.target.value)} />
            </div>
            <Button disabled={busy || !selected} onClick={trigger}>▶ 手动触发训练（Round 0）</Button>
            {activeAttempt && (
              <Button variant="destructive" onClick={() => stop(activeAttempt.id)}>■ 停止</Button>
            )}
          </div>
          {detail && (
            <div>
              <div className="mb-1 flex justify-between text-xs text-muted-foreground">
                <span>预算（C+R≤B，账户 {detail.budget.status}）</span>
                <span className="font-mono">
                  C={detail.budget.consumed_s}s R={detail.budget.reserved_s}s / B={detail.budget.limit_s}s
                </span>
              </div>
              <Progress value={used} />
            </div>
          )}
          {note && <pre className="max-h-24 overflow-auto rounded bg-muted p-2 text-xs">{note}</pre>}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardHeader><CardTitle className="text-base">Runs / Attempts</CardTitle></CardHeader>
          <CardContent>
            {runs.length === 0 && <p className="text-sm text-muted-foreground">暂无 run。</p>}
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Run</TableHead><TableHead>状态</TableHead><TableHead>Attempt</TableHead>
                  <TableHead>状态</TableHead><TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.flatMap((r) =>
                  r.attempts.length === 0
                    ? [
                        <TableRow key={r.id}>
                          <TableCell className="font-mono">R{r.round_no} {r.role}</TableCell>
                          <TableCell><Badge variant="secondary">{r.status}</Badge></TableCell>
                          <TableCell className="text-muted-foreground">—</TableCell>
                          <TableCell /><TableCell />
                        </TableRow>
                      ]
                    : r.attempts.map((a, i) => (
                        <TableRow key={a.id}>
                          {i === 0 && (
                            <TableCell className="font-mono" rowSpan={r.attempts.length}>
                              R{r.round_no} {r.role}
                            </TableCell>
                          )}
                          {i === 0 && (
                            <TableCell rowSpan={r.attempts.length}>
                              <Badge variant="secondary">{r.status}</Badge>
                            </TableCell>
                          )}
                          <TableCell className="font-mono text-xs">{a.id.slice(0, 14)}…</TableCell>
                          <TableCell><Badge variant={
                            a.status === 'RUNNING' ? 'default' :
                            a.status === 'FINISHED' ? 'secondary' : 'outline'
                          }>{a.status}</Badge></TableCell>
                          <TableCell className="text-right">
                            {['RUNNING', 'REMOTE_QUEUED', 'ACKED', 'SUBMITTED', 'STOP_REQUESTED', 'STOP_SENT']
                              .includes(a.status) && (
                              <Button size="sm" variant="ghost" onClick={() => stop(a.id)}>停止</Button>
                            )}
                          </TableCell>
                        </TableRow>
                      ))
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle className="text-base">
              日志尾{activeAttempt ? ` · ${activeAttempt.id.slice(0, 12)}…` : ''}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div ref={logRef} className="h-80 overflow-auto rounded bg-zinc-950 p-3 font-mono text-xs leading-5 text-zinc-200">
              {shownLog.length === 0 && <span className="text-zinc-500">（暂无输出）</span>}
              {shownLog.map((l, i) => <div key={i}>{l}</div>)}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

export default function MonitorPage() {
  return (
    <Suspense fallback={<div className="p-6 text-sm text-muted-foreground">加载监视视图…</div>}>
      <MonitorPageInner />
    </Suspense>
  );
}
