import { useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';

export type WidthBounds = { value: number; min: number; max: number };

export function clampWidth(width: number, bounds: WidthBounds): number {
  return Math.round(Math.min(bounds.max, Math.max(bounds.min, width)));
}

export function readStoredWidth(key: string, bounds: WidthBounds): number {
  if (typeof window === 'undefined') return bounds.value;
  const stored = Number(window.localStorage.getItem(key));
  return Number.isFinite(stored) && stored > 0 ? clampWidth(stored, bounds) : bounds.value;
}

export function storeWidth(key: string, width: number) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(key, String(width));
  } catch {
    /* A full or blocked store only costs the remembered width, never the session. */
  }
}

export function PaneResizer({
  side, label, width, bounds, onResize, onResizingChange,
}: {
  side: 'left' | 'right';
  label: string;
  width: number;
  bounds: WidthBounds;
  onResize: (width: number) => void;
  onResizingChange: (resizing: boolean) => void;
}) {
  const [active, setActive] = useState(false);
  const originRef = useRef({ pointer: 0, width });
  const finish = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!active) return;
    setActive(false);
    onResizingChange(false);
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  return (
    <div
      className={`pane-resizer ${side}${active ? ' active' : ''}`}
      role="separator"
      tabIndex={0}
      aria-orientation="vertical"
      aria-label={label}
      title="Drag to resize. Use arrow keys when focused, or double-click to reset."
      aria-valuenow={width}
      aria-valuemin={bounds.min}
      aria-valuemax={bounds.max}
      onPointerDown={(event) => {
        event.preventDefault();
        event.currentTarget.setPointerCapture?.(event.pointerId);
        originRef.current = { pointer: event.clientX, width };
        setActive(true);
        onResizingChange(true);
      }}
      onPointerMove={(event) => {
        if (!active) return;
        const delta = event.clientX - originRef.current.pointer;
        onResize(clampWidth(originRef.current.width + (side === 'right' ? delta : -delta), bounds));
      }}
      onPointerUp={finish}
      onPointerCancel={finish}
      onLostPointerCapture={finish}
      onDoubleClick={() => onResize(clampWidth(bounds.value, bounds))}
      onKeyDown={(event) => {
        const step = event.shiftKey ? 48 : 16;
        if (event.key === 'ArrowLeft') {
          event.preventDefault();
          onResize(clampWidth(width + (side === 'right' ? -step : step), bounds));
        } else if (event.key === 'ArrowRight') {
          event.preventDefault();
          onResize(clampWidth(width + (side === 'right' ? step : -step), bounds));
        } else if (event.key === 'Home') {
          event.preventDefault();
          onResize(clampWidth(bounds.value, bounds));
        }
      }}
    />
  );
}
