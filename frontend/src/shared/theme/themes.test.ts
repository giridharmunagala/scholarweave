// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { readStoredPreference, STORAGE_KEY, THEME_VERSION_KEY } from './themes';

describe('readStoredPreference', () => {
  afterEach(() => localStorage.clear());

  it.each([
    ['nebula', 'dark'],
    ['amoled', 'dark'],
    ['paper', 'sepia'],
    ['sunrise', 'sepia'],
    ['prism', 'light'],
    ['blossom', 'light'],
    ['system', 'system'],
  ])('moves a saved %s choice to %s once', (saved, expected) => {
    localStorage.setItem(THEME_VERSION_KEY, '2');
    localStorage.setItem(STORAGE_KEY, saved);

    expect(readStoredPreference()).toBe(expected);
    expect(localStorage.getItem(THEME_VERSION_KEY)).toBe('3');
  });

  it('treats a pre-version-2 paper value as the untouched default', () => {
    localStorage.setItem(STORAGE_KEY, 'paper');

    expect(readStoredPreference()).toBe('light');
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('falls back to Light for unknown values', () => {
    localStorage.setItem(THEME_VERSION_KEY, '3');
    localStorage.setItem(STORAGE_KEY, 'neon');

    expect(readStoredPreference()).toBe('light');
  });
});
