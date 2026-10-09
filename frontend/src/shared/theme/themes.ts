/** Theme catalogue and the small amount of DOM plumbing a theme needs. */

export type ThemeId = 'light' | 'sepia' | 'dark';
export type ThemePreference = ThemeId | 'system';

export interface ThemeDefinition {
  id: ThemeId;
  label: string;
  description: string;
  scheme: 'light' | 'dark';
  /** Background, surface and accent, so the swatch previews the palette. */
  swatch: [string, string, string];
}

export const THEMES: ThemeDefinition[] = [
  {
    id: 'light',
    label: 'Light',
    description: 'Neutral white with a muted blue accent',
    scheme: 'light',
    swatch: ['#f5f6f8', '#ffffff', '#2f5c9e'],
  },
  {
    id: 'sepia',
    label: 'Sepia',
    description: 'Warm, low-glare paper for long reading',
    scheme: 'light',
    swatch: ['#f3eee4', '#fbf8f2', '#8a4f2e'],
  },
  {
    id: 'dark',
    label: 'Dark',
    description: 'Neutral graphite for low light',
    scheme: 'dark',
    swatch: ['#16181c', '#1f2227', '#7ea3dc'],
  },
];

export const STORAGE_KEY = 'scholarweave-theme';
/*
 * Version 3 replaced the earlier catalogue of decorative themes with three
 * neutral ones. Saved choices from older versions move to the closest new
 * theme once. Before version 2, a stored "paper" was the untouched default, so
 * it is dropped rather than mapped. The pre-paint script in index.html mirrors
 * this rule.
 */
export const THEME_VERSION_KEY = 'scholarweave-theme-version';
const THEME_VERSION = '3';
const LEGACY_THEMES: Record<string, ThemeId> = {
  prism: 'light', cloud: 'light', snow: 'light', mint: 'light', blossom: 'light',
  paper: 'sepia', citrus: 'sepia', sunrise: 'sepia',
  nebula: 'dark', slate: 'dark', nord: 'dark', aurora: 'dark', forest: 'dark', amoled: 'dark',
};
export const DEFAULT_LIGHT: ThemeId = 'light';
export const DEFAULT_DARK: ThemeId = 'dark';

const IDS = new Set<string>(THEMES.map((theme) => theme.id));

export function isThemeId(value: unknown): value is ThemeId {
  return typeof value === 'string' && IDS.has(value);
}

function migrateStoredPreference(): void {
  const version = localStorage.getItem(THEME_VERSION_KEY);
  if (version === THEME_VERSION) return;
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved && !version && saved === 'paper') localStorage.removeItem(STORAGE_KEY);
  else if (saved && saved in LEGACY_THEMES) localStorage.setItem(STORAGE_KEY, LEGACY_THEMES[saved]);
  localStorage.setItem(THEME_VERSION_KEY, THEME_VERSION);
}

export function readStoredPreference(): ThemePreference {
  try {
    migrateStoredPreference();
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