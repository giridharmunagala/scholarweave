import { AppShell } from '../components/layout/AppShell';
import { usePathname, useSearchParams } from '../lib/router';
import { DashboardPage } from '../pages/DashboardPage';
import { NotesPage } from '../pages/NotesPage';
import { PapersPage } from '../pages/PapersPage';
import { RunsPage } from '../pages/RunsPage';
import { SettingsPage } from '../pages/SettingsPage';
import { WorkflowGalleryPage } from '../pages/WorkflowGalleryPage';
import { WorkflowsPage } from '../pages/WorkflowsPage';

export default function App() {
  const pathname = usePathname();
  const [searchParams] = useSearchParams();

  let page = <DashboardPage />;
  if (pathname === '/notes') {
    page = <NotesPage />;
  } else if (pathname === '/papers' || pathname.startsWith('/papers/')) {
    page = <PapersPage />;
  } else if (pathname === '/agents' || pathname === '/workflows') {
    // `?documentId=` deep links from the Papers page open the editor with an agent chooser.
    page = searchParams.get('documentId') ? <WorkflowsPage /> : <WorkflowGalleryPage />;
  } else if (pathname.startsWith('/agents/') || pathname.startsWith('/workflows/')) {
    page = <WorkflowsPage />;
  } else if (pathname === '/runs' || pathname.startsWith('/runs/')) {
    page = <RunsPage />;
  } else if (pathname === '/settings') {
    page = <SettingsPage />;
  }
  return (
    <AppShell>{page}</AppShell>
  );
}
