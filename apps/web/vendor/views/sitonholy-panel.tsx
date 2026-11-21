"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Cloud, LogIn, LogOut, Plus, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "~/vendor/ui/components/ui/badge";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "~/vendor/ui/components/ui/dialog";
import { Input } from "~/vendor/ui/components/ui/input";
import { Label } from "~/vendor/ui/components/ui/label";
import {
  sitonholyCaptcha,
  sitonholyInstances,
  sitonholyLogin,
  sitonholyLogout,
  sitonholyPools,
  sitonholyStatus,
  type SitonholyCaptcha,
} from "~/vendor/views/gpu-sitonholy";

export function SitonholyLoginDialog({
  open,
  onOpenChange,
  onSuccess,
}: {
  open: boolean;
  onOpenChange: (next: boolean) => void;
  onSuccess?: () => void | Promise<void>;
}) {
  const qc = useQueryClient();
  const [loginMode, setLoginMode] = useState<"password" | "cookie">("password");
  const [loginForm, setLoginForm] = useState({
    base_url: "http://10.12.54.221",
    username: "",
    password: "",
    verify_code: "",
    session_cookie: "",
    token: "",
  });
  const [captcha, setCaptcha] = useState<SitonholyCaptcha | null>(null);
  const [captchaLoading, setCaptchaLoading] = useState(false);
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);

  const loadCaptcha = async (baseUrl = loginForm.base_url) => {
    setCaptchaLoading(true);
    try {
      const cap = await sitonholyCaptcha(baseUrl);
      setCaptcha(cap);
      if (!cap.image_data_url && cap.verify_code) {
        setLoginForm((f) => ({ ...f, verify_code: cap.verify_code }));
      }
    } catch {
      setCaptcha(null);
    } finally {
      setCaptchaLoading(false);
    }
  };

  const handleOpenChange = (next: boolean) => {
    onOpenChange(next);
    if (next) {
      setLoginError(null);
      void loadCaptcha(loginForm.base_url);
    }
  };

  const submitLogin = async () => {
    setLoginBusy(true);
    setLoginError(null);
    try {
      if (loginMode === "cookie") {
        await sitonholyLogin({
          base_url: loginForm.base_url,
          username: loginForm.username || undefined,
          session_cookie: loginForm.session_cookie,
          token: loginForm.token,
        });
      } else {
        await sitonholyLogin({
          base_url: loginForm.base_url,
          username: loginForm.username,
          password: loginForm.password,
          verify_code: loginForm.verify_code,
          captcha_cookie: captcha?.captcha_cookie ?? "",
          time_str: captcha?.server_time ?? "",
        });
      }
      onOpenChange(false);
      setLoginForm((f) => ({ ...f, password: "", session_cookie: "", token: "" }));
      toast.success("已登录思腾合力");
      await qc.invalidateQueries({ queryKey: ["sitonholy-status"] });
      await qc.invalidateQueries({ queryKey: ["sitonholy-pools"] });
      await qc.invalidateQueries({ queryKey: ["sitonholy-instances"] });
      await qc.invalidateQueries({ queryKey: ["sitonholy-images"] });
      await qc.invalidateQueries({ queryKey: ["sitonholy-volumes"] });
      await onSuccess?.();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setLoginError(msg);
      if (loginMode === "password") {
        void loadCaptcha(loginForm.base_url);
      }
    } finally {
      setLoginBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>登录思腾合力 SCM</DialogTitle>
          <DialogDescription>
            凭证仅保存在 Control 宿主机的 0600 凭据库中；密码登录会自动对齐平台北京时间，不受本地系统时钟影响。
          </DialogDescription>
        </DialogHeader>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant={loginMode === "password" ? "default" : "outline"}
            onClick={() => setLoginMode("password")}
          >
            账号密码（推荐）
          </Button>
          <Button
            size="sm"
            variant={loginMode === "cookie" ? "default" : "outline"}
            onClick={() => setLoginMode("cookie")}
          >
            导入 Cookie / Token
          </Button>
        </div>
        <div className="grid gap-3">
          <div className="grid gap-1.5">
            <Label>平台地址</Label>
            <Input
              value={loginForm.base_url}
              onChange={(e) => setLoginForm({ ...loginForm, base_url: e.target.value })}
            />
          </div>
          {loginMode === "password" ? (
            <>
              <div className="grid gap-1.5">
                <Label>用户名</Label>
                <Input
                  value={loginForm.username}
                  onChange={(e) => setLoginForm({ ...loginForm, username: e.target.value })}
                  placeholder="例如 zhengherong"
                />
              </div>
              <div className="grid gap-1.5">
                <Label>密码</Label>
                <Input
                  type="password"
                  autoComplete="current-password"
                  value={loginForm.password}
                  onChange={(e) => setLoginForm({ ...loginForm, password: e.target.value })}
                />
              </div>
              {captcha?.image_data_url ? (
                <div className="grid gap-1.5">
                  <div className="flex items-center justify-between">
                    <Label>图形验证码（时间 {captcha.server_time}）</Label>
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      disabled={captchaLoading}
                      onClick={() => void loadCaptcha()}
                    >
                      换一张
                    </Button>
                  </div>
                  <div className="flex items-center gap-2">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={captcha.image_data_url} alt="captcha" className="h-9 rounded border" />
                    <Input
                      value={loginForm.verify_code}
                      onChange={(e) => setLoginForm({ ...loginForm, verify_code: e.target.value })}
                      placeholder="输入验证码"
                    />
                  </div>
                </div>
              ) : captcha ? (
                <div className="flex items-center justify-between gap-2 text-caption text-emerald-600">
                  <span>已自动同步北京时间（{captcha.server_time}）与验证码</span>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    disabled={captchaLoading}
                    onClick={() => void loadCaptcha()}
                  >
                    刷新
                  </Button>
                </div>
              ) : (
                <div className="flex items-center justify-between text-caption text-muted-foreground">
                  <span>{captchaLoading ? "正在同步平台时间…" : "尚未获取到校时响应，仍可尝试登录"}</span>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    disabled={captchaLoading}
                    onClick={() => void loadCaptcha()}
                  >
                    重试获取
                  </Button>
                </div>
              )}
            </>
          ) : (
            <>
              <div className="grid gap-1.5">
                <Label>SESSION Cookie</Label>
                <Input
                  value={loginForm.session_cookie}
                  onChange={(e) => setLoginForm({ ...loginForm, session_cookie: e.target.value })}
                  placeholder="从浏览器 Cookie 复制 SESSION"
                />
              </div>
              <div className="grid gap-1.5">
                <Label>Admin-Token (localStorage)</Label>
                <Input
                  value={loginForm.token}
                  onChange={(e) => setLoginForm({ ...loginForm, token: e.target.value })}
                  placeholder="可选，从 localStorage['Admin-Token'] 复制"
                />
              </div>
            </>
          )}
          {loginError && <p className="text-caption text-destructive">{loginError}</p>}
        </div>
        <DialogFooter>
          <Button onClick={() => void submitLogin()} disabled={loginBusy}>
            {loginBusy ? "登录中…" : "连接并保存"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * `/gpus` 页面的管理员专属思腾合力入口摘要卡：
 * 仅在管理员视角渲染，展示可提交余卡/运行实例/接入统计，并直达独立管理页 `/gpus/sitonholy`。
 */
export function SitonholyPanel({
  isAdmin = true,
  onServersChanged,
}: {
  isAdmin?: boolean;
  onServersChanged: () => void;
}) {
  const qc = useQueryClient();
  const [loginOpen, setLoginOpen] = useState(false);

  const { data: status, isLoading: loadingStatus } = useQuery({
    queryKey: ["sitonholy-status"],
    queryFn: () => sitonholyStatus(false),
    enabled: isAdmin,
    staleTime: 30_000,
    retry: false,
  });

  const loggedIn = isAdmin && Boolean(status?.logged_in);

  const { data: poolsData, isFetching: fetchingPools, refetch: refetchPools } = useQuery({
    queryKey: ["sitonholy-pools-summary"],
    queryFn: () => sitonholyPools(false),
    enabled: isAdmin && loggedIn,
    staleTime: 30_000,
    retry: false,
  });

  const { data: instancesData, isFetching: fetchingInstances, refetch: refetchInstances } = useQuery({
    queryKey: ["sitonholy-instances-summary"],
    queryFn: () => sitonholyInstances({ page_size: 50 }),
    enabled: isAdmin && loggedIn,
    staleTime: 30_000,
    retry: false,
  });

  if (!isAdmin) {
    return null;
  }

  const allPools = poolsData?.pools ?? [];
  const visiblePools = allPools.filter((p) => p.user_visible !== false);
  const availGpus = visiblePools.reduce((sum, p) => sum + (p.available || 0), 0);
  const totalGpus = visiblePools.reduce((sum, p) => sum + (p.total || 0), 0);
  const visibleSummary = visiblePools
    .map((p) => `${p.pool_name}: ${p.available}/${p.total}`)
    .join(" · ");

  const instances = instancesData?.instances ?? [];
  const runningInstances = instances.filter((i) => String(i.status ?? "") === "1");
  const attachedInstances = instances.filter((i) => Boolean(i.registered_server));

  const handleLogout = async () => {
    try {
      await sitonholyLogout();
      toast.info("已退出思腾合力会话");
      await qc.invalidateQueries({ queryKey: ["sitonholy-status"] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <Cloud className="size-4 text-primary" />
          <span className="font-medium">思腾合力校园云算力（10.12.54.221）</span>
          {loadingStatus ? (
            <Badge variant="outline">检查中…</Badge>
          ) : loggedIn ? (
            <Badge variant="secondary">已连接：{status?.nick_name || status?.username}</Badge>
          ) : (
            <Badge variant="outline">未连接</Badge>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {!loggedIn ? (
            <>
              <Button size="sm" variant="outline" onClick={() => setLoginOpen(true)}>
                <LogIn className="mr-1 size-3.5" />
                登录思腾合力
              </Button>
              <Link href="/gpus/sitonholy">
                <Button size="sm">
                  打开思腾合力控制台
                  <ArrowRight className="ml-1 size-3.5" />
                </Button>
              </Link>
            </>
          ) : (
            <>
              <Button
                size="sm"
                variant="outline"
                disabled={fetchingPools || fetchingInstances}
                onClick={() => {
                  void refetchPools();
                  void refetchInstances();
                  onServersChanged();
                }}
              >
                <RefreshCw
                  className={`mr-1 size-3.5 ${fetchingPools || fetchingInstances ? "animate-spin" : ""}`}
                />
                刷新
              </Button>
              <Link href="/gpus/sitonholy">
                <Button size="sm">
                  <Plus className="mr-1 size-3.5" />
                  申请 / 管理云算力
                  <ArrowRight className="ml-1 size-3.5" />
                </Button>
              </Link>
              <Button size="sm" variant="ghost" onClick={() => void handleLogout()}>
                <LogOut className="mr-1 size-3.5" />
                退出
              </Button>
            </>
          )}
        </div>
      </div>

      {!loggedIn ? (
        <p className="mt-2 text-caption text-muted-foreground">
          登录后可在专用「思腾合力云算力管理台」一键申请 GPU 实例、打开内置 Web 终端并自动接入 AutoLab 训练调度。
        </p>
      ) : (
        <div className="mt-3 grid gap-2 sm:grid-cols-3">
          <div className="rounded-md border bg-muted/30 px-3 py-2">
            <div className="text-caption text-muted-foreground">我的可用资源池</div>
            <div className="mt-0.5 font-mono text-sm font-semibold">
              {visiblePools.length > 0 ? `${availGpus} / ${totalGpus} 卡空闲` : "暂无显式可用池"}
              {visibleSummary && (
                <span className="ml-1.5 text-caption font-normal text-muted-foreground">
                  ({visibleSummary})
                </span>
              )}
            </div>
          </div>
          <div className="rounded-md border bg-muted/30 px-3 py-2">
            <div className="text-caption text-muted-foreground">平台容器实例</div>
            <div className="mt-0.5 font-mono text-sm font-semibold">
              {runningInstances.length} 个运行中
              <span className="ml-1.5 text-caption font-normal text-muted-foreground">
                / 共 {instances.length} 条记录
              </span>
            </div>
          </div>
          <div className="rounded-md border bg-muted/30 px-3 py-2">
            <div className="text-caption text-muted-foreground">已接入 AutoLab</div>
            <div className="mt-0.5 font-mono text-sm font-semibold">
              {attachedInstances.length} 台服务器
              {attachedInstances.length > 0 && (
                <span className="ml-1.5 text-caption font-normal text-muted-foreground">
                  ({attachedInstances.map((i) => i.registered_server?.name).filter(Boolean).join(", ")})
                </span>
              )}
            </div>
          </div>
        </div>
      )}

      <SitonholyLoginDialog
        open={loginOpen}
        onOpenChange={setLoginOpen}
        onSuccess={() => {
          void refetchPools();
          void refetchInstances();
          onServersChanged();
        }}
      />
    </div>
  );
}
