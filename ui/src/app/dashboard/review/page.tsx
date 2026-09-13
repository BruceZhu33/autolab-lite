'use client';
import { useState } from 'react';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { cn } from '@/lib/utils';

const modules = [
  { name: '模块 1 · 注意力稀疏化', state: '已定稿', ok: true },
  { name: '模块 2 · 损失函数替换', state: '评审中', ok: false },
  { name: '整体汇总 · 冲突检查', state: '未开始', ok: false }
];
const questions = ['主指标选 val_f1 还是 val_auc？', '验证集划分按患者分组吗？', '目标量级（论文参照 0.82）可达性确认？'];
const msgs = [
  { who: 'me', text: '模块想法：把注意力做稀疏化，参考论文 #2 的 Top-k 方案。' },
  { who: 'ai', text: '已对照文献库（2 篇）：Top-k 稀疏化与论文 #2 §3.2 高度重合，差异点建议放在「可学习门控 + 训练期退火」。初稿方案已生成，反问清单剩 3 个缺口。' },
  { who: 'me', text: '按患者分组划分，主指标 val_f1。' },
  { who: 'ai', text: '已更新方案 v3：划分协议按对象分组并做交叉校验（D43）。反问清单剩 1 项未回答。' }
];

export default function ReviewPage() {
  const [checked, setChecked] = useState<string[]>([]);
  const done = checked.length === questions.length;
  return (
    <div className="flex flex-1 flex-col gap-4 p-4 md:gap-6 md:p-6">
      <div className="grid gap-4 xl:grid-cols-[260px_1fr_300px]">
        <Card className="h-fit">
          <CardHeader><CardTitle className="text-sm">模块清单</CardTitle></CardHeader>
          <CardContent className="space-y-2">
            {modules.map(m => (
              <div key={m.name} className="flex items-center justify-between rounded-lg border p-2.5 text-xs">
                <span>{m.name}</span>
                <span className={cn('flex items-center gap-1', m.ok ? 'text-emerald-500' : 'text-muted-foreground')}>
                  <span className="size-1.5 rounded-full bg-current" />{m.state}
                </span>
              </div>
            ))}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-sm">评审对话 · 模块 2</CardTitle>
            <Badge variant="outline">检索范围：仅已导入文献</Badge>
          </CardHeader>
          <CardContent className="space-y-4">
            {msgs.map((m, i) => (
              <div key={i} className={cn('flex gap-2.5', m.who === 'me' && 'flex-row-reverse')}>
                <Avatar className="size-8 border">
                  <AvatarFallback className={cn('text-xs', m.who === 'ai' && 'bg-primary text-primary-foreground')}>
                    {m.who === 'me' ? '我' : 'AI'}
                  </AvatarFallback>
                </Avatar>
                <div className={cn(
                  'max-w-[78%] rounded-xl border px-3.5 py-2.5 text-sm leading-relaxed',
                  m.who === 'me' ? 'bg-primary/10' : 'bg-card'
                )}>{m.text}</div>
              </div>
            ))}
          </CardContent>
        </Card>
        <Card className="h-fit">
          <CardHeader>
            <CardTitle className="text-sm">反问清单</CardTitle>
            <CardDescription>全部回答后方可进入整体确认</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {questions.map(q => (
              <label key={q} className="flex items-start gap-2.5 text-sm">
                <Checkbox checked={checked.includes(q)}
                  onCheckedChange={v => setChecked(c => v ? [...c, q] : c.filter(x => x !== q))} />
                <span className="leading-snug">{q}</span>
              </label>
            ))}
            <Button className="w-full" disabled={!done} size="sm">确认定稿（D21 闸门）</Button>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
