import { lazy, Suspense } from 'react';
import { AppShell } from './AppShell';
import { useLocation } from './router';
import { Loading } from '../shared/components/Ui';

const DashboardPage = lazy(() => import('../features/dashboard/DashboardPage'));
const ChatPage = lazy(() => import('../features/chat/ChatPage'));
const AgentsGalleryPage = lazy(() => import('../features/agents/gallery/AgentsGalleryPage'));
const AgentEditorPage = lazy(() => import('../features/agents/editor/AgentEditorPage'));
const ToolsPage = lazy(() => import('../features/tools/ToolsPage'));
const RunsPage = lazy(() => import('../features/runs/RunsPage'));
const PapersPage = lazy(() => import('../features/documents/PapersPage'));
const WorkspacePage = lazy(() => import('../features/workspace/WorkspacePage'));
const SettingsPage = lazy(() => import('../features/providers/SettingsPage'));

export default function App() {
  const { pathname } = useLocation();
  let page = <DashboardPage />;
  if (pathname === '/chat') page = <ChatPage />;
  else if (pathname === '/agents') page = <AgentsGalleryPage />;
  else if (pathname.startsWith('/agents/')) page = <AgentEditorPage />;
  else if (pathname === '/tools') page = <ToolsPage />;
  else if (pathname.startsWith('/runs')) page = <RunsPage />;
  else if (pathname.startsWith('/papers')) page = <PapersPage />;
  else if (pathname.startsWith('/workspace')) page = <WorkspacePage />;
  else if (pathname === '/settings') page = <SettingsPage />;

  return (
    <AppShell>
      <Suspense fallback={<Loading label="Loading page…" />}>{page}</Suspense>
    </AppShell>
  );
}
