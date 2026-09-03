import { useEffect, useRef, useState } from 'react';
import { Icon, ModeIcon } from './Icons';
import { useTheme } from '../theme/ThemeProvider';
import { THEMES, type ThemePreference } from '../theme/themes';

export function ThemeSwitcher() {
  const { preference, definition, setPreference } = useTheme();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const choose = (next: ThemePreference) => {
    setPreference(next);
    setOpen(false);
  };

  return (
    <div className="theme-menu" ref={containerRef}>
      <button
        type="button"
        className="theme-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Theme: ${preference === 'system' ? 'match system' : definition.label}`}
        title="Change theme"
        onClick={() => setOpen((value) => !value)}
      >
        <ModeIcon scheme={preference === 'system' ? 'system' : definition.scheme} />
        <span className="rail-label">Theme</span>
      </button>
      {open ? (
        <div className="theme-popover" role="menu" aria-label="Theme">
          {(['light', 'dark'] as const).map((scheme) => (
            <div className="theme-group" key={scheme}>
              <span className="theme-group-label">{scheme === 'light' ? 'Light' : 'Dark'}</span>
              {THEMES.filter((theme) => theme.scheme === scheme).map((theme) => (
                <button
                  key={theme.id}
                  type="button"
                  role="menuitemradio"
                  aria-checked={preference === theme.id}
                  className="theme-option"
                  onClick={() => choose(theme.id)}
                >
                  <span className="swatch" aria-hidden="true">
                    {theme.swatch.map((color) => (
                      <i key={color} style={{ background: color }} />
                    ))}
                  </span>
                  <span className="stack-tight" style={{ gap: 0 }}>
                    {theme.label}
                    <small className="muted">{theme.description}</small>
                  </span>
                  {preference === theme.id ? <Icon name="check" size={15} className="check" /> : null}
                </button>
              ))}
            </div>
          ))}
          <hr />
          <button
            type="button"
            role="menuitemradio"
            aria-checked={preference === 'system'}
            className="theme-option"
            onClick={() => choose('system')}
          >
            <span className="swatch" aria-hidden="true">
              <i style={{ background: '#f3ebdf' }} />
              <i style={{ background: '#131520' }} />
            </span>
            <span className="stack-tight" style={{ gap: 0 }}>
              Match system
              <small className="muted">Follow the OS setting</small>
            </span>
            {preference === 'system' ? <Icon name="check" size={15} className="check" /> : null}
          </button>
        </div>
      ) : null}
    </div>
  );
}
