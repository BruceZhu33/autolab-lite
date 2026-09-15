import { notFound } from 'next/navigation';
import WorkspaceView from '@/features/workspace/components/workspace-view';
import { getWorkspace } from '@/features/workspace/fixtures';

export default async function WorkspacePage({
  params
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = await params;
  const fixture = getWorkspace(projectId);
  if (!fixture) notFound();
  return <WorkspaceView fixture={fixture} />;
}
