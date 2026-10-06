import type { IssueStatus } from "./types";
import { StatusIcon } from "./status-icon";
import { statusCatalog, statusLabelOf } from "./status";

export function StatusHeading({
  status,
  count,
}: {
  status: IssueStatus;
  count: number;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="inline-flex items-center gap-1.5 text-caption font-semibold">
        <StatusIcon
          category={statusCatalog.categoryOf(status)}
          color={statusCatalog.colorOf(status)}
          icon={statusCatalog.iconOf(status)}
          status={status}
          className="h-3 w-3"
        />
        {statusLabelOf(status)}
      </span>
      <span className="text-caption text-muted-foreground">{count}</span>
    </div>
  );
}
