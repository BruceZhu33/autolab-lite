'use client';
/**
 * P4 · 方案评审（M3）：论文登记（D17）→ 模块两段式评审（D22：初稿+反问+文献对照
 * → 回答收敛 → 人工裁决）→ 整体汇总 → 确认定稿（D21，结论入 plan 新版本）。
 */
import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '@/lib/autolab/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';

interface ProjectRow { id: string; name: string; status: string }
interface PaperRow { id: string; title: string; source_kind: string; abstract: string }
interface Turn { stage: string; role: string; content_json: string; created_at: string }
interface ModuleRow {
  id: string; identity: string; idea_text: string; status: string; turns: Turn[];
}

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'outline' | 'destructive'> = {
  ACCEPTED: 'default', REVIEWING: 'secondary', PROPOSED: 'outline', REJECTED: 'destructive',
};

export default function ReviewPage() {
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [selected, setSelected] = useState('');
  const [papers, setPapers] = useState<PaperRow[]>([]);
  const [modules, setModules] = useState<ModuleRow[]>([]);
  const [synthesis, setSynthesis] = useState<Record<string, unknown> | null>(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState('');
  const [paperUrl, setPaperUrl] = useState('');
  const [modForm, setModForm] = useState({ identity: '', idea_text: '' });
  const [answers, setAnswers] = useState<Record<string, string>>({});

  useEffect(() => {
    api<ProjectRow[]>('GET', '/api/v1/projects')
      .then((l) => { setProjects(l); setSelected((c) => c || l[0]?.id || ''); })
      .catch((e) => setNote(String(e)));
  }, []);

  const refresh = useCallback(async () => {
    if (!selected) return;
    try {
      setPapers(await api<PaperRow[]>('GET', `/api/v1/projects/${selected}/papers`));
      setModules(await api<ModuleRow[]>('GET', `/api/v1/projects/${selected}/modules`));
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    }
  }, [selected]);
  useEffect(() => { refresh(); }, [refresh]);

  const addPaper = async (mode: 'url' | 'upload', file?: File) => {
    setBusy('paper');
    try {
      if (mode === 'url' && paperUrl) {
        const r = await api<{ title: string }>('POST',
          `/api/v1/projects/${selected}/papers`, { url: paperUrl });
        setNote(`已登记：${r.title}`);
        setPaperUrl('');
      } else if (mode === 'upload' && file) {
        const fd = new FormData();
        fd.append('file', file);
        const cfg = await (await import('@/lib/autolab/client')).getSidecar();
        const res = await fetch(`${cfg.baseUrl}/api/v1/projects/${selected}/papers/upload`, {
          method: 'POST', headers: { 'X-Capability-Token': cfg.token }, body: fd });
        if (!res.ok) throw new Error(await res.text());
        setNote('已上传登记');
      }
      await refresh();
    } catch (e) {
      setNote(String(e));
    } finally { setBusy(''); }
  };

  const propose = async () => {
    setBusy('propose');
    try {
      const r = await api<{ draft: string; questions: string[]; literature: string; risk: string }>(
        'POST', `/api/v1/projects/${selected}/modules`, modForm);
      setNote(`初稿完成——反问 ${r.questions.length} 条\n${r.draft}\n\n文献对照：${r.literature}`);
      setModForm({ identity: '', idea_text: '' });
      await refresh();
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally { setBusy(''); }
  };

  const answer = async (moduleId: string) => {
    setBusy(`ans-${moduleId}`);
    try {
      const r = await api<{ status: string; conclusions: string }>(
        'POST', `/api/v1/modules/${moduleId}/answers`, { answers: answers[moduleId] || '' });
      setNote(`模块状态：${r.status}\n${r.conclusions}`);
      await refresh();
    } catch (e) {
      setNote(String(e));
    } finally { setBusy(''); }
  };

  const decide = async (moduleId: string, verdict: 'accept' | 'reject') => {
    try {
      await api('POST', `/api/v1/modules/${moduleId}/decision`, { verdict });
      await refresh();
    } catch (e) { setNote(String(e)); }
  };

  const synthesize = async () => {
    setBusy('synth');
    try {
      const r = await api<Record<string, unknown>>(
        'POST', `/api/v1/projects/${selected}/review/synthesize`);
      setSynthesis(r);
      setNote(`整体汇总完成（ready=${r.ready}）`);
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally { setBusy(''); }
  };

  const finalize = async () => {
    if (!window.confirm('确认定稿（D21）？评审结论将写入 plan 新版本，环境就绪后冻结进入全自动。'))
      return;
    setBusy('final');
    try {
      const r = await api<{ protocol_id: string; ver: number; modules_accepted: number }>(
        'POST', `/api/v1/projects/${selected}/review/finalize`);
      setNote(`✔ 已定稿：plan v${r.ver}（${r.modules_accepted} 模块）。到项目页走"确认定稿（冻结 plan）"进入全自动。`);
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally { setBusy(''); }
  };

  const acceptedCount = modules.filter((m) => m.status === 'ACCEPTED').length;

  return (
    <div className="flex flex-1 flex-col gap-4 p-4 md:gap-6 md:p-6">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
          <div className="space-y-1.5">
            <CardTitle>方案评审（P4 · 两段式 D22）</CardTitle>
            <CardDescription>论文登记 → 逐模块评审（初稿+反问+文献对照）→ 整体汇总 → 确认定稿</CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <select className="rounded border bg-transparent px-2 py-1.5 text-sm"
                    value={selected} onChange={(e) => setSelected(e.target.value)}>
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.status}</option>)}
            </select>
            <Button size="sm" variant="outline" disabled={busy === 'synth' || !acceptedCount}
                    onClick={synthesize}>整体汇总</Button>
            <Button size="sm" disabled={busy === 'final' || !synthesis} onClick={finalize}>
              确认定稿（D21）
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-64 flex-1">
              <Label className="text-xs">论文链接（arXiv 优先自动抓取 D17）</Label>
              <Input className="font-mono" value={paperUrl}
                     onChange={(e) => setPaperUrl(e.target.value)}
                     placeholder="https://arxiv.org/abs/2401.00001" />
            </div>
            <Button size="sm" variant="outline" disabled={busy === 'paper' || !paperUrl}
                    onClick={() => addPaper('url')}>登记</Button>
            <label className="text-xs">
              <input type="file" accept="application/pdf" className="hidden"
                     onChange={(e) => e.target.files?.[0] && addPaper('upload', e.target.files[0])} />
              <span className="cursor-pointer rounded border px-2.5 py-1.5">上传 PDF 兜底</span>
            </label>
          </div>
          {papers.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {papers.map((p) => (
                <Badge key={p.id} variant="outline" className="max-w-80 truncate">
                  📄 {p.title || p.id}
                </Badge>
              ))}
            </div>
          )}
          {note && <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-muted p-2 text-xs">{note}</pre>}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader><CardTitle className="text-base">提出模块</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            <div className="space-y-1">
              <Label className="text-xs">模块标识（identity）</Label>
              <Input value={modForm.identity}
                     onChange={(e) => setModForm({ ...modForm, identity: e.target.value })}
                     placeholder="gated-encoder" />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">模块想法（自然语言，可粗可细）</Label>
              <Textarea rows={4} value={modForm.idea_text}
                        onChange={(e) => setModForm({ ...modForm, idea_text: e.target.value })}
                        placeholder="在 encoder 后加可学习门控…" />
            </div>
            <Button size="sm" disabled={busy === 'propose' || !modForm.identity} onClick={propose}>
              生成初稿 + 反问清单 + 文献对照
            </Button>
          </CardContent>
        </Card>

        <div className="space-y-4 lg:col-span-2">
          {modules.length === 0 && (
            <p className="text-sm text-muted-foreground">尚无模块——左边提出第一个想法。</p>
          )}
          {modules.map((m) => {
            const draft = m.turns.find((t) => t.stage === 'module_draft' && t.role === 'assistant');
            const d = draft ? JSON.parse(draft.content_json) : null;
            return (
              <Card key={m.id}>
                <CardHeader className="flex flex-row items-center justify-between py-3">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{m.identity}</span>
                    <Badge variant={STATUS_VARIANT[m.status] ?? 'outline'}>{m.status}</Badge>
                  </div>
                  {(m.status === 'REVIEWING' || m.status === 'PROPOSED') && (
                    <div className="flex gap-1.5">
                      <Button size="sm" variant="outline" onClick={() => decide(m.id, 'reject')}>驳回</Button>
                      <Button size="sm" onClick={() => decide(m.id, 'accept')}>接受</Button>
                    </div>
                  )}
                </CardHeader>
                <CardContent className="space-y-2 text-sm">
                  <p className="text-muted-foreground">{m.idea_text}</p>
                  {d && (
                    <div className="rounded border p-2 text-xs leading-5">
                      <p><strong>初稿：</strong>{d.draft}</p>
                      <p className="mt-1"><strong>反问清单：</strong></p>
                      <ul className="list-disc pl-4">
                        {(d.questions || []).map((q: string, i: number) => <li key={i}>{q}</li>)}
                      </ul>
                      <p className="mt-1"><strong>文献对照：</strong>{d.literature}</p>
                    </div>
                  )}
                  {m.status === 'REVIEWING' && (
                    <div className="space-y-1.5">
                      <Textarea rows={2} placeholder="逐条回答反问（问题：答案；问题：答案）"
                                value={answers[m.id] || ''}
                                onChange={(e) => setAnswers({ ...answers, [m.id]: e.target.value })} />
                      <Button size="sm" disabled={busy === `ans-${m.id}`} onClick={() => answer(m.id)}>
                        提交回答（LLM 收敛判定）
                      </Button>
                    </div>
                  )}
                </CardContent>
              </Card>
            );
          })}
          {synthesis && (
            <Card>
              <CardHeader><CardTitle className="text-base">整体汇总</CardTitle></CardHeader>
              <CardContent className="text-sm">
                <pre className="whitespace-pre-wrap text-xs">
                  {JSON.stringify(synthesis, null, 2)}
                </pre>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
