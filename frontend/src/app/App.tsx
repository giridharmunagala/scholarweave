import { lazy, Suspense } from 'react';
import { AppShell } from './AppShell';
import { useLocation } from './router';
import { Loading } from '../shared/components/Ui';

const ChatPage = lazy(() => import('../features/chat/ChatPage'));
const DeepWorkPage = lazy(() => import('../features/chat/DeepWorkPage'));
const PapersPage = lazy(() => import('../features/documents/PapersPage'));
const WorkspacePage = lazy(() => import('../features/workspace/WorkspacePage'));
const SettingsPage = lazy(() => import('../features/providers/SettingsPage'));

export default function App() {
  const { pathname } = useLocation();
  let page = <ChatPage />;
  if (pathname.startsWith('/deep-work')) page = <DeepWorkPage />;
  else if (pathname.startsWith('/library/notes') || pathname.startsWith('/workspace')) page = <WorkspacePage />;
  else if (pathname.startsWith('/library') || pathname.startsWith('/papers')) page = <PapersPage />;
  else if (pathname === '/settings') page = <SettingsPage />;

  return (
    <AppShell>
      <Suspense fallback={<Loading label="Loading page…" />}>{page}</Suspense>
    </AppShell>
  );
}
