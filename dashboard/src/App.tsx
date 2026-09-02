import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import Charts from './components/Charts'
import Controls from './components/Controls'
import ErrorBoundary from './components/ErrorBoundary'
import Header from './components/Header'
import StatsCards from './components/StatsCards'
import {
  ALL_SOURCES_OPTION,
  queryKeyOf,
  useDashboardData,
} from './hooks/useDashboardData'
import { useTheme } from './hooks/useTheme'
import type { Interval, TimeWindow } from './types/api'
import { todayLocal } from './utils/dateUtils'
import { serverNowMinutes } from './utils/serverClock'

const REFRESH_INTERVAL_MS = 30_000

/** Exhaustive over `TimeWindow` so a new entry fails to compile until added. */
const WINDOW_MINUTES: Record<TimeWindow, number> = {
  '1h': 60,
  '2h': 120,
  '5h': 300,
  '24h': 1440,
}

const App = () => {
  const { preference, theme, setPreference } = useTheme()
  const {
    sources,
    snapshot,
    failedKey,
    loading,
    isRefreshing,
    error,
    dismissError,
    loadSources,
    loadData,
  } = useDashboardData()

  const [today, setToday] = useState(todayLocal)
  const [selectedSource, setSelectedSource] = useState(ALL_SOURCES_OPTION)
  const [selectedDate, setSelectedDate] = useState(today)
  const [timeWindow, setTimeWindow] = useState<TimeWindow>('24h')
  const [interval, setInterval] = useState<Interval>(5)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [hasLoaded, setHasLoaded] = useState(false)
  const [isVisible, setIsVisible] = useState(
    () => document.visibilityState === 'visible',
  )

  // Only ever render numbers that belong to the current selection. After a
  // failed reload the previous snapshot is still in state, but its key no
  // longer matches, so it cannot be shown under the new date or source.
  const queryKey = queryKeyOf(selectedSource, selectedDate, interval)
  const current = snapshot?.key === queryKey ? snapshot : null

  // Load the source list once; every later refresh runs off the single
  // scheduler below rather than a timer of its own.
  useEffect(() => {
    void loadSources()
  }, [loadSources])

  // Drop back to ALL if the selected source disappears from the list.
  useEffect(() => {
    if (!sources.includes(selectedSource)) {
      setSelectedSource(ALL_SOURCES_OPTION)
    }
  }, [sources, selectedSource])

  useEffect(() => {
    void loadData(selectedSource, selectedDate, interval)
  }, [loadData, selectedSource, selectedDate, interval])

  useEffect(() => {
    if (!loading && (current !== null || error !== null)) setHasLoaded(true)
  }, [loading, current, error])

  // Latest-value ref so the scheduler and the visibility listener can refresh
  // without re-subscribing on every parameter change.
  const refreshRef = useRef<(silent: boolean) => void>(() => undefined)
  useEffect(() => {
    refreshRef.current = (silent: boolean) => {
      void loadSources({ silent: true })
      void loadData(selectedSource, selectedDate, interval, { silent })
    }
  })

  const isLatestDate = selectedDate >= today
  // Polling a past date re-fetches a day that can no longer change, and a
  // hidden tab does not need the traffic at all.
  const pollingEnabled = autoRefresh && isLatestDate
  const shouldPoll = pollingEnabled && isVisible
  // Deliberately excludes visibility: the listener below reads this *while*
  // the tab is still marked hidden, to decide whether to catch up on return.
  const pollingEnabledRef = useRef(pollingEnabled)

  useEffect(() => {
    pollingEnabledRef.current = pollingEnabled
  }, [pollingEnabled])

  /**
   * One refresh, from the scheduler, the visibility listener, or the manual
   * button. Re-reads the wall clock first: a session left open across midnight
   * must follow the new day, otherwise it would refresh yesterday once and then
   * tear down its own timer when `isLatestDate` goes false — auto-refresh would
   * stop for good. Manual refresh goes through here too, so clicking the button
   * just after midnight cannot strand the view on yesterday.
   */
  const tickRef = useRef<(silent: boolean) => void>(() => undefined)
  useEffect(() => {
    tickRef.current = (silent: boolean) => {
      const now = todayLocal()
      if (now !== today) {
        setToday(now)
        if (selectedDate === today) {
          // Sitting on "today" when it rolled over: move with it and let the
          // parameter effect load the new day. Refreshing here would re-fetch
          // the old date from a closure that is already stale.
          setSelectedDate(now)
          return
        }
      }
      refreshRef.current(silent)
    }
  })

  useEffect(() => {
    if (!shouldPoll) return
    const timer = window.setInterval(() => {
      tickRef.current(true)
    }, REFRESH_INTERVAL_MS)
    return () => {
      window.clearInterval(timer)
    }
  }, [shouldPoll])

  useEffect(() => {
    const onVisibilityChange = () => {
      const visible = document.visibilityState === 'visible'
      setIsVisible(visible)
      // Catch up once on return, including across a midnight that passed while
      // the tab was hidden.
      if (visible && pollingEnabledRef.current) tickRef.current(true)
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [])

  const handleRefresh = useCallback(() => {
    tickRef.current(false)
  }, [])

  const visibleRows = useMemo(() => {
    const allRows = current?.rows ?? []
    if (allRows.length === 0 || timeWindow === '24h' || selectedDate !== today) {
      return allRows
    }

    const windowMinutes = WINDOW_MINUTES[timeWindow]

    // Row `time` values are server-local; filtering against the browser's
    // clock would misalign the window whenever the two differ. `allRows` is
    // non-empty here only after a response has arrived, which is what learns
    // the server clock in the first place, so it is always known by now.
    const currentMinutes = serverNowMinutes() ?? 0
    const startMinutes = Math.max(0, currentMinutes - windowMinutes)

    return allRows.filter((row) => {
      const [h, m] = row.time.split(':').map(Number)
      const rowMinutes = (h ?? 0) * 60 + (m ?? 0)
      return rowMinutes >= startMinutes && rowMinutes <= currentMinutes
    })
  }, [current?.rows, timeWindow, selectedDate, today])

  return (
    <div className="min-h-screen bg-bg0 px-4 py-5 font-sans text-fg1 sm:px-6 lg:px-8">
      <div className="mx-auto max-w-7xl">
        <Header themePreference={preference} onThemeChange={setPreference} />

        <ErrorBoundary>
          {error !== null && (
            <div
              role="alert"
              className="mb-3 flex items-start justify-between gap-3 rounded-card border border-red bg-bg1 px-3 py-2 text-sm"
            >
              <span className="break-words text-fg1">
                <span className="font-semibold text-red">Error: </span>
                {error}
              </span>
              <button
                type="button"
                onClick={dismissError}
                className="shrink-0 rounded px-1 text-fg4 transition-colors hover:text-fg1"
                aria-label="Dismiss error"
              >
                ✕
              </button>
            </div>
          )}

          <Controls
            sources={sources}
            selectedSource={selectedSource}
            onSourceChange={setSelectedSource}
            selectedDate={selectedDate}
            onDateChange={setSelectedDate}
            today={today}
            timeWindow={timeWindow}
            onTimeWindowChange={setTimeWindow}
            interval={interval}
            onIntervalChange={setInterval}
            autoRefresh={autoRefresh}
            onAutoRefreshChange={setAutoRefresh}
            onRefresh={handleRefresh}
            loading={loading}
            isRefreshing={isRefreshing}
          />

          {autoRefresh && !isLatestDate && (
            <p className="mb-3 text-xs text-fg4">
              Auto-refresh is paused: {selectedDate} is not today, so its
              statistics can no longer change.
            </p>
          )}

          <StatsCards
            dailyStats={current?.daily ?? null}
            loading={loading}
            scope={selectedSource}
          />

          <Charts
            rows={visibleRows}
            chartSources={current?.sources ?? []}
            theme={theme}
            loading={loading}
            hasLoaded={hasLoaded}
            failed={current === null && failedKey === queryKey}
          />
        </ErrorBoundary>
      </div>
    </div>
  )
}

export default App
