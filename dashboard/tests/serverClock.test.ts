import { afterEach, describe, expect, test } from 'bun:test'

import {
  recordServerTime,
  resetServerClockForTest,
  serverNowMinutes,
  serverToday,
} from '../src/utils/serverClock'

const originalNow = Date.now

afterEach(() => {
  Date.now = originalNow
  resetServerClockForTest()
})

describe('server clock', () => {
  test('uses the server timezone rather than the viewer timezone', () => {
    Date.now = () => Date.parse('2026-09-04T16:00:00Z')
    recordServerTime('2026-09-05T00:00:00+08:00')
    expect(serverToday()).toBe('2026-09-05')
    expect(serverNowMinutes()).toBe(0)
  })

  test('advances across server midnight after the last response', () => {
    let now = Date.parse('2026-09-04T15:59:00Z')
    Date.now = () => now
    recordServerTime('2026-09-04T23:59:00+08:00')
    now += 2 * 60_000
    expect(serverToday()).toBe('2026-09-05')
    expect(serverNowMinutes()).toBe(1)
  })
})
