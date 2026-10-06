"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  CheckCircle2, Compass, FileUp, FlaskConical, Lightbulb, Loader2, PenLine,
  Repeat, Sparkles,
} from "lucide-react";
import {
  draftProjectDirection, fetchProjects, fetchProjectDirection,
  saveProjectDirection, uploadProjectPaper, type ProjectListItem,
} from "@/lib/api";
import { EmptyState } from "@/components/empty-state";
import { ApiError } from "@/components/api-error";
import { MiniMarkdown } from "@/components/mini-markdown";
import { Button } from "~/vendor/ui/components/ui/button";
import { PageHeader, PAGE_GUTTER, PAGE_RAIL } from "@/vendor/views/layout/page-header";

/**
 * 方向页(D-028,替代原方案评审):Baseline 通过后,从论文 PDF + 代码 +
 * baseline 指标提炼研究方向——LLM 一键出草稿,owner 编辑后确认即成为
 * 迭代执行依据。旧的 A/B/C 方案卡/评分 Agent/报告流程保留在后端但入口
 * 已下线(老项目已采纳方案继续有效,双门兼容)。
 *
 * 确认后的下一步就是自动迭代:迭代只在 baseline 确认过的数据集上跑。
 */

export default function DirectionPage() {
  const queryClient = useQueryClient();
  const { data: projects = [], error } = useQuery({
    queryKey: ["projects"],
    queryFn: fetchProjects,
    refetchInterval: 8000,
  });
  // 方向相关阶段:review(待提炼)/iterate/confirm/done(可回看或改方向)
  const candidates = projects.filter((p) =>
    ["review", "iterate", "confirm", "done"].includes(p.stage));
  const [selectedId, setSelectedId] = useState("");
  // 看板卡片跳转带 ?project= 预选
  useEffect(() => {
    const from = new URLSearchParams(window.location.search).get("project");
    if (from) setSelectedId(from);
  }, []);
  const selected = candidates.find((p) => p.id === selectedId) ?? candidates[0];

  const direction = useQuery({
    queryKey: ["direction", selected?.id ?? ""],
    queryFn: () => fetchProjectDirection(selected!.id),
    enabled: !!selected,
  });
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);

  const data = direction.data;
  const confirmed = !!data?.confirmedAt;

  // 进入编辑态时用当前文本初始化
  useEffect(() => {
    if (editing) setText(data?.directionText || "");
  }, [editing, data?.directionText]);

  const refresh = () =>
    void queryClient.invalidateQueries({ queryKey: ["direction", selected?.id ?? ""] });

  const draft = async () => {
    if (!selected) return;
    setDrafting(true);
    toast.info("正在提炼方向…", { description: "论文 + 代码 + baseline 指标 → 方向草稿" });
    try {
      const result = await draftProjectDirection(selected.id);
      toast.success("方向草稿已生成,请过目后确认");
      setEditing(true);
      setText(result.directionText);
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setDrafting(false);
    }
  };

  const save = async (confirm: boolean) => {
    if (!selected) return;
    setSaving(true);
    try {
      await saveProjectDirection(selected.id, { text: text.trim(), confirm });
      toast.success(confirm ? "方向已确认,可以去启动自动迭代了" : "草稿已保存");
      setEditing(false);
      refresh();
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const upload = async (file: File) => {
    if (!selected) return;
    setUploading(true);
    try {
      await uploadProjectPaper(selected.id, file);
      toast.success(`已上传论文 ${file.name}`);
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader>
        <Compass className="size-4 text-muted-foreground" />
        <h1 className="text-title-sm font-semibold tracking-tight">方向</h1>
        {candidates.length > 1 ? (
          <div className="ml-1 flex gap-1">
            {candidates.map((p) => (
              <button key={p.id} type="button" onClick={() => setSelectedId(p.id)}
                      className={`rounded-full px-2 py-0.5 text-caption font-medium ${
                        p.id === selected?.id ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-muted"}`}>
                {p.name}
              </button>
            ))}
          </div>
        ) : selected ? (
          <span className="ml-1 text-caption text-muted-foreground">{selected.name}</span>
        ) : null}
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          论文 + 代码 + baseline 指标 → 研究方向 · 确认后进自动迭代
        </p>
      </PageHeader>

      {error ? (
        <ApiError
          error={error}
          onRetry={() => void queryClient.invalidateQueries({ queryKey: ["projects"] })}
        />
      ) : candidates.length === 0 ? (
        <div className={`${PAGE_GUTTER} flex flex-1`}>
          <EmptyState
            icon={Compass}
            title="还没有待确立方向的项目"
            description="Baseline 通过的项目会出现在这里:从论文与代码提炼研究方向,确认后即可开跑自动迭代。"
            action={
              <Link href="/baseline">
                <Button variant="outline" size="sm" className="gap-1.5">
                  <FlaskConical className="size-3.5" />
                  前往 Baseline
                </Button>
              </Link>
            }
            secondary={
              <Link href="/projects" className="text-caption text-muted-foreground hover:text-foreground hover:underline">
                查看项目列表 →
              </Link>
            }
          />
        </div>
      ) : !selected ? null : (
        <div className={`flex-1 overflow-y-auto ${PAGE_GUTTER}`}>
          <div className={`${PAGE_RAIL} mx-auto max-w-4xl space-y-3 py-4`}>
            {/* 素材区:论文 / 数据集指标——提炼方向的输入 */}
            <section className="rounded-xl border-[0.5px] border-surface-border bg-surface p-3.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-caption font-semibold text-muted-foreground">素材</span>
                <span className="flex items-center gap-1 text-micro text-muted-foreground">
                  <FileUp className="size-3" />论文
                </span>
                {(data?.papers ?? []).map((name) => (
                  <span key={name} title={name}
                        className="max-w-52 truncate rounded-full bg-muted/60 px-2 py-0.5 font-mono text-micro text-muted-foreground">
                    {name}
                  </span>
                ))}
                <label className="cursor-pointer">
                  <input
                    type="file" accept="application/pdf" className="hidden"
                    disabled={uploading}
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file) void upload(file);
                      e.target.value = "";
                    }}
                  />
                  <span className={`inline-flex items-center gap-1 rounded-full border border-dashed
                                   border-surface-border px-2 py-0.5 text-micro text-muted-foreground
                                   hover:border-brand/40 hover:text-foreground ${uploading ? "opacity-50" : ""}`}>
                    {uploading ? <Loader2 className="size-3 animate-spin" /> : null}
                    + 上传 PDF
                  </span>
                </label>
                <span className="mx-1 text-micro text-muted-foreground/50">·</span>
                <span className="flex items-center gap-1 text-micro text-muted-foreground">
                  <FlaskConical className="size-3" />基线
                </span>
                {(data?.datasets ?? []).map((d) => (
                  <span key={d.name}
                        className="rounded-full bg-info/10 px-2 py-0.5 font-mono text-micro text-info">
                    {d.name}:{Object.values(d.metrics)[0] !== undefined
                      ? String(Object.values(d.metrics)[0]) : "—"}
                  </span>
                ))}
                {(data?.datasets ?? []).length === 0 ? (
                  <span className="text-micro text-muted-foreground">(无数据集快照)</span>
                ) : null}
              </div>
            </section>

            {/* 主区:草稿编辑 / 已确认展示 / 一键提炼 */}
            {direction.isLoading ? (
              <div className="flex justify-center py-16">
                <Loader2 className="size-4 animate-spin text-muted-foreground" />
              </div>
            ) : editing ? (
              <section className="rounded-xl border-[0.5px] border-brand/30 bg-surface p-3.5">
                <div className="mb-2 flex items-center gap-2">
                  <PenLine className="size-4 text-brand" />
                  <h2 className="text-caption font-semibold">
                    {confirmed ? "修改方向(确认将失效,保存后需重新确认)" : "方向草稿"}
                  </h2>
                </div>
                <textarea
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  rows={14}
                  spellCheck={false}
                  className="w-full resize-y rounded-lg border border-surface-border bg-muted/20
                             p-3 text-caption leading-relaxed outline-none
                             focus-visible:ring-2 focus-visible:ring-brand/40"
                  placeholder={"## 研究目标\n…\n## 可试的改进假设\n…\n## 约束\n…\n## 明确不做什么\n…"}
                />
                <div className="mt-2.5 flex flex-wrap items-center gap-2">
                  <Button size="sm" disabled={saving || !text.trim()} onClick={() => void save(true)}
                          className="gap-1.5">
                    {saving ? <Loader2 className="size-3.5 animate-spin" /> : <CheckCircle2 className="size-3.5" />}
                    保存并确认
                  </Button>
                  <Button variant="outline" size="sm" disabled={saving} onClick={() => void save(false)}
                          className="gap-1.5">
                    仅保存草稿
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>
                    取消
                  </Button>
                  <span className="ml-auto text-micro text-muted-foreground">
                    确认后即成为自动迭代的执行依据(旧「已采纳方案」同样有效)
                  </span>
                </div>
              </section>
            ) : confirmed && data?.directionText ? (
              <section className="rounded-xl border-[0.5px] border-success/30 bg-surface p-3.5">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <Compass className="size-4 text-success" />
                  <h2 className="text-caption font-semibold">研究方向(已确认)</h2>
                  <span className="rounded-full bg-success/10 px-1.5 py-0.5 text-micro font-medium text-success">
                    迭代执行依据
                  </span>
                  <span className="ml-auto flex gap-1.5">
                    <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-micro"
                            onClick={() => void draft()} disabled={drafting}>
                      {drafting ? <Loader2 className="size-3 animate-spin" /> : <Sparkles className="size-3" />}
                      重新提炼
                    </Button>
                    <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-micro"
                            onClick={() => setEditing(true)}>
                      <PenLine className="size-3" />
                      编辑
                    </Button>
                  </span>
                </div>
                <div className="max-h-[46vh] overflow-y-auto pr-1">
                  <MiniMarkdown text={data.directionText} />
                </div>
                <div className="mt-2.5 flex items-center gap-2 border-t border-surface-border/60 pt-2.5">
                  <Lightbulb className="size-3.5 text-brand" />
                  <span className="text-micro text-muted-foreground">
                    方向不是一次性的:迭代中随时回来改,或让循环 awaiting 裁决时改向
                  </span>
                  <Link href="/iteration" className="ml-auto">
                    <Button size="sm" className="gap-1.5">
                      <Repeat className="size-3.5" />
                      去启动自动迭代 →
                    </Button>
                  </Link>
                </div>
              </section>
            ) : data?.directionText ? (
              <section className="rounded-xl border-[0.5px] border-warning/40 bg-surface p-3.5">
                <div className="mb-2 flex items-center gap-2">
                  <PenLine className="size-4 text-warning" />
                  <h2 className="text-caption font-semibold">方向草稿(未确认)</h2>
                  <span className="ml-auto flex gap-1.5">
                    <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-micro"
                            onClick={() => void draft()} disabled={drafting}>
                      {drafting ? <Loader2 className="size-3 animate-spin" /> : <Sparkles className="size-3" />}
                      重新提炼
                    </Button>
                    <Button size="sm" className="h-7 gap-1 px-2 text-micro"
                            onClick={() => setEditing(true)}>
                      <PenLine className="size-3" />
                      编辑并确认
                    </Button>
                  </span>
                </div>
                <div className="max-h-[42vh] overflow-y-auto pr-1 opacity-80">
                  <MiniMarkdown text={data.directionText} />
                </div>
              </section>
            ) : (
              <section className="flex min-h-[40vh] flex-col items-center justify-center gap-2
                                  rounded-xl border-[0.5px] border-dashed border-surface-border p-6 text-center">
                <span className="flex size-12 items-center justify-center rounded-full bg-brand/10">
                  <Sparkles className="size-6 text-brand" />
                </span>
                <h2 className="mt-2 text-title-sm font-semibold">从论文与代码提炼研究方向</h2>
                <p className="max-w-md text-caption leading-relaxed text-muted-foreground">
                  上传论文 PDF(可选)后点「一键提炼」:LLM 读论文、代码概览与 baseline 指标,
                  产出方向草稿(目标 / 假设 / 约束 / 不做什么);也可以直接手写。
                  确认后即为自动迭代的执行依据。
                </p>
                <div className="mt-3 flex items-center gap-2">
                  <Button onClick={() => void draft()} disabled={drafting} className="gap-1.5">
                    {drafting ? <Loader2 className="size-4 animate-spin" /> : <Sparkles className="size-4" />}
                    一键提炼方向
                  </Button>
                  <Button variant="outline" onClick={() => setEditing(true)} className="gap-1.5">
                    <PenLine className="size-4" />
                    手写方向
                  </Button>
                </div>
              </section>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
