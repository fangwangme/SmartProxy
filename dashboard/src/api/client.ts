import type {
  DailyStats,
  Interval,
  OverviewResponse,
  TimeseriesPoint,
} from '../types/api'
import { recordServerTime } from '../utils/serverClock'

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
  // Same-origin, so custom response headers are always readable; use this to
  // learn the server's clock (see utils/serverClock).
  recordServerTime(response.headers.get('X-Server-Time'))
  if (!response.ok) {
    // HTTP/2 and HTTP/3 dropped the status reason phrase, so `statusText` is
    // routinely empty — joining unconditionally would leave a trailing space.
    const status = [String(response.status), response.statusText]
      .filter(Boolean)
      .join(' ')
    throw new Error(`${path} failed: ${status}`)
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
