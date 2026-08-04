import type { SVGProps } from 'react';

export type IconName =
  | 'dashboard'
  | 'notes'
  | 'papers'
  | 'workflow'
  | 'runs'
  | 'settings'
  | 'plus'
  | 'search'
  | 'close'
  | 'check'
  | 'alert'
  | 'info'
  | 'trash'
  | 'refresh'
  | 'play'
  | 'save'
  | 'upload'
  | 'file'
  | 'folder'
  | 'chevronRight'
  | 'chevronDown'
  | 'arrowRight'
  | 'menu'
  | 'sparkle'
  | 'clock'
  | 'database'
  | 'copy'
  | 'cursor'
  | 'grid'
  | 'external'
  | 'login'
  | 'cpu'
  | 'braces'
  | 'send'
  | 'layers'
  | 'sliders'
  | 'command'
  | 'arrowLeft'
  | 'link'
  | 'expand'
  | 'collapse';

const paths: Record<IconName, string> = {
  dashboard: 'M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6V11h-6v9Zm0-16v5h6V4h-6Z',
  notes: 'M6 3h9l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm8 1.5V9h4.5M8.5 13h7M8.5 17h4.5',
  papers: 'M7 3h7l5 5v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm6 1.5V9h4.5',
  workflow: 'M5 4h5v4H5V4Zm9 12h5v4h-5v-4Zm-9 0h5v4H5v-4ZM7.5 8v4m9 4v-2a2 2 0 0 0-2-2h-7',
  runs: 'M4 6h16M4 12h16M4 18h9',
  settings:
    'M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm8.4-2.6.1-.9-.1-.9 1.8-1.4-1.7-3-2.2.7a7.5 7.5 0 0 0-1.6-.9l-.3-2.3H10.6l-.3 2.3c-.6.2-1.1.5-1.6.9l-2.2-.7-1.7 3 1.8 1.4-.1.9.1.9-1.8 1.4 1.7 3 2.2-.7c.5.4 1 .7 1.6.9l.3 2.3h3.8l.3-2.3c.6-.2 1.1-.5 1.6-.9l2.2.7 1.7-3-1.8-1.4Z',
  plus: 'M12 5v14M5 12h14',
  search: 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Zm5.2-1.8L21 21',
  close: 'M6 6l12 12M18 6 6 18',
  check: 'm5 13 4.5 4.5L19 7',
  alert: 'M12 8v5m0 3.5v.5M10.3 3.9 2.6 17.4A2 2 0 0 0 4.3 20.4h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z',
  info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-9v4.5M12 7.8v.4',
  trash: 'M4 7h16M10 11v6m4-6v6M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13M9 7V4h6v3',
  refresh: 'M20 12a8 8 0 1 1-2.6-5.9M20 4v4.5h-4.5',
  play: 'M8 5.5v13l11-6.5-11-6.5Z',
  save: 'M5 4h11l3 3v13H5V4Zm3 0v6h7V4M8 20v-6h8v6',
  upload: 'M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3',
  file: 'M7 3h7l5 5v13H7V3Zm7 .5V9h5.5',
  folder: 'M3 6a1 1 0 0 1 1-1h5l2 2.5h8a1 1 0 0 1 1 1V19a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6Z',
  chevronRight: 'm9.5 5.5 7 6.5-7 6.5',
  chevronDown: 'm5.5 9.5 6.5 7 6.5-7',
  arrowRight: 'M4 12h15m0 0-6-6m6 6-6 6',
  menu: 'M4 7h16M4 12h16M4 17h16',
  sparkle: 'M12 3.5 13.9 9l5.6 2-5.6 2-1.9 5.5L10.1 13 4.5 11l5.6-2L12 3.5ZM18.5 3v3.5M20.2 4.8h-3.4',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-14v5.2l3.4 2',
  database: 'M12 8.5c4.4 0 8-1.2 8-2.75S16.4 3 12 3 4 4.2 4 5.75 7.6 8.5 12 8.5ZM4 5.75v12.5C4 19.8 7.6 21 12 21s8-1.2 8-2.75V5.75M20 12c0 1.55-3.6 2.75-8 2.75S4 13.55 4 12',
  copy: 'M9 9V5a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-4M5 9h9a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1Z',
  grid: 'M4 5h6v6H4V5Zm10 0h6v4h-6V5Zm0 8h6v6h-6v-6ZM4 15h6v4H4v-4Z',
  cursor: 'm5.5 3.6 12.4 6.9-5.4 1.6-1.6 5.4L5.5 3.6Zm7.6 9.8 4.4 6.6',
  external: 'M14 4h6v6M20 4l-8.5 8.5M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
  login: 'M13 4h5a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-5M11 8.5 14.5 12 11 15.5M14 12H4',
  cpu: 'M8.5 8.5h7v7h-7v-7ZM6 6h12v12H6V6ZM9.5 3v3m5-3v3m-5 15v-3m5 3v-3M3 9.5h3m-3 5h3m15-5h3m-3 5h3',
  braces: 'M9 3.5c-2 0-2.5 1-2.5 2.7v2.1c0 1.4-.8 2.2-2.2 2.4v.6c1.4.2 2.2 1 2.2 2.4v2.1c0 1.7.5 2.7 2.5 2.7M15 3.5c2 0 2.5 1 2.5 2.7v2.1c0 1.4.8 2.2 2.2 2.4v.6c-1.4.2-2.2 1-2.2 2.4v2.1c0 1.7-.5 2.7-2.5 2.7',
  send: 'M21 3 10.5 13.5M21 3l-6.8 18-3.7-7.5L3 9.8 21 3Z',
  layers: 'M12 3.5 21 8l-9 4.5L3 8l9-4.5ZM3 12.5 12 17l9-4.5M3 16.8 12 21.3l9-4.5',
  sliders: 'M4 7h9m3 0h4M4 17h4m3 0h9M14.5 4.5v5M9.5 14.5v5',
  command: 'M9 9h6v6H9V9Zm0 0V6.5a2.5 2.5 0 1 0-2.5 2.5H9Zm6 0V6.5A2.5 2.5 0 1 1 17.5 9H15Zm0 6v2.5a2.5 2.5 0 1 0 2.5-2.5H15Zm-6 0v2.5A2.5 2.5 0 1 1 6.5 15H9Z',
  arrowLeft: 'M20 12H5m0 0 6-6m-6 6 6 6',
  link: 'M10.5 13.5a4 4 0 0 0 5.7 0l2.6-2.6a4 4 0 0 0-5.7-5.7l-1.5 1.5M13.5 10.5a4 4 0 0 0-5.7 0l-2.6 2.6a4 4 0 0 0 5.7 5.7l1.5-1.5',
  expand: 'M9 20H4v-5m0 5 6.5-6.5M15 4h5v5m0-5-6.5 6.5',
  collapse: 'M4 15h5v5m-5 0 6.5-6.5M20 9h-5V4m5 0-6.5 6.5',
};

const filled = new Set<IconName>(['play', 'dashboard']);

interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'name'> {
  name: IconName;
  size?: number;
}

export function Icon({ name, size = 16, className, ...props }: IconProps) {
  const isFilled = filled.has(name);
  return (
    <svg
      className={className ? `icon ${className}` : 'icon'}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={isFilled ? 'currentColor' : 'none'}
      stroke={isFilled ? 'none' : 'currentColor'}
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      <path d={paths[name]} />
    </svg>
  );
}
