import { titleCase } from '../../lib/format';

const intentMap: Record<string, string> = {
  uploaded: 'muted',
  ready: 'success',
  running: 'info',
  pending: 'warning',
  completed: 'success',
  failed: 'danger',
  cancelled: 'muted',
  skipped: 'warning',
  ok: 'success',
  healthy: 'success',
  degraded: 'warning',
};

export function StatusBadge({ status }: { status: string }) {
  return <span className={`status-badge ${intentMap[status] || 'muted'}`}>{titleCase(status)}</span>;
}
