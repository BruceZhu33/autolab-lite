"use client";

import { get, post } from "./client";

export type StageKey = "setup" | "baseline" | "research";
export type StageState = "done" | "active" | "attention" | "pending";
export type StageCell = { state: StageState; summary: string; link: string };

export type BoardCard = {
  projectId: string;
  title: string;
  stage: StageKey;
  stages: Record<StageKey, StageCell>;
  needsYou: boolean;
  updatedAt: string;
};

export type Overview = {
  running: number;
  queued: number;
  failed: number;
  cancelling: number;
  servers: Array<{ id: string; name: string; status_hint: string | null; gpu_slots: number }>;
  projects: Array<{ id: string; name: string }>;
};

export type ModelSettings = { configured: boolean; baseUrl: string; model: string };

export type Account = { id: string; name: string; token: string; created_at: string; revoked: boolean };

export type WhoAmI = { kind: "owner" | "agent" | "member"; name: string; identity: string };

export type FsListing = {
  path: string;
  parent: string | null;
  entries: Array<{ name: string; path: string }>;
};

export const fetchOverview = () => get<Overview>("/api/overview");
export const fetchBoard = () => get<BoardCard[]>("/api/board");
export const whoami = () => get<WhoAmI>("/api/whoami");
export const listAccounts = () => get<Account[]>("/api/owner/accounts");
export const createAccount = (name: string) => post<Account>("/api/owner/accounts", { name });
export const revokeAccount = (id: string) => post<{ ok: boolean }>(`/api/owner/accounts/${id}/revoke`, {});
export const fetchModelSettings = () => get<ModelSettings>("/api/settings/model");
export const saveModelSettings = (input: { api_key: string; base_url?: string; model?: string }) =>
  post<ModelSettings>("/api/owner/settings/model", input);

/** 列 Control 所在机器(你的电脑)的目录。 */
export const browseLocalFs = (path: string) =>
  get<FsListing>(`/api/owner/fs/browse?path=${encodeURIComponent(path)}`);

/** 列 GPU 服务器上的目录(local 服务器即本机)。 */
export const browseServerFs = (serverId: string, path: string) =>
  get<FsListing>(`/api/owner/servers/${serverId}/fs/browse?path=${encodeURIComponent(path)}`);
