'use client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';

const items = [
  ['终止原因', '目标达成（val_f1 ≥ 0.85）'], ['总轮次', '12'],
  ['累计资源', '85.4 / 120 GPU·h'],
  ['最终结论', '证据支持优于 BASE（+0.070，均值±标准差）'],
  ['复现绑定', 'commit · 环境 · 数据/划分 · seed · plan 版本'],
  ['缺失项', '消融 #5 未完成（OOM，已标注）']
];
const abl = [
  ['完整方法', '0.871 ± 0.004', '3 seed'],
  ['− 注意力稀疏化', '0.812 ± 0.006', '关键移除项'],
  ['− 损失函数替换', '0.849 ± 0.005', ''],
  ['− 门控退火', '0.858 ± 0.003', '矩阵重校验后新增（D36）'],
  ['BASE', '0.801 ± 0.005', '补齐同组 seed（D34）']
];

export default function ReportPage() {
  return (
    <div className="flex flex-1 flex-col gap-4 p-4 md:gap-6 md:p-6">
      <Card>
        <CardHeader>
          <CardTitle>阶段报告 · my-model-v1</CardTitle>
          <CardDescription>任何终态自动生成（Q05），不为补报告启动训练</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-x-8 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
            {items.map(([k, v]) => (
              <div key={k} className="space-y-0.5">
                <div className="text-xs text-muted-foreground">{k}</div>
                <div className="text-sm font-medium">{v}</div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>消融矩阵</CardTitle>
          <Button variant="outline" size="sm">下载图表包 · PNG/SVG/PDF + 数据源</Button>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow><TableHead>配置</TableHead><TableHead>val_f1</TableHead><TableHead>备注</TableHead></TableRow>
            </TableHeader>
            <TableBody>
              {abl.map(r => (
                <TableRow key={r[0]}>
                  <TableCell className="font-medium">{r[0]}</TableCell>
                  <TableCell className="font-mono">{r[1]}</TableCell>
                  <TableCell className="text-muted-foreground text-xs">{r[2]}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="mt-3 text-xs text-muted-foreground">缺失项显式留空并标注原因，不补零、不伪填（A07）。</p>
        </CardContent>
      </Card>
    </div>
  );
}
