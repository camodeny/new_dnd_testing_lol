// @vitest-environment jsdom
import React, { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/api'
import type { CapacityUiEvent } from '@/lib/capacity'
import { useCampaignFunding, type CampaignFunding } from './useCampaignFunding'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))

const mockedFetch = vi.mocked(apiFetch)

let container: HTMLDivElement
let root: Root
const seen: { latest: CampaignFunding | null } = { latest: null }

function Harness(props: {
  campaignId: string | null
  onEvent?: (event: CapacityUiEvent) => void
  navigate?: (url: string) => void
}) {
  seen.latest = useCampaignFunding(props)
  return null
}

function renderHook(props: React.ComponentProps<typeof Harness>) {
  return act(async () => { root.render(<Harness {...props} />) })
}

beforeEach(() => {
  vi.clearAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  seen.latest = null
})

afterEach(async () => {
  await act(async () => { root.unmount() })
  container.remove()
})

describe('useCampaignFunding', () => {
  it('starts checkout server-side and hands off to Stripe-hosted checkout', async () => {
    mockedFetch.mockResolvedValueOnce({
      funding_operation: { id: 'op-1', status: 'pending' },
      checkout_url: 'https://checkout.stripe.test/pay/cs_1',
    })
    const events: CapacityUiEvent[] = []
    const navigate = vi.fn()
    await renderHook({ campaignId: 'c1', navigate, onEvent: (e) => events.push(e) })
    await act(async () => { await seen.latest!.startAddFunds(1000) })
    expect(mockedFetch).toHaveBeenCalledOnce()
    const [url, options] = mockedFetch.mock.calls[0]
    expect(url).toBe('/campaigns/c1/funding/checkout')
    const body = JSON.parse((options as { body: string }).body)
    expect(body.amount_cents).toBe(1000)
    expect(body.success_url).toContain('/campaigns/c1?funding=success')
    expect(body.cancel_url).toContain('/campaigns/c1?funding=cancel')
    expect((options as { headers: Record<string, string> }).headers['Idempotency-Key']).toBeTruthy()
    expect(navigate).toHaveBeenCalledWith('https://checkout.stripe.test/pay/cs_1')
    expect(events).toEqual([{ type: 'resume_choice', path: 'add_funds' }])
    expect(seen.latest!.starting).toBe(false)
    expect(seen.latest!.error).toBeNull()
  })

  it('surfaces checkout failures without navigating', async () => {
    mockedFetch.mockRejectedValueOnce(new Error('Payment provider unavailable'))
    const navigate = vi.fn()
    await renderHook({ campaignId: 'c1', navigate })
    await act(async () => { await seen.latest!.startAddFunds(500) })
    expect(navigate).not.toHaveBeenCalled()
    expect(seen.latest!.error).toContain('Payment provider unavailable')
  })

  it('recovers the pending operation after redirect and polls to terminal', async () => {
    mockedFetch.mockImplementation(async (url: string) => {
      if (String(url).endsWith('/funding/operations')) {
        return {
          funding_operations: [
            { id: 'old', status: 'confirmed', created_at: '2026-01-01T00:00:00Z' },
            { id: 'pending-1', status: 'pending', created_at: '2026-01-02T00:00:00Z' },
          ],
        }
      }
      if (String(url).endsWith('/funding/operations/pending-1')) {
        return { funding_operation: { id: 'pending-1', status: 'confirmed' }, capacity: {} }
      }
      throw new Error(`unexpected ${url}`)
    })
    await renderHook({ campaignId: 'c1' })
    let status: string | null = null
    await act(async () => { status = await seen.latest!.syncAfterReturn() })
    expect(status).toBe('confirmed')
  })

  it('returns null after redirect when nothing awaits payment', async () => {
    mockedFetch.mockResolvedValueOnce({ funding_operations: [] })
    await renderHook({ campaignId: 'c1' })
    let status: string | null = 'unset'
    await act(async () => { status = await seen.latest!.syncAfterReturn() })
    expect(status).toBeNull()
  })
})
