import { useEffect, useRef, type ReactNode } from 'react';
import { IconButton } from './Ui';
import './settings-drawer.css';

export function SettingsDrawer({ id, title, open, onClose, children }: {
  id: string;
  title: string;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const element = dialog.current;
    if (!open || !element) return;
    const returnFocus = document.activeElement;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    element.showModal();
    return () => {
      element.close();
      document.body.style.overflow = overflow;
      if (returnFocus instanceof HTMLElement && returnFocus.isConnected) returnFocus.focus();
    };
  }, [open]);

  return (
    <dialog ref={dialog} id={id} className="settings-drawer" aria-label={title}
      onCancel={(event) => { event.preventDefault(); onClose(); }}
      onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="settings-drawer-panel">
        <header className="settings-drawer-header">
          <h2>{title}</h2>
          <IconButton icon="close" label={`Close ${title.toLowerCase()}`} onClick={onClose} />
        </header>
        <div className="settings-drawer-body">{children}</div>
      </div>
    </dialog>
  );
}
