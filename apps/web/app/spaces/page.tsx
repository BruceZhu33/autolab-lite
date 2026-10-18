"use client";

/** 空间与文档管理:文件夹式导航——大厅是空间瓷砖网格(图标+名称+徽标+文档数),
 * 点进去是该空间的详情(成员/授权/文档上传与浏览)。风格沿用控制台 tokens。 */

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ArrowLeft, FileUp, Folder, FolderPlus, Loader2, Lock, Plus, Trash2, Users,
} from "lucide-react";
import {
  clearKbToken, kbAddMember, kbCreateSpace, kbDelete, kbGet, kbGrantAgent, kbLogin,
  kbMembers, kbToken, kbUpload, kbUploadJob, type KbDoc, type KbMember, type KbSpace,
} from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "~/vendor/views/layout/page-header";

const FIELD = "w-full rounded-md border-[0.5px] border-surface-border bg-background px-2 text-caption";

function LoginCard({ onOk }: { onOk: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const go = async () => {
    try {
      await kbLogin(username.trim(), password);
      onOk();
    } catch (err) {
      toast.error(`登录失败:${(err as Error).message}`);
    }
  };
  return (
    <div className="mx-auto mt-10 max-w-sm rounded-xl border-[0.5px] border-surface-border bg-card p-4">
      <p className="text-caption font-semibold">先登录知识库</p>
      <p className="mt-0.5 text-micro text-muted-foreground">空间与文档的管理需要你的 labnote 账号。</p>
      <input className={`${FIELD} mt-3 h-8`} placeholder="用户名" value={username}
             onChange={(e) => setUsername(e.target.value)} />
      <input className={`${FIELD} mt-2 h-8`} placeholder="密码" type="password" value={password}
             onChange={(e) => setPassword(e.target.value)}
             onKeyDown={(e) => e.key === "Enter" && void go()} />
      <Button size="sm" className="mt-3 w-full" onClick={() => void go()}>登录</Button>
    </div>
  );
}

/* ------------------------------------------------------------------ 大厅 */

function Lobby({ spaces, docsAll, onOpen, onNew }: {
  spaces: KbSpace[]; docsAll: KbDoc[];
  onOpen: (id: string) => void; onNew: () => void;
}) {
  const countOf = (id: string) => docsAll.filter((d) => d.space_id === id).length;
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
      <button onClick={onNew}
              className="flex h-28 flex-col items-center justify-center gap-1.5 rounded-xl border-[0.5px] border-dashed border-surface-border text-muted-foreground transition-colors hover:border-brand hover:bg-brand/5 hover:text-foreground">
        <FolderPlus className="size-6" />
        <span className="text-caption">新建空间</span>
      </button>
      {spaces.map((sp) => (
        <button key={sp.id} onClick={() => onOpen(sp.id)}
                className="flex h-28 flex-col rounded-xl border-[0.5px] border-surface-border bg-card p-3 text-left shadow-[var(--surface-shadow)] transition-colors hover:border-brand/60 hover:bg-brand/5">
          <div className="flex items-start justify-between">
            {sp.visibility === "private"
              ? <Lock className="size-4 text-muted-foreground" />
              : <Folder className="size-4 text-brand/70" />}
            <span className="rounded-full bg-muted/60 px-1.5 text-micro tabular-nums text-muted-foreground">
              {countOf(sp.id)}
            </span>
          </div>
          <p className="mt-auto truncate text-caption font-semibold" title={sp.name ?? sp.id}>
            {sp.name ?? sp.id}
          </p>
          <p className="truncate text-micro text-muted-foreground">
            {sp.is_default ? "默认 · " : ""}{sp.my_role ?? "—"}
          </p>
        </button>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ 详情 */

function SpaceDetail({ space, isAdmin, onBack }: {
  space: KbSpace; isAdmin: boolean; onBack: () => void;
}) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [memberName, setMemberName] = useState("");
  const [memberRole, setMemberRole] = useState("reader");
  const [busy, setBusy] = useState<string | null>(null);
  const isOwner = space.my_role === "owner";

  const docs = useQuery({
    queryKey: ["kb-space-docs", space.id],
    queryFn: () => kbGet<KbDoc[]>(`/documents?space_id=${encodeURIComponent(space.id)}`),
  });
  const members = useQuery({
    queryKey: ["kb-space-members", space.id], enabled: isOwner,
    queryFn: () => kbMembers(space.id),
  });

  const upload = async (file: File) => {
    setBusy("upload");
    toast.info(`${file.name} 已提交解析…`);
    try {
      const started = await kbUpload(file, space.id);
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const job = await kbUploadJob(started.job_id);
        if (job.status === "failed") throw new Error(job.error || "解析失败");
        if (job.status === "completed") break;
      }
      toast.success(`${file.name} 已入「${space.name}」`);
      void queryClient.invalidateQueries({ queryKey: ["kb-space-docs", space.id] });
      void queryClient.invalidateQueries({ queryKey: ["kb-docs-all"] });
    } catch (err) {
      toast.error(`上传失败:${(err as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  const grant = async () => {
    setBusy("grant");
    try {
      await kbGrantAgent(space.id);
      toast.success("已授权:研究 Agent 可读取此空间");
      void queryClient.invalidateQueries({ queryKey: ["kb-space-members", space.id] });
    } catch (err) {
      toast.error(`授权失败:${(err as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  const addMember = async () => {
    if (!memberName.trim()) return;
    try {
      await kbAddMember(space.id, memberName.trim(), memberRole);
      toast.success(`已添加成员 ${memberName.trim()}(${memberRole})`);
      setMemberName("");
      setAdding(false);
      void queryClient.invalidateQueries({ queryKey: ["kb-space-members", space.id] });
    } catch (err) {
      toast.error(`添加失败:${(err as Error).message}`);
    }
  };

  const removeDoc = async (filename: string) => {
    try {
      await kbDelete(`/documents/${encodeURIComponent(filename)}`);
      void queryClient.invalidateQueries({ queryKey: ["kb-space-docs", space.id] });
      void queryClient.invalidateQueries({ queryKey: ["kb-docs-all"] });
    } catch (err) {
      toast.error(`删除失败:${(err as Error).message}`);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={onBack}
                className="inline-flex h-7 items-center gap-1 rounded-md border-[0.5px] border-surface-border bg-surface px-2 text-micro shadow-[var(--surface-shadow)] hover:bg-muted/40">
          <ArrowLeft className="size-3" />全部空间
        </button>
        <h2 className="text-caption font-semibold">
          {space.visibility === "private" ? <Lock className="mr-1 inline size-3.5 align-[-3px]" /> : null}
          {space.name ?? space.id}
        </h2>
        <span className="rounded-full bg-muted/60 px-1.5 text-micro text-muted-foreground">
          {space.visibility === "private" ? "私有" : "共享"}{space.is_default ? " · 默认" : ""} · 我的角色 {space.my_role ?? "—"}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          {isOwner ? (
            <>
              <Button size="sm" variant="outline" className="h-7 gap-1 px-2 text-micro"
                      disabled={busy !== null} onClick={() => void grant()}>
                {busy === "grant" ? <Loader2 className="size-3 animate-spin" /> : null}授权 Agent
              </Button>
              <Button size="sm" variant="outline" className="h-7 gap-1 px-2 text-micro"
                      onClick={() => setAdding((v) => !v)}>
                <Users className="size-3" />成员
              </Button>
            </>
          ) : null}
          <label className="inline-flex h-7 cursor-pointer items-center gap-1 rounded-md border-[0.5px] border-surface-border bg-surface px-2.5 text-micro shadow-[var(--surface-shadow)] hover:bg-muted/40"
                 title="上传(pdf/docx/xlsx)到此空间">
            {busy === "upload"
              ? <Loader2 className="size-3 animate-spin" />
              : <FileUp className="size-3" />}
            上传文档
            <input type="file" className="hidden" accept=".pdf,.docx,.doc,.xlsx,.xls"
                   onChange={(e) => e.target.files?.[0] && void upload(e.target.files[0])} />
          </label>
        </div>
      </div>

      {adding && isOwner ? (
        <div className="flex flex-wrap items-center gap-1.5 rounded-xl border-[0.5px] border-surface-border bg-card p-2">
          <input className={`${FIELD} h-7 max-w-40`} placeholder="用户名"
                 value={memberName} onChange={(e) => setMemberName(e.target.value)} />
          <select className="rounded border-[0.5px] border-surface-border bg-background px-1 text-micro"
                  value={memberRole} onChange={(e) => setMemberRole(e.target.value)}>
            <option value="reader">reader(可读)</option>
            <option value="editor">editor(可传)</option>
            <option value="owner">owner(可管)</option>
          </select>
          <Button size="sm" className="h-7 px-2 text-micro" onClick={() => void addMember()}>添加</Button>
        </div>
      ) : null}
      {isOwner && members.data ? (
        <p className="text-micro text-muted-foreground">
          成员:{members.data.map((m: KbMember) => `${m.username}(${m.role})`).join("、") || "仅自己"}
        </p>
      ) : null}

      <div className="rounded-xl border-[0.5px] border-surface-border bg-card p-3">
        <p className="text-micro font-semibold text-muted-foreground">文档({(docs.data ?? []).length})</p>
        <ul className="mt-1.5 space-y-1">
          {(docs.data ?? []).map((d) => (
            <li key={d.filename} className="flex items-center gap-2 rounded-md px-1.5 py-1 text-caption hover:bg-muted/30">
              <span className="min-w-0 flex-1 truncate" title={d.filename}>{d.filename}</span>
              <span className="shrink-0 text-micro tabular-nums text-muted-foreground">
                {d.chunk_count ?? "—"} 块
              </span>
              {isAdmin ? (
                <button className="shrink-0 text-muted-foreground/60 hover:text-destructive"
                        title="删除(需管理员)" onClick={() => void removeDoc(d.filename)}>
                  <Trash2 className="size-3" />
                </button>
              ) : null}
            </li>
          ))}
          {!(docs.data ?? []).length
            ? <li className="px-1.5 py-2 text-micro text-muted-foreground">
                暂无文档;右上角「上传文档」
              </li> : null}
        </ul>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 页面 */

export default function SpacesPage() {
  const [authed, setAuthed] = useState(false);
  const [ready, setReady] = useState(false);
  const [openId, setOpenId] = useState("");
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [priv, setPriv] = useState(true);
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!kbToken()) { setReady(true); return; }
    void kbGet("/auth/me").then(() => setAuthed(true)).catch(() => clearKbToken())
      .finally(() => setReady(true));
  }, []);

  const me = useQuery({
    queryKey: ["kb-me", authed], enabled: authed,
    queryFn: () => kbGet<{ username: string; role: string }>("/auth/me"),
  });
  const spaces = useQuery({
    queryKey: ["kb-spaces-all", authed], enabled: authed,
    queryFn: () => kbGet<KbSpace[]>("/spaces"),
  });
  const docsAll = useQuery({
    queryKey: ["kb-docs-all", authed], enabled: authed,
    queryFn: () => kbGet<KbDoc[]>("/documents"),
  });

  const create = async () => {
    if (!name.trim()) return toast.error("填空间名");
    try {
      await kbCreateSpace(name.trim(), priv ? "private" : "shared");
      toast.success(`空间已创建:${name.trim()}`);
      setName("");
      setCreating(false);
      void queryClient.invalidateQueries({ queryKey: ["kb-spaces-all", authed] });
    } catch (err) {
      toast.error(`创建失败:${(err as Error).message}`);
    }
  };

  if (!ready) return null;
  const opened = (spaces.data ?? []).find((sp) => sp.id === openId);
  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <FolderPlus className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">空间与文档</h1>
        <p className="ml-2 hidden truncate text-caption text-muted-foreground md:block">
          {opened ? (opened.name ?? opened.id) : "论文与笔记按空间组织;问答在「知识对话」页"}
        </p>
      </PageHeader>
      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${PAGE_RAIL} py-4`}>
          {!authed ? <LoginCard onOk={() => setAuthed(true)} />
          : opened ? <SpaceDetail space={opened} isAdmin={me.data?.role === "admin"}
                                  onBack={() => setOpenId("")} />
          : creating ? (
            <section className="mx-auto mt-10 max-w-sm rounded-xl border-[0.5px] border-surface-border bg-card p-4">
              <p className="text-caption font-semibold">
                <Plus className="mr-1 inline size-3.5 align-[-2px]" />新建空间
              </p>
              <input className={`${FIELD} mt-3 h-8`} placeholder="空间名"
                     value={name} onChange={(e) => setName(e.target.value)} />
              <label className="mt-2 flex items-center gap-1.5 text-micro text-muted-foreground">
                <input type="checkbox" checked={priv} onChange={(e) => setPriv(e.target.checked)} />
                私有(仅自己与受邀成员可见)
              </label>
              <div className="mt-3 flex gap-2">
                <Button size="sm" onClick={() => void create()}>创建</Button>
                <Button size="sm" variant="ghost" onClick={() => setCreating(false)}>取消</Button>
              </div>
            </section>
          ) : (
            <Lobby spaces={spaces.data ?? []} docsAll={docsAll.data ?? []}
                   onOpen={setOpenId} onNew={() => setCreating(true)} />
          )}
        </div>
      </div>
    </div>
  );
}
