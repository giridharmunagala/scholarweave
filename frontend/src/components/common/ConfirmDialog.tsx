import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Icon } from './Icon';

interface ConfirmOptions {
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: 'danger' | 'default';
}

type Resolver = (confirmed: boolean) => void;

const ConfirmContext = createContext<((options: ConfirmOptions) => Promise<boolean>) | null>(null);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [request, setRequest] = useState<ConfirmOptions | null>(null);
  const resolver = useRef<Resolver | null>(null);
  const confirmButton = useRef<HTMLButtonElement | null>(null);

  const settle = useCallback((confirmed: boolean) => {
    resolver.current?.(confirmed);
    resolver.current = null;
    setRequest(null);
  }, []);

  const confirm = useCallback((options: ConfirmOptions) => {
    setRequest(options);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  useEffect(() => {
    if (!request) return undefined;
    confirmButton.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        settle(false);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [request, settle]);

  const value = useMemo(() => confirm, [confirm]);

  return (
    <ConfirmContext.Provider value={value}>
      {children}
      {request ? (
        <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && settle(false)}>
          <div className="modal" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title">
            <div className="modal-head">
              <span className="modal-head-icon">
                <Icon name={request.tone === 'default' ? 'info' : 'alert'} size={17} />
              </span>
              <div>
                <h2 id="confirm-title">{request.title}</h2>
                {request.description ? <p>{request.description}</p> : null}
              </div>
            </div>
            <div className="button-row end">
              <button type="button" className="button subtle" onClick={() => settle(false)}>
                {request.cancelLabel || 'Cancel'}
              </button>
              <button
                type="button"
                ref={confirmButton}
                className={request.tone === 'default' ? 'button primary' : 'button danger'}
                onClick={() => settle(true)}
              >
                {request.confirmLabel || 'Delete'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </ConfirmContext.Provider>
  );
}

export function useConfirm() {
  const context = useContext(ConfirmContext);
  if (!context) {
    throw new Error('useConfirm must be used inside a ConfirmProvider');
  }
  return context;
}
