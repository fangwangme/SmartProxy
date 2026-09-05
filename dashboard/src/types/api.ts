/**
 * Wire types for the dashboard endpoints in `src/api/server.py`.
 *
 * `success_rate` is `null` for a time slot with no traffic — the backend
 * deliberately distinguishes "no requests" from "0% success", so charts must
 * break the line rather than draw it down to the floor.
 */

export const INTERVALS = [1, 2, 5, 15, 60] as const

/** The server hard-validates `interval` against exactly this set. */
export type Interval = (typeof INTERVALS)[number]

export const TIME_WINDOWS = ['1h', '2h', '5h', '24h'] as const

export type TimeWindow = (typeof TIME_WINDOWS)[number]

/** `GET /api/stats/daily`, and the `daily` member of an overview source. */
export interface DailyStats {
  total_requests: number
  total_success: number
  success_rate: number
}

/** One element of `GET /api/stats/timeseries`. */
export interface TimeseriesPoint {
  /** `HH:MM`, local to the server. */
  time: string
  /** `null` when the slot saw no traffic. */
  success_rate: number | null
  total_requests: number
  success_count: number
}

export interface OverviewSource {
  source: string
  daily: DailyStats
  timeseries: TimeseriesPoint[]
}

/** `GET /api/stats/overview`. */
export interface OverviewResponse {
  sources: OverviewSource[]
}

/** A single source's numbers at one point in time, in chart-facing casing. */
export interface ChartPoint {
  successRate: number | null
  totalRequests: number
  successCount: number
}

/**
 * One row of chart data.
 *
 * Keyed by source rather than spread across dynamic top-level keys
 * (`row[source]`, `row[`${source}__req`]`) so the shape is expressible in
 * TypeScript without an index signature.
 */
export interface ChartRow {
  time: string
  bySource: Record<string, ChartPoint>
}
