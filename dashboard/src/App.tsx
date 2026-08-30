import { useCallback, useEffect, useRef, useState } from 'react'

import Charts from './components/Charts'
import Controls from './components/Controls'
import ErrorBoundary from './components/ErrorBoundary'
import Header from './components/Header'
import StatsCards from './components/StatsCards'
import { ALL_SOURCES_OPTION, useDashboardData } from './hooks/useDashboardData'
import { useTheme } from './hooks/useTheme'
import type { Interval } from './types/api'
import { todayLocal } from './utils/dateUtils'

const REFRESH_INTERVAL_MS = 30_000

const App = () => {
  const { preference, theme, setPreference } = useTheme()
  const {
    sources,
    dailyStats,
    chartRows,
    chartSources,
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
  const [interval, setInterval] = useState<Interval>(5)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [hasLoaded, setHasLoaded] = useState(false)
  const [isVisible, setIsVisible] = useState(
    () => document.visibilityState === 'visible',
  )

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
    if (!loading && (dailyStats !== null || error !== null)) setHasLoaded(true)
  }, [loading, dailyStats, error])

  // Latest-value ref so the scheduler and the visibility listener can refresh
  // without re-subscribing on every parameter change.
  const refreshRef = useRef<(silent: boolean) => void>(() => undefined)
  useEffect(() => {
    refreshRef.current = (silent: boolean) => {
      void loadSources({ silent: true })
      void loadData(selectedSource, selectedDate, interval, { silent })
    }
  })

  const isLatestDate = selectedDate === today
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

  useEffect(() => {
    if (!shouldPoll) return
    const timer = window.setInterval(() => {
      // Re-evaluate "today" so a session left open past midnight notices the
      // rollover instead of silently polling yesterday forever.
      setToday(todayLocal())
      refreshRef.current(true)
    }, REFRESH_INTERVAL_MS)
    return () => {
      window.clearInterval(timer)
    }
  }, [shouldPoll])

  useEffect(() => {
    const onVisibilityChange = () => {
      const visible = document.visibilityState === 'visible'
      setIsVisible(visible)
      if (visible) {
        setToday(todayLocal())
        // Catch up once on return; the scheduler resumes from here.
        if (pollingEnabledRef.current) refreshRef.current(true)
      }
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [])

  const handleRefresh = useCallback(() => {
    setToday(todayLocal())
    refreshRef.current(false)
  }, [])

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
            dailyStats={dailyStats}
            loading={loading}
            scope={selectedSource}
          />

          <Charts
            rows={chartRows}
            chartSources={chartSources}
            theme={theme}
            loading={loading}
            hasLoaded={hasLoaded}
          />
        </ErrorBoundary>
      </div>
    </div>
  )
}

export default App
