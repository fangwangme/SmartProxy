import type { Config } from 'tailwindcss'
import plugin from 'tailwindcss/plugin'

import { GRUVBOX, TOKEN_NAMES, type Palette } from './src/theme/gruvbox'

/** `#rrggbb` -> `"r g b"`, the channel form Tailwind's `<alpha-value>` needs. */
const channels = (hex: string): string => {
  const value = Number.parseInt(hex.slice(1), 16)
  return `${(value >> 16) & 0xff} ${(value >> 8) & 0xff} ${value & 0xff}`
}

const customProperties = (palette: Palette): Record<string, string> =>
  Object.fromEntries(
    TOKEN_NAMES.map((token) => [`--gb-${token}`, channels(palette[token])]),
  )

const colors = Object.fromEntries(
  TOKEN_NAMES.map((token) => [token, `rgb(var(--gb-${token}) / <alpha-value>)`]),
) as Record<keyof Palette, string>

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: ['selector', '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        ...colors,
        // The primary accent. Named separately so intent reads at the call site.
        accent: colors.orange,
      },
      fontFamily: {
        sans: [
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Inter',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
        mono: [
          'ui-monospace',
          'SFMono-Regular',
          'JetBrains Mono',
          'Menlo',
          'Consolas',
          'monospace',
        ],
      },
      borderRadius: {
        card: '0.625rem',
      },
    },
  },
  plugins: [
    plugin(({ addBase }) => {
      addBase({
        ':root': { ...customProperties(GRUVBOX.light), colorScheme: 'light' },
        '[data-theme="dark"]': {
          ...customProperties(GRUVBOX.dark),
          colorScheme: 'dark',
        },
      })
    }),
  ],
} satisfies Config
