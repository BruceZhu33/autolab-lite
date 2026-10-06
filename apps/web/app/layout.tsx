import type { Metadata } from "next";
import "./globals.css";
import { QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "@/components/theme-provider";
import { DashboardLayout } from "@/vendor/views/layout/dashboard-layout";
import { Toaster } from "@/vendor/ui/components/ui/sonner";
import { queryClient } from "@/lib/query";

export const metadata: Metadata = {
  title: "AutoLab",
  description: "Personal GPU experiment workbench",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>
        <ThemeProvider>
          <QueryClientProvider client={queryClient}>
            <DashboardLayout>{children}</DashboardLayout>
            <Toaster position="top-center" />
          </QueryClientProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
