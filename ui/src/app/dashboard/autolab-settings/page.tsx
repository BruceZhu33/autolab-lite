'use client';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';

export default function SettingsPage() {
  return (
    <div className="flex flex-1 flex-col gap-4 p-4 md:gap-6 md:p-6">
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>LLM 供应商</CardTitle>
            <CardDescription>按使用点分别配置（D11）· OpenAI 兼容协议</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label>① 评审对话 / ② 迭代分析与改码</Label>
              <Input placeholder="https://open.bigmodel.cn/api/paas/v4" />
            </div>
            <div className="space-y-2">
              <Label>API Key</Label>
              <Input type="password" placeholder="sk-..." />
            </div>
            <div className="space-y-2">
              <Label>模型</Label>
              <Input placeholder="glm-4.7" />
            </div>
            <Button variant="outline" size="sm">测试连通性</Button>
          </CardContent>
        </Card>
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>文献检索模式（D42）</CardTitle>
              <CardDescription>查重结论将标注检索范围与时间</CardDescription>
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
              <CardTitle>项目级默认预算</CardTitle>
              <CardDescription>GPU 预算为强制项（D32）</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label>GPU 时长预算（GPU·h）</Label>
                <Input type="number" defaultValue={120} />
              </div>
              <div className="space-y-2">
                <Label>LLM 费用预算（元，可选）</Label>
                <Input type="number" defaultValue={50} />
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
