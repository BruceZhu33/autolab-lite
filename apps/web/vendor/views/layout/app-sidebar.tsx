"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTheme } from "next-themes";
import {
  BarChart3, Bot, ChevronDown, Circle, CircleDot, Compass, FlaskConical,
  FolderKanban, GitBranch, Gpu, ListChecks, MessageCircle, Moon, Repeat,
  Search, Settings, Sparkles, Sun, User,
} from "lucide-react";

import {
  Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent,
  SidebarGroupLabel, SidebarHeader, SidebarMenu, SidebarMenuBadge,
  SidebarMenuButton, SidebarMenuItem, SidebarSeparator,
} from "~/vendor/ui/components/ui/sidebar";

/**
 * 用户设计稿的侧栏(part 1/4):工作区切换器、搜索槽、New 入口、
 * 个人导航(Chat、GPU Servers,不属于工作流)、Work 分组(研究流程视图)、
 * AI Team 分组、底部 Analytics/Settings/用户。
 * 未接线的项禁用并标「待接线」,不假装可用。
 */

/** 个人导航:不属于 Work 研究工作流的全局入口(对应原 personalNav)。 */
const PERSONAL = [
  { label: "Chat", icon: MessageCircle, href: "/chat", key: "chat" },
  { label: "GPU Servers", icon: Gpu, href: "/gpus", key: "gpus" },
];

/** Work:研究流程;Baseline/方向/项目是独立页面,其余下钻看板对应列。 */
const WORK = [
  { label: "实验看板", icon: ListChecks, href: "/", key: "board" },
  { label: "项目", icon: FolderKanban, href: "/projects", key: "projects" },
  { label: "Baseline", icon: CircleDot, href: "/baseline", key: "baseline" },
  { label: "方向", icon: Compass, href: "/review", key: "review" },
  { label: "Iteration", icon: Repeat, href: "/iteration", key: "iteration" },
  { label: "Confirm", icon: Circle, href: "/?stage=confirm", key: "confirm", stage: "confirm" },
  { label: "Done", icon: FlaskConical, href: "/?stage=done", key: "done", stage: "done" },
];

/** AI Team:Agents 已可用(Claude Code + GLM + 提示词);Skills 待接线。 */
const AI_TEAM = [
  { label: "Agents", icon: Bot, href: "/agents", key: "agents" },
  { label: "Skills", icon: Sparkles, key: "skills" },
];

export function AppSidebar({ agentsWorking = 0 }: { agentsWorking?: number }) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const stageParam = searchParams.get("stage");
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  // 带阶段的项按 pathname+stage 精确点亮;实验看板在无下钻时点亮。
  const active = (href?: string, stage?: string) => {
    if (!href) return false;
    if (stage) return pathname === "/" && stageParam === stage;
    if (href === "/") return pathname === "/" && !stageParam;
    return pathname.startsWith(href);
  };

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" tooltip="AutoLab">
              <div className="flex size-8 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
                <BarChart3 className="size-4" />
              </div>
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-semibold">AutoLab</span>
                <span className="truncate text-xs text-muted-foreground">个人工作区</span>
              </div>
              <ChevronDown className="ml-auto size-4 text-muted-foreground" />
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton size="sm" disabled tooltip="搜索待接线">
              <Search className="size-4" />
              <span className="text-muted-foreground">Search…</span>
              <SidebarMenuBadge>⌘K</SidebarMenuBadge>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              {PERSONAL.map((item) => (
                <SidebarMenuItem key={item.key}>
                  <SidebarMenuButton
                    isActive={active(item.href)}
                    tooltip={item.href ? item.label : `${item.label} 待接线`}
                    disabled={!item.href}
                    onClick={item.href ? () => router.push(item.href!) : undefined}
                  >
                    <item.icon />
                    <span>{item.label}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup>
          <SidebarGroupLabel>Work</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {WORK.map((item) => (
                <SidebarMenuItem key={item.key}>
                  <SidebarMenuButton
                    isActive={active(item.href, (item as { stage?: string }).stage)}
                    tooltip={item.label}
                    onClick={() => router.push(item.href!)}
                  >
                    <item.icon />
                    <span>{item.label}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={active("/runs")} tooltip="Runs"
                  onClick={() => router.push("/runs")}
                >
                  <GitBranch />
                  <span>Runs</span>
                  {agentsWorking > 0 ? <SidebarMenuBadge>{agentsWorking}</SidebarMenuBadge> : null}
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup>
          <SidebarGroupLabel>AI Team</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {AI_TEAM.map((item) => (
                <SidebarMenuItem key={item.key}>
                  <SidebarMenuButton
                    isActive={active(item.href)}
                    tooltip={item.href ? item.label : `${item.label} 待接线`}
                    disabled={!item.href}
                    onClick={item.href ? () => router.push(item.href!) : undefined}
                  >
                    <item.icon />
                    <span>{item.label}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter className="gap-1 pb-2.5">
        <SidebarSeparator className="mb-1" />
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton disabled tooltip="Analytics 待接线">
              <BarChart3 />
              <span>Analytics</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              tooltip="切换深浅色"
              onClick={() => setTheme(mounted && resolvedTheme === "dark" ? "light" : "dark")}
            >
              {mounted && resolvedTheme === "dark" ? <Sun /> : <Moon />}
              <span>{mounted && resolvedTheme === "dark" ? "浅色模式" : "深色模式"}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              tooltip="Settings" onClick={() => router.push("/settings")}
            >
              <Settings />
              <span>Settings</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem className="mt-1.5">
            <SidebarMenuButton size="sm" tooltip="owner（本地会话）">
              <span className="flex size-5 items-center justify-center rounded-full bg-sidebar-accent text-micro font-semibold">
                O
              </span>
              <span className="truncate">owner</span>
              <User className="ml-auto size-3.5 text-muted-foreground" />
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  );
}
