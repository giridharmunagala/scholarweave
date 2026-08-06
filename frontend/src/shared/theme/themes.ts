/** Theme catalogue and the small amount of DOM plumbing a theme needs. */

export type ThemeId = 'paper' | 'cloud' | 'slate' | 'amoled' | 'forest';
export type ThemePreference = ThemeId | 'system';

export interface ThemeDefinition {
  id: ThemeId;
  label: string;
  description: string;
  scheme: 'light' | 'dark';
  /** Representative colours for the picker swatch. */
  swatch: [string, string, string];
}

export const THEMES: ThemeDefinition[] = [
  {
    id: 'paper',
    label: 'Paper',
    description: 'Warm, low-glare light',
    scheme: 'light',
    swatch: ['#f6f3ec', '#fffdf9', '#2f5eb5'],
  },
  {
    id: 'cloud',
    label: 'Cloud',
    description: 'Crisp, cool light',
    scheme: 'light',
    swatch: ['#eff3f9', '#ffffff', '#2563eb'],
  },
  {
    id: 'slate',
    label: 'Slate',
    description: 'Balanced dark',
    scheme: 'dark',
    swatch: ['#0b0f16', '#1a2130', '#6f8dff'],
  },
  {
    id: 'forest',
    label: 'Forest',
    description: 'Calm green dark',
    scheme: 'dark',
    swatch: ['#0a1210', '#1f2f28', '#5fd39a'],
  },
  {
    id: 'amoled',
    label: 'Amoled',
    description: 'True black for OLED',
    scheme: 'dark',
    swatch: ['#000000', '#191b22', '#5eeaff'],
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
