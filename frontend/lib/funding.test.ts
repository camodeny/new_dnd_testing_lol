import { describe, expect, it, vi, beforeEach } from 'vitest'
import { apiFetch } from '@/lib/api'
import {
  DEFAULT_FUNDING_CENTS,
  FUNDING_PRESETS_CENTS,
  buildFundingReturnUrls,
  formatFundingAmount,
  isTerminalFundingStatus,
  newestPendingOperation,
  pollFundingOperation,
  type FundingOperation,
} from './funding'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))

function op(overrides: Partial<FundingOperation> = {}): FundingOperation {
  return {
    id: 'op-1',
    campaign_id: 'c',
    amount_cents: 1000,
    currency: 'usd',
    status: 'pending',
    checkout_url: 'https://checkout.stripe.test/pay/cs_1',
    created_at: '2026-01-01T00:00:00Z',
    confirmed_at: null,
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('buildFundingReturnUrls', () => {
  it('returns to the same campaign live table, never elsewhere', () => {
    const urls = buildFundingReturnUrls('https://app.test/', 'camp-1')
    expect(urls.successUrl).toBe('https://app.test/campaigns/camp-1?funding=success')
    expect(urls.cancelUrl).toBe('https://app.test/campaigns/camp-1?funding=cancel')
  })
})

describe('funding presets', () => {
  it('are positive integer cents with a sane default', () => {
    expect(FUNDING_PRESETS_CENTS.length).toBeGreaterThan(0)
    for (const preset of FUNDING_PRESETS_CENTS) {
      expect(Number.isInteger(preset)).toBe(true)
      expect(preset).toBeGreaterThan(0)
    }
    expect(FUNDING_PRESETS_CENTS).toContain(DEFAULT_FUNDING_CENTS)
  })

  it('formats amounts without model/quality language', () => {
    const label = formatFundingAmount(1000)
    expect(label).toContain('10')
    expect(label.toLowerCase()).not.toContain('model')
  })
})

describe('isTerminalFundingStatus', () => {
  it('treats only confirmed/failed/canceled as terminal', () => {
    expect(isTerminalFundingStatus('pending')).toBe(false)
    expect(isTerminalFundingStatus('confirmed')).toBe(true)
    expect(isTerminalFundingStatus('failed')).toBe(true)
    expect(isTerminalFundingStatus('canceled')).toBe(true)
  })
})

describe('pollFundingOperation', () => {
  it('resolves when Stripe-confirmed server-side', async () => {
    vi.mocked(apiFetch)
      .mockResolvedValueOnce({ funding_operation: op({ status: 'pending' }), capacity: {} })
      .mockResolvedValueOnce({ funding_operation: op({ status: 'confirmed' }), capacity: {} })
    const final = await pollFundingOperation('c', 'op-1', { sleep: async () => {} })
    expect(final.status).toBe('confirmed')
    expect(vi.mocked(apiFetch)).toHaveBeenCalledTimes(2)
    expect(vi.mocked(apiFetch)).toHaveBeenCalledWith('/campaigns/c/funding/operations/op-1')
  })

  it('surfaces failure/cancel without inventing success', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      funding_operation: op({ status: 'failed', checkout_url: null }),
      capacity: {},
    })
    const final = await pollFundingOperation('c', 'op-1', { sleep: async () => {} })
    expect(final.status).toBe('failed')
  })

  it('returns the last known state on timeout rather than guessing', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      funding_operation: op({ status: 'pending' }),
      capacity: {},
    })
    const final = await pollFundingOperation('c', 'op-1', {
      timeoutMs: 0,
      intervalMs: 1,
      sleep: async () => {},
    })
    expect(final.status).toBe('pending')
  })
})

describe('newestPendingOperation', () => {
  it('picks the newest pending operation for redirect recovery', () => {
    const old = op({ id: 'old', status: 'pending', created_at: '2026-01-01T00:00:00Z' })
    const fresh = op({ id: 'fresh', status: 'pending', created_at: '2026-01-02T00:00:00Z' })
    const done = op({ id: 'done', status: 'confirmed', created_at: '2026-01-03T00:00:00Z' })
    expect(newestPendingOperation([old, fresh, done])?.id).toBe('fresh')
    expect(newestPendingOperation([done])).toBeNull()
    expect(newestPendingOperation([])).toBeNull()
    expect(newestPendingOperation(null)).toBeNull()
  })
})
