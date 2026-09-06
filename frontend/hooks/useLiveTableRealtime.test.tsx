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

  it('reconciles broadcasts that race a quiet poll instead of erasing them', async () => {
    const pending: Array<() => void> = []
    let snapshotCalls = 0
    mockedFetch.mockImplementation(async (url: string) => {
      if (url.includes('/realtime/authorize')) return {}
      if (url.includes('/snapshot')) {
        snapshotCalls += 1
        // First (subscribe-time) fetch resolves at once; the interval poll hangs.
        if (snapshotCalls === 1) return SNAPSHOT
        return new Promise((resolve) => {
          pending.push(() => resolve(SNAPSHOT))
        })
      }
      throw new Error(`unexpected ${url}`)
    })
    const handlers: Array<(payload: unknown) => void> = []
    const fakeChannel: Record<string, unknown> = {}
    fakeChannel.on = (_event: unknown, _filter: unknown, handler: (payload: unknown) => void) => {
      handlers.push(handler)
      return fakeChannel
    }
    fakeChannel.subscribe = (cb: (status: string) => void) => {
      cb('SUBSCRIBED')
      return fakeChannel
    }
    mockedChannel.mockImplementation(() => fakeChannel as never)

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
    expect(seen.latest?.messages).toEqual([])

    // Fire the interval poll and leave its snapshot request in flight.
    await act(async () => {
      vi.advanceTimersByTime(6000)
    })
    expect(pending.length).toBe(1)

    // A broadcast arrives after the snapshot DB read but before it resolves.
    const ev = {
      type: 'submission.created',
      event_id: 'submission:sub-1',
      campaign_id: 'c1',
      thread_id: 't1',
      // Must exceed the snapshot high-water mark (test snapshot revision 1).
      sequence: 2,
      id: 'sub-1',
      raw_content: 'hi',
      segments: [{ type: 'ooc', text: 'hi' }],
      user_id: 'u1',
      dedupe_key: 'sub-1',
    }
    await act(async () => {
      handlers.forEach((h) => h({ payload: ev }))
    })
    expect(seen.latest?.messages.map((m) => m.id)).toContain('sub-1')

    // The older snapshot resolves and is adopted: the raced broadcast must survive.
    await act(async () => {
      pending.forEach((resolve) => resolve())
    })
    expect(seen.latest?.messages.map((m) => m.id)).toContain('sub-1')

    await act(async () => {
      root.unmount()
    })
    container.remove()
  })

  it('drops stale snapshot responses that resolve out of order', async () => {
    const mkSnap = (revision: number) => ({
      history: { messages: [], pagination: { next_cursor: null, has_more: false } },
      dm_state: null,
      dm_messages: [],
      revision,
    })
    const deferred: Array<() => void> = []
    let snapshotCalls = 0
    mockedFetch.mockImplementation(async (url: string) => {
      if (url.includes('/realtime/authorize')) return {}
      if (url.includes('/snapshot')) {
        snapshotCalls += 1
        if (snapshotCalls === 1) return mkSnap(1)
        // Older request hangs; newer request hangs separately.
        const snap = snapshotCalls === 2 ? mkSnap(10) : mkSnap(11)
        return new Promise((resolve) => {
          deferred.push(() => resolve(snap))
        })
      }
      throw new Error(`unexpected ${url}`)
    })
    const fakeChannel: Record<string, unknown> = {}
    fakeChannel.on = (..._args: unknown[]) => fakeChannel
    fakeChannel.subscribe = (cb: (status: string) => void) => {
      cb('SUBSCRIBED')
      return fakeChannel
    }
    mockedChannel.mockImplementation(() => fakeChannel as never)

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
    expect(seen.latest?.revision).toBe(1)

    // Two overlapping refreshes: older request (rev 10) starts first.
    await act(async () => {
      void seen.latest?.refresh()
    })
    await act(async () => {
      void seen.latest?.refresh()
    })
    expect(deferred.length).toBe(2)

    // Newer response (rev 11) resolves first and is adopted.
    await act(async () => {
      deferred[1]()
    })
    expect(seen.latest?.revision).toBe(11)

    // Older response (rev 10) resolves last and must be dropped.
    await act(async () => {
      deferred[0]()
    })
    expect(seen.latest?.revision).toBe(11)

    await act(async () => {
      root.unmount()
    })
    container.remove()
  })
})
