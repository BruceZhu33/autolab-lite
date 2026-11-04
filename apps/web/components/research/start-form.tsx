"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Database, GitBranch, Lightbulb, Loader2, PlayCircle, Plus,
  ShieldCheck, SlidersHorizontal, Trash2,
} from "lucide-react";
import {
  fetchProjectDatasets, fetchSpecs, kbGet, kbToken, startCampaign, startResearch,
  type KbSpace, type Project, type ResearchTier,
} from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";

const TIERS: Array<{ key: ResearchTier; title: string; badge: string; desc: string }> = [
  { key: "light", title: "轻量 · 人机协作", badge: "≤3 轮",
    desc: "每轮结束停下给你看报告,你回复后才进下一轮" },
  { key: "medium", title: "中量", badge: "≤5 轮", desc: "自动连续迭代,出现提升或轮数用完即停" },
  { key: "heavy", title: "重量 · 过夜", badge: "不限轮",
    desc: "达到显著阈值(Δ≥0.01)才停,GPU 小时兜底" },
];

const FIELD = "w-full rounded-md border-[0.5px] border-surface-border bg-background px-2 text-caption";

export function ResearchStartForm({ project }: { project: Project }) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"single" | "campaign">("single");
  const [tier, setTier] = useState<ResearchTier>("light");
  const [idea, setIdea] = useState("");
  const [campaignIdeas, setCampaignIdeas] = useState<string[]>(["", ""]);
  const [reference, setReference] = useState("");
  const [gpuHours, setGpuHours] = useState("");
  const [network, setNetwork] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [kbAssist, setKbAssist] = useState(false);
  const [kbSpaces, setKbSpaces] = useState<string[]>([]);
  const [critic, setCritic] = useState(false);
  const [reviewer, setReviewer] = useState(false);
  const [agentSpecId, setAgentSpecId] = useState("");
  const [starting, setStarting] = useState(false);

  const kbLoggedIn = typeof window !== "undefined" && !!kbToken();
  const kbSpacesQuery = useQuery({
    queryKey: ["kb-spaces-research"], enabled: kbLoggedIn,
    queryFn: () => kbGet<KbSpace[]>("/spaces"),
  });
  const { data: dsData } = useQuery({
    queryKey: ["datasets", project.id],
    queryFn: () => fetchProjectDatasets(project.id),
  });
  const specsQuery = useQuery({
    queryKey: ["specs", "research"],
    queryFn: () => fetchSpecs("research"),
  });
  const availableSpecs = (specsQuery.data ?? []).filter(
    (s) => s.status === "active" || s.status === "candidate",
  );
  const activeSpec = availableSpecs.find((s) => s.status === "active");
  const confirmed = (dsData?.datasets ?? []).filter((d) => d.state === "confirmed");

  const toggle = (name: string) =>
    setPicked((prev) => prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]);

  const updateCampaignIdea = (idx: number, val: string) =>
    setCampaignIdeas((prev) => prev.map((v, i) => (i === idx ? val : v)));

  const addCampaignIdea = () =>
    setCampaignIdeas((prev) => (prev.length < 3 ? [...prev, ""] : prev));

  const removeCampaignIdea = (idx: number) =>
    setCampaignIdeas((prev) => (prev.length > 1 ? prev.filter((_, i) => i !== idx) : prev));

  const start = async () => {
    setStarting(true);
    try {
      if (mode === "campaign") {
        const cleaned = campaignIdeas.map((s) => s.trim()).filter(Boolean);
        if (cleaned.length === 0) {
          toast.error("请至少填写 1 个并行研究方向");
          return;
        }
        await startCampaign(project.id, {
          ideas: cleaned,
          tier,
          datasets: picked.length ? picked : undefined,
          gpu_hours: tier === "heavy" && gpuHours ? Number(gpuHours) : undefined,
          critic,
          reviewer,
          agent_spec_id: agentSpecId || undefined,
        });
        toast.success(`已分叉启动 ${cleaned.length} 个并行研究分支`);
        setCampaignIdeas(["", ""]);
        void queryClient.invalidateQueries({ queryKey: ["campaigns", project.id] });
        void queryClient.invalidateQueries({ queryKey: ["projects"] });
      } else {
        await startResearch(project.id, {
          tier, idea, reference, network,
          gpu_hours: tier === "heavy" && gpuHours ? Number(gpuHours) : undefined,
          datasets: picked.length ? picked : undefined,
          kb_assist: kbAssist,
          kb_spaces: kbAssist && kbSpaces.length ? kbSpaces : undefined,
          critic,
          reviewer,
          agent_spec_id: agentSpecId || undefined,
        });
        setIdea("");
        void queryClient.invalidateQueries({ queryKey: ["run-latest", project.id] });
        void queryClient.invalidateQueries({ queryKey: ["project-runs", project.id] });
      }
    } catch (err) {
      toast.error(`启动失败:${(err as Error).message}`);
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="space-y-2.5 rounded-xl border-[0.5px] border-surface-border bg-card p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-caption font-semibold">
          <Lightbulb className="mr-1 inline size-3.5 align-[-2px] text-brand" />开始研究
        </p>
        <div className="inline-flex rounded-lg border-[0.5px] border-surface-border bg-muted/40 p-0.5 text-micro">
          <button
            type="button"
            onClick={() => setMode("single")}
            className={`rounded-md px-2.5 py-1 font-medium transition-colors ${
              mode === "single"
                ? "bg-background text-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            单线迭代
          </button>
          <button
            type="button"
            onClick={() => {
              setMode("campaign");
              if (tier === "light") setTier("medium");
            }}
            className={`inline-flex items-center gap-1 rounded-md px-2.5 py-1 font-medium transition-colors ${
              mode === "campaign"
                ? "bg-background text-brand shadow-xs"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <GitBranch className="size-3" />
            并行分支 (Campaign · 1–3 方向)
          </button>
        </div>
      </div>

      {mode === "single" ? (
        <>
          <textarea
            value={idea}
            onChange={(e) => setIdea(e.target.value)}
            rows={3}
            placeholder="你的想法(可选):想验证什么、往哪个方向改、哪些不要碰…"
            className={`${FIELD} py-1.5`}
          />
          <input
            value={reference}
            onChange={(e) => setReference(e.target.value)}
            placeholder="参考代码库(可选):GitHub URL 或本机路径;论文在项目页上传"
            className={`${FIELD} h-8`}
          />
          {kbLoggedIn ? (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <label className="flex items-center gap-1.5 text-caption">
                <input
                  type="checkbox"
                  checked={kbAssist}
                  onChange={(e) => setKbAssist(e.target.checked)}
                />
                知识库协助(Agent 定方向会先查论文/往轮结论)
              </label>
              {kbAssist ? (kbSpacesQuery.data ?? []).map((sp) => (
                <label
                  key={sp.id}
                  className="flex items-center gap-1 text-micro"
                  title="要在检索范围里就勾上;空间需已在「空间与文档」页授权给 Agent"
                >
                  <input
                    type="checkbox"
                    checked={kbSpaces.includes(sp.id)}
                    onChange={(e) => setKbSpaces((prev) =>
                      e.target.checked ? [...prev, sp.id] : prev.filter((x) => x !== sp.id))}
                  />
                  {sp.name ?? sp.id}
                </label>
              )) : null}
            </div>
          ) : (
            <p className="text-micro text-muted-foreground">
              知识库未登录(在「知识对话」或「空间与文档」页登录后,这里可以让 Agent 检索你授权的空间;未登录也始终能查实验回流)
            </p>
          )}
        </>
      ) : (
        <div className="space-y-1.5 rounded-lg border-[0.5px] border-surface-border bg-background/50 p-2.5">
          <div className="flex items-center justify-between">
            <span className="text-micro text-muted-foreground">
              每个方向会自动 <code className="rounded bg-muted px-1 font-mono">git worktree</code> 检出独立分支并启动研究会话（共享数据零拷贝）；全部结束后由 Python 按已验收增益选出冠军。
            </span>
            {campaignIdeas.length < 3 ? (
              <Button
                type="button"
                size="xs"
                variant="outline"
                className="shrink-0 gap-1"
                onClick={addCampaignIdea}
              >
                <Plus className="size-3" />加一个方向
              </Button>
            ) : null}
          </div>
          {campaignIdeas.map((val, idx) => (
            <div key={idx} className="flex items-center gap-1.5">
              <span className="w-12 shrink-0 font-mono text-micro text-muted-foreground">
                方向 #{idx + 1}
              </span>
              <input
                value={val}
                onChange={(e) => updateCampaignIdea(idx, e.target.value)}
                placeholder={
                  idx === 0
                    ? "如:把 PVL 模块的蒙特卡洛采样换成 EMA 解析聚合"
                    : idx === 1
                    ? "如:引入多尺度边界感知损失函数"
                    : "如:冻结编码器前两层 + 调整预热学习率"
                }
                className={`${FIELD} h-8 flex-1`}
              />
              {campaignIdeas.length > 1 ? (
                <button
                  type="button"
                  onClick={() => removeCampaignIdea(idx)}
                  title="移除该方向"
                  className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-destructive"
                >
                  <Trash2 className="size-3.5" />
                </button>
              ) : null}
            </div>
          ))}
        </div>
      )}

      {confirmed.length > 1 ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="flex items-center gap-1 text-micro text-muted-foreground">
            <Database className="size-3" />迭代数据集(不选=全部):
          </span>
          {confirmed.map((d) => (
            <label key={d.name} className="flex items-center gap-1 text-caption">
              <input type="checkbox" checked={picked.includes(d.name)} onChange={() => toggle(d.name)} />
              {d.name}
            </label>
          ))}
        </div>
      ) : null}

      <div className="grid gap-2 md:grid-cols-3">
        {TIERS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTier(t.key)}
            className={`rounded-lg border-[0.5px] p-2 text-left ${tier === t.key
              ? "border-brand bg-brand/5" : "border-surface-border hover:bg-muted/40"}`}
          >
            <p className="text-caption font-medium">{t.title}
              <span className="ml-1.5 rounded-full bg-muted/60 px-1.5 text-micro text-muted-foreground">{t.badge}</span>
            </p>
            <p className="mt-0.5 text-micro text-muted-foreground">{t.desc}</p>
          </button>
        ))}
      </div>

      {/* Phase C2 独立审阅闸门 + Phase C4 AgentSpec 规格选择 */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-surface-border/60 pt-2">
        <label
          className="flex items-center gap-1.5 text-micro text-muted-foreground"
          title="run_experiment 提交 GPU 前独立审阅训练口径、数据比例与重复实验风险"
        >
          <input
            type="checkbox"
            checked={critic}
            onChange={(e) => setCritic(e.target.checked)}
          />
          <ShieldCheck className="size-3 text-info" />
          实验前审阅 (Critic)
        </label>
        <label
          className="flex items-center gap-1.5 text-micro text-muted-foreground"
          title="end_round 收束前独立审阅多 seed 证据、保护文件哈希与结论可信度"
        >
          <input
            type="checkbox"
            checked={reviewer}
            onChange={(e) => setReviewer(e.target.checked)}
          />
          <ShieldCheck className="size-3 text-info" />
          轮末证据审阅 (Reviewer)
        </label>

        {availableSpecs.length > 0 ? (
          <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
            <SlidersHorizontal className="size-3" />
            Agent 规格
            <select
              value={agentSpecId}
              onChange={(e) => setAgentSpecId(e.target.value)}
              className="h-7 rounded-md border-[0.5px] border-surface-border bg-background px-1.5 text-micro"
            >
              <option value="">
                默认 ({activeSpec ? `当前生效:${activeSpec.label}` : "系统内置"})
              </option>
              {availableSpecs.map((sp) => (
                <option key={sp.id} value={sp.id}>
                  [{sp.status === "active" ? "生效中" : "候选"}] {sp.label} ({sp.hash.slice(0, 8)})
                </option>
              ))}
            </select>
          </label>
        ) : null}

        {tier === "heavy" ? (
          <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
            GPU 小时上限
            <input
              value={gpuHours}
              onChange={(e) => setGpuHours(e.target.value)}
              placeholder="24"
              className={`${FIELD} h-7 w-16`}
            />
          </label>
        ) : null}

        {mode === "single" ? (
          <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
            <input
              type="checkbox"
              checked={network}
              onChange={(e) => setNetwork(e.target.checked)}
            />
            服务器可访问外网
          </label>
        ) : null}

        <Button
          size="sm"
          className="ml-auto gap-1"
          disabled={starting}
          onClick={() => void start()}
        >
          {starting ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : mode === "campaign" ? (
            <GitBranch className="size-3.5" />
          ) : (
            <PlayCircle className="size-3.5" />
          )}
          {mode === "campaign" ? "分叉并启动" : "启动"}
        </Button>
      </div>
    </div>
  );
}
