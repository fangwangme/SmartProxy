import { useMemo } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { GRUVBOX, seriesColor, type ResolvedTheme } from '../theme/gruvbox'
import type { ChartRow } from '../types/api'

/**
 * Dynamically computes XAxis tick labels matching data points based on the
 * visible time range.
 */
const computeTicks = (rows: ChartRow[]): string[] => {
  if (rows.length === 0) return []
  const firstTime = rows[0]?.time
  const lastTime = rows[rows.length - 1]?.time
  if (!firstTime || !lastTime) return []

  const [h1, m1] = firstTime.split(':').map(Number)
  const [h2, m2] = lastTime.split(':').map(Number)
  const firstMin = (h1 ?? 0) * 60 + (m1 ?? 0)
  const lastMin = (h2 ?? 0) * 60 + (m2 ?? 0)
  const duration = lastMin - firstMin

  let rowInterval = 60
  if (rows.length > 1) {
    const [hNext, mNext] = (rows[1]?.time ?? '').split(':').map(Number)
    const nextMin = (hNext ?? 0) * 60 + (mNext ?? 0)
    if (nextMin > firstMin) {
      rowInterval = nextMin - firstMin
    }
  }

  let stepMinutes: number
  if (duration <= 75) {
    // ~1h window: tick every 10m (or 15m if interval is 15m)
    stepMinutes = rowInterval === 15 ? 15 : 10
  } else if (duration <= 150) {
    // ~2h window: tick every 15m or 30m
    stepMinutes = rowInterval >= 30 ? 30 : (rowInterval === 15 ? 30 : 15)
  } else if (duration <= 360) {
    // ~5h window: tick every 30m or 60m
    stepMinutes = rowInterval >= 60 ? 60 : 30
  } else {
    // Full day (24h): tick every 2h
    stepMinutes = 120
  }

  const ticks: string[] = []
  for (const row of rows) {
    const [h, m] = row.time.split(':').map(Number)
    const totalMin = (h ?? 0) * 60 + (m ?? 0)
    if (totalMin % stepMinutes === 0) {
      ticks.push(row.time)
    }
  }

  // Fallback: if no rows fell on exact stepMinutes boundaries, take evenly spaced rows
  if (ticks.length === 0) {
    const count = Math.min(rows.length, 6)
    const stride = Math.max(1, Math.floor(rows.length / count))
    for (let i = 0; i < rows.length; i += stride) {
      const t = rows[i]?.time
      if (t) ticks.push(t)
    }
  }

  return ticks
}

const VOLUME_DASH = '5 4'

interface TooltipPayloadEntry {
  payload?: ChartRow
}

interface ChartTooltipProps {
  active?: boolean
  label?: string | number
  payload?: readonly TooltipPayloadEntry[]
  chartSources?: string[]
  colors?: Record<string, string>
}

const ChartTooltip = ({
  active,
  label,
  payload,
  chartSources = [],
  colors = {},
}: ChartTooltipProps) => {
  const row = payload?.[0]?.payload
  if (!active || !row) return null

  const entries = chartSources
    .map((source) => ({ source, point: row.bySource[source] }))
    .filter((entry) => entry.point !== undefined)

  return (
    <div className="rounded-md border border-bg3 bg-bg0h px-3 py-2 text-xs">
      <p className="font-medium text-fg2">{String(label ?? row.time)}</p>
      {entries.length === 0 ? (
        <p className="mt-1 text-fg4">No traffic</p>
      ) : (
        <table className="mt-1.5 border-separate border-spacing-x-2 border-spacing-y-0.5">
          <tbody>
            {entries.map(({ source, point }) => (
              <tr key={source}>
                <td className="text-fg1">
                  <span className="flex items-center gap-1.5">
                    <span
                      aria-hidden
                      className="inline-block h-2 w-2 rounded-full"
                      style={{ backgroundColor: colors[source] }}
                    />
                    {source}
                  </span>
                </td>
                <td className="text-right font-medium tabular-nums text-fg0">
                  {point?.successRate === null || point === undefined
                    ? '—'
                    : `${String(point.successRate)}%`}
                </td>
                <td className="text-right tabular-nums text-fg4">
                  {(point?.successCount ?? 0).toLocaleString()}/
                  {(point?.totalRequests ?? 0).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

/**
 * One entry per source rather than one per line: each swatch shows the solid
 * success-rate stroke and the dashed request-volume stroke in the same hue, so
 * ten sources cost ten legend entries instead of twenty.
 */
const SeriesLegend = ({
  chartSources,
  colors,
}: {
  chartSources: string[]
  colors: Record<string, string>
}) => (
  <ul className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-fg3">
    {chartSources.map((source) => (
      <li key={source} className="flex items-center gap-1.5">
        <svg width="24" height="8" aria-hidden className="shrink-0">
          <line
            x1="0"
            y1="4"
            x2="10"
            y2="4"
            stroke={colors[source]}
            strokeWidth="2"
          />
          <line
            x1="13"
            y1="4"
            x2="24"
            y2="4"
            stroke={colors[source]}
            strokeWidth="2"
            strokeDasharray={VOLUME_DASH}
            strokeOpacity="0.7"
          />
        </svg>
        {source}
      </li>
    ))}
  </ul>
)

const ChartSkeleton = () => (
  <div className="flex h-full w-full flex-col justify-end gap-2 p-4">
    {[70, 45, 85, 30, 60].map((width) => (
      <div
        key={width}
        className="h-2 animate-pulse rounded bg-bg2"
        style={{ width: `${String(width)}%` }}
      />
    ))}
  </div>
)

const EmptyState = ({ message }: { message: string }) => (
  <div className="flex h-full items-center justify-center px-4 text-center text-sm text-fg4">
    {message}
  </div>
)

interface ChartsProps {
  rows: ChartRow[]
  chartSources: string[]
  theme: ResolvedTheme
  loading: boolean
  hasLoaded: boolean
  /** The current selection has no data *because* its request failed. */
  failed: boolean
}

const Charts = ({
  rows,
  chartSources,
  theme,
  loading,
  hasLoaded,
  failed,
}: ChartsProps) => {
  const palette = GRUVBOX[theme]

  const colors = useMemo(() => {
    const map: Record<string, string> = {}
    chartSources.forEach((source, index) => {
      map[source] = seriesColor(palette, index)
    })
    return map
  }, [chartSources, palette])

  const ticks = useMemo(() => computeTicks(rows), [rows])

  const hasData = rows.length > 0 && chartSources.length > 0

  const axis = {
    stroke: palette.bg3,
    tick: { fill: palette.fg4, fontSize: 11 },
    tickLine: false,
  }

  return (
    <section className="rounded-card border border-bg3 bg-bg1 px-3 pb-3 pt-2.5">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold text-fg2">Traffic by interval</h2>
        <p className="text-xs text-fg4">
          solid = success rate (left) · dashed = requests (right) · gaps = no
          traffic
        </p>
      </div>

      <div className="h-[clamp(300px,56vh,620px)]">
        {loading ? (
          <ChartSkeleton />
        ) : !hasData ? (
          <EmptyState
            message={
              failed
                ? 'Could not load statistics for this selection. Fix the error above, then refresh.'
                : hasLoaded
                  ? 'No traffic recorded for this source on the selected date.'
                  : 'Loading statistics…'
            }
          />
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={rows}
              margin={{ top: 4, right: 4, left: -8, bottom: 0 }}
            >
              <CartesianGrid strokeDasharray="3 3" stroke={palette.bg2} />
              <XAxis dataKey="time" ticks={ticks} {...axis} />
              <YAxis
                yAxisId="rate"
                domain={[0, 100]}
                unit="%"
                width={52}
                {...axis}
              />
              <YAxis
                yAxisId="volume"
                orientation="right"
                allowDecimals={false}
                width={48}
                // Head-room multiplier: volume stays a low band under the
                // success-rate lines instead of sweeping the full height.
                domain={[0, (max: number) => Math.max(1, Math.ceil(max * 2.2))]}
                {...axis}
              />
              <Tooltip
                cursor={{ stroke: palette.bg4 }}
                content={
                  <ChartTooltip chartSources={chartSources} colors={colors} />
                }
              />

              {/* Volume first so the success-rate strokes paint on top. */}
              {chartSources.map((source) => (
                <Line
                  key={`${source}-volume`}
                  yAxisId="volume"
                  type="linear"
                  name={`${source} requests`}
                  dataKey={(row: ChartRow) =>
                    row.bySource[source]?.totalRequests ?? 0
                  }
                  stroke={colors[source]}
                  strokeWidth={1.25}
                  strokeDasharray={VOLUME_DASH}
                  strokeOpacity={0.45}
                  dot={false}
                  activeDot={false}
                  isAnimationActive={false}
                />
              ))}
              {chartSources.map((source) => (
                <Line
                  key={`${source}-rate`}
                  yAxisId="rate"
                  type="linear"
                  name={source}
                  dataKey={(row: ChartRow) =>
                    row.bySource[source]?.successRate ?? null
                  }
                  stroke={colors[source]}
                  strokeWidth={1.75}
                  dot={false}
                  activeDot={{ r: 3 }}
                  isAnimationActive={false}
                  connectNulls={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {hasData && !loading && (
        <SeriesLegend chartSources={chartSources} colors={colors} />
      )}
    </section>
  )
}

export default Charts
