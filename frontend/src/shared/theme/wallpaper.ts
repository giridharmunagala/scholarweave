/**
 * Custom wallpapers.
 *
 * A theme ships its own generated wallpaper, but any theme can be pointed at an
 * image of your own instead. The pieces here are deliberately pure so the same
 * values drive both the live shell and the preview in settings — what you see in
 * the preview is literally the CSS the app will run.
 */

import type { ThemeId } from './themes';

export type WallpaperFit = 'cover' | 'contain' | 'tile';

export interface CustomWallpaper {
  theme: ThemeId;
  /**
   * The downscaled image as a data URL, or null to keep the theme's own wallpaper
   * and only override how it is presented. That is what lets the show-through
   * setting be used on a stock theme without uploading anything.
   */
  image: string | null;
  /** The original file name, so a saved wallpaper stays recognisable. */
  name: string;
  /** How much of the theme's own background is laid over the image, as a percentage. */
  dim: number;
  /** How much the image is softened, in pixels. */
  blur: number;
  fit: WallpaperFit;
  /**
   * How far the interface opens up over the wallpaper, 0–100. Drives `--glass-open`.
   * A theme with no saved wallpaper stays at 0, which is its stock appearance.
   */
  glass: number;
  updatedAt: number;
}

/*
 * Photographs carry far more contrast than the generated wallpapers do, so a new
 * upload starts dimmed and slightly softened. That keeps text readable on the
 * first render instead of asking people to discover the sliders after the fact.
 */
export const DEFAULT_DIM = 55;
export const DEFAULT_BLUR = 8;
export const MAX_DIM = 92;
export const MAX_BLUR = 40;
/* Enough show-through to see the picture through every surface without the text
 * having to fight it. */
export const DEFAULT_GLASS = 60;
export const MAX_GLASS = 100;
/** What the themes ship at, so the slider reads true before anything is changed. */
export const STOCK_GLASS = 30;

/** Uploads are downscaled to this width before storage. */
export const MAX_WIDTH = 2560;
/** Anything past this after downscaling is refused rather than silently failing. */
export const MAX_BYTES = 8 * 1024 * 1024;

export function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, Math.round(value)));
}

const SIZE: Record<WallpaperFit, string> = { cover: 'cover', contain: 'contain', tile: 'auto' };
const REPEAT: Record<WallpaperFit, string> = { cover: 'no-repeat', contain: 'no-repeat', tile: 'repeat' };

export const WALLPAPER_VARS = [
  '--wallpaper',
  '--wallpaper-dim',
  '--wallpaper-blur',
  '--wallpaper-size',
  '--wallpaper-repeat',
  '--glass-open',
] as const;

/**
 * The custom properties `.app-wallpaper` reads. Returning them instead of writing
 * them is what lets the settings preview render a theme it is not currently on.
 */
export function wallpaperVars(wallpaper: CustomWallpaper | null): Record<string, string> {
  if (!wallpaper) return {};
  const vars: Record<string, string> = {
    '--wallpaper-dim': `${clamp(wallpaper.dim, 0, MAX_DIM)}%`,
    '--wallpaper-blur': `${clamp(wallpaper.blur, 0, MAX_BLUR)}px`,
    '--wallpaper-size': SIZE[wallpaper.fit] ?? 'cover',
    '--wallpaper-repeat': REPEAT[wallpaper.fit] ?? 'no-repeat',
    // Unitless, because it is multiplied into both lengths and percentages.
    '--glass-open': `${clamp(wallpaper.glass, 0, MAX_GLASS) / 100}`,
  };
  // No image means "the theme's own wallpaper, presented differently", so the
  // variable is left alone rather than overridden with an empty url().
  if (wallpaper.image) vars['--wallpaper'] = `url("${wallpaper.image}")`;
  return vars;
}

/** Write the wallpaper on an element, clearing the overrides when there is none. */
export function applyWallpaperVars(element: HTMLElement, wallpaper: CustomWallpaper | null): void {
  const vars = wallpaperVars(wallpaper);
  for (const name of WALLPAPER_VARS) {
    const value = vars[name];
    if (value) element.style.setProperty(name, value);
    else element.style.removeProperty(name);
  }
}

export function draftWallpaper(theme: ThemeId, image: string | null, name: string): CustomWallpaper {
  return {
    theme,
    image,
    name,
    dim: image ? DEFAULT_DIM : 0,
    blur: image ? DEFAULT_BLUR : 0,
    fit: 'cover',
    glass: DEFAULT_GLASS,
    updatedAt: Date.now(),
  };
}
