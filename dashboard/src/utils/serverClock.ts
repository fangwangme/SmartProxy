/**
 * Tracks the server's current local wall-clock minute, learned from the
 * `X-Server-Time` (`HH:MM`) response header the API sends on every call.
 *
 * `TimeseriesPoint.time` is documented as local to the server, so any
 * "now"-relative filtering against it must use the server's clock — the
 * server's timezone (and any plain clock skew) can differ from the viewer's,
 * so the browser's own clock cannot substitute for it.
 */
let minutesOfDay: number | null = null

export const recordServerTime = (headerValue: string | null): void => {
  if (!headerValue) return
  const [h, m] = headerValue.split(':').map(Number)
  if (h === undefined || m === undefined || Number.isNaN(h) || Number.isNaN(m)) {
    return
  }
  minutesOfDay = h * 60 + m
}

/** Minutes since midnight in the server's local time, or `null` until the first response arrives. */
export const serverNowMinutes = (): number | null => minutesOfDay
