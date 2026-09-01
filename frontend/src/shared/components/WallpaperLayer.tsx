import { useEffect } from 'react';
import { useTheme } from '../theme/ThemeProvider';
import { applyWallpaperVars } from '../theme/wallpaper';
import { useCustomWallpaper } from '../theme/wallpaperStore';

/*
 * One fixed layer behind the whole shell. Both the generated wallpapers and any
 * uploaded one render through it, so dimming and softening work the same either
 * way, and the glass surfaces above have a single thing to blur.
 */
export function WallpaperLayer() {
  const { theme } = useTheme();
  const wallpaper = useCustomWallpaper(theme);

  useEffect(() => {
    applyWallpaperVars(document.documentElement, wallpaper);
  }, [wallpaper]);

  return <div className="app-wallpaper" aria-hidden="true" />;
}
