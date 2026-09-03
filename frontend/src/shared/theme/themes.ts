/** Theme catalogue and the small amount of DOM plumbing a theme needs. */

export type ThemeId = 'paper' | 'slate';
export type ThemePreference = ThemeId | 'system';

export interface ThemeDefinition {
  id: ThemeId;
  label: string;
  description: string;
  scheme: 'light' | 'dark';
  /** The three hues a theme combines, so the swatch previews the palette. */
  swatch: [string, string, string];
}

export const THEMES: ThemeDefinition[] = [
  {
    id: 'paper',
    label: 'Paper',
    description: 'Plum, sand and terracotta',
    scheme: 'light',
    swatch: ['#3d2b32', '#eee2ce', '#c0562a'],
  },
  {
    id: 'slate',
    label: 'Slate',
    description: 'Indigo, charcoal and cyan',
    scheme: 'dark',
    swatch: ['#241b56', '#1b1f2a', '#3ec9e0'],
  },
];

export const STORAGE_KEY = 'scholarweave-theme';
export const DEFAULT_LIGHT: ThemeId = 'paper';
export const DEFAULT_DARK: ThemeId = 'slate';

const IDS = new Set<string>(THEMES.map((theme) => theme.id));

export function isThemeId(value: unknown): value is ThemeId {
  return typeof value === 'string' && IDS.has(value);
}

export function readStoredPreference(): ThemePreference {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'system' || isThemeId(saved)) return saved;
  } catch {
    /* Private mode or blocked storage: fall back to the default. */
  }
  return DEFAULT_LIGHT;
}

export function prefersDark(): boolean {
  return typeof window !== 'undefined' && window.matchMedia('(prefers-color-scheme: dark)').matches;
}

export function resolveTheme(preference: ThemePreference): ThemeId {
  if (preference === 'system') return prefersDark() ? DEFAULT_DARK : DEFAULT_LIGHT;
  return preference;
}

export function applyTheme(preference: ThemePreference): ThemeId {
  const resolved = resolveTheme(preference);
  const root = document.documentElement;
  root.dataset.theme = resolved;
  root.dataset.themePreference = preference;
  try {
    localStorage.setItem(STORAGE_KEY, preference);
  } catch {
    /* Persisting the choice is best effort. */
  }
  return resolved;
}
