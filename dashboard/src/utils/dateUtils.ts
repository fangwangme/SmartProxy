/** `Date` -> `YYYY-MM-DD` in the viewer's local timezone (never UTC). */
export const toLocalYYYYMMDD = (date: Date): string => {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${String(year)}-${month}-${day}`
}

/** Today's date, re-evaluated on call so a session can survive midnight. */
export const todayLocal = (): string => toLocalYYYYMMDD(new Date())

/** Shift a `YYYY-MM-DD` string by whole days, staying in local time. */
export const shiftLocalDate = (date: string, offsetDays: number): string => {
  const [year, month, day] = date.split('-').map(Number)
  const shifted = new Date(year ?? 1970, (month ?? 1) - 1, day ?? 1)
  shifted.setDate(shifted.getDate() + offsetDays)
  return toLocalYYYYMMDD(shifted)
}
