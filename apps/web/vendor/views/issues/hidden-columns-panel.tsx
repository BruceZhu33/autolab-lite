"use client";

import { Eye, MoreHorizontal } from "lucide-react";
import type { IssueStatus } from "./types";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "~/vendor/ui/components/ui/dropdown-menu";
import { StatusIcon } from "./status-icon";
import { statusCatalog, statusLabelOf } from "./status";
import { useViewStoreApi } from "./view-store";

/**
 * 「已隐藏列」侧板:隐藏列仍可从这里恢复显示。
 */
export function HiddenColumnsPanel({
  hiddenStatuses,
  renderRow,
}: {
  hiddenStatuses: IssueStatus[];
  renderRow: (status: IssueStatus) => React.ReactNode;
}) {
  return (
    <div className="flex w-[240px] shrink-0 flex-col">
      <div className="mb-2 flex items-center gap-2 px-1">
        <span className="text-body font-medium text-muted-foreground">已隐藏的列</span>
      </div>
      <div className="flex-1 space-y-0.5">
        {hiddenStatuses.map((status) => renderRow(status))}
      </div>
    </div>
  );
}

export function HiddenColumnRow({
  status,
  total,
}: {
  status: IssueStatus;
  total?: number;
}) {
  const viewStoreApi = useViewStoreApi();
  return (
    <div className="flex items-center justify-between rounded-lg px-2.5 py-2 hover:bg-muted/50">
      <div className="flex items-center gap-2">
        <StatusIcon
          category={statusCatalog.categoryOf(status)}
          color={statusCatalog.colorOf(status)}
          icon={statusCatalog.iconOf(status)}
          status={status}
          className="h-3.5 w-3.5"
        />
        <span className="text-body">{statusLabelOf(status)}</span>
      </div>
      <div className="flex items-center gap-1.5">
        {total !== undefined && (
          <span className="text-caption text-muted-foreground">{total}</span>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label="显示此列"
                className="rounded-full text-muted-foreground"
              >
                <MoreHorizontal className="size-3.5" />
              </Button>
            }
          />
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={() => viewStoreApi.getState().showStatus(status)}>
              <Eye className="size-3.5" />
              显示此列
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}
