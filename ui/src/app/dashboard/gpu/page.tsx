'use client';
/**
 * P1 · 服务器与 GPU 管理（M1 最小面）
 * 注册（local/ssh 双模）→ 连通性测试（SSH+GPU 探测）→ agent 探活 → 卡清单。
 */
import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '@/lib/autolab/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger
} from '@/components/ui/dialog';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';

interface ServerRow {
  id: string; name: string; host: string; port: number; username: string;
  transport_mode: 'ssh' | 'local'; status: string; agent_port: number | null;
  agent_schema_ver: string | null; last_seen_at: string | null;
  agent_capabilities_json?: string;
}
interface GpuRow {
  id: string; gpu_index: number; model_name: string; vram_mb: number; status: string;
}
interface ServerDetail extends ServerRow { gpus: GpuRow[] }

const statusColor: Record<string, string> = {
  REACHABLE: 'bg-emerald-500', UNREACHABLE: 'bg-red-500', UNKNOWN: 'bg-muted-foreground/40'
};

function agentVer(s: ServerDetail): string | null {
  try {
    return JSON.parse(s.agent_capabilities_json || '{}').agent_ver ?? null;
  } catch {
    return null;
  }
}

export default function GpuPage() {
  const [servers, setServers] = useState<ServerDetail[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string>('');
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    name: '', host: '', port: '22', username: '', auth_kind: 'key', secret: '',
    transport_mode: 'ssh', agent_port: '7800', agent_token: ''
  });

  const refresh = useCallback(async () => {
    try {
      const list = await api<ServerRow[]>('GET', '/api/v1/servers');
      const details = await Promise.all(
        list.map((s) => api<ServerDetail>('GET', `/api/v1/servers/${s.id}`))
      );
      setServers(details);
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  const act = async (
    id: string,
    op: 'connectivity-test' | 'agent/probe' | 'agent/install' | 'agent/upgrade',
    body?: unknown
  ) => {
    setBusy(`${id}:${op}`);
    try {
      const r = await api<Record<string, unknown>>('POST', `/api/v1/servers/${id}/${op}`, body);
      setNote(`${op} → ${JSON.stringify(r).slice(0, 220)}`);
      await refresh();
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setBusy(null);
    }
  };

  const addGpu = async (serverId: string) => {
    const model = window.prompt('GPU 型号（无 nvidia-smi 环境手工登记）', 'cpu-fake');
    if (!model) return;
    try {
      await api('POST', `/api/v1/servers/${serverId}/gpus`, {
        gpu_index: 0, model_name: model, vram_mb: 24576
      });
      await refresh();
    } catch (e) {
      setNote(String(e));
    }
  };

  const submit = async () => {
    try {
      await api('POST', '/api/v1/servers', {
        ...form,
        port: Number(form.port),
        agent_port: form.agent_port ? Number(form.agent_port) : null
      });
      setOpen(false);
      setForm({ ...form, name: '', host: '', secret: '', agent_token: '' });
      await refresh();
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    }
  };

  return (
    <div className="flex flex-1 flex-col gap-4 p-4 md:gap-6 md:p-6">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div className="space-y-1.5">
            <CardTitle>服务器与 GPU（P1）</CardTitle>
            <CardDescription>
              注册 GPU 服务器（SSH 直连 D14 / local 开发模式）· 连通性测试 · agent 探活
            </CardDescription>
          </div>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger render={<Button size="sm">＋ 注册服务器</Button>} />
            <DialogContent className="sm:max-w-lg">
              <DialogHeader>
                <DialogTitle>注册服务器</DialogTitle>
                <DialogDescription>
                  生产：SSH + 服务器上先启动 gpu-agent（--token）。开发：local 模式直连本机 agent。
                </DialogDescription>
              </DialogHeader>
              <div className="grid gap-3">
                <div className="grid grid-cols-2 gap-3">
                  <div><Label>名称</Label><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="gpu-a" /></div>
                  <div><Label>模式</Label>
                    <select className="mt-1 w-full rounded border bg-transparent px-2 py-1.5 text-sm" value={form.transport_mode}
                            onChange={(e) => setForm({ ...form, transport_mode: e.target.value })}>
                      <option value="ssh">ssh（生产）</option><option value="local">local（开发）</option>
                    </select>
                  </div>
                </div>
                <div className="grid grid-cols-3 gap-3">
                  <div><Label>Host</Label><Input value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} placeholder="192.168.x.x" /></div>
                  <div><Label>端口</Label><Input value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} /></div>
                  <div><Label>用户</Label><Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="juan" /></div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div><Label>认证</Label>
                    <select className="mt-1 w-full rounded border bg-transparent px-2 py-1.5 text-sm" value={form.auth_kind}
                            onChange={(e) => setForm({ ...form, auth_kind: e.target.value })}>
                      <option value="key">密钥（私钥路径）</option><option value="password">密码</option>
                    </select>
                  </div>
                  <div><Label>凭据（密钥路径/密码）</Label><Input value={form.secret} onChange={(e) => setForm({ ...form, secret: e.target.value })} placeholder="~/.ssh/id_ed25519" /></div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div><Label>agent 端口</Label><Input value={form.agent_port} onChange={(e) => setForm({ ...form, agent_port: e.target.value })} /></div>
                  <div><Label>agent token</Label><Input value={form.agent_token} onChange={(e) => setForm({ ...form, agent_token: e.target.value })} /></div>
                </div>
              </div>
              <DialogFooter>
                <Button onClick={submit}>注册</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </CardHeader>
        <CardContent className="space-y-4">
          {servers.length === 0 && (
            <p className="text-sm text-muted-foreground">尚无服务器。注册一台并完成连通性测试后即可立项。</p>
          )}
          {servers.map((s) => (
            <div key={s.id} className="rounded-lg border p-4 space-y-3">
              <div className="flex flex-wrap items-center gap-3">
                <Badge variant="outline" className="gap-1.5">
                  <span className={`size-1.5 rounded-full ${statusColor[s.status] ?? 'bg-muted'}`} />
                  {s.name}
                </Badge>
                <span className="font-mono text-xs text-muted-foreground">
                  {s.transport_mode === 'local' ? '本机' : `${s.username}@${s.host}:${s.port}`}
                  {s.agent_port ? ` · agent:${s.agent_port}` : ' · 无 agent'}
                  {agentVer(s) && ` · agent v${agentVer(s)}`}
                </span>
                <span className="ml-auto flex gap-2">
                  <Button size="sm" variant="outline" disabled={busy === `${s.id}:connectivity-test`}
                          onClick={() => act(s.id, 'connectivity-test')}>连通性测试</Button>
                  <Button size="sm" variant="outline" disabled={!s.agent_port || busy === `${s.id}:agent/probe`}
                          onClick={() => act(s.id, 'agent/probe')}>agent 探活</Button>
                  <Button size="sm" variant="outline" disabled={busy === `${s.id}:agent/install`}
                          onClick={() => act(s.id, 'agent/install')}>部署/修复 Agent</Button>
                  <Button size="sm" variant="ghost" disabled={busy === `${s.id}:agent/upgrade`}
                          onClick={() => {
                            if (window.confirm('升级会重启服务器上的 agent；有在途任务时将被拒绝。继续？'))
                              act(s.id, 'agent/upgrade', { force: false });
                          }}>升级</Button>
                  <Button size="sm" variant="ghost" onClick={() => addGpu(s.id)}>＋ GPU</Button>
                </span>
              </div>
              {s.gpus.length > 0 && (
                <Table>
                  <TableHeader><TableRow><TableHead>#</TableHead><TableHead>型号</TableHead><TableHead>显存</TableHead><TableHead>状态</TableHead></TableRow></TableHeader>
                  <TableBody>
                    {s.gpus.map((g) => (
                      <TableRow key={g.id}>
                        <TableCell className="font-mono">{g.gpu_index}</TableCell>
                        <TableCell>{g.model_name}</TableCell>
                        <TableCell className="font-mono">{(g.vram_mb / 1024).toFixed(0)} GB</TableCell>
                        <TableCell><Badge variant={g.status === 'IDLE' ? 'secondary' : 'default'}>{g.status}</Badge></TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </div>
          ))}
          {note && <pre className="max-h-32 overflow-auto rounded bg-muted p-2 text-xs">{note}</pre>}
        </CardContent>
      </Card>
    </div>
  );
}
