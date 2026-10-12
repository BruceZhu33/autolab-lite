"use client";

/** 阶段页的项目切换:?project= 预选(看板跳转带过来),多项目时显示标签。 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchProjects, type Project } from "@/lib/api";

export function useProjectSelection(filter: (p: Project) => boolean) {
  const { data: all = [], isLoading, error } = useQuery({
    queryKey: ["projects"], queryFn: fetchProjects, refetchInterval: 8000,
  });
  const [selectedId, setSelectedId] = useState("");
  useEffect(() => {
    const fromUrl = new URLSearchParams(window.location.search).get("project");
    if (fromUrl) setSelectedId(fromUrl);
  }, []);
  const projects = all.filter(filter);
  const selected = projects.find((p) => p.id === selectedId) ?? projects[0] ?? null;
  return { projects, selected, select: setSelectedId, isLoading, error };
}

export function ProjectTabs({ projects, selected, onSelect }: {
  projects: Project[]; selected: Project | null; onSelect: (id: string) => void;
}) {
  if (projects.length < 2) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {projects.map((p) => (
        <button key={p.id} type="button" onClick={() => onSelect(p.id)}
                className={`rounded-full px-3 py-1 text-caption font-medium transition-colors ${
                  p.id === selected?.id ? "bg-accent text-accent-foreground"
                    : "bg-muted/50 text-muted-foreground hover:bg-muted"}`}>
          {p.name}
          {p.needsYou ? <span className="ml-1.5 inline-block size-1.5 rounded-full bg-warning" /> : null}
        </button>
      ))}
    </div>
  );
}
