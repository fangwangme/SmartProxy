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

/** Consumed by `tailwind.config.ts` to generate one utility per token. */
export const TOKEN_NAMES = Object.keys(GRUVBOX.dark) as TokenName[]

/**
 * Chart series base hues, ordered so that neighbouring series stay
 * distinguishable. Bright variants resolve in dark mode, faded ones in light.
 *
 * Gruvbox only defines seven, but issue #15's motivating case is ten sources,
 * and in the combined chart a source owns a whole hue (solid = success rate,
 * dashed = volume). Repeating a hue would make two sources indistinguishable,
 * so indices past the seventh get derived variants instead — see `seriesColor`.
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

const clampChannel = (value: number): number =>
  Math.max(0, Math.min(255, Math.round(value)))

const parseHex = (hex: string): [number, number, number] => {
  const value = Number.parseInt(hex.slice(1), 16)
  return [(value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff]
}

const toHex = (channels: readonly number[]): string =>
  `#${channels.map((c) => clampChannel(c).toString(16).padStart(2, '0')).join('')}`

/** Linear blend of two hex colours; `ratio` is how far to move toward `target`. */
const mix = (hex: string, target: string, ratio: number): string => {
  const from = parseHex(hex)
  const to = parseHex(target)
  return toHex(from.map((channel, index) => {
    const other = to[index] ?? channel
    return channel + (other - channel) * ratio
  }))
}

/**
 * Tiers applied to the base hues, in order. Each is a blend target read from
 * the active palette plus a ratio, so both directions stay theme-aware:
 * `fg0` is cream in dark mode and near-black in light mode, so tier 1 lightens
 * on a dark ground and deepens on a light one — contrast improves either way.
 */
const SERIES_TIERS: { toward: TokenName; ratio: number }[] = [
  { toward: 'fg0', ratio: 0 },
  { toward: 'fg0', ratio: 0.45 },
  { toward: 'bg0', ratio: 0.34 },
]

/** Distinct colours available before any repeat: 7 hues x 3 tiers. */
export const SERIES_COLOR_COUNT = SERIES_HUES.length * SERIES_TIERS.length

export const seriesColor = (palette: Palette, index: number): string => {
  const safeIndex = Math.max(0, index) % SERIES_COLOR_COUNT
  const hue = SERIES_HUES[safeIndex % SERIES_HUES.length]
  const tier = SERIES_TIERS[Math.floor(safeIndex / SERIES_HUES.length)]
  if (hue === undefined || tier === undefined) return palette.gray
  const base = palette[hue]
  return tier.ratio === 0 ? base : mix(base, palette[tier.toward], tier.ratio)
}
