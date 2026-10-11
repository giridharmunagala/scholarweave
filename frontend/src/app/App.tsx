import { lazy, Suspense } from 'react';
import { AppShell } from './AppShell';
import { useLocation } from './router';
import { Loading } from '../shared/components/Ui';

const ChatPage = lazy(() => import('../features/chat/ChatPage'));
const DeepWorkPage = lazy(() => import('../features/deep-work/DeepWorkPage'));
const PapersPage = lazy(() => import('../features/documents/PapersPage'));
const WorkspacePage = lazy(() => import('../features/workspace/WorkspacePage'));
const SettingsPage = lazy(() => import('../features/providers/SettingsPage'));
const TracksPage = lazy(() => import('../features/tracks/TracksPage'));
const BriefPage = lazy(() => import('../features/brief/BriefPage'));

export default function App() {
  const { pathname, search } = useLocation();
  let page = <ChatPage />;
  if (pathname.startsWith('/library/notes') || pathname.startsWith('/workspace')) page = <WorkspacePage />;
  else if (pathname.startsWith('/library') || pathname.startsWith('/papers')) page = <PapersPage />;
  else if (pathname === '/settings') page = <SettingsPage />;
  else if (pathname.startsWith('/tracks')) page = <TracksPage />;
  else if (pathname.startsWith('/brief')) page = <BriefPage />;
  else if (pathname.startsWith('/deep-work') || new URLSearchParams(search).has('reviews')) page = <DeepWorkPage />;

  return (
    <AppShell>
      <Suspense fallback={<Loading label="Loading page…" />}>{page}</Suspense>
    </AppShell>
  );
}
