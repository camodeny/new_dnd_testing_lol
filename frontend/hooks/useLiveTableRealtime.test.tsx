// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiFetch } from '@/lib/api'
import { supabase } from '@/lib/supabase'
import { useLiveTableRealtime } from './useLiveTableRealtime'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
vi.mock('@/lib/supabase', () => ({
  supabase: { channel: vi.fn(), removeChannel: vi.fn() },
}))

const mockedFetch = vi.mocked(apiFetch)
const mockedChannel = vi.mocked(supabase.channel)

const SNAPSHOT = {
  history: { messages: [], pagination: { next_cursor: null, has_more: false } },
  dm_state: null,
  dm_messages: [],
  revision: 1,
}

function snapshotCalls() {
  return mockedFetch.mock.calls.filter(([url]) => String(url).includes('/snapshot'))
}

describe('useLiveTableRealtime snapshot fallback', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mockedFetch.mockImplementation(async (url: string) => {
      if (url.includes('/realtime/authorize')) return {}
      if (url.includes('/snapshot')) return SNAPSHOT
      throw new Error(`unexpected ${url}`)
    })
    // Connected-but-silent channel: SUBSCRIBED immediately, never delivers.
    const fakeChannel: Record<string, unknown> = {}
    fakeChannel.on = (..._args: unknown[]) => fakeChannel
    fakeChannel.subscribe = (cb: (status: string) => void) => {
      cb('SUBSCRIBED')
      return fakeChannel
    }
    mockedChannel.mockImplementation(() => fakeChannel as never)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('keeps polling the snapshot while subscribed but silent, without flapping phase', async () => {
    const seen: { latest: ReturnType<typeof useLiveTableRealtime> | null } = { latest: null }
    function Harness() {
      seen.latest = useLiveTableRealtime({ campaignId: 'c1', threadId: 't1' })
      return null
    }
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(<Harness />)
    })
    expect(snapshotCalls().length).toBeGreaterThanOrEqual(1)
    expect(seen.latest?.phase).toBe('live')
    expect(seen.latest?.connected).toBe(true)

    // Advance past the 5s fallback interval with zero broadcasts delivered.
    await act(async () => {
      vi.advanceTimersByTime(6000)
    })
    expect(snapshotCalls().length).toBeGreaterThanOrEqual(2)
    // Quiet polls must not flap the banner back to reconciling.
    expect(seen.latest?.phase).toBe('live')
    expect(seen.latest?.hasSnapshot).toBe(true)

    await act(async () => {
      root.unmount()
    })
    container.remove()
  })
})
