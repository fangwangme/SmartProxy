import { useCallback, useEffect } from 'react'

import { INTERVALS, type Interval } from '../types/api'
import { shiftLocalDate } from '../utils/dateUtils'
import { handleRadioGroupKeys } from '../utils/radioGroupKeys'
import { ChevronLeftIcon, ChevronRightIcon, RefreshIcon } from './icons'

interface ControlsProps {
  sources: string[]
  selectedSource: string
  onSourceChange: (source: string) => void
  selectedDate: string
  onDateChange: (date: string) => void
  /** Today in local time, re-evaluated by the scheduler so it survives midnight. */
  today: string
  interval: Interval
  onIntervalChange: (interval: Interval) => void
  autoRefresh: boolean
  onAutoRefreshChange: (enabled: boolean) => void
  onRefresh: () => void
  loading: boolean
  isRefreshing: boolean
}

/** Every control shares one height so the toolbar reads as a single band. */
const FIELD = 'h-8 text-sm text-fg1'
/** A joined group: one border around the set, hairlines between the members. */
const GROUP = 'flex items-stretch overflow-hidden rounded-md border border-bg3 bg-bg0h'
const SEGMENT =
  'flex items-center justify-center px-2.5 text-fg3 transition-colors hover:bg-bg2 hover:text-fg0 disabled:cursor-not-allowed disabled:text-bg4 disabled:hover:bg-transparent'
const LABEL = 'mb-1 block text-[0.7rem] font-medium uppercase tracking-wide text-fg4'

const Controls = ({
  sources,
  selectedSource,
  onSourceChange,
  selectedDate,
  onDateChange,
  today,
  interval,
  onIntervalChange,
  autoRefresh,
  onAutoRefreshChange,
  onRefresh,
  loading,
  isRefreshing,
}: ControlsProps) => {
  const isLatestDate = selectedDate >= today

  const stepDate = useCallback(
    (offsetDays: number) => {
      const next = shiftLocalDate(selectedDate, offsetDays)
      // Future dates have no data; the API would only return an empty day.
      if (next > today) return
      onDateChange(next)
    },
    [selectedDate, today, onDateChange],
  )

  // Left/right arrows step the day, but only while nothing is focused. Any
  // focused control owns its own arrow keys — the radiogroups below use them
  // to move between members, and stealing them here both broke that and
  // changed the date behind the user's back.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return
      const active = document.activeElement
      if (active !== null && active !== document.body) return

      if (event.key === 'ArrowLeft') stepDate(-1)
      else if (event.key === 'ArrowRight') stepDate(1)
    }

    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [stepDate])

  return (
    <div className="mb-3 flex flex-wrap items-end gap-x-5 gap-y-3 rounded-card border border-bg3 bg-bg1 px-3 py-2.5">
      <div className="min-w-[9rem] flex-1 sm:flex-none">
        <label htmlFor="source-select" className={LABEL}>
          Source
        </label>
        <select
          id="source-select"
          value={selectedSource}
          onChange={(event) => {
            onSourceChange(event.target.value)
          }}
          className={`${FIELD} w-full rounded-md border border-bg3 bg-bg0h px-2 transition-colors hover:border-bg4 sm:w-44`}
        >
          {sources.map((source) => (
            <option key={source} value={source}>
              {source}
            </option>
          ))}
        </select>
      </div>

      <div>
        <span className={LABEL}>Date</span>
        <div className={`${GROUP} ${FIELD} divide-x divide-bg3`}>
          <button
            type="button"
            onClick={() => {
              stepDate(-1)
            }}
            className={SEGMENT}
            aria-label="Previous day"
          >
            <ChevronLeftIcon />
          </button>
          <input
            type="date"
            id="date-picker"
            aria-label="Date"
            value={selectedDate}
            max={today}
            onChange={(event) => {
              // `max` marks a future date invalid but does not block typing it.
              const value = event.target.value
              if (value) onDateChange(value > today ? today : value)
            }}
            // The group clips overflow, so the shared ring's offset would be
            // cut off — draw it inset instead of suppressing it.
            className="w-[8.5rem] border-0 bg-transparent px-2 text-center text-sm text-fg1 focus-visible:ring-inset focus-visible:ring-offset-0"
          />
          <button
            type="button"
            onClick={() => {
              stepDate(1)
            }}
            disabled={isLatestDate}
            className={SEGMENT}
            aria-label="Next day"
          >
            <ChevronRightIcon />
          </button>
        </div>
      </div>

      <div>
        <span className={LABEL}>Interval</span>
        <div
          role="radiogroup"
          aria-label="Aggregation interval"
          onKeyDown={handleRadioGroupKeys}
          className={`${GROUP} ${FIELD} divide-x divide-bg3`}
        >
          {INTERVALS.map((value) => {
            const selected = interval === value
            return (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={selected}
                tabIndex={selected ? 0 : -1}
                onClick={() => {
                  onIntervalChange(value)
                }}
                className={`${SEGMENT} min-w-[2.75rem] tabular-nums ${
                  selected
                    ? 'bg-accent/15 font-medium text-accent hover:bg-accent/15 hover:text-accent'
                    : ''
                }`}
              >
                {value}m
              </button>
            )
          })}
        </div>
      </div>

      <div className="flex flex-1 items-center justify-end gap-2">
        <button
          type="button"
          role="switch"
          aria-checked={autoRefresh}
          onClick={() => {
            onAutoRefreshChange(!autoRefresh)
          }}
          className={`${FIELD} group flex items-center gap-2 rounded-md px-2 text-fg3 transition-colors hover:bg-bg2 hover:text-fg0`}
        >
          <span
            className={`relative h-4 w-7 shrink-0 rounded-full transition-colors ${
              autoRefresh ? 'bg-accent' : 'bg-bg3 group-hover:bg-bg4'
            }`}
          >
            <span
              className={`absolute left-0.5 top-0.5 h-3 w-3 rounded-full bg-bg0h transition-transform ${
                autoRefresh ? 'translate-x-3' : 'translate-x-0'
              }`}
            />
          </span>
          <span className="whitespace-nowrap">Auto 30s</span>
        </button>

        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          aria-label="Refresh now"
          title="Refresh now"
          className={`${FIELD} flex items-center gap-1.5 rounded-md border border-bg3 bg-bg0h px-2.5 text-fg3 transition-colors hover:bg-bg2 hover:text-fg0 disabled:cursor-not-allowed disabled:text-bg4 disabled:hover:bg-transparent`}
        >
          <RefreshIcon
            className={loading || isRefreshing ? 'animate-spin' : undefined}
          />
          <span className="sr-only md:not-sr-only">Refresh</span>
        </button>
      </div>
    </div>
  )
}

export default Controls
