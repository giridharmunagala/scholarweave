import { lazy, Suspense } from 'react';
import { AppShell } from './AppShell';
import { useLocation } from './router';
import { Loading } from '../shared/components/Ui';

const ChatPage = lazy(() => import('../features/chat/ChatPage'));
const ToolsPage = lazy(() => import('../features/tools/ToolsPage'));
const RunsPage = lazy(() => import('../features/runs/RunsPage'));
const PapersPage = lazy(() => import('../features/documents/PapersPage'));
const WorkspacePage = lazy(() => import('../features/workspace/WorkspacePage'));
const SettingsPage = lazy(() => import('../features/providers/SettingsPage'));

export default function App() {
  const { pathname } = useLocation();
  let page = <ChatPage />;
  if (pathname === '/tools') page = <ToolsPage />;
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
