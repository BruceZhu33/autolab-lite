"use client";

import { get, post, request } from "./client";
import type { StageCell, StageKey } from "./system";

/** 基线版本徽标:一眼分清项目现在拿什么当对比对象 */
export type BaselineBadge = {
  version: string;       // v1 / v2 …
  label: string;         // 原始基线 / EMA-PVL / …
  kind: "confirmed" | "adopted" | "forked";
  kindLabel: string;     // 原始 / 采纳 / 分叉
  date: string;          // YYYY-MM-DD
  forkedFrom?: string;   // 分叉来源项目名
};

export type Project = {
  id: string;
  name: string;
  server: { id: string; name: string; transport_mode: string };
  workdir: string;
  output_root: string;
  githubUrl: string | null;
  localWorkspace: string | null;
  papersCount: number;
  baselinePassedAt: string | null;
  baseline?: BaselineBadge | null;
  stage: StageKey;
  stages: Record<StageKey, StageCell>;
  needsYou: boolean;
  updated_at: string;
};

export type ServerOption = {
  id: string;
  name: string;
  transport_mode: string;
  gpu_slots: number;
};

export type NewProject =
  | { name: string; server_id: string; source: { kind: "dir"; path: string }; local_workspace?: string }
  | { name: string; server_id: string; source: { kind: "github"; url: string }; local_workspace?: string };

/** 基线版本(研究分叉用):key = current / 历史序号字符串 */
export type BaselineVersion = {
  key: string;
  label: string;
  passed_at: string | null;
  run_id: string | null;
  datasets: string[];
};

export const fetchBaselineVersions = (projectId: string) =>
  get<{ versions: BaselineVersion[] }>(`/api/projects/${projectId}/baseline/versions`);

/** 研究分叉:从现有项目的一个基线版本开新方向(worktree 检出代码,数据链接共享,基线继承)。 */
export const forkProject = (input: {
  source_project_id: string; name: string; baseline_version?: string;
}) => post<{ project: Project; code_commit: string }>("/api/owner/projects/fork", input);

export const fetchProjects = () => get<Project[]>("/api/projects");
export const fetchServerOptions = () => get<ServerOption[]>("/api/servers");

export const createProject = (input: NewProject) =>
  post<{ project: Project }>("/api/owner/projects", input);

export function createProjectFromZip(name: string, serverId: string, file: File) {
  const form = new FormData();
  form.append("name", name);
  form.append("server_id", serverId);
  form.append("file", file);
  return post<{ project: Project }>("/api/owner/projects/upload", form);
}

export const updateProject = (
  id: string, input: { github_url?: string; workdir?: string; local_workspace?: string },
) => request<{ project: Project }>(`/api/owner/projects/${id}`, { method: "PATCH", body: input });

export const deleteProject = (id: string) =>
  request<{ deleted: string; name: string }>(`/api/owner/projects/${id}`, { method: "DELETE" });

export const syncProjectCode = (id: string, source?: "github" | "local") =>
  post<{ source: string; detail: string }>(`/api/owner/projects/${id}/code/sync`,
                                           source ? { source } : {});

export const fetchPapers = (id: string) => get<string[]>(`/api/projects/${id}/papers`);

export function uploadPaper(id: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  return post<{ path: string; size: number }>(`/api/owner/projects/${id}/papers`, form);
}

/** 服务器上已有的数据/权重目录 → 链接到工作区 target。 */
export const linkAsset = (id: string, targetPath: string, serverPath: string) =>
  post<{ target: string }>(`/api/owner/projects/${id}/assets/link`,
                           { target_path: targetPath, server_path: serverPath });

/** 上传 zip 解包到工作区 target。 */
export function uploadAsset(id: string, targetPath: string, file: File) {
  const form = new FormData();
  form.append("target_path", targetPath);
  form.append("file", file);
  return post<{ target: string }>(`/api/owner/projects/${id}/assets/upload`, form);
}
