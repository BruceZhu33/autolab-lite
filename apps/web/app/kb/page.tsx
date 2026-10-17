"use client";

/** 知识库(LABNOTE)内嵌页:与论文/笔记/往轮实验结论对话。
 * 身份 = owner 自己的 labnote 账号(登录一次存本机);请求经 control 代理,
 * 能看到自己有权限的全部空间。autolab 服务链路(PAT)不受影响。 */

import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { BookOpen, FileUp, Loader2, LogOut, Plus, Send, Trash2 } from "lucide-react";
import {
  clearKbToken, kbChatStream, kbCreateSpace, kbDelete, kbGet, kbLogin, kbPost,
  kbPut, kbRegister, kbToken, kbUpload, kbUploadJob, type KbDoc, type KbSession,
  type KbSpace,
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
  const [creatingSpace, setCreatingSpace] = useState(false);
  const [newSpaceName, setNewSpaceName] = useState("");
  const [newSpacePrivate, setNewSpacePrivate] = useState(true);
  const [uploadSpace, setUploadSpace] = useState("");
  const [docFilter, setDocFilter] = useState("");
  const bottom = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();

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
  const documents = useQuery({
    queryKey: ["kb-docs", user], enabled: !!user,
    queryFn: () => kbGet<KbDoc[]>("/documents"),
    refetchInterval: 20000,
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
    if (!sessionId || !next.length) return;
    setSpaceIds(next);
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

  const upload = async (file: File) => {
    const target = uploadSpace || spaceIds[0]
      || (spaces.data ?? []).find((sp) => sp.is_default)?.id
      || (spaces.data ?? [])[0]?.id || "";
    if (!target) return toast.error("先建一个空间再上传");
    toast.info(`${file.name} 已提交解析(空间 ${spaceName(target)})`);
    try {
      const started = await kbUpload(file, target);
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const job = await kbUploadJob(started.job_id);
        if (job.status === "failed") throw new Error(job.error || "解析失败");
        if (job.status === "completed") break;
      }
      toast.success(`${file.name} 已入库(空间 ${spaceName(target)})`);
      void queryClient.invalidateQueries({ queryKey: ["kb-docs", user] });
    } catch (err) {
      toast.error(`上传失败:${(err as Error).message}`);
    }
  };

  const spaceName = (id: string) =>
    (spaces.data ?? []).find((sp) => sp.id === id)?.name ?? id;

  const createSpace = async () => {
    const name = newSpaceName.trim();
    if (!name) return toast.error("填空间名");
    try {
      await kbCreateSpace(name, newSpacePrivate ? "private" : "shared");
      toast.success(`空间已创建:${name}(${newSpacePrivate ? "私有,仅你与邀请的成员" : "共享,全员可读"})`);
      setNewSpaceName("");
      setCreatingSpace(false);
      void queryClient.invalidateQueries({ queryKey: ["kb-spaces", user] });
    } catch (err) {
      toast.error(`建空间失败:${(err as Error).message}`);
    }
  };

  const removeDoc = async (filename: string) => {
    try {
      await kbDelete(`/documents/${encodeURIComponent(filename)}`);
      void queryClient.invalidateQueries({ queryKey: ["kb-docs", user] });
    } catch (err) {
      toast.error(`删除失败:${(err as Error).message}`);
    }
  };

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
        <aside className={`${PAGE_RAIL} w-56 shrink-0 space-y-3 overflow-y-auto py-3`}>
          <div className="rounded-xl border-[0.5px] border-surface-border bg-card p-2.5">
            <div className="flex items-center justify-between">
              <p className="text-caption font-semibold">会话</p>
              <button className="text-muted-foreground hover:text-foreground" onClick={newChat}
                      title="新会话"><Plus className="size-3.5" /></button>
            </div>
            <ul className="mt-1.5 space-y-0.5">
              {(sessions.data?.sessions ?? []).slice().reverse().slice(0, 20).map((s) => (
                <li key={s.session_id}>
                  <button
                    className={`w-full truncate rounded-md px-1.5 py-1 text-left text-micro ${
                      s.session_id === sessionId ? "bg-accent" : "hover:bg-muted/50"}`}
                    onClick={() => setSessionId(s.session_id)}
                    title={`${s.session_id} · ${s.message_count} 条`}>
                    {s.session_id}
                  </button>
                </li>
              ))}
            </ul>
          </div>
          <div className="rounded-xl border-[0.5px] border-surface-border bg-card p-2.5">
            <div className="flex items-center justify-between">
              <p className="text-caption font-semibold">检索空间</p>
              <button className="text-muted-foreground hover:text-foreground"
                      title="新建空间(私有=仅自己可见)" onClick={() => setCreatingSpace((v) => !v)}>
                <Plus className="size-3.5" />
              </button>
            </div>
            <p className="mt-0.5 text-micro text-muted-foreground">会话绑定的检索域(勾选才算)</p>
            {creatingSpace ? (
              <div className="mt-1.5 space-y-1 rounded-md bg-muted/30 p-1.5">
                <input className={`${FIELD} h-7`} placeholder="空间名" value={newSpaceName}
                       onChange={(e) => setNewSpaceName(e.target.value)} />
                <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
                  <input type="checkbox" checked={newSpacePrivate}
                         onChange={(e) => setNewSpacePrivate(e.target.checked)} />
                  私有(仅自己与邀请的成员可见)
                </label>
                <div className="flex gap-1.5">
                  <Button size="sm" variant="outline" className="h-6 px-2 text-micro"
                          onClick={() => void createSpace()}>创建</Button>
                  <Button size="sm" variant="ghost" className="h-6 px-2 text-micro"
                          onClick={() => setCreatingSpace(false)}>取消</Button>
                </div>
              </div>
            ) : null}
            <div className="mt-1.5 space-y-1">
              {(spaces.data ?? []).map((sp) => (
                <label key={sp.id} className="flex items-center gap-1.5 text-micro">
                  <input type="checkbox" checked={spaceIds.includes(sp.id)}
                         onChange={(e) => void bindSpaces(
                           e.target.checked ? [...spaceIds, sp.id]
                             : spaceIds.filter((x) => x !== sp.id) || [sp.id])} />
                  {sp.visibility === "private" ? "🔒 " : ""}{sp.name ?? sp.id}
                  {sp.is_default ? <span className="text-muted-foreground/60">(默认)</span> : null}
                </label>
              ))}
            </div>
          </div>
          <div className="rounded-xl border-[0.5px] border-surface-border bg-card p-2.5">
            <div className="flex items-center justify-between">
              <p className="text-caption font-semibold">文档</p>
              <div className="flex items-center gap-1.5">
                <select className="max-w-24 rounded border-[0.5px] border-surface-border bg-background px-1 text-micro"
                        value={uploadSpace} onChange={(e) => setUploadSpace(e.target.value)}
                        title="上传目标空间(需该空间 editor 权限)">
                  <option value="">跟随会话</option>
                  {(spaces.data ?? []).map((sp) => (
                    <option key={sp.id} value={sp.id}>{sp.name ?? sp.id}</option>
                  ))}
                </select>
                <label className="cursor-pointer text-muted-foreground hover:text-foreground"
                       title="上传(pdf/docx/xlsx)">
                  <FileUp className="size-3.5" />
                  <input type="file" className="hidden" accept=".pdf,.docx,.doc,.xlsx,.xls"
                         onChange={(e) => e.target.files?.[0] && void upload(e.target.files[0])} />
                </label>
              </div>
            </div>
            <select className="mt-1.5 w-full rounded border-[0.5px] border-surface-border bg-background px-1 text-micro"
                    value={docFilter} onChange={(e) => setDocFilter(e.target.value)}>
              <option value="">全部空间</option>
              {(spaces.data ?? []).map((sp) => (
                <option key={sp.id} value={sp.id}>{sp.name ?? sp.id}</option>
              ))}
            </select>
            <ul className="mt-1.5 space-y-0.5">
              {(documents.data ?? [])
                .filter((d) => !docFilter || d.space_id === docFilter).slice(0, 30).map((d) => (
                <li key={`${d.space_id}/${d.filename}`} className="flex items-center gap-1">
                  <span className="shrink-0 rounded bg-muted/50 px-1 text-micro text-muted-foreground"
                        title={d.space_id}>{spaceName(d.space_id || "")}</span>
                  <span className="min-w-0 flex-1 truncate text-micro" title={d.filename}>
                    {d.filename}
                  </span>
                  {isAdmin ? (
                    <button className="text-muted-foreground/60 hover:text-destructive"
                            onClick={() => void removeDoc(d.filename)} title="删除(需管理员)">
                      <Trash2 className="size-3" />
                    </button>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        </aside>
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden pb-3">
          <div className="flex-1 overflow-y-auto rounded-xl border-[0.5px] border-surface-border bg-card p-3">
            {messages.length === 0 ? (
              <p className="mt-8 text-center text-caption text-muted-foreground">
                问点什么——比如「EGDK 在低标注预算下的结论是什么」「往轮 25% 预算为什么失败」
              </p>
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
