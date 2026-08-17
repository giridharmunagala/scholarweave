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
    description: 'Balanced dark',
    scheme: 'dark',
    swatch: ['#0b0f16', '#1a2130', '#6f8dff'],
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
