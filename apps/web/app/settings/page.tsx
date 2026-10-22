"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Copy, Loader2, LogIn, LogOut, Plug, ShieldCheck, Sun, Moon, Monitor, UserCircle2 } from "lucide-react";
import { useTheme } from "next-themes";
import {
  apiToken, apiUrl, clearApiToken, fetchModelSettings, fetchOverview,
  listAccounts, revokeAccount, saveModelSettings, setApiToken, whoami,
} from "@/lib/api";
import type { Account } from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import { Input } from "~/vendor/ui/components/ui/input";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "~/vendor/views/layout/page-header";

const KB_TOKEN_KEY = "autolab.kb_token";

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

/** 统一登录与身份:LABNOTE 账号一号通行(知识库+控制台),角色自动映射权限。 */
function IdentityCard() {
  const me = useQuery({ queryKey: ["whoami"], queryFn: whoami, retry: false });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: listAccounts, enabled: me.data?.kind === "owner", retry: false });
  const qc = useQueryClient();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!username.trim() || !password) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${apiUrl}/api/auth/${mode === "login" ? "login" : "register"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        setError(data.error || data.detail || `${mode === "login" ? "登录" : "注册"}失败(${res.status})`);
        return;
      }
      setApiToken(data.token);
      if (data.kb_token) window.localStorage.setItem(KB_TOKEN_KEY, data.kb_token);
      toast.success(mode === "login"
        ? `已登录:${data.name}(${data.role})`
        : `已注册并登录:${data.name}(${data.role})`);
      setPassword("");
      void qc.invalidateQueries();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const logout = () => {
    clearApiToken();
    window.localStorage.removeItem(KB_TOKEN_KEY);
    void qc.invalidateQueries();
    toast.success("已退出登录");
  };

  const onRevoke = async (acct: Account) => {
    try {
      await revokeAccount(acct.id);
      toast.success(`已停用「${acct.name}」的访问`);
      void qc.invalidateQueries({ queryKey: ["accounts"] });
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  const roleLabel = me.data?.kind === "owner" ? "管理员" : me.data?.kind === "member" ? "只读成员" : me.data?.kind === "agent" ? "程序访问(agent)" : "";

  return (
    <Card title="账户与访问"
          desc="账号与知识库(LABNOTE)通用:一个账号登录两处。管理员可管理实验;成员只读浏览。">
      {me.data ? (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <UserCircle2 className="size-8 text-brand" />
            <div className="min-w-0">
              <p className="text-body font-medium">{me.data.name}</p>
              <p className="text-caption text-muted-foreground">{roleLabel}</p>
            </div>
            <Button size="sm" variant="outline" className="ml-auto gap-1.5" onClick={logout}>
              <LogOut className="size-3.5" />退出登录
            </Button>
          </div>
          {typeof window !== "undefined" && !window.localStorage.getItem(KB_TOKEN_KEY) ? (
            <p className="rounded-lg bg-warning/10 px-3 py-2 text-caption text-warning">
              知识库会话未关联——当前是令牌直登。退出后用「用户名 + 密码」重新登录,
              即可同时启用知识库与知识空间页面。
            </p>
          ) : null}
        </div>
      ) : (
        <div className="space-y-2">
          <div className="flex gap-2">
            {(["login", "register"] as const).map((m) => (
              <button key={m}
                      className={`rounded-full px-2.5 py-0.5 text-caption transition-colors ${
                        mode === m ? "bg-accent font-medium" : "text-muted-foreground hover:text-foreground"}`}
                      onClick={() => { setMode(m); setError(null); }}>
                {m === "login" ? "登录" : "注册新账号"}
              </button>
            ))}
          </div>
          <p className="text-micro text-muted-foreground">
            {mode === "login"
              ? "账号与知识库(LABNOTE)通用;管理员可管理实验,成员只读浏览。"
              : "没有账号?注册一个实验室成员账号(普通成员,可读共享空间);注册即登录。"}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Input className="max-w-44" placeholder="用户名" value={username}
                   onChange={(e) => setUsername(e.target.value)} spellCheck={false} />
            <Input className="max-w-44" type="password" placeholder="密码" value={password}
                   onChange={(e) => setPassword(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && void submit()} />
            <Button size="sm" className="gap-1.5" disabled={busy || !username.trim() || !password} onClick={() => void submit()}>
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : <LogIn className="size-3.5" />}
              {mode === "login" ? "登录" : "注册并登录"}
            </Button>
          </div>
          {error ? <p className="text-caption text-destructive">{error}</p> : null}
          <p className="text-micro text-muted-foreground">
            忘记密码请联系知识库管理员;管理员账号只能在 LABNOTE 站点(凭邀请码)注册。
          </p>
        </div>
      )}

      {accounts.data ? (
        <div className="mt-4 border-t border-surface-border pt-3">
          <div className="mb-1.5 flex items-center gap-1.5">
            <ShieldCheck className="size-3.5 text-muted-foreground" />
            <span className="text-caption font-semibold">成员访问({accounts.data.filter((a) => !a.revoked).length} 位生效)</span>
          </div>
          {accounts.data.length > 0 ? (
            <div className="space-y-1">
              {accounts.data.map((acct) => (
                <div key={acct.id}
                     className="flex items-center gap-2 rounded-lg border-[0.5px] border-surface-border px-2.5 py-1.5">
                  <UserCircle2 className={`size-4 ${acct.revoked ? "text-muted-foreground/40" : "text-muted-foreground"}`} />
                  <span className={`text-caption font-medium ${acct.revoked ? "text-muted-foreground/50 line-through" : "text-foreground"}`}>
                    {acct.name}
                  </span>
                  <span className="text-micro text-muted-foreground">{acct.created_at.slice(0, 10)}</span>
                  {acct.revoked ? (
                    <span className="ml-auto text-micro text-muted-foreground">已停用</span>
                  ) : (
                    <Button size="xs" variant="ghost" className="ml-auto text-micro" onClick={() => void onRevoke(acct)}>停用访问</Button>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-caption text-muted-foreground">还没有成员登录过;成员用知识库账号登录后自动出现在这里。</p>
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
    <Card title="服务连接" desc="程序化访问(API 客户端、脚本)使用访问令牌;日常使用请用上方账号登录。">
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
          placeholder="访问令牌(owner-… / member-… / agent-…)"
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
        <h1 className="text-title-sm font-semibold tracking-tight">设置</h1>
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
