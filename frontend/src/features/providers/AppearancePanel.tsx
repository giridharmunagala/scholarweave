import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../shared/components/Icons';
import { useTheme } from '../../shared/theme/ThemeProvider';
import { THEMES, type ThemeId } from '../../shared/theme/themes';
import {
  MAX_BLUR,
  MAX_DIM,
  MAX_GLASS,
  STOCK_GLASS,
  clamp,
  draftWallpaper,
  wallpaperVars,
  type CustomWallpaper,
  type WallpaperFit,
} from '../../shared/theme/wallpaper';
import {
  readWallpaperFile,
  removeWallpaper,
  saveWallpaper,
  useCustomWallpaper,
  useCustomisedThemes,
} from '../../shared/theme/wallpaperStore';

const FITS: { value: WallpaperFit; label: string }[] = [
  { value: 'cover', label: 'Fill' },
  { value: 'contain', label: 'Fit' },
  { value: 'tile', label: 'Tile' },
];

const same = (a: CustomWallpaper | null, b: CustomWallpaper | null) =>
  a === b ||
  Boolean(
    a &&
      b &&
      a.image === b.image &&
      a.dim === b.dim &&
      a.blur === b.blur &&
      a.glass === b.glass &&
      a.fit === b.fit,
  );

/*
 * The preview is the whole point of this panel: an uploaded photo can wreck
 * legibility in ways a swatch will never show. So it renders the real thing — the
 * same glass tokens, the same wallpaper layer, the same dim, blur and show-through
 * — and scopes itself with `data-theme` so you can judge a theme you are not
 * currently using without switching to it.
 */
function Preview({ theme, wallpaper }: { theme: ThemeId; wallpaper: CustomWallpaper | null }) {
  return (
    <div className="wp-preview" data-theme={theme} style={wallpaperVars(wallpaper)}>
      <div className="wp-preview-back" />
      <div className="wp-preview-shell">
        <div className="wp-preview-rail">
          <span className="wp-preview-mark" />
          <i />
          <i />
          <i />
        </div>
        <div className="wp-preview-list">
          <b />
          <i />
          <i />
          <i />
        </div>
        <div className="wp-preview-canvas">
          <strong>Readable on top</strong>
          <p>Body text sits here. Secondary text sits here.</p>
          <span className="wp-preview-chip">tool call</span>
        </div>
      </div>
    </div>
  );
}

export function AppearancePanel() {
  const { theme: activeTheme } = useTheme();
  const [target, setTarget] = useState<ThemeId>(activeTheme);
  const saved = useCustomWallpaper(target);
  const customised = useCustomisedThemes();
  const [draft, setDraft] = useState<CustomWallpaper | null>(saved);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setDraft(saved);
    setError(null);
  }, [saved, target]);

  const customisedSet = new Set(customised ? customised.split(',') : []);
  const dirty = !same(draft, saved);

  /*
   * The controls stay live even before anything is uploaded. Until then they
   * describe the theme's own wallpaper — the honest baseline — which means
   * show-through is usable without bringing your own picture at all.
   */
  const base: CustomWallpaper =
    draft ?? { ...draftWallpaper(target, null, 'Built-in wallpaper'), dim: 0, blur: 0, glass: STOCK_GLASS };

  const edit = (patch: Partial<CustomWallpaper>) => setDraft({ ...base, ...patch });

  const pick = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const { image, name } = await readWallpaperFile(file);
      const fresh = draftWallpaper(target, image, name);
      // Keep whatever adjustments are already dialled in; only the picture changes.
      setDraft(draft ? { ...draft, image, name, updatedAt: fresh.updatedAt } : fresh);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'That image could not be read.');
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = '';
    }
  };

  return (
    <section className="panel appearance-panel">
      <header className="panel-head">
        <h2>Wallpaper</h2>
        <p>
          Every theme ships a generated backdrop. Open the interface up over it, or point it at an
          image of your own. Saved in this browser only, per theme, so each theme keeps its own.
        </p>
      </header>

      <div className="field">
        <label htmlFor="wallpaper-theme">Theme</label>
        <select
          id="wallpaper-theme"
          value={target}
          onChange={(event) => setTarget(event.target.value as ThemeId)}
        >
          {THEMES.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
              {customisedSet.has(item.id) ? ' — custom' : ''}
              {item.id === activeTheme ? ' (in use)' : ''}
            </option>
          ))}
        </select>
        <small>Changing this previews another theme without switching to it.</small>
      </div>

      <Preview theme={target} wallpaper={draft} />

      {error ? <p className="wp-error">{error}</p> : null}

      <div className="wp-actions">
        <input
          ref={fileInput}
          id="wallpaper-file"
          type="file"
          accept="image/*"
          className="wp-file"
          onChange={(event) => void pick(event.target.files?.[0])}
        />
        <label className="button" htmlFor="wallpaper-file">
          <Icon name="upload" size={15} />
          {base.image ? 'Replace image' : 'Choose image'}
        </label>
        <button
          type="button"
          className="button primary"
          disabled={!draft || !dirty || busy}
          onClick={() => void saveWallpaper(draft as CustomWallpaper)}
        >
          <Icon name="check" size={15} />
          Apply
        </button>
        <button
          type="button"
          className="button ghost"
          disabled={(!saved && !draft) || busy}
          onClick={() => (saved ? void removeWallpaper(target) : setDraft(null))}
        >
          <Icon name="trash" size={15} />
          Restore built-in
        </button>
        {busy ? <span className="wp-note">Processing image…</span> : null}
        {!busy && dirty ? <span className="wp-note">Not applied yet.</span> : null}
      </div>

      <div className="wp-controls">
        <div className="field wp-wide">
          <label htmlFor="wallpaper-glass">Show through — {base.glass}%</label>
          <input
            id="wallpaper-glass"
            type="range"
            min={0}
            max={MAX_GLASS}
            value={base.glass}
            onChange={(event) => edit({ glass: clamp(Number(event.target.value), 0, MAX_GLASS) })}
          />
          <small>
            Thins every surface — rail, chat list, thread and panels — so the wallpaper reads
            through the whole app. Themes ship at {STOCK_GLASS}%; drop it to 0 for solid panels.
          </small>
        </div>
        <div className="field">
          <label htmlFor="wallpaper-dim">Dim — {base.dim}%</label>
          <input
            id="wallpaper-dim"
            type="range"
            min={0}
            max={MAX_DIM}
            value={base.dim}
            onChange={(event) => edit({ dim: clamp(Number(event.target.value), 0, MAX_DIM) })}
          />
          <small>Lays the theme background over the picture. Raise it if text is hard to read.</small>
        </div>
        <div className="field">
          <label htmlFor="wallpaper-blur">Soften — {base.blur}px</label>
          <input
            id="wallpaper-blur"
            type="range"
            min={0}
            max={MAX_BLUR}
            value={base.blur}
            onChange={(event) => edit({ blur: clamp(Number(event.target.value), 0, MAX_BLUR) })}
          />
          <small>Blurs detail so a busy picture stops competing with the interface.</small>
        </div>
        <div className="field">
          <label htmlFor="wallpaper-fit">Fit</label>
          <select
            id="wallpaper-fit"
            value={base.fit}
            onChange={(event) => edit({ fit: event.target.value as WallpaperFit })}
          >
            {FITS.map((fit) => (
              <option key={fit.value} value={fit.value}>
                {fit.label}
              </option>
            ))}
          </select>
          <small className="wp-filename">{base.image ? base.name : 'Theme wallpaper'}</small>
        </div>
      </div>
    </section>
  );
}
