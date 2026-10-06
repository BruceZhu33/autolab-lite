"use client";

/**
 * 本地视图状态垫片(替代原 useViewStore 内核):只保留看板需要的
 * grouping/sortBy/卡片字段可见性/隐藏列。UI 是投影,这里不保存任何业务状态。
 */
import { create } from "zustand";
import type { IssueStatus } from "./types";

export interface CardPropertyVisibility {
  description: boolean;
  labels: boolean;
  childProgress: boolean;
}

interface ViewStore {
  grouping: "status";
  sortBy: "position";
  cardProperties: CardPropertyVisibility;
  hiddenStatuses: IssueStatus[];
  hideStatus: (status: IssueStatus) => void;
  showStatus: (status: IssueStatus) => void;
}

export const useViewStore = create<ViewStore>((set) => ({
  grouping: "status",
  sortBy: "position",
  cardProperties: { description: true, labels: true, childProgress: true },
  hiddenStatuses: [],
  hideStatus: (status) =>
    set((s) =>
      s.hiddenStatuses.includes(status)
        ? s
        : { ...s, hiddenStatuses: [...s.hiddenStatuses, status] },
    ),
  showStatus: (status) =>
    set((s) => ({ ...s, hiddenStatuses: s.hiddenStatuses.filter((x) => x !== status) })),
}));

/** 与原 useViewStoreApi 等价:返回带 getState() 的 store 对象。 */
export const useViewStoreApi = () => useViewStore;
