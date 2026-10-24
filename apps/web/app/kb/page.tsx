"use client";

/** 知识对话(LABNOTE)页:与论文/笔记/往轮实验结论问答。
 * 身份 = owner 自己的 labnote 账号(登录一次存本机);请求经 control 代理,
 * 能看到自己有权限的全部空间。autolab 服务链路(PAT)不受影响。 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { BookOpen, FolderPlus, Loader2, LogOut, Plus, Trash2 } from "lucide-react";
import {
  apiToken, clearKbToken, kbChatStream, kbDelete, kbGet, kbLogin, kbPut, kbToken,
  type KbSession, type KbSpace,
} from "@/lib/api";
import { ChatComposer } from "@/components/chat/chat-composer";
import { AssistantMessage, JumpToLatest, UserMessage } from "@/components/chat/chat-message";
import { useScrollPinning } from "@/components/chat/use-scroll-pinning";
import { Button } from "~/vendor/ui/components/ui/button";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "~/vendor/views/layout/page-header";

const FIELD = "w-full rounded-md border-[0.5px] border-surface-border bg-background px-2 text-caption";

function LoginCard({ onOk }: { onOk: (username: string) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  // 本表单只服务「令牌直登的 owner 补知识库身份」(/api/kb/login 要 owner 令牌);
  // 没有控制台令牌时直接引导统一登录,别给一个注定 403 的表单。
  if (!apiToken()) {
    return (
      <div className="mx-auto mt-10 max-w-sm rounded-xl border-[0.5px] border-surface-border bg-card p-4 text-center">
        <p className="text-caption font-semibold">先登录控制台账号</p>
        <p className="mt-1.5 text-micro leading-relaxed text-muted-foreground">
          知识库身份随控制台登录一起发放:到「设置 → 账户与访问」用 labnote 账号登录后,
          这里即可直接使用,无需单独登录。
        </p>
        <a href="/settings"><Button size="sm" className="mt-3">去设置页登录</Button></a>
      </div>
    );
  }
  const go = async () => {
    if (!username.trim() || !password) return toast.error("填用户名和密码");
    setBusy(true);
    try {
      const r = await kbLogin(username.trim(), password);
      onOk(r.username);
      toast.success(`已登录知识库:${r.username}`);
    } catch (err) {
      toast.error(`登录失败:${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mx-auto mt-10 max-w-sm rounded-xl border-[0.5px] border-surface-border bg-card p-4">
      <p className="text-caption font-semibold">登录知识库</p>
      <p className="mt-1.5 text-micro text-muted-foreground">
        用你的 labnote 账号(实验室成员账号);登录一次存在本机浏览器。
      </p>
      <input className={`${FIELD} mt-3 h-8`} placeholder="用户名" value={username}
             onChange={(e) => setUsername(e.target.value)} />
      <input className={`${FIELD} mt-2 h-8`} placeholder="密码" type="password" value={password}
             onChange={(e) => setPassword(e.target.value)}
             onKeyDown={(e) => e.key === "Enter" && void go()} />
      <Button size="sm" className="mt-3 w-full" disabled={busy} onClick={() => void go()}>
        {busy ? <Loader2 className="size-3.5 animate-spin" /> : null}登录
      </Button>
      <p className="mt-2 text-center text-micro text-muted-foreground">
        没有账号?<a href="/settings" className="text-brand hover:underline">去「设置 → 账户与访问」注册</a>
      </p>
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
  const { containerRef, showJumpControl, handleScroll, scrollToBottom } =
    useScrollPinning([messages, sending]);

  useEffect(() => {
    if (!kbToken()) return;
    void kbGet<{ username: string; role: string }>("/auth/me")
      .then((me) => { setUser(me.username); setIsAdmin(me.role === "admin"); })
      .catch(() => clearKbToken());
  }, []);

  // 站内跳转预填(如研究页「去知识对话问」),只填不自动发送
  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("q");
    if (q) setInput(q);
  }, []);

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

  const removeSession = async (sid: string) => {
    if (!window.confirm("删除该会话?聊天记录不可恢复。")) return;
    try {
      await kbDelete(`/sessions/${encodeURIComponent(sid)}`);
      if (sessionId === sid) {  // 删的是当前打开的会话:回到空白新会话态
        setSessionId("");
        setMessages([]);
      }
      toast.success("会话已删除");
      void sessions.refetch();
    } catch (err) {
      toast.error(`删除会话失败:${(err as Error).message}`);
    }
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
    // 首次发言自动开会话;sessionId 在本闭包里是旧值,必须用局部 sid,
    // 否则流式请求会带着空 session_id 发出去。
    const sid = sessionId || `autolab-${Date.now().toString(36)}`;
    if (!sessionId) setSessionId(sid);
    setInput("");
    setMessages((prev) => {
      const base = sessionId ? prev : []; // 自动开会话时丢掉旧空壳
      return [...base, { role: "user", text }];
    });
    setSending(true);
    let acc = "";
    setMessages((prev) => [...prev, { role: "assistant", text: "" }]);
    try {
      await kbChatStream(text, sid, (chunk) => {
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
          <h1 className="text-title-sm font-semibold tracking-tight">知识对话</h1>
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
        <h1 className="text-title-sm font-semibold tracking-tight">知识对话</h1>
        <p className="ml-2 hidden truncate text-caption text-muted-foreground md:block">
          {user}{spaceIds.length ? ` · 空间:${spaceIds.join(",")}` : ""}
        </p>
        <button className="ml-auto text-micro text-muted-foreground hover:text-foreground"
                onClick={() => { clearKbToken(); setUser(""); }}>
          <LogOut className="mr-1 inline size-3.5 align-[-3px]" />退出
        </button>
      </PageHeader>
      <div className={`flex flex-1 gap-3 overflow-hidden ${PAGE_GUTTER}`}>
        {/* PAGE_RAIL(w-full max-w-1440)是页面内容容器的类,挂在固定宽侧栏上会
            让 w-full 覆盖 w-44(编译 CSS 中 .w-full 在后),侧栏吞掉整行、对话区
            被挤为 0 宽(真机 2026-10-20)。侧栏只要定宽 + 不收缩。 */}
        <aside className="flex w-44 shrink-0 flex-col gap-2 py-3">
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
                      {s.title
                        || (s.message_count
                          ? (s.session_id.startsWith("autolab-") ? s.session_id.slice(7) : s.session_id)
                          : "新会话")}
                    </span>
                    {s.message_count
                      ? <span className="shrink-0 rounded-full bg-muted/60 px-1 text-micro tabular-nums text-muted-foreground">
                          {s.message_count}
                        </span> : null}
                  </button>
                  <button
                    className="shrink-0 rounded-md p-1 text-muted-foreground/50 hover:bg-muted/50 hover:text-destructive"
                    title="删除该会话" onClick={() => void removeSession(s.session_id)}>
                    <Trash2 className="size-3" />
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
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex flex-wrap items-center gap-1.5 pb-1.5">
            <span className="text-micro text-muted-foreground">检索域:</span>
            {(spaces.data ?? []).map((sp) => {
              const on = spaceIds.includes(sp.id);
              return (
                <button key={sp.id}
                        className={`rounded-full border-[0.5px] px-2 py-0.5 text-micro transition-colors ${
                          on ? "border-brand bg-brand/10 text-brand"
                             : "border-surface-border text-muted-foreground hover:bg-muted/40 hover:text-foreground"}`}
                        title={on ? "点击移出检索域" : "点击加入检索域"}
                        onClick={() => void bindSpaces(
                          on ? spaceIds.filter((x) => x !== sp.id) : [...spaceIds, sp.id])}>
                  {sp.visibility === "private" ? "🔒 " : ""}{sp.name ?? sp.id}
                </button>
              );
            })}
            <a href="/spaces"
               className="ml-auto inline-flex h-7 shrink-0 items-center gap-1 rounded-md bg-surface px-2.5 text-micro shadow-[var(--surface-shadow)] hover:bg-muted/40">
              管理空间与文档<FolderPlus className="size-3" />
            </a>
          </div>
          <div className="relative min-h-0 flex-1">
            {showJumpControl ? <JumpToLatest onClick={() => scrollToBottom(true)} /> : null}
            <div ref={containerRef} onScroll={handleScroll}
                 className="cds-scroll-fade-y h-full overflow-y-auto pr-1">
              {messages.length === 0 ? (
                <div className="flex h-full flex-col items-center justify-center gap-2.5 text-center">
                  <p className="font-serif text-title text-foreground">与知识库对话</p>
                  <p className="text-caption text-muted-foreground">
                    论文、笔记、往轮实验结论
                    {spaceIds.length ? ` · 检索域:${spaceIds.map((x) => spaceName(x)).join("、")}` : ""}
                  </p>
                  <div className="flex max-w-xl flex-wrap justify-center gap-1.5 py-2">
                    {["EGDK 在低标注预算下的结论是什么", "往轮 25% 预算为什么失败", "这个方向有哪些可行改进"].map((q) => (
                      <button key={q}
                              className="rounded-full border-[0.5px] border-surface-border bg-surface px-3 py-1.5 text-caption text-muted-foreground shadow-[var(--surface-shadow)] transition-colors hover:text-foreground"
                              onClick={() => setInput(q)}>{q}</button>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col justify-end py-4">
                  {messages.map((m, i) => m.role === "user"
                    ? <UserMessage key={i} text={m.text} />
                    : <AssistantMessage key={i} text={m.text}
                        streaming={sending && i === messages.length - 1} />)}
                </div>
              )}
            </div>
          </div>
          <div className="mx-auto w-full max-w-3xl px-3 pb-3 pt-2">
            <ChatComposer
              value={input} onChange={setInput} onSend={() => void send()} sending={sending}
              placeholder={sessionId ? "提问…" : "直接提问会自动开一个新会话"}
              footer={<span className="hidden sm:inline">Enter 发送 · Shift + Enter 换行</span>}
            />
          </div>
        </main>
      </div>
    </div>
  );
}
