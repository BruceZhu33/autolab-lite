"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { Copy, Loader2, Plug, Sun, Moon, Monitor } from "lucide-react";
import { useTheme } from "next-themes";
import { apiToken, apiUrl, clearApiToken, fetchOverview, setApiToken } from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "@/vendor/views/layout/page-header";

function Card({ title, desc, children }: { title: string; desc?: string; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border-[0.5px] border-surface-border bg-surface p-4 shadow-[var(--surface-shadow)]">
      <h2 className="text-label font-semibold">{title}</h2>
      {desc ? <p className="mt-0.5 text-caption text-muted-foreground">{desc}</p> : null}
      <div className="mt-3">{children}</div>
    </section>
  );
}

function AppearanceSettings() {
  const { theme, setTheme, resolvedTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const options = [
    { key: "light", label: "浅色", icon: Sun },
    { key: "dark", label: "深色", icon: Moon },
    { key: "system", label: "跟随系统", icon: Monitor },
  ];
  return (
    <Card
      title="外观"
      desc={mounted ? `当前生效:${resolvedTheme === "dark" ? "深色" : "浅色"}(侧栏底部也有快捷切换)` : undefined}
    >
      <div className="flex gap-2">
        {options.map((option) => (
          <Button
            key={option.key}
            size="sm"
            variant={mounted && theme === option.key ? "default" : "outline"}
            className="gap-1.5"
            onClick={() => setTheme(option.key)}
          >
            <option.icon className="size-3.5" />
            {option.label}
          </Button>
        ))}
      </div>
    </Card>
  );
}

function ConnectionSettings() {
  const [token, setToken] = useState("");
  const [saved, setSaved] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);

  useEffect(() => {
    setToken(apiToken());
  }, []);

  const save = () => {
    if (token.trim()) {
      setApiToken(token.trim());
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    } else {
      clearApiToken();
    }
  };

  const test = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const overview = await fetchOverview();
      setTestResult(
        `连接成功 · 运行 ${overview.running} · 排队 ${overview.queued} · 失败 ${overview.failed} · 服务器 ${overview.servers.length} · 项目 ${overview.projects.length}`,
      );
    } catch (e) {
      setTestResult(`连接失败:${(e as Error).message}`);
    } finally {
      setTesting(false);
    }
  };

  const copyUrl = async () => {
    try {
      await navigator.clipboard.writeText(apiUrl);
      toast.success("已复制 API 地址");
    } catch {
      toast.error("复制失败,请手动选择复制");
    }
  };

  return (
    <Card title="Control 连接" desc="token 只存在本机浏览器:agent 只读,owner 可批准。">
      <div className="flex flex-wrap items-center gap-2">
        <code className="rounded-md border border-surface-border bg-muted/40 px-2 py-1.5 font-mono text-caption">
          {apiUrl}
        </code>
        <Button size="sm" variant="ghost" onClick={() => void copyUrl()} className="gap-1.5">
          <Copy className="size-3.5" />
          复制
        </Button>
      </div>
      <p className="mt-1 text-micro text-muted-foreground">
        构建时可用环境变量 NEXT_PUBLIC_AUTOLAB_API 覆盖。
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Input
          type="password"
          className="max-w-md font-mono text-caption"
          placeholder="粘贴 API token(owner-… 或 agent-…)"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          spellCheck={false}
        />
        <Button size="sm" variant="outline" onClick={save}>{saved ? "已保存" : "保存"}</Button>
        <Button size="sm" onClick={() => void test()} disabled={testing} className="gap-1.5">
          {testing ? <Loader2 className="size-3.5 animate-spin" /> : <Plug className="size-3.5" />}
          测试连接
        </Button>
      </div>
      {testResult && (
        <p className={`mt-2 text-caption ${testResult.startsWith("连接成功") ? "text-success" : "text-destructive"}`}>
          {testResult}
        </p>
      )}
    </Card>
  );
}

export default function SettingsPage() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <h1 className="text-title-sm font-semibold tracking-tight">Settings</h1>
      </PageHeader>
      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${PAGE_RAIL} max-w-2xl space-y-3 py-4`}>
          <ConnectionSettings />
          <AppearanceSettings />
          <Card title="服务器登记" desc="在 GPU Servers 页注册:local 需与已启动 Runner 一致的端口/token/HMAC;SSH 注册时 TOFU 固定主机密钥并验证认证。">
            <p className="text-caption text-muted-foreground">
              Runner 远程部署/升级尚未进入 Web;凭据只存 Control 本机(0600),不进数据库、不进日志。
            </p>
          </Card>
        </div>
      </div>
    </div>
  );
}
