import { useRef, useState } from 'react';
import { pickLocalPath } from '../../api/pathPicker';

export function LocalPathPicker({ label, kind, value, onChange, onBusyChange }: {
  label: string;
  kind: 'file' | 'folder';
  value: string;
  onChange: (path: string) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const active = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const browse = async () => {
    if (active.current) return;
    active.current = true;
    setBusy(true); setError('');
    onBusyChange?.(true);
    try {
      const path = await pickLocalPath(kind);
      if (path !== null) onChange(path);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      active.current = false;
      setBusy(false);
      onBusyChange?.(false);
    }
  };
  return <div>
    <label>{label}<input readOnly value={value} placeholder={`Choose a ${kind} with Browse`} /></label>
    <button type="button" className="button secondary" disabled={busy} onClick={() => void browse()}>
      {busy ? 'Picking…' : 'Browse…'}
    </button>
    {busy && <p role="status">Choose a {kind} in the file manager on the backend desktop, or cancel. The dialog closes after two minutes.</p>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
