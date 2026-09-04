/** A server-local clock anchored by the full ISO-8601 response timestamp. */
interface ClockAnchor {
  serverEpochMs: number
  receivedEpochMs: number
  offsetMinutes: number
}

let anchor: ClockAnchor | null = null

const offsetFromIso = (value: string): number | null => {
  if (value.endsWith('Z')) return 0
  const match = value.match(/([+-])(\d{2}):(\d{2})$/)
  if (!match) return null
  const hours = Number(match[2])
  const minutes = Number(match[3])
  if (hours > 23 || minutes > 59) return null
  return (match[1] === '-' ? -1 : 1) * (hours * 60 + minutes)
}

export const recordServerTime = (headerValue: string | null): void => {
  if (!headerValue) return
  const serverEpochMs = Date.parse(headerValue)
  const offsetMinutes = offsetFromIso(headerValue)
  if (!Number.isFinite(serverEpochMs) || offsetMinutes === null) return
  anchor = { serverEpochMs, receivedEpochMs: Date.now(), offsetMinutes }
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event('smartproxy-server-clock'))
  }
}

const currentServerDate = (): Date | null => {
  if (!anchor) return null
  const elapsed = Math.max(0, Date.now() - anchor.receivedEpochMs)
  return new Date(
    anchor.serverEpochMs + elapsed + anchor.offsetMinutes * 60_000,
  )
}

export const serverNowMinutes = (): number | null => {
  const current = currentServerDate()
  return current ? current.getUTCHours() * 60 + current.getUTCMinutes() : null
}

export const serverToday = (): string | null => {
  const current = currentServerDate()
  if (!current) return null
  const year = current.getUTCFullYear()
  const month = String(current.getUTCMonth() + 1).padStart(2, '0')
  const day = String(current.getUTCDate()).padStart(2, '0')
  return `${String(year)}-${month}-${day}`
}

export const resetServerClockForTest = (): void => {
  anchor = null
}
