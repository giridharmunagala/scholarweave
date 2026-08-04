import { useEffect, useId, useRef, type ReactNode } from 'react';
import { Icon } from './Icon';

interface ModalProps {
  title: string;
  description?: string;
  children: ReactNode;
  onClose: () => void;
  className?: string;
  labelledBy?: string;
}

/**
 * A compact, dependency-free modal shell shared by protected authoring tasks.
 * It restores focus, supports Escape/backdrop dismissal, and keeps Tab navigation inside.
 */
export function Modal({ title, description, children, onClose, className = '', labelledBy }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const closeRef = useRef(onClose);
  const generatedId = useId();
  const titleId = labelledBy || `modal-title-${generatedId}`;
  closeRef.current = onClose;

  useEffect(() => {
    previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    const first = dialog?.querySelector<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    requestAnimationFrame(() => first?.focus());

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== 'Tab' || !dialog) return;
      const focusable = [...dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )];
      if (!focusable.length) return;
      const firstFocus = focusable[0];
      const lastFocus = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === firstFocus) {
        event.preventDefault();
        lastFocus.focus();
      } else if (!event.shiftKey && document.activeElement === lastFocus) {
        event.preventDefault();
        firstFocus.focus();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      previousFocus.current?.focus();
    };
  }, []);

  return (
    <div className="modal-backdrop authoring-modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={dialogRef} className={`modal authoring-modal ${className}`} role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className="authoring-modal-header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description ? <p>{description}</p> : null}
          </div>
          <button type="button" className="button subtle icon-only sm" onClick={onClose} aria-label={`Close ${title}`}>
            <Icon name="close" size={15} />
          </button>
        </header>
        {children}
      </div>
    </div>
  );
}
