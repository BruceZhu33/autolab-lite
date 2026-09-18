"use client";

import { usePathname, useRouter } from "next/navigation";
import { FlaskConical, Gpu, ListChecks, Settings } from "lucide-react";

import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarFooter,
} from "~/vendor/ui/components/ui/sidebar";

/**
 * Trimmed Multica app sidebar: same primitives and density as the original
 * (collapsible, keyboard toggle, tooltips), menu reduced per the 07 checklist
 * to Board / Runs / GPU Servers / Settings. Member/Agent/Inbox/search/help
 * sections were cut together with their @multica/core dependencies.
 */

export const NAV_ITEMS = [
  { href: "/", label: "Board", icon: ListChecks, key: "board" },
  { href: "/runs", label: "Runs", icon: FlaskConical, key: "runs" },
  { href: "/gpus", label: "GPU Servers", icon: Gpu, key: "gpus" },
  { href: "/settings", label: "Settings", icon: Settings, key: "settings" },
] as const;

export function AppSidebar({ running = 0 }: { running?: number }) {
  const pathname = usePathname();
  const router = useRouter();
  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" onClick={() => router.push("/")}>
              <div className="flex size-8 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
                <FlaskConical className="size-4" />
              </div>
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-semibold">AutoLab</span>
                <span className="truncate text-xs text-muted-foreground">
                  UI derived from Multica
                </span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Workspace</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {NAV_ITEMS.map((item) => {
                const active =
                  item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
                return (
                  <SidebarMenuItem key={item.key}>
                    <SidebarMenuButton
                      isActive={active}
                      tooltip={item.label}
                      onClick={() => router.push(item.href)}
                    >
                      <item.icon />
                      <span>{item.label}</span>
                    </SidebarMenuButton>
                    {item.key === "runs" && running > 0 ? (
                      <SidebarMenuBadge>{running}</SidebarMenuBadge>
                    ) : null}
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter />
    </Sidebar>
  );
}
