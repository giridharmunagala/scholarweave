import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  applyTheme,
  prefersDark,
  readStoredPreference,
  resolveTheme,
  THEMES,
  type ThemeDefinition,
  type ThemeId,
  type ThemePreference,
} from './themes';

interface ThemeState {
  /** What the user picked, which may be `system`. */
  preference: ThemePreference;
  /** The theme actually painted right now. */
  theme: ThemeId;
  definition: ThemeDefinition;
  setPreference: (preference: ThemePreference) => void;
  /** Jump between the light and dark theme of the current family. */
  toggle: () => void;
}

const ThemeContext = createContext<ThemeState | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(() => readStoredPreference());
  const [theme, setTheme] = useState<ThemeId>(() => resolveTheme(readStoredPreference()));

  useEffect(() => {
    setTheme(applyTheme(preference));
  }, [preference]);

  useEffect(() => {
    if (preference !== 'system') return;
    const query = window.matchMedia('(prefers-color-scheme: dark)');
    const sync = () => setTheme(applyTheme('system'));
    query.addEventListener('change', sync);
    return () => query.removeEventListener('change', sync);
  }, [preference]);

  const setPreference = useCallback((next: ThemePreference) => setPreferenceState(next), []);

  const toggle = useCallback(() => {
    setPreferenceState((current) => {
      const active = resolveTheme(current);
      const isDark = THEMES.find((item) => item.id === active)?.scheme === 'dark';
      if (current === 'system') return isDark ? 'paper' : 'slate';
      return isDark ? 'paper' : 'slate';
    });
  }, []);

  const value = useMemo<ThemeState>(() => {
    const definition = THEMES.find((item) => item.id === theme) ?? THEMES[0];
    return { preference, theme, definition, setPreference, toggle };
  }, [preference, theme, setPreference, toggle]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeState {
  const value = useContext(ThemeContext);
  if (!value) throw new Error('useTheme requires ThemeProvider.');
  return value;
}

export { THEMES, prefersDark };
export type { ThemeId, ThemePreference, ThemeDefinition };
