import { notFound } from 'next/navigation';
import RunDetailView from '@/features/workspace/components/run-detail-view';
import { getRun } from '@/features/workspace/fixtures';

export default async function RunDetailPage({
  params
}: {
  params: Promise<{ projectId: string; runId: string }>;
}) {
  const { runId } = await params;
  const run = getRun(runId);
  if (!run) notFound();
  return <RunDetailView run={run} />;
}
