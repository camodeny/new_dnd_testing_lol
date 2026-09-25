import { describe, expect, it } from 'vitest'
import {
  CAPACITY_LOW_THRESHOLD_PCT,
  deriveCapacityView,
  isCapacityPausedError,
} from './capacity'

describe('deriveCapacityView', () => {
  it('shows a simple normal percentage', () => {
    expect(deriveCapacityView({ percent_used: 42, ai_paused: false, grace_active: false }))
      .toEqual({ state: 'normal', percent: 42 })
  })

  it('rounds and clamps the display percentage', () => {
    expect(deriveCapacityView({ percent_used: 42.49 }).percent).toBe(42)
    expect(deriveCapacityView({ percent_used: 42.5 }).percent).toBe(43)
    expect(deriveCapacityView({ percent_used: 140 }).percent).toBe(100)
    expect(deriveCapacityView({ percent_used: -3 }).percent).toBe(0)
  })

  it('flags low capacity at the threshold without alarm', () => {
    expect(deriveCapacityView({ percent_used: CAPACITY_LOW_THRESHOLD_PCT }).state).toBe('low')
    expect(deriveCapacityView({ percent_used: CAPACITY_LOW_THRESHOLD_PCT - 1 }).state).toBe('normal')
  })

  it('prefers grace over the low band while the current moment finishes', () => {
    expect(deriveCapacityView({ percent_used: 99, grace_active: true, ai_paused: false }))
      .toEqual({ state: 'grace', percent: 99 })
  })

  it('prefers paused over every other signal', () => {
    expect(deriveCapacityView({ percent_used: 100, grace_active: true, ai_paused: true }).state)
      .toBe('paused')
    expect(deriveCapacityView({ ai_paused: true }).state).toBe('paused')
  })

  it('keeps funded/BYOK-style payloads percentage-only', () => {
    // A funded campaign with spend and a BYOK-flavored zero-amount marker
    // surface both project to the same simple view shape.
    expect(deriveCapacityView({ percent_used: 12, funded_cents: 500, contributor_count: 2 }))
      .toEqual({ state: 'normal', percent: 12 })
  })

  it('never invents a percentage on projection failure', () => {
    expect(deriveCapacityView(null)).toEqual({ state: 'unavailable', percent: null })
    expect(deriveCapacityView(undefined)).toEqual({ state: 'unavailable', percent: null })
    expect(deriveCapacityView({})).toEqual({ state: 'normal', percent: null })
    expect(deriveCapacityView({ percent_used: Number.NaN })).toEqual({ state: 'normal', percent: null })
  })

  it('exposes only the simple view, never raw accounting', () => {
    const view = deriveCapacityView({
      campaign_id: 'c',
      percent_used: 55,
      funded_cents: 1000,
      consumed_cents: 550,
      remaining_cents: 450,
      contributor_count: 3,
      overage_allowance_cents: 100,
      ai_paused: false,
      grace_active: false,
    })
    expect(Object.keys(view).sort()).toEqual(['percent', 'state'])
  })
})

describe('isCapacityPausedError', () => {
  it('detects the 409 ai_paused_capacity pause signal', () => {
    const error = Object.assign(new Error('paused'), {
      status: 409,
      data: { detail: { code: 'ai_paused_capacity', draft_safe: true } },
    })
    expect(isCapacityPausedError(error)).toBe(true)
  })

  it('rejects other failures so drafts are not misheld', () => {
    expect(isCapacityPausedError(new Error('nope'))).toBe(false)
    expect(isCapacityPausedError(Object.assign(new Error('x'), { status: 500, data: {} }))).toBe(false)
    expect(isCapacityPausedError(
      Object.assign(new Error('x'), { status: 409, data: { detail: { code: 'other' } } }),
    )).toBe(false)
    expect(isCapacityPausedError(null)).toBe(false)
  })
})
