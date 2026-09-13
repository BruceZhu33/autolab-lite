'use client';
/**
 * P3 · 立项与项目列表（M1 最小面）
 * 立项（BASE 本地目录 + 数据登记 + 卡池 + 预算）→ 环境搭建 → 冻结（T-FREEZE）。
 */
import { useCallback, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { api, ApiError } from '@/lib/autolab/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Progress } from '@/components/ui/progress';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger
} from '@/components/ui/dialog';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';

interface ProjectRow {
  id: string; name: string; status: string; state_revision: number; outcome: string | null;
}
interface ProjectDetail extends ProjectRow {
  budget: { limit_s: number; consumed_s: number; reserved_s: number };
  replicas: { server_id: string; status: string }[];
  split_validations: { name: string; status: string }[];
  env_versions: { env_name: string; status: string; server_id: string }[];
  protocol: { id: string; status: string };
  pool: { gpu_id: string; model_name: string; pool_status: string }[];
}
interface GpuRow { id: string; server_id: string; model_name: string; gpu_index: number }

const PHASE_LABEL: Record<string, string> = {
  INIT: '已立项', ENV_SETUP: '环境搭建中', REVIEW_GATE: '待定稿', BASELINE_ACCEPTANCE: '基线验收',
  SEARCH: '搜索迭代', PAUSED: '已暂停', FINISHING: '收尾', REPORTED: '已报告'
};

export default function ProjectsPage() {
  const router = useRouter();
  const [projects, setProjects] = useState<ProjectDetail[]>([]);
  const [gpus, setGpus] = useState<GpuRow[]>([]);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    name: '', direction_text: '', base_source_ref: '', budget_limit_s: '3600',
    data_logical_name: 'dataset', data_local_path: '', train_command: 'python train.py'
  });
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const refresh = useCallback(async () => {
    try {
      const list = await api<ProjectRow[]>('GET', '/api/v1/projects');
      const details = await Promise.all(
        list.map((p) => api<ProjectDetail>('GET', `/api/v1/projects/${p.id}`))
      );
      setProjects(details);
      const servers = await api<{ id: string }[]>('GET', '/api/v1/servers');
      const perServer = await Promise.all(
        servers.map((s) => api<GpuRow[]>('GET', `/api/v1/servers/${s.id}/gpus`))
      );
      setGpus(perServer.flat());
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [refresh]);

  const toggle = (id: string) => {
    const next = new Set(picked);
    if (next.has(id)) next.delete(id); else next.add(id);
    setPicked(next);
  };

  const submit = async () => {
    try {
      const r = await api<{ project_id: string }>('POST', '/api/v1/projects', {
        name: form.name, direction_text: form.direction_text,
        base_source_kind: 'local', base_source_ref: form.base_source_ref,
        budget_limit_s: Number(form.budget_limit_s),
        data: { logical_name: form.data_logical_name, local_path: form.data_local_path },
        gpu_ids: [...picked], train_command: form.train_command
      });
      setOpen(false);
      setNote(`项目已创建：${r.project_id}（INIT）`);
      await refresh();
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    }
  };

  const envSetup = async (p: ProjectDetail) => {
    setBusy(`${p.id}:setup`);
    try {
      await api('POST', `/api/v1/projects/${p.id}/env-setup`);
      setNote('环境搭建已启动（后台）；观察状态推进到 待定稿。');
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setBusy(null);
    }
  };

  const freeze = async (p: ProjectDetail) => {
    setBusy(`${p.id}:freeze`);
    try {
      await api('POST', `/api/v1/plan-protocols/${p.protocol.id}/freeze`, {
        expected_state_revision: p.state_revision, next_round: 0
      });
      setNote('协议已冻结（T-FREEZE）→ 基线验收。可去「迭代监视」手动触发 Round 0。');
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-1 flex-col gap-4 p-4 md:gap-6 md:p-6">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div className="space-y-1.5">
            <CardTitle>项目（P3）</CardTitle>
            <CardDescription>立项 → 环境搭建 → 定稿冻结 → 手动触发训练（M1 纵切）</CardDescription>
          </div>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger render={<Button size="sm">＋ 新建项目</Button>} />
            <DialogContent className="sm:max-w-xl">
              <DialogHeader>
                <DialogTitle>立项</DialogTitle>
                <DialogDescription>
                  BASE 本地目录将被复制快照进项目库（D19，原目录不动）；数据登记主副本路径（D16）。
                </DialogDescription>
              </DialogHeader>
              <div className="grid gap-3">
                <div className="grid grid-cols-2 gap-3">
                  <div><Label>项目名</Label><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="my-model-v1" /></div>
                  <div><Label>预算上限（秒）</Label><Input value={form.budget_limit_s} onChange={(e) => setForm({ ...form, budget_limit_s: e.target.value })} /></div>
                </div>
                <div><Label>大方向</Label><Input value={form.direction_text} onChange={(e) => setForm({ ...form, direction_text: e.target.value })} placeholder="提升 XX 任务的 YY" /></div>
                <div><Label>BASE 代码目录（本机路径）</Label><Input className="font-mono" value={form.base_source_ref} onChange={(e) => setForm({ ...form, base_source_ref: e.target.value })} placeholder="/Users/me/code/base-repo" /></div>
                <div className="grid grid-cols-2 gap-3">
                  <div><Label>数据名称</Label><Input value={form.data_logical_name} onChange={(e) => setForm({ ...form, data_logical_name: e.target.value })} /></div>
                  <div><Label>数据主副本目录（本机）</Label><Input className="font-mono" value={form.data_local_path} onChange={(e) => setForm({ ...form, data_local_path: e.target.value })} placeholder="/Users/me/data/toy" /></div>
                </div>
                <div><Label>训练命令（bash，可用 $AUTOLAB_CODE_DIR / $AUTOLAB_DATA_DIR / $AUTOLAB_OUT_DIR）</Label>
                  <Input className="font-mono" value={form.train_command} onChange={(e) => setForm({ ...form, train_command: e.target.value })} /></div>
                <div>
                  <Label>GPU 卡池（可跨服务器，D15）</Label>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {gpus.length === 0 && <span className="text-xs text-muted-foreground">先到「服务器 / GPU」登记</span>}
                    {gpus.map((g) => (
                      <button key={g.id} type="button" onClick={() => toggle(g.id)}
                              className={`rounded border px-2 py-1 text-xs ${picked.has(g.id) ? 'border-primary bg-primary/10' : ''}`}>
                        {g.model_name} #{g.gpu_index}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
              <DialogFooter>
                <Button onClick={submit} disabled={picked.size === 0 || !form.name || !form.base_source_ref}>创建</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </CardHeader>
        <CardContent className="space-y-4">
          {projects.length === 0 && <p className="text-sm text-muted-foreground">尚无项目。</p>}
          {projects.map((p) => {
            const used = p.budget.limit_s
              ? Math.round(((p.budget.consumed_s + p.budget.reserved_s) / p.budget.limit_s) * 100)
              : 0;
            return (
              <div key={p.id} className="rounded-lg border p-4 space-y-3">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-medium">{p.name}</span>
                  <Badge variant="outline">{PHASE_LABEL[p.status] ?? p.status}</Badge>
                  <span className="font-mono text-xs text-muted-foreground">{p.id.slice(0, 14)}… · rev {p.state_revision}</span>
                  <span className="ml-auto flex gap-2">
                    {p.status === 'INIT' && (
                      <Button size="sm" variant="outline" disabled={busy === `${p.id}:setup`} onClick={() => envSetup(p)}>搭建环境</Button>
                    )}
                    {p.status === 'ENV_SETUP' && (
                      <Button size="sm" variant="outline" disabled={busy === `${p.id}:setup`} onClick={() => envSetup(p)}>重试失败步骤</Button>
                    )}
                    {p.status === 'REVIEW_GATE' && (
                      <Button size="sm" disabled={busy === `${p.id}:freeze`} onClick={() => freeze(p)}>确认定稿（冻结 plan）</Button>
                    )}
                    <Button size="sm" variant="ghost" onClick={() => router.push(`/dashboard?project=${p.id}`)}>监视 →</Button>
                  </span>
                </div>
                <div className="grid gap-3 text-xs text-muted-foreground sm:grid-cols-3">
                  <div>副本：{p.replicas.map((r) => r.status).join(', ') || '—'}</div>
                  <div>环境：{p.env_versions.map((e) => `${e.env_name}:${e.status}`).join(', ') || '—'}</div>
                  <div>划分校验：{p.split_validations.map((s) => `${s.name}=${s.status}`).join(', ') || '—'}</div>
                </div>
                <div>
                  <div className="mb-1 flex justify-between text-xs text-muted-foreground">
                    <span>预算 C+R ≤ B</span>
                    <span className="font-mono">C={p.budget.consumed_s}s R={p.budget.reserved_s}s / {p.budget.limit_s}s</span>
                  </div>
                  <Progress value={used} />
                </div>
              </div>
            );
          })}
          {note && <pre className="max-h-32 overflow-auto rounded bg-muted p-2 text-xs">{note}</pre>}
        </CardContent>
      </Card>
    </div>
  );
}
