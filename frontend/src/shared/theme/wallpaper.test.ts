// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest';
import {
  DEFAULT_BLUR,
  DEFAULT_DIM,
  DEFAULT_GLASS,
  MAX_GLASS,
  MAX_BLUR,
  MAX_DIM,
  applyWallpaperVars,
  clamp,
  draftWallpaper,
  wallpaperVars,
  type CustomWallpaper,
} from './wallpaper';

const make = (patch: Partial<CustomWallpaper> = {}): CustomWallpaper => ({
  theme: 'cloud',
  image: 'data:image/jpeg;base64,AAAA',
  name: 'shot.jpg',
  dim: DEFAULT_DIM,
  blur: DEFAULT_BLUR,
  fit: 'cover',
  glass: 0,
  updatedAt: 0,
  ...patch,
});

describe('wallpaperVars', () => {
  it('returns nothing when there is no custom wallpaper, leaving the theme default in place', () => {
    expect(wallpaperVars(null)).toEqual({});
  });

  it('maps a cover wallpaper onto the layer variables', () => {
    expect(wallpaperVars(make())).toEqual({
      '--wallpaper': 'url("data:image/jpeg;base64,AAAA")',
      '--wallpaper-size': 'cover',
      '--wallpaper-repeat': 'no-repeat',
      '--wallpaper-dim': `${DEFAULT_DIM}%`,
      '--wallpaper-blur': `${DEFAULT_BLUR}px`,
      '--glass-open': '0',
    });
  });

  it('emits show-through as a unitless fraction, because it scales both lengths and percentages', () => {
    expect(wallpaperVars(make({ glass: 60 }))['--glass-open']).toBe('0.6');
    expect(wallpaperVars(make({ glass: 0 }))['--glass-open']).toBe('0');
    expect(wallpaperVars(make({ glass: 999 }))['--glass-open']).toBe(`${MAX_GLASS / 100}`);
  });

  it('leaves the theme wallpaper alone when there is no image, so show-through works on a stock theme', () => {
    const vars = wallpaperVars(make({ image: null, glass: 40 }));
    expect(vars).not.toHaveProperty('--wallpaper');
    expect(vars['--glass-open']).toBe('0.4');
  });

  it('switches sizing and repetition for contain and tile', () => {
    expect(wallpaperVars(make({ fit: 'contain' }))['--wallpaper-size']).toBe('contain');
    expect(wallpaperVars(make({ fit: 'contain' }))['--wallpaper-repeat']).toBe('no-repeat');
    expect(wallpaperVars(make({ fit: 'tile' }))['--wallpaper-size']).toBe('auto');
    expect(wallpaperVars(make({ fit: 'tile' }))['--wallpaper-repeat']).toBe('repeat');
  });

  it('clamps stored values that fall outside the supported range', () => {
    const vars = wallpaperVars(make({ dim: 400, blur: -20 }));
    expect(vars['--wallpaper-dim']).toBe(`${MAX_DIM}%`);
    expect(vars['--wallpaper-blur']).toBe('0px');
  });
});

describe('clamp', () => {
  it('keeps a value inside its bounds and rejects nonsense', () => {
    expect(clamp(50, 0, 100)).toBe(50);
    expect(clamp(-1, 0, 100)).toBe(0);
    expect(clamp(101, 0, 100)).toBe(100);
    expect(clamp(Number.NaN, 0, 100)).toBe(0);
  });
});

describe('draftWallpaper', () => {
  it('starts from readable defaults rather than a raw, full-strength photo', () => {
    const draft = draftWallpaper('slate', 'data:image/jpeg;base64,BBBB', 'x.jpg');
    expect(draft.theme).toBe('slate');
    expect(draft.dim).toBe(DEFAULT_DIM);
    expect(draft.blur).toBe(DEFAULT_BLUR);
    expect(draft.fit).toBe('cover');
    expect(draft.glass).toBe(DEFAULT_GLASS);
    expect(DEFAULT_DIM).toBeGreaterThan(0);
    expect(DEFAULT_BLUR).toBeGreaterThan(0);
    expect(DEFAULT_BLUR).toBeLessThanOrEqual(MAX_BLUR);
  });
});

describe('applyWallpaperVars', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('style');
  });

  it('writes the variables onto an element', () => {
    applyWallpaperVars(document.documentElement, make({ dim: 30, blur: 4 }));
    const style = document.documentElement.style;
    expect(style.getPropertyValue('--wallpaper-dim')).toBe('30%');
    expect(style.getPropertyValue('--wallpaper-blur')).toBe('4px');
    expect(style.getPropertyValue('--wallpaper')).toContain('url(');
  });

  it('clears every variable it owns when the wallpaper is removed', () => {
    applyWallpaperVars(document.documentElement, make());
    applyWallpaperVars(document.documentElement, null);
    const style = document.documentElement.style;
    for (const name of ['--wallpaper', '--wallpaper-size', '--wallpaper-repeat', '--wallpaper-dim', '--wallpaper-blur', '--glass-open']) {
      expect(style.getPropertyValue(name)).toBe('');
    }
  });
});
