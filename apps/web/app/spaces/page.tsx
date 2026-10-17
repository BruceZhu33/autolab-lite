"use client";

/** 知识空间管理:空间的创建/成员/授权,文档的上传/删除/按空间浏览。
 * 与 /kb(纯对话)分工——那里只留会话与检索域,重管理动作都在这里。 */

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { FileUp, FolderPlus, Loader2, Lock, Plus, Trash2, Users } from "lucide-react";
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

function SpaceCard({ space, isAdmin }: { space: KbSpace; isAdmin: boolean }) {
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

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["kb-space-docs", space.id] });
    void queryClient.invalidateQueries({ queryKey: ["kb-docs-all"] });
  };

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
      refresh();
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

  return (
    <section className="rounded-xl border-[0.5px] border-surface-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-caption font-semibold">
          {space.visibility === "private" ? <Lock className="mr-1 inline size-3.5 align-[-3px]" /> : null}
          {space.name ?? space.id}
        </p>
        <span className="rounded-full bg-muted/60 px-1.5 text-micro text-muted-foreground">
          {space.visibility === "private" ? "私有" : "共享"}
          {space.is_default ? " · 默认" : ""}
        </span>
        <span className="text-micro text-muted-foreground">我的角色:{space.my_role ?? "—"}</span>
        <div className="ml-auto flex items-center gap-1.5">
          {isOwner ? (
            <>
              <Button size="sm" variant="outline" className="h-6 gap-1 px-2 text-micro"
                      disabled={busy !== null} onClick={() => void grant()}>
                {busy === "grant" ? <Loader2 className="size-3 animate-spin" /> : null}授权 Agent
              </Button>
              <Button size="sm" variant="outline" className="h-6 gap-1 px-2 text-micro"
                      onClick={() => setAdding((v) => !v)}>
                <Users className="size-3" />成员
              </Button>
            </>
          ) : null}
          <label className="cursor-pointer text-muted-foreground hover:text-foreground"
                 title="上传(pdf/docx/xlsx)到此空间">
            {busy === "upload" ? <Loader2 className="size-3.5 animate-spin" /> : <FileUp className="size-3.5" />}
            <input type="file" className="hidden" accept=".pdf,.docx,.doc,.xlsx,.xls"
                   onChange={(e) => e.target.files?.[0] && void upload(e.target.files[0])} />
          </label>
        </div>
      </div>

      {adding && isOwner ? (
        <div className="mt-2 flex flex-wrap items-center gap-1.5 rounded-md bg-muted/30 p-1.5">
          <input className={`${FIELD} h-7 max-w-40`} placeholder="用户名"
                 value={memberName} onChange={(e) => setMemberName(e.target.value)} />
          <select className="rounded border-[0.5px] border-surface-border bg-background px-1 text-micro"
                  value={memberRole} onChange={(e) => setMemberRole(e.target.value)}>
            <option value="reader">reader(可读)</option>
            <option value="editor">editor(可传)</option>
            <option value="owner">owner(可管)</option>
          </select>
          <Button size="sm" className="h-6 px-2 text-micro" onClick={() => void addMember()}>添加</Button>
        </div>
      ) : null}
      {isOwner && members.data ? (
        <p className="mt-1 text-micro text-muted-foreground">
          成员:{members.data.map((m: KbMember) => `${m.username}(${m.role})`).join("、") || "仅自己"}
        </p>
      ) : null}

      <ul className="mt-2 space-y-0.5">
        {(docs.data ?? []).map((d) => (
          <li key={d.filename} className="flex items-center gap-2 text-micro">
            <span className="min-w-0 flex-1 truncate" title={d.filename}>{d.filename}</span>
            <span className="shrink-0 tabular-nums text-muted-foreground">{d.chunk_count ?? "—"} 块</span>
            {isAdmin ? (
              <button className="shrink-0 text-muted-foreground/60 hover:text-destructive"
                      title="删除(需管理员)"
                      onClick={() => void kbDelete(`/documents/${encodeURIComponent(d.filename)}`)
                        .then(refresh).catch((e) => toast.error(`删除失败:${e.message}`))}>
                <Trash2 className="size-3" />
              </button>
            ) : null}
          </li>
        ))}
        {!(docs.data ?? []).length
          ? <li className="text-micro text-muted-foreground">暂无文档;右上角上传</li> : null}
      </ul>
    </section>
  );
}

export default function SpacesPage() {
  const [authed, setAuthed] = useState(false);
  const [ready, setReady] = useState(false);
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
  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <FolderPlus className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">知识空间</h1>
        <p className="ml-2 hidden truncate text-caption text-muted-foreground md:block">
          空间/成员/文档管理;对话在「知识库」页
        </p>
        {authed ? (
          <Button size="sm" variant="outline" className="ml-auto h-7 gap-1 px-2 text-micro"
                  onClick={() => setCreating((v) => !v)}>
            <Plus className="size-3" />新建空间
          </Button>
        ) : null}
      </PageHeader>
      <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
        <div className={`${PAGE_RAIL} space-y-3 py-4`}>
          {!authed ? <LoginCard onOk={() => setAuthed(true)} /> : (
            <>
              {creating ? (
                <section className="rounded-xl border-[0.5px] border-surface-border bg-card p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <input className={`${FIELD} h-8 max-w-56`} placeholder="空间名"
                           value={name} onChange={(e) => setName(e.target.value)} />
                    <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
                      <input type="checkbox" checked={priv} onChange={(e) => setPriv(e.target.checked)} />
                      私有(仅自己与受邀成员)
                    </label>
                    <Button size="sm" className="h-7" onClick={() => void create()}>创建</Button>
                  </div>
                </section>
              ) : null}
              {(spaces.data ?? []).map((sp) => (
                <SpaceCard key={sp.id} space={sp} isAdmin={me.data?.role === "admin"} />
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
