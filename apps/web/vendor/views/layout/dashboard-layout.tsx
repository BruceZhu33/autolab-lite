"use client";

import type { ReactNode } from "react";
import { SidebarInset, SidebarProvider } from "~/vendor/ui/components/ui/sidebar";
import { AppSidebar } from "./app-sidebar";

interface DashboardLayoutProps {
  children: ReactNode;
}

/**
 * Trimmed Multica dashboard shell (07 checklist): keeps the provider/sidebar/
 * inset structure and visuals; cuts DashboardGuard, WorkspacePresencePrefetch,
 * GlobalShortcuts, ModalRegistry, search slot and the nav progress sweep.
 */
export function DashboardLayout({ children }: DashboardLayoutProps) {
  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset>{children}</SidebarInset>
    </SidebarProvider>
  );
}
