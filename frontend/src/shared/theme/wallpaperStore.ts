/**
 * Where custom wallpapers live.
 *
 * IndexedDB rather than localStorage: a wallpaper is megabytes of image data and
 * localStorage's ~5MB quota is shared with everything else the app keeps there.
 * Storage is local to the browser, which suits a preference nobody else needs to
 * see, and it means no backend, no schema change, and no API contract to
 * regenerate for what is purely a matter of taste.
 */

import { useEffect, useSyncExternalStore } from 'react';
import type { ThemeId } from './themes';
import { DEFAULT_GLASS, MAX_BYTES, MAX_WIDTH, type CustomWallpaper } from './wallpaper';

const DB_NAME = 'scholarweave-appearance';
const STORE = 'wallpapers';
const VERSION = 1;

const cache = new Map<ThemeId, CustomWallpaper>();
const listeners = new Set<() => void>();
let loading: Promise<void> | null = null;
let loaded = false;

function emit() {
  for (const listener of listeners) listener();
}

/*
 * Records written before the show-through setting existed have no `glass`. They
 * were saved when the surfaces were fixed and opaque, so they are read back at the
 * value that reproduces exactly what their owner last saw.
 */
function normalise(row: CustomWallpaper): CustomWallpaper {
  if (typeof row.glass === 'number') return row;
  return { ...row, glass: row.image ? 0 : DEFAULT_GLASS };
}

function openDb(): Promise<IDBDatabase | null> {
  if (typeof indexedDB === 'undefined') return Promise.resolve(null);
  return new Promise((resolve) => {
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.open(DB_NAME, VERSION);
    } catch {
      resolve(null);
      return;
    }
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: 'theme' });
    };
    request.onsuccess = () => resolve(request.result);
    // Blocked or private-mode failures fall back to the built-in wallpapers.
    request.onerror = () => resolve(null);
  });
}

function transact<T>(mode: IDBTransactionMode, run: (store: IDBObjectStore) => IDBRequest<T>) {
  return openDb().then(
    (db) =>
      new Promise<T | null>((resolve) => {
        if (!db) {
          resolve(null);
          return;
        }
        try {
          const request = run(db.transaction(STORE, mode).objectStore(STORE));
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => resolve(null);
        } catch {
          resolve(null);
        }
      }),
  );
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function peek(theme: ThemeId): CustomWallpaper | null {
  return cache.get(theme) ?? null;
}

export function customisedThemes(): ThemeId[] {
  return [...cache.keys()];
}

export function loadAll(): Promise<void> {
  if (loaded) return Promise.resolve();
  if (!loading) {
    loading = transact<CustomWallpaper[]>('readonly', (store) => store.getAll()).then((rows) => {
      for (const row of rows ?? []) cache.set(row.theme, normalise(row));
      loaded = true;
      loading = null;
      emit();
    });
  }
  return loading;
}

export async function saveWallpaper(wallpaper: CustomWallpaper): Promise<void> {
  const next = { ...wallpaper, updatedAt: Date.now() };
  cache.set(next.theme, next);
  emit();
  await transact('readwrite', (store) => store.put(next) as IDBRequest<unknown>);
}

export async function removeWallpaper(theme: ThemeId): Promise<void> {
  cache.delete(theme);
  emit();
  await transact('readwrite', (store) => store.delete(theme) as IDBRequest<unknown>);
}

/** Subscribe to the wallpaper for one theme, loading the store on first use. */
export function useCustomWallpaper(theme: ThemeId): CustomWallpaper | null {
  useEffect(() => {
    void loadAll();
  }, []);
  return useSyncExternalStore(
    subscribe,
    () => peek(theme),
    () => null,
  );
}

/** Subscribe to which themes have a custom wallpaper, for the settings list. */
export function useCustomisedThemes(): string {
  useEffect(() => {
    void loadAll();
  }, []);
  return useSyncExternalStore(
    subscribe,
    () => customisedThemes().sort().join(','),
    () => '',
  );
}

/**
 * Decode, downscale and re-encode an upload. A 12MP phone photo is far more
 * detail than a blurred backdrop can show, and storing it whole would make every
 * read from IndexedDB slow, so it is capped on the way in.
 */
export async function readWallpaperFile(file: File): Promise<{ image: string; name: string }> {
  if (!file.type.startsWith('image/')) throw new Error('That file is not an image.');

  const source = await createImageBitmap(file).catch(() => {
    throw new Error('That image could not be decoded.');
  });
  const scale = Math.min(1, MAX_WIDTH / source.width);
  const width = Math.max(1, Math.round(source.width * scale));
  const height = Math.max(1, Math.round(source.height * scale));

  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext('2d');
  if (!context) throw new Error('This browser cannot process the image.');
  context.drawImage(source, 0, 0, width, height);
  source.close();

  // JPEG at 0.85: a wallpaper sits behind blurred glass, so the artefacts are
  // invisible while the saving over PNG is roughly tenfold.
  const image = canvas.toDataURL('image/jpeg', 0.85);
  if (image.length > MAX_BYTES) throw new Error('That image is too large, even after downscaling.');
  return { image, name: file.name };
}
