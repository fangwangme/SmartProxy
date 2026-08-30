/**
 * Gruvbox palette — the single source of truth for dashboard colour.
 *
 * `tailwind.config.ts` reads this module to emit the `--gb-*` custom properties
 * (light on `:root`, dark on `[data-theme="dark"]`) and to map every Tailwind
 * colour utility onto them, so a theme switch is a single attribute flip.
 * Recharts cannot resolve CSS variables for `stroke`/`fill`, so chart code
 * imports the resolved hex values from here instead — same constants, no drift.
 */

export type ResolvedTheme = 'light' | 'dark'

export interface Palette {
  /** Page ground. */
  bg0: string
  /** Hard variant of the ground, used for the toolbar/input wells. */
  bg0h: string
  /** Card surface. */
  bg1: string
  /** Raised control surface. */
  bg2: string
  /** Borders — Gruvbox dark is too low-contrast for shadow-based elevation. */
  bg3: string
  /** Hover border / disabled foreground. */
  bg4: string
  /** Strongest foreground. */
  fg0: string
  /** Body foreground. */
  fg1: string
  fg2: string
  fg3: string
  /** Muted foreground — labels, axes. */
  fg4: string
  /** Neutral grey, identical in both themes. */
  gray: string
  red: string
  green: string
  yellow: string
  blue: string
  purple: string
  aqua: string
  orange: string
}

export const GRUVBOX: Record<ResolvedTheme, Palette> = {
  light: {
    bg0: '#fbf1c7',
    bg0h: '#f9f5d7',
    bg1: '#ebdbb2',
    bg2: '#d5c4a1',
    bg3: '#bdae93',
    bg4: '#a89984',
    fg0: '#282828',
    fg1: '#3c3836',
    fg2: '#504945',
    fg3: '#665c54',
    fg4: '#7c6f64',
    gray: '#928374',
    red: '#9d0006',
    green: '#79740e',
    yellow: '#b57614',
    blue: '#076678',
    purple: '#8f3f71',
    aqua: '#427b58',
    orange: '#af3a03',
  },
  dark: {
    bg0: '#282828',
    bg0h: '#1d2021',
    bg1: '#3c3836',
    bg2: '#504945',
    bg3: '#665c54',
    bg4: '#7c6f64',
    fg0: '#fbf1c7',
    fg1: '#ebdbb2',
    fg2: '#d5c4a1',
    fg3: '#bdae93',
    fg4: '#a89984',
    gray: '#928374',
    red: '#fb4934',
    green: '#b8bb26',
    yellow: '#fabd2f',
    blue: '#83a598',
    purple: '#d3869b',
    aqua: '#8ec07c',
    orange: '#fe8019',
  },
}

export type TokenName = keyof Palette

export const TOKEN_NAMES = Object.keys(GRUVBOX.dark) as TokenName[]

/**
 * Chart series hues, ordered so that neighbouring series stay distinguishable.
 * Bright variants resolve in dark mode, faded ones in light mode.
 */
const SERIES_HUES = [
  'orange',
  'aqua',
  'blue',
  'purple',
  'green',
  'yellow',
  'red',
] as const satisfies readonly TokenName[]

export const seriesColor = (palette: Palette, index: number): string => {
  const hue = SERIES_HUES[index % SERIES_HUES.length]
  return hue === undefined ? palette.gray : palette[hue]
}
