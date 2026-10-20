"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Copy, Loader2, Plug, Sun, Moon, Monitor, UserCog, UserPlus } from "lucide-react";
import { useTheme } from "next-themes";
import {
  apiToken, apiUrl, clearApiToken, createAccount, fetchModelSettings, fetchOverview,
  listAccounts, revokeAccount, saveModelSettings, setApiToken, whoami,
} from "@/lib/api";
import type { Account } from "@/lib/api";
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

function IdentityCard() {
  const me = useQuery({ queryKey: ["whoami"], queryFn: whoami, retry: false });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: listAccounts, enabled: me.data?.kind === "owner", retry: false });
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const kindLabel = me.data?.kind === "owner" ? "Owner(管理员)" : me.data?.kind === "member" ? `成员:${me.data.name}` : me.data?.kind === "agent" ? "Agent(只读)" : "…";

  const onCreate = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const acct = await createAccount(name.trim());
      toast.success(`账号「${acct.name}」已创建,token 已复制`, {
        description: acct.token,
      });
      await navigator.clipboard.writeText(acct.token).catch(() => {});
      setName("");
      void qc.invalidateQueries({ queryKey: ["accounts"] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const onRevoke = async (acct: Account) => {
    try {
      await revokeAccount(acct.id);
      toast.success(`「${acct.name}」已吊销`);
      void qc.invalidateQueries({ queryKey: ["accounts"] });
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  return (
    <Card title="当前身份" desc={me.data ? `登录为 ${kindLabel}` : "未登录(下方粘贴 token 登录)"}>
      {accounts.data ? (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <Input className="max-w-60" placeholder="同学名字" value={name}
                   onChange={(e) => setName(e.target.value)} />
            <Button size="sm" className="gap-1.5" disabled={busy || !name.trim()} onClick={() => void onCreate()}>
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : <UserPlus className="size-3.5" />}开账号
            </Button>
          </div>
          {accounts.data.length > 0 ? (
            <div className="space-y-1">
              {accounts.data.map((acct) => (
                <div key={acct.id}
                     className="flex items-center gap-2 rounded-lg border-[0.5px] border-surface-border px-2.5 py-1.5">
                  <UserCog className={`size-3.5 ${acct.revoked ? "text-muted-foreground/40" : "text-brand"}`} />
                  <span className={`text-caption font-medium ${acct.revoked ? "text-muted-foreground/50 line-through" : "text-foreground"}`}>
                    {acct.name}
                  </span>
                  <code className="min-w-0 flex-1 truncate font-mono text-micro text-muted-foreground">{acct.token}</code>
                  {acct.revoked ? (
                    <span className="text-micro text-muted-foreground">已吊销</span>
                  ) : (
                    <Button size="xs" variant="ghost" className="text-micro" onClick={() => void onRevoke(acct)}>吊销</Button>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-caption text-muted-foreground">还没有成员账号。开账号后把 token 发给同学,粘贴即可登录(只读)。</p>
          )}
        </div>
      ) : null}
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
    <Card title="Control 连接" desc="token 只存在本机浏览器:owner 可批准,成员/agent 只读。">
      <div className="flex flex-wrap items-center gap-2">
        <code className="rounded-md border border-surface-border bg-muted/40 px-2 py-1.5 font-mono text-caption">
          {apiUrl}
        </code>
        <Button size="sm" variant="ghost" onClick={() => void copyUrl()} className="gap-1.5">
          <Copy className="size-3.5" />
          复制
        </Button>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Input
          type="password"
          className="max-w-md font-mono text-caption"
          placeholder="粘贴你的 token(owner-… / member-… / agent-…)"
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

function ModelSettings() {
  const [apiKey, setApiKey] = useState("");
  const [revealed, setRevealed] = useState(false);
  const [saving, setSaving] = useState(false);
  const { data: settings } = useQuery({
    queryKey: ["model-settings"], queryFn: fetchModelSettings,
  });

  const save = async () => {
    setSaving(true);
    try {
      const saved = await saveModelSettings({ api_key: apiKey.trim() });
      toast.success(saved.configured ? "模型设置已保存" : "已保存");
      setApiKey("");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card title="模型(Agent 用的 LLM)"
          desc={`当前:${settings?.model ?? "…"} @ ${settings?.baseUrl ?? "…"}。key 只存 Control 本机凭据文件,Agent 会话共用。`}>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          type={revealed ? "text" : "password"}
          className="max-w-md font-mono text-caption"
          placeholder={settings?.configured ? "已配置(输入可覆盖)" : "粘贴 API Key"}
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          spellCheck={false}
        />
        <Button size="sm" variant="outline" onClick={() => setRevealed((v) => !v)}>
          {revealed ? "隐藏" : "显示"}
        </Button>
        <Button size="sm" onClick={() => void save()} disabled={saving || !apiKey.trim()}>
          {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}保存
        </Button>
      </div>
      <p className="mt-1 text-micro text-muted-foreground">
        默认 GLM(open.bigmodel.cn 的 Anthropic 兼容端点);base_url / model 可在凭据文件 model:api-key 条目里覆盖。
      </p>
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
          <IdentityCard />
          <ConnectionSettings />
          <ModelSettings />
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
