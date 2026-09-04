/** Theme catalogue and the small amount of DOM plumbing a theme needs. */

export type ThemeId =
  | 'paper'
  | 'cloud'
  | 'snow'
  | 'mint'
  | 'blossom'
  | 'citrus'
  | 'sunrise'
  | 'slate'
  | 'nord'
  | 'aurora'
  | 'forest'
  | 'amoled';
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
    id: 'cloud',
    label: 'Cloud',
    description: 'Teal, ivory and coral',
    scheme: 'light',
    swatch: ['#0e3f45', '#f2ece1', '#e2603c'],
  },
  {
    id: 'snow',
    label: 'Snow',
    description: 'Pure white, vivid indigo',
    scheme: 'light',
    swatch: ['#ffffff', '#f3f4ff', '#4f46e5'],
  },
  {
    id: 'mint',
    label: 'Mint',
    description: 'Fresh teal with layered greens',
    scheme: 'light',
    swatch: ['#eaf6f1', '#fbfffd', '#0b8f6b'],
  },
  {
    id: 'blossom',
    label: 'Blossom',
    description: 'White with rose and violet',
    scheme: 'light',
    swatch: ['#fdf4f8', '#ffffff', '#d43f8d'],
  },
  {
    id: 'citrus',
    label: 'Citrus',
    description: 'White with amber and lime',
    scheme: 'light',
    swatch: ['#fdfaf0', '#ffffff', '#c2740a'],
  },
  {
    id: 'sunrise',
    label: 'Sunrise',
    description: 'Warm light with coral accents',
    scheme: 'light',
    swatch: ['#fdf4ef', '#fbeee5', '#e0533f'],
  },
  {
    id: 'slate',
    label: 'Slate',
    description: 'Indigo, charcoal and cyan',
    scheme: 'dark',
    swatch: ['#241b56', '#1b1f2a', '#3ec9e0'],
  },
  {
    id: 'nord',
    label: 'Nord',
    description: 'Cool arctic dark',
    scheme: 'dark',
    swatch: ['#2e3440', '#3f4757', '#88c0d0'],
  },
  {
    id: 'aurora',
    label: 'Aurora',
    description: 'Deep indigo, vivid violet',
    scheme: 'dark',
    swatch: ['#0c0a1b', '#282349', '#a77bff'],
  },
  {
    id: 'forest',
    label: 'Forest',
    description: 'Pine, bark and amber',
    scheme: 'dark',
    swatch: ['#14382a', '#21221a', '#e9b24d'],
  },
  {
    id: 'amoled',
    label: 'Amoled',
    description: 'Violet, true black and neon',
    scheme: 'dark',
    swatch: ['#1c0c40', '#000000', '#35e6f2'],
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
