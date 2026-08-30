import type { DailyStats } from '../types/api'

interface StatsCardsProps {
  dailyStats: DailyStats | null
  loading: boolean
  /** Label for the slice the numbers describe: a source name, or `ALL`. */
  scope: string
}

const rateTone = (rate: number, hasTraffic: boolean): string => {
  if (!hasTraffic) return 'text-fg3'
  if (rate >= 95) return 'text-green'
  if (rate >= 80) return 'text-yellow'
  return 'text-red'
}

const Kpi = ({
  label,
  value,
  tone = 'text-fg0',
}: {
  label: string
  value: string
  tone?: string
}) => (
  <div className="px-3 py-2">
    <dt className="text-[0.7rem] font-medium uppercase tracking-wider text-fg4">
      {label}
    </dt>
    <dd className={`mt-0.5 text-lg font-semibold tabular-nums ${tone}`}>
      {value}
    </dd>
  </div>
)

const SkeletonKpi = () => (
  <div className="px-3 py-2">
    <div className="h-3 w-24 animate-pulse rounded bg-bg2" />
    <div className="mt-1.5 h-5 w-16 animate-pulse rounded bg-bg2" />
  </div>
)

const StatsCards = ({ dailyStats, loading, scope }: StatsCardsProps) => {
  const shell =
    'mb-3 grid grid-cols-2 divide-bg3 rounded-card border border-bg3 bg-bg1 sm:grid-cols-4 sm:divide-x'

  if (!dailyStats) {
    if (!loading) return null
    return (
      <dl className={shell}>
        <SkeletonKpi />
        <SkeletonKpi />
        <SkeletonKpi />
        <SkeletonKpi />
      </dl>
    )
  }

  const hasTraffic = dailyStats.total_requests > 0
  const failed = dailyStats.total_requests - dailyStats.total_success

  return (
    <dl className={shell}>
      <Kpi label="Scope" value={scope} tone="text-fg1" />
      <Kpi
        label="Requests (day)"
        value={dailyStats.total_requests.toLocaleString()}
      />
      <Kpi label="Failed (day)" value={failed.toLocaleString()} />
      <Kpi
        label="Success rate (day)"
        value={hasTraffic ? `${String(dailyStats.success_rate)}%` : '—'}
        tone={rateTone(dailyStats.success_rate, hasTraffic)}
      />
    </dl>
  )
}

export default StatsCards
