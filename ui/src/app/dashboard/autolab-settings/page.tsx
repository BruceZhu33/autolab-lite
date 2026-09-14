'use client';
/**
 * P7 · 设置（M2-2 最小面）：LLM 使用点配置（D11）+ 连通性测试。
 * 其余（文献模式/预算默认值）为占位，随 M3/M4 接真实数据源。
 */
import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '@/lib/autolab/client';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Badge } from '@/components/ui/badge';

interface LlmRow { use_point: string; base_url: string; model: string; enabled: boolean; engine?: string }

export default function SettingsPage() {
  const [rows, setRows] = useState<LlmRow[]>([]);
  const [form, setForm] = useState({ base_url: '', model: '', api_key: '', engine: 'builtin' });
  const [usePoint, setUsePoint] = useState('round_analysis');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState('');

  const refresh = useCallback(async () => {
    try {
      const r = await api<{ settings: LlmRow[] }>('GET', '/api/v1/settings/llm');
      setRows(r.settings);
    } catch (e) {
      setNote(String(e));
    }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  const save = async () => {
    setBusy('save');
    try {
      await api('PUT', '/api/v1/settings/llm', { use_point: usePoint, ...form });
      setNote(`已保存（${usePoint}）；密钥入凭据存储`);
      setForm({ ...form, api_key: '' });
      await refresh();
    } catch (e) {
      setNote(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setBusy('');
    }
  };

  const test = async () => {
    setBusy('test');
    try {
      const r = await api<{ ok: boolean; reply?: string; error?: string }>(
        'POST', `/api/v1/settings/llm/test?use_point=${usePoint}`);
      setNote(r.ok ? `✅ 连通正常：${r.reply}` : `❌ ${r.error}`);
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy('');
    }
  };

  const current = rows.find((r) => r.use_point === usePoint);

  return (
    <div className="flex flex-1 flex-col gap-4 p-4 md:gap-6 md:p-6">
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>LLM 供应商（OpenAI 兼容）</CardTitle>
            <CardDescription>按使用点分别配置（D11）· 密钥存平台凭据存储（D30）</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap gap-2">
              {[
                ['round_analysis', '② 迭代分析与改码'],
                ['self_repair', '③ 崩溃自修复'],
                ['review', '① 评审对话'],
                ['report', '④ 报告生成'],
              ].map(([k, label]) => (
                <button key={k} type="button" onClick={() => setUsePoint(k)}
                        className={`rounded border px-2.5 py-1 text-xs ${usePoint === k ? 'border-primary bg-primary/10' : ''}`}>
                  {label}
                </button>
              ))}
            </div>
            {current && (
              <p className="text-xs text-muted-foreground">
                当前：{current.base_url || '—'} · {current.model || '—'} · 引擎 {current.engine || 'builtin'} ·{' '}
                <Badge variant={current.enabled ? 'default' : 'secondary'}>
                  {current.enabled ? '已启用' : '未启用'}
                </Badge>
              </p>
            )}
            <div className="space-y-2">
              <Label>Base URL</Label>
              <Input className="font-mono" value={form.base_url}
                     onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                     placeholder="https://open.bigmodel.cn/api/paas/v4" />
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label>模型</Label>
                <Input value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })}
                       placeholder="glm-4.7 / gpt-4o / deepseek-chat" />
              </div>
              <div className="space-y-2">
                <Label>提案引擎（②使用点专用）</Label>
                <select className="w-full rounded border bg-transparent px-2 py-1.5 text-sm"
                        value={form.engine}
                        onChange={(e) => setForm({ ...form, engine: e.target.value })}>
                  <option value="builtin">builtin（自研薄客户端）</option>
                  <option value="rdagent">rdagent（RD-Agent 真身·版本锁定）</option>
                </select>
              </div>
              <div className="space-y-2">
                <Label>API Key（留空则保持不变）</Label>
                <Input type="password" value={form.api_key}
                       onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                       placeholder="sk-..." />
              </div>
            </div>
            <div className="flex gap-2">
              <Button size="sm" onClick={save} disabled={busy === 'save' || !form.base_url || !form.model}>
                保存
              </Button>
              <Button size="sm" variant="outline" onClick={test} disabled={busy === 'test'}>
                测试连通性
              </Button>
            </div>
            {note && <pre className="max-h-24 overflow-auto rounded bg-muted p-2 text-xs">{note}</pre>}
          </CardContent>
        </Card>
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>文献检索模式（D42）</CardTitle>
              <CardDescription>查重结论将标注检索范围与时间（M3 接入）</CardDescription>
            </CardHeader>
            <CardContent>
              <RadioGroup defaultValue="local" className="gap-2.5">
                <label className="flex items-center gap-2.5 text-sm">
                  <RadioGroupItem value="local" />仅已导入文献
                </label>
                <label className="flex items-center gap-2.5 text-sm">
                  <RadioGroupItem value="external" />外部检索（arXiv / Semantic Scholar）
                </label>
              </RadioGroup>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>说明</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground space-y-1.5">
              <p>· ②「迭代分析与改码」是 M2 改码循环的使用点——配置后即可在监视页「生成下一轮」。</p>
              <p>· ①③④ 使用点分别服务于评审对话（M3）、崩溃自修复（M2 后段）、报告（M4）。</p>
              <p>· 预算为项目级强制项，在立项时设置（D32）。</p>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
