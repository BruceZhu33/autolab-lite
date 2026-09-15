"use client";

import { use } from "react";
import { IssueDetailRoute } from "@multica/views/issues/components";
import { ErrorBoundary } from "@multica/ui/components/common/error-boundary";
import { AutolabStageGate } from "./autolab-stage-gate";

export default function IssueDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <ErrorBoundary resetKeys={[id]}>
      {/* AutoLab 阶段过门操作条：显式主操作，替代藏在 Properties 里的状态选择器 */}
      <AutolabStageGate issueId={id} />
      <IssueDetailRoute routeId={id} />
    </ErrorBoundary>
  );
}
