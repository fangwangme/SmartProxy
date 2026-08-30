import type { KeyboardEvent } from 'react'

const NAV_KEYS = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End']

/**
 * Standard WAI-ARIA keyboard behaviour for a `role="radiogroup"`: arrows move
 * between members with wraparound, Home/End jump to the ends, and selection
 * follows focus.
 *
 * Attach this to the group. Declaring `role="radio"` promises these keys work;
 * without it the arrows fall through to whatever else is listening on the page.
 */
export const handleRadioGroupKeys = (event: KeyboardEvent<HTMLElement>): void => {
  if (!NAV_KEYS.includes(event.key)) return

  const radios = [
    ...event.currentTarget.querySelectorAll<HTMLElement>('[role="radio"]'),
  ]
  const active = radios.indexOf(document.activeElement as HTMLElement)
  if (active === -1) return

  event.preventDefault()

  let next: number
  if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = radios.length - 1
  else {
    const step = event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 1
    next = (active + step + radios.length) % radios.length
  }

  const target = radios[next]
  if (!target) return
  target.focus()
  target.click()
}
