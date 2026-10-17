"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTheme } from "next-themes";
import {
  BarChart3, BookOpen, ChevronDown, FlaskConical, FolderKanban, FolderPlus,
  GitBranch, Gpu, Lightbulb, ListChecks, Moon, Search, Settings, Sun, User,
} from "lucide-react";

import {
  Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent,
  SidebarGroupLabel, SidebarHeader, SidebarMenu, SidebarMenuBadge,
  SidebarMenuButton, SidebarMenuItem, SidebarSeparator,
} from "~/vendor/ui/components/ui/sidebar";

/**
 * 侧栏:Work 分组(立项 → Baseline → 研究 三段工作流)、全局入口
 * (实验运行 / GPU 服务器)、底部 Analytics/Settings/用户。
 * 未接线的项禁用并标「待接线」,不假装可用。
 */

/** 全局入口:研究与工作流之外的工具页。 */
const PERSONAL = [
  { label: "实验运行", icon: GitBranch, href: "/runs", key: "runs" },
  { label: "知识库", icon: BookOpen, href: "/kb", key: "kb" },
  { label: "知识空间", icon: FolderPlus, href: "/spaces", key: "spaces" },
  { label: "GPU 服务器", icon: Gpu, href: "/gpus", key: "gpus" },
];

/** Work:研究流程三段——立项 → Baseline → 研究。 */
const WORK = [
  { label: "实验看板", icon: ListChecks, href: "/", key: "board" },
  { label: "项目", icon: FolderKanban, href: "/projects", key: "projects" },
  { label: "Baseline", icon: FlaskConical, href: "/baseline", key: "baseline" },
  { label: "研究", icon: Lightbulb, href: "/research", key: "research" },
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
