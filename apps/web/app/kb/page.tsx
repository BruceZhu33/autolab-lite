"use client";

/** 知识库(LABNOTE)内嵌页:与论文/笔记/往轮实验结论对话。
 * 身份 = owner 自己的 labnote 账号(登录一次存本机);请求经 control 代理,
 * 能看到自己有权限的全部空间。autolab 服务链路(PAT)不受影响。 */

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { BookOpen, Loader2, LogOut, Plus, Send } from "lucide-react";
import {
  clearKbToken, kbChatStream, kbGet, kbLogin, kbPut, kbRegister, kbToken,
  type KbSession, type KbSpace,
} from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "~/vendor/views/layout/page-header";

const FIELD = "w-full rounded-md border-[0.5px] border-surface-border bg-background px-2 text-caption";

function LoginCard({ onOk }: { onOk: (username: string) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const go = async () => {
    if (!username.trim() || !password) return toast.error("填用户名和密码");
    setBusy(true);
    try {
      const r = mode === "login"
        ? await kbLogin(username.trim(), password)
        : await kbRegister(username.trim(), password);
      onOk(r.username);
      toast.success(mode === "login" ? `已登录知识库:${r.username}` : `已注册并登录:${r.username}`);
    } catch (err) {
      toast.error(`${mode === "login" ? "登录" : "注册"}失败:${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mx-auto mt-10 max-w-sm rounded-xl border-[0.5px] border-surface-border bg-card p-4">
      <div className="flex gap-2">
        {(["login", "register"] as const).map((m) => (
          <button key={m}
                  className={`rounded-full px-2.5 py-0.5 text-caption ${mode === m ? "bg-accent font-medium" : "text-muted-foreground hover:text-foreground"}`}
                  onClick={() => setMode(m)}>
            {m === "login" ? "登录" : "注册新账号"}
          </button>
        ))}
      </div>
      <p className="mt-1.5 text-micro text-muted-foreground">
        {mode === "login"
          ? "用你的 labnote 账号(实验室成员账号);登录一次存在本机浏览器。"
          : "没有账号?注册一个(普通成员,可读共享空间);注册即登录。"}
      </p>
      <input className={`${FIELD} mt-3 h-8`} placeholder="用户名" value={username}
             onChange={(e) => setUsername(e.target.value)} />
      <input className={`${FIELD} mt-2 h-8`} placeholder="密码" type="password" value={password}
             onChange={(e) => setPassword(e.target.value)}
             onKeyDown={(e) => e.key === "Enter" && void go()} />
      <Button size="sm" className="mt-3 w-full gap-1" disabled={busy} onClick={() => void go()}>
        {busy ? <Loader2 className="size-3.5 animate-spin" /> : null}
        {mode === "login" ? "登录" : "注册并登录"}
      </Button>
    </div>
  );
}

export default function KnowledgePage() {
  const [user, setUser] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [sessionId, setSessionId] = useState("");
  const [messages, setMessages] = useState<{ role: "user" | "assistant"; text: string }[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [spaceIds, setSpaceIds] = useState<string[]>([]);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!kbToken()) return;
    void kbGet<{ username: string; role: string }>("/auth/me")
      .then((me) => { setUser(me.username); setIsAdmin(me.role === "admin"); })
      .catch(() => clearKbToken());
  }, []);
  useEffect(() => { bottom.current?.scrollIntoView({ block: "nearest" }); }, [messages]);

  const sessions = useQuery({
    queryKey: ["kb-sessions", user], enabled: !!user,
    queryFn: () => kbGet<{ sessions: KbSession[] }>("/sessions"),
    refetchInterval: 15000,
  });
  const spaces = useQuery({
    queryKey: ["kb-spaces", user], enabled: !!user,
    queryFn: () => kbGet<KbSpace[]>("/spaces"),
  });

  useEffect(() => {
    if (sessionId && spaces.data) {
      void kbGet<{ space_ids: string[] }>(`/sessions/${sessionId}/spaces`)
        .then((r) => setSpaceIds(r.space_ids ?? []))
        .catch(() => setSpaceIds([]));
    }
  }, [sessionId, spaces.data]);

  const newChat = () => {
    setSessionId(`autolab-${Date.now().toString(36)}`);
    setMessages([]);
  };

  const bindSpaces = async (next: string[]) => {
    if (!sessionId) return;
    let target = next;
    if (!target.length) {
      // 后端要求会话至少绑一个检索空间:取消到最后一个时回落默认空间,而不是弹回刚取消的
      target = [(spaces.data ?? []).find((sp) => sp.is_default)?.id ?? "lab-shared"];
      toast.info("会话至少要绑一个检索空间,已回到默认空间");
    }
    setSpaceIds(target);
    try {
      await kbPut(`/sessions/${sessionId}/spaces`, { space_ids: next });
    } catch (err) {
      toast.error(`空间绑定失败:${(err as Error).message}`);
    }
  };

  const send = async () => {
    const text = input.trim();
    if (!text || sending) return;
    if (!sessionId) newChat();
    setInput("");
    setMessages((prev) => [...prev, { role: "user", text }]);
    setSending(true);
    let acc = "";
    setMessages((prev) => [...prev, { role: "assistant", text: "" }]);
    try {
      await kbChatStream(text, sessionId, (chunk) => {
        acc += chunk;
        setMessages((prev) => {
          const copy = [...prev];
          copy[copy.length - 1] = { role: "assistant", text: acc };
          return copy;
        });
      });
      void sessions.refetch();
    } catch (err) {
      toast.error(`对话失败:${(err as Error).message}`);
      setMessages((prev) => {
        const copy = [...prev];
        copy[copy.length - 1] = { role: "assistant", text: acc || "(失败)" };
        return copy;
      });
    } finally {
      setSending(false);
    }
  };

;

  const spaceName = (id: string) =>
    (spaces.data ?? []).find((sp) => sp.id === id)?.name ?? id;

;

;

;

  if (!user) {
    return (
      <div className="flex h-full flex-col">
        <PageHeader>
          <BookOpen className="size-4 text-muted-foreground" />
          <h1 className="text-title-sm font-semibold tracking-tight">知识库</h1>
        </PageHeader>
        <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
          <LoginCard onOk={setUser} />
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <BookOpen className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">知识库</h1>
        <p className="ml-2 hidden truncate text-caption text-muted-foreground md:block">
          {user}{spaceIds.length ? ` · 空间:${spaceIds.join(",")}` : ""}
        </p>
        <button className="ml-auto text-micro text-muted-foreground hover:text-foreground"
                onClick={() => { clearKbToken(); setUser(""); }}>
          <LogOut className="mr-1 inline size-3.5 align-[-3px]" />退出
        </button>
      </PageHeader>
      <div className={`flex flex-1 gap-3 overflow-hidden ${PAGE_GUTTER}`}>
        <aside className={`${PAGE_GUTTER} ${PAGE_RAIL} flex w-60 shrink-0 flex-col gap-2 py-3`}>
          <Button size="sm" variant="outline" className="w-full gap-1" onClick={newChat}>
            <Plus className="size-3.5" />新会话
          </Button>
          <div className="flex min-h-0 flex-1 flex-col rounded-xl border-[0.5px] border-surface-border bg-card p-2">
            <p className="text-micro font-semibold text-muted-foreground">会话</p>
            <ul className="mt-1 min-h-0 flex-1 space-y-0.5 overflow-y-auto">
              {(sessions.data?.sessions ?? []).slice().reverse().slice(0, 30).map((s) => (
                <li key={s.session_id} className="flex items-center">
                  <button
                    className={`flex min-w-0 flex-1 items-center gap-1 rounded-md px-1.5 py-1 text-left text-micro ${
                      s.session_id === sessionId ? "bg-accent" : "hover:bg-muted/50"}`}
                    onClick={() => setSessionId(s.session_id)}
                    title={`${s.session_id} · ${s.message_count} 条`}>
                    <span className="min-w-0 flex-1 truncate">
                      {s.session_id.startsWith("autolab-") ? s.session_id.slice(7) : s.session_id}
                    </span>
                    {s.message_count
                      ? <span className="shrink-0 rounded-full bg-muted/60 px-1 text-micro tabular-nums text-muted-foreground">
                          {s.message_count}
                        </span> : null}
                  </button>
                </li>
              ))}
              {!(sessions.data?.sessions ?? []).length
                ? <li className="px-1.5 py-2 text-micro text-muted-foreground">
                    还没有会话;点上面「新会话」开始
                  </li> : null}
            </ul>
          </div>
        </aside>
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden pb-3">
          <div className="mb-2 flex flex-wrap items-center gap-1.5">
            <span className="text-micro text-muted-foreground">检索域:</span>
            {(spaces.data ?? []).map((sp) => {
              const on = spaceIds.includes(sp.id);
              return (
                <button key={sp.id}
                        className={`rounded-full border-[0.5px] px-2 py-0.5 text-micro ${
                          on ? "border-brand bg-brand/10 text-foreground"
                             : "border-surface-border text-muted-foreground hover:bg-muted/40"}`}
                        title={on ? "点击移出检索域" : "点击加入检索域"}
                        onClick={() => void bindSpaces(
                          on ? spaceIds.filter((x) => x !== sp.id) : [...spaceIds, sp.id])}>
                  {sp.visibility === "private" ? "🔒 " : ""}{sp.name ?? sp.id}
                </button>
              );
            })}
            <a href="/spaces" className="ml-auto text-micro text-muted-foreground hover:text-foreground hover:underline">
              管理空间与文档 →
            </a>
          </div>
          <div className="flex-1 overflow-y-auto rounded-xl border-[0.5px] border-surface-border bg-card p-3">
            {messages.length === 0 ? (
              <div className="flex h-full flex-col items-center justify-center gap-3">
                <BookOpen className="size-6 text-muted-foreground/50" />
                <p className="text-caption text-muted-foreground">
                  与知识库对话——论文、笔记、往轮实验结论
                  {spaceIds.length ? `(检索域:${spaceIds.map((x) => spaceName(x)).join("、")})` : ""}
                </p>
                <div className="flex flex-wrap justify-center gap-1.5">
                  {["EGDK 在低标注预算下的结论是什么", "往轮 25% 预算为什么失败", "这个方向有哪些可行改进"].map((q) => (
                    <button key={q}
                            className="rounded-full border-[0.5px] border-surface-border px-2.5 py-1 text-micro text-muted-foreground hover:bg-muted/40 hover:text-foreground"
                            onClick={() => setInput(q)}>{q}</button>
                  ))}
                </div>
              </div>
            ) : messages.map((m, i) => (
              <div key={i} className={`mb-2 max-w-[85%] rounded-lg px-2.5 py-1.5 text-caption ${
                m.role === "user" ? "ml-auto bg-accent" : "bg-muted/40"}`}>
                <p className="whitespace-pre-wrap">{m.text ||
                  (sending && i === messages.length - 1 ? "…" : "")}</p>
              </div>
            ))}
            <div ref={bottom} />
          </div>
          <div className="mt-2 flex gap-2">
            <input className={`${FIELD} h-9`} placeholder={sessionId ? "提问…" : "点左侧 + 开个新会话"}
                   value={input} onChange={(e) => setInput(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && void send()} />
            <Button size="sm" className="gap-1" disabled={sending || !input.trim()} onClick={() => void send()}>
              {sending ? <Loader2 className="size-3.5 animate-spin" /> : <Send className="size-3.5" />}
            </Button>
          </div>
        </main>
      </div>
    </div>
  );
}
