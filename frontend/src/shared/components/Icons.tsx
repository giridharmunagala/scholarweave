import type { SVGProps } from 'react';

/**
 * Small stroke-based icon set. Kept inline so icons inherit `currentColor`
 * and stay in step with the active theme without shipping an icon font.
 */

export type IconName =
  | 'overview'
  | 'builder'
  | 'agents'
  | 'tools'
  | 'runs'
  | 'papers'
  | 'workspace'
  | 'settings'
  | 'search'
  | 'menu'
  | 'sidebar'
  | 'check'
  | 'palette'
  | 'plus'
  | 'upload'
  | 'download'
  | 'scan'
  | 'trash'
  | 'play'
  | 'stop'
  | 'save'
  | 'copy'
  | 'close'
  | 'arrowRight'
  | 'sparkle'
  | 'inbox'
  | 'shield'
  | 'clock'
  | 'user'
  | 'sliders'
  | 'bulb'
  | 'globe'
  | 'refresh'
  | 'speaker'
  | 'microphone'
  | 'expand'
  | 'file';

const PATHS: Record<IconName, string> = {
  overview: 'M3 10.5 12 3l9 7.5M5 9.8V20a1 1 0 0 0 1 1h3.5v-6h5v6H18a1 1 0 0 0 1-1V9.8',
  builder: 'M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.4-.7L3 21l1.8-5A8.3 8.3 0 0 1 4 11.5 8.4 8.4 0 0 1 12.5 3 8.4 8.4 0 0 1 21 11.5Z',
  agents: 'M12 3v3m-7 3h14a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1Zm4 4v2m6-2v2M9 21h6',
  tools: 'M14.7 6.3a4 4 0 0 1 5.3 5L21 12.4l-2 2-1.3-1.2-5.4 5.4a2.5 2.5 0 0 1-3.6-3.5l5.4-5.4L12.7 8ZM6.5 13.5 3 17a2.1 2.1 0 0 0 3 3l3.5-3.5',
  runs: 'M12 21a9 9 0 1 1 9-9m-9-4.5V12l3 2m4 6 2.5-2.5L19 15',
  papers: 'M7 3h7l5 5v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm7 0v5h5M9.5 13h6m-6 3.5h4',
  workspace: 'M4 6.5A1.5 1.5 0 0 1 5.5 5H10l2 2.2h6.5A1.5 1.5 0 0 1 20 8.7v9.8a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5Z',
  settings: 'M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4Zm7.4-2.4a7.7 7.7 0 0 0 0-1.6l2-1.5-2-3.4-2.3 1a7.7 7.7 0 0 0-1.4-.8L15.4 4h-4l-.3 2.5a7.7 7.7 0 0 0-1.4.8l-2.3-1-2 3.4 2 1.5a7.7 7.7 0 0 0 0 1.6l-2 1.5 2 3.4 2.3-1c.44.33.91.6 1.4.8l.3 2.5h4l.3-2.5c.49-.2.96-.47 1.4-.8l2.3 1 2-3.4Z',
  search: 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Zm5.2-1.8L21 21',
  menu: 'M4 7h16M4 12h16M4 17h16',
  sidebar: 'M4.5 5h15a.5.5 0 0 1 .5.5v13a.5.5 0 0 1-.5.5h-15a.5.5 0 0 1-.5-.5v-13a.5.5 0 0 1 .5-.5Zm5 0v14',
  check: 'm5 12.8 4.5 4.4L19 7',
  palette: 'M12 21a9 9 0 1 1 9-9c0 2-1.6 2.6-3 2.6h-1.6a2 2 0 0 0-1.4 3.4 1.6 1.6 0 0 1-1.2 3H12Zm-4.5-9.5h.01M10.5 8h.01M15 8h.01',
  plus: 'M12 5v14M5 12h14',
  upload: 'M12 16V4m-4.5 4L12 3.5 16.5 8M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3',
  download: 'M12 4v12m-4.5-4L12 16.5l4.5-4.5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3',
  scan: 'M4 8V5a1 1 0 0 1 1-1h3m8 0h3a1 1 0 0 1 1 1v3M4 16v3a1 1 0 0 0 1 1h3m8 0h3a1 1 0 0 0 1-1v-3M7 12h10',
  trash: 'M4 7h16M9.5 7V5h5v2m-8 0 .8 13a1 1 0 0 0 1 1h6.4a1 1 0 0 0 1-1L16.5 7',
  play: 'M8 5.5v13l11-6.5-11-6.5Z',
  stop: 'M7 7h10v10H7Z',
  save: 'M5 4h11l3 3v13H5V4Zm3 0v6h8V4M8 20v-6h8v6',
  copy: 'M8 8h11a1 1 0 0 1 1 1v11H9a1 1 0 0 1-1-1V8Zm-4 8H3V4a1 1 0 0 1 1-1h12v1',
  close: 'M6 6l12 12M18 6 6 18',
  arrowRight: 'M5 12h14m-6-6 6 6-6 6',
  sparkle: 'M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3Zm6.5 9.5.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2Z',
  inbox: 'M4 13h4l1.5 3h5L16 13h4M4 13l2.4-7.3A1 1 0 0 1 7.3 5h9.4a1 1 0 0 1 .95.7L20 13v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-5Z',
  shield: 'M12 3.5 19 6v6c0 4.2-2.9 7.4-7 8.5-4.1-1.1-7-4.3-7-8.5V6l7-2.5Z',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-13.5V12l3 2',
  user: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8.5a7 7 0 0 1 14 0',
  sliders: 'M4 7h9m3 0h4M4 17h4m3 0h9M16 4.5v5M8 14.5v5',
  bulb: 'M9.5 18h5m-4.5 3h4M12 3a6 6 0 0 1 3.7 10.7c-.6.5-.95 1.1-1.05 1.8l-.1.5h-5.1l-.1-.5c-.1-.7-.45-1.3-1.05-1.8A6 6 0 0 1 12 3Z',
  globe: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-18c-2.5 2.2-3.8 5.2-3.8 9s1.3 6.8 3.8 9c2.5-2.2 3.8-5.2 3.8-9S14.5 5.2 12 3ZM3.5 9.5h17m-17 5h17',
  refresh: 'M20 12a8 8 0 1 1-2.6-5.9M20 4v4.5h-4.5',
  speaker: 'M11 5 6.5 8.5H4a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h2.5L11 19V5Zm4 3.5a5 5 0 0 1 0 7M17.5 6a8 8 0 0 1 0 12',
  microphone: 'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Zm-7 9a7 7 0 0 0 14 0M12 19v3m-4 0h8',
  expand: 'M9 4H5a1 1 0 0 0-1 1v4m11-5h4a1 1 0 0 1 1 1v4M9 20H5a1 1 0 0 1-1-1v-4m11 5h4a1 1 0 0 0 1-1v-4',
  file: 'M7 3h7l5 5v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm7 0v5h5',
};

export interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'name'> {
  name: IconName;
  size?: number;
}

export function Icon({ name, size = 18, ...props }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      <path d={PATHS[name]} />
    </svg>
  );
}

/** Theme mode glyphs live apart because they use fills as well as strokes. */
export function ModeIcon({ scheme, size = 18 }: { scheme: 'light' | 'dark' | 'system'; size?: number }) {
  if (scheme === 'system') {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <rect x="3" y="4.5" width="18" height="12" rx="1.5" />
        <path d="M9 20h6m-3-3.5V20" />
      </svg>
    );
  }
  if (scheme === 'dark') {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z" />
      </svg>
    );
  }
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4" />
    </svg>
  );
}
