import type { Metadata } from "next";
import "./globals.css";
import { QueryClientProvider } from "@tanstack/react-query";
import { DashboardLayout } from "@/vendor/views/layout/dashboard-layout";
import { queryClient } from "@/lib/query";

export const metadata: Metadata = {
  title: "AutoLab",
  description: "Personal GPU experiment workbench",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body>
        <QueryClientProvider client={queryClient}>
          <DashboardLayout>{children}</DashboardLayout>
        </QueryClientProvider>
      </body>
    </html>
  );
}
