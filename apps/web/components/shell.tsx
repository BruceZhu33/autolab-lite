"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

const NAV = [
  { href: "/", label: "Board" },
  { href: "/runs", label: "Runs" },
  { href: "/gpus", label: "GPU Servers" },
];

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="dark flex h-screen">
      <aside className="w-52 shrink-0 border-r border-[var(--sidebar-border)] bg-[var(--sidebar)] text-[var(--sidebar-foreground)]">
        <div className="flex h-12 items-center gap-2 border-b border-[var(--sidebar-border)] px-4">
          <span className="inline-block size-4 rounded-[5px] bg-[var(--sidebar-primary)]" />
          <span className="text-sm font-semibold tracking-tight">AutoLab</span>
        </div>
        <nav className="space-y-0.5 p-2 text-[13px]">
          {NAV.map((item) => {
            const active =
              item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`block rounded-md px-2.5 py-1.5 ${
                  active
                    ? "bg-[var(--sidebar-accent)] text-[var(--sidebar-accent-foreground)]"
                    : "hover:bg-[var(--sidebar-hover,var(--sidebar-accent))]/60"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="mt-auto px-4 pb-3 text-[11px] text-[var(--faint-foreground,var(--muted-foreground))]">
          UI derived from Multica (internal use)
        </div>
      </aside>
      <main className="min-w-0 flex-1 overflow-auto bg-[var(--page-canvas)] p-5">
        {children}
      </main>
    </div>
  );
}

export function PageHeader({ title, sub }: { title: string; sub?: string }) {
  return (
    <header className="mb-4">
      <h1 className="text-base font-semibold tracking-tight">{title}</h1>
      {sub ? (
        <p className="mt-0.5 text-xs text-[var(--muted-foreground)]">{sub}</p>
      ) : null}
    </header>
  );
}
