import { useCallback, useEffect, useRef, useState } from 'react'

import {
  fetchDailyStats,
  fetchOverview,
  fetchSources,
  fetchTimeseries,
  isAbortError,
} from '../api/client'
import type {
  ChartPoint,
  ChartRow,
  DailyStats,
  Interval,
  OverviewSource,
  TimeseriesPoint,
} from '../types/api'

/** Synthetic option for "every source at once"; the server never returns it. */
export const ALL_SOURCES_OPTION = 'ALL'

const EMPTY_DAILY: DailyStats = {
  total_requests: 0,
  total_success: 0,
  success_rate: 0,
}

/**
 * Identifies the query a set of numbers came from. Rendering compares this
 * against the current selection, so a result can never be displayed under a
 * date or source it does not belong to — including after a failed reload,
 * where the previous result is still in state.
 */
export const queryKeyOf = (
  source: string,
  date: string,
  interval: Interval,
): string => `${source}|${date}|${String(interval)}`

/**
 * One complete, self-consistent set of dashboard numbers. Committed in a single
 * `setState` only after every request behind it has succeeded, so the KPI row
 * and the chart can never disagree about which day they are showing.
 */
export interface DashboardSnapshot {
  key: string
  daily: DailyStats
  rows: ChartRow[]
  sources: string[]
}

interface LoadOptions {
  /** Refresh in the background: keep the current chart, skip the skeleton. */
  silent?: boolean
}

const sameArray = (left: readonly string[], right: readonly string[]): boolean =>
  left.length === right.length && left.every((item, index) => item === right[index])

const toChartPoint = (point: TimeseriesPoint): ChartPoint => ({
  successRate: point.success_rate,
  totalRequests: point.total_requests,
  successCount: point.success_count,
})

const singleSourceRows = (
  source: string,
  points: TimeseriesPoint[],
): ChartRow[] =>
  points.map((point) => ({
    time: point.time,
    bySource: { [source]: toChartPoint(point) },
  }))

const mergedRows = (sources: OverviewSource[]): ChartRow[] => {
  const byTime = new Map<string, ChartRow>()
  for (const item of sources) {
    for (const point of item.timeseries) {
      let row = byTime.get(point.time)
      if (!row) {
        row = { time: point.time, bySource: {} }
        byTime.set(point.time, row)
      }
      row.bySource[item.source] = toChartPoint(point)
    }
  }
  return [...byTime.values()].sort((a, b) => a.time.localeCompare(b.time))
}

const aggregateDaily = (sources: OverviewSource[]): DailyStats => {
  const totalRequests = sources.reduce(
    (sum, item) => sum + item.daily.total_requests,
    0,
  )
  const totalSuccess = sources.reduce(
    (sum, item) => sum + item.daily.total_success,
    0,
  )
  return {
    total_requests: totalRequests,
    total_success: totalSuccess,
    success_rate:
      totalRequests > 0
        ? Number(((totalSuccess / totalRequests) * 100).toFixed(2))
        : 0,
  }
}

/**
 * Owns every dashboard fetch. Scheduling lives in `App` so the whole page runs
 * off a single timer; this hook only exposes the loaders it drives.
 */
export const useDashboardData = () => {
  const [sources, setSources] = useState<string[]>([ALL_SOURCES_OPTION])
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null)
  const [loading, setLoading] = useState(false)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /**
   * The query whose load failed, if any. Tracked separately from `error`
   * because the error banner is dismissible: clearing the banner must not make
   * the chart claim the day had no traffic when it was never fetched.
   */
  const [failedKey, setFailedKey] = useState<string | null>(null)

  const requestIdRef = useRef(0)
  const activeControllerRef = useRef<AbortController | null>(null)
  const sourcesControllerRef = useRef<AbortController | null>(null)

  // Never leave a request running after the dashboard unmounts.
  useEffect(
    () => () => {
      activeControllerRef.current?.abort()
      activeControllerRef.current = null
      sourcesControllerRef.current?.abort()
      sourcesControllerRef.current = null
    },
    [],
  )

  const loadSources = useCallback(async (options: LoadOptions = {}) => {
    sourcesControllerRef.current?.abort()
    const controller = new AbortController()
    sourcesControllerRef.current = controller

    try {
      const data = await fetchSources(controller.signal)
      const normalized = [
        ...new Set(data.filter((item) => item && item !== ALL_SOURCES_OPTION)),
      ]
      const next = [ALL_SOURCES_OPTION, ...normalized]
      setSources((previous) => (sameArray(previous, next) ? previous : next))
    } catch (caught) {
      if (isAbortError(caught)) return
      console.error(caught)
      if (!options.silent) {
        setError(
          'Could not reach the proxy service. Check that it is running and try again.',
        )
      }
    }
  }, [])

  const loadData = useCallback(
    async (
      source: string,
      date: string,
      interval: Interval,
      options: LoadOptions = {},
    ) => {
      if (!source || !date) return

      const silent = options.silent ?? false
      activeControllerRef.current?.abort()

      const controller = new AbortController()
      activeControllerRef.current = controller
      const requestId = requestIdRef.current + 1
      requestIdRef.current = requestId

      const isCurrent = () =>
        requestIdRef.current === requestId && !controller.signal.aborted

      if (silent) setIsRefreshing(true)
      else setLoading(true)
      setError(null)

      try {
        let next: DashboardSnapshot
        const key = queryKeyOf(source, date, interval)

        if (source === ALL_SOURCES_OPTION) {
          const overview = await fetchOverview(date, interval, controller.signal)
          if (!isCurrent()) return

          const fulfilled = overview.sources
          // Prefer sources that actually saw traffic; fall back to all of them
          // so an idle day still renders a legend instead of an empty chart.
          const active = fulfilled
            .filter((item) => item.daily.total_requests > 0)
            .map((item) => item.source)

          next = {
            key,
            daily: fulfilled.length > 0 ? aggregateDaily(fulfilled) : EMPTY_DAILY,
            rows: mergedRows(fulfilled),
            sources:
              active.length > 0 ? active : fulfilled.map((item) => item.source),
          }
        } else {
          // Both together: committing the daily total before the timeseries
          // arrives would leave the KPI row and the chart on different days if
          // the second request failed.
          const [daily, points] = await Promise.all([
            fetchDailyStats(source, date, controller.signal),
            fetchTimeseries(source, date, interval, controller.signal),
          ])
          if (!isCurrent()) return

          next = {
            key,
            daily,
            rows: singleSourceRows(source, points),
            sources: [source],
          }
        }

        setSnapshot(next)
        setFailedKey((previous) => (previous === next.key ? null : previous))
      } catch (caught) {
        if (isAbortError(caught)) return
        console.error(caught)
        if (isCurrent()) {
          setError(
            caught instanceof Error ? caught.message : 'Failed to load statistics.',
          )
          setFailedKey(queryKeyOf(source, date, interval))
        }
      } finally {
        if (isCurrent()) {
          activeControllerRef.current = null
          setLoading(false)
          setIsRefreshing(false)
        }
      }
    },
    [],
  )

  const dismissError = useCallback(() => {
    setError(null)
  }, [])

  return {
    sources,
    snapshot,
    failedKey,
    loading,
    isRefreshing,
    error,
    dismissError,
    loadSources,
    loadData,
  }
}
