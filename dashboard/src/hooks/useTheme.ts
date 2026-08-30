import { useCallback, useEffect, useState } from 'react'

import type { ResolvedTheme } from '../theme/gruvbox'

export type ThemePreference = 'system' | 'light' | 'dark'

/** Kept in sync with the pre-hydration bootstrap script in `index.html`. */
export const THEME_STORAGE_KEY = 'smartproxy-dashboard-theme'

const DARK_QUERY = '(prefers-color-scheme: dark)'

const readStoredPreference = (): ThemePreference => {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    if (stored === 'light' || stored === 'dark' || stored === 'system') {
      return stored
    }
  } catch {
    // Storage can be unavailable (private mode, blocked site data). Fall back
    // to following the system preference rather than failing to render.
  }
  return 'system'
}

const readSystemTheme = (): ResolvedTheme =>
  window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light'

/**
 * Theme controller: follows `prefers-color-scheme` by default, remembers an
 * explicit choice in `localStorage`, and applies the result as `data-theme` on
 * `<html>` where the Gruvbox custom properties are defined.
 */
export const useTheme = () => {
  const [preference, setPreferenceState] =
    useState<ThemePreference>(readStoredPreference)
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>(readSystemTheme)

  useEffect(() => {
    const media = window.matchMedia(DARK_QUERY)
    const onChange = (event: MediaQueryListEvent) => {
      setSystemTheme(event.matches ? 'dark' : 'light')
    }
    media.addEventListener('change', onChange)
    return () => {
      media.removeEventListener('change', onChange)
    }
  }, [])

  const theme: ResolvedTheme = preference === 'system' ? systemTheme : preference

  useEffect(() => {
    document.documentElement.dataset['theme'] = theme
  }, [theme])

  const setPreference = useCallback((next: ThemePreference) => {
    setPreferenceState(next)
    try {
      if (next === 'system') {
        window.localStorage.removeItem(THEME_STORAGE_KEY)
      } else {
        window.localStorage.setItem(THEME_STORAGE_KEY, next)
      }
    } catch {
      // Non-fatal: the choice simply will not survive a reload.
    }
  }, [])

  return { preference, theme, setPreference }
}
