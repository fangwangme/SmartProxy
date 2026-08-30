import type {
  DailyStats,
  Interval,
  OverviewResponse,
  TimeseriesPoint,
} from '../types/api'

const API_BASE_URL = '/api'

/** The one place that knows the base URL, error shape, and abort threading. */
const getJson = async <T>(
  path: string,
  params: Record<string, string>,
  signal?: AbortSignal,
): Promise<T> => {
  const query = new URLSearchParams(params).toString()
  const url = query ? `${API_BASE_URL}${path}?${query}` : `${API_BASE_URL}${path}`

  const response = await fetch(url, { signal })
  if (!response.ok) {
    throw new Error(
      `${path} failed: ${String(response.status)} ${response.statusText}`,
    )
  }
  return (await response.json()) as T
}

export const fetchSources = (signal?: AbortSignal): Promise<string[]> =>
  getJson<string[]>('/sources', {}, signal)

export const fetchDailyStats = (
  source: string,
  date: string,
  signal?: AbortSignal,
): Promise<DailyStats> =>
  getJson<DailyStats>('/stats/daily', { source, date }, signal)

export const fetchTimeseries = (
  source: string,
  date: string,
  interval: Interval,
  signal?: AbortSignal,
): Promise<TimeseriesPoint[]> =>
  getJson<TimeseriesPoint[]>(
    '/stats/timeseries',
    { source, date, interval: String(interval) },
    signal,
  )

export const fetchOverview = (
  date: string,
  interval: Interval,
  signal?: AbortSignal,
): Promise<OverviewResponse> =>
  getJson<OverviewResponse>(
    '/stats/overview',
    { date, interval: String(interval) },
    signal,
  )

export const isAbortError = (error: unknown): boolean =>
  error instanceof DOMException && error.name === 'AbortError'
