import { describe, it, expect } from 'vitest'
import {
  dedupeEvents,
  deriveDmState,
  reconcileBufferedEvents,
  sortEventsBySequence,
  type RealtimeEvent,
  type SnapshotForRealtime,
} from './realtime'

describe('realtime helpers — dedupe/out-of-order/stream-scoped', () => {
  it('dedupeEvents drops duplicate event_id', () => {
    const evs: RealtimeEvent[] = [
      { type: 'dm.chunk', event_id: 'c1', campaign_id: 'c', thread_id: 't', sequence: 6, stream_id: 's', text: 'a' },
      { type: 'dm.chunk', event_id: 'c1', campaign_id: 'c', thread_id: 't', sequence: 6, stream_id: 's', text: 'a' },
      { type: 'dm.chunk', event_id: 'c2', campaign_id: 'c', thread_id: 't', sequence: 7, stream_id: 's', text: 'b' },
    ]
    expect(dedupeEvents(evs).map((e) => e.event_id)).toEqual(['c1', 'c2'])
  })

  it('reconcileBufferedEvents is stream-scoped: stale A chunk/status dropped after rollover to B', () => {
    const snap: SnapshotForRealtime = {
      revision: 5,
      reconciliation: { history_high_water_mark: 5 },
      dm_state: { status: 'streaming', streaming: true, stream_id: 'stream-B', last_sequence: 2, visible_text: 'hello world', chunk_count: 3 } as any,
      dm_messages: [{ id: 'stream-A', turn_id: 'tA', attempt_id: 'aA', final_text: 'old', status: 'completed' } as any],
      history: { messages: [] },
    }
    const buffered: RealtimeEvent[] = [
      { type: 'dm.chunk', event_id: 'cA', campaign_id: 'c', thread_id: 't', stream_id: 'stream-A', sequence: 1, text: 'stale A' },
      { type: 'dm.chunk', event_id: 'cB', campaign_id: 'c', thread_id: 't', stream_id: 'stream-B', sequence: 3, text: 'new B' },
      { type: 'dm.chunk', event_id: 'cC', campaign_id: 'c', thread_id: 't', stream_id: 'stream-C', sequence: 0, text: 'new C' },
      { type: 'dm.status', event_id: 'sA', campaign_id: 'c', thread_id: 't', stream_id: 'stream-A', status: 'completed' },
      { type: 'dm.status', event_id: 'sB', campaign_id: 'c', thread_id: 't', stream_id: 'stream-B', status: 'streaming' },
      { type: 'submission.created', event_id: 'sub6', campaign_id: 'c', thread_id: 't', sequence: 6, id: 'sub6' },
      { type: 'submission.created', event_id: 'sub5', campaign_id: 'c', thread_id: 't', sequence: 5, id: 'sub5' },
    ]
    const rec = reconcileBufferedEvents(snap, buffered)
    const ids = rec.map((r) => r.event_id)
    expect(ids).not.toContain('cA')
    expect(ids).toContain('cB')
    expect(ids).toContain('cC')
    expect(ids).not.toContain('sA')
    expect(ids).toContain('sB')
    expect(ids).toContain('sub6')
    expect(ids).not.toContain('sub5')
  })

  describe('deriveDmState — duplicate and out-of-order convergence', () => {
    const base = {
      status: 'streaming' as const,
      streaming: true,
      stream_id: 's1',
      last_sequence: 5,
      visible_text: 'hello ',
      chunk_count: 6,
    } as any

    it('duplicate chunk 6 does not duplicate visible_text / last_sequence / chunk_count', () => {
      const chunk6: RealtimeEvent = { type: 'dm.chunk', event_id: 'dm-chunk:s1:6', campaign_id: 'c', thread_id: 't', stream_id: 's1', sequence: 6, text: 'world' }
      const dup6: RealtimeEvent = { type: 'dm.chunk', event_id: 'dm-chunk:s1:6', campaign_id: 'c', thread_id: 't', stream_id: 's1', sequence: 6, text: 'world' }
      // first application
      let state = deriveDmState(base, [chunk6])
      expect(state?.visible_text).toBe('hello world')
      expect(state?.last_sequence).toBe(6)
      expect(state?.chunk_count).toBe(7)
      // duplicate
      state = deriveDmState(base, [chunk6, dup6])
      expect(state?.visible_text).toBe('hello world')
      expect(state?.last_sequence).toBe(6)
      expect(state?.chunk_count).toBe(7)
      // sequence duplicate with different event_id — helper's contiguous logic treats second 6 as already incorporated (seq < expected)
      const dupSeq: RealtimeEvent = { type: 'dm.chunk', event_id: 'different-id', campaign_id: 'c', thread_id: 't', stream_id: 's1', sequence: 6, text: 'world' }
      state = deriveDmState(base, [chunk6, dupSeq])
      expect(state?.visible_text).toBe('hello world')
      expect(state?.last_sequence).toBe(6)
      expect(state?.chunk_count).toBe(7)
    })

    it('7-before-6 derives visible_text in sorted contiguous order and waits for gap', () => {
      const chunk6: RealtimeEvent = { type: 'dm.chunk', event_id: 'c6', campaign_id: 'c', thread_id: 't', stream_id: 's1', sequence: 6, text: 'world' }
      const chunk7: RealtimeEvent = { type: 'dm.chunk', event_id: 'c7', campaign_id: 'c', thread_id: 't', stream_id: 's1', sequence: 7, text: '!' }
      // Only 7 arrives first — gap at 6, so visible_text stays base
      let state = deriveDmState(base, [chunk7])
      expect(state?.visible_text).toBe('hello ')
      expect(state?.last_sequence).toBe(5)
      expect(state?.chunk_count).toBe(6)
      // Then 6 arrives — now both 6 and 7 are contiguous
      state = deriveDmState(base, [chunk7, chunk6])
      expect(state?.visible_text).toBe('hello world!')
      expect(state?.last_sequence).toBe(7)
      expect(state?.chunk_count).toBe(8)
      // Also 6,7 in order
      state = deriveDmState(base, [chunk6, chunk7])
      expect(state?.visible_text).toBe('hello world!')
    })

    it('contiguous advancement only: gap at 6 blocks 7 and 8', () => {
      const chunks: RealtimeEvent[] = [
        { type: 'dm.chunk', event_id: 'c7', campaign_id: 'c', thread_id: 't', stream_id: 's1', sequence: 7, text: 'b' },
        { type: 'dm.chunk', event_id: 'c8', campaign_id: 'c', thread_id: 't', stream_id: 's1', sequence: 8, text: 'c' },
      ]
      let state = deriveDmState(base, chunks)
      expect(state?.visible_text).toBe('hello ')
      // add 6
      chunks.push({ type: 'dm.chunk', event_id: 'c6', campaign_id: 'c', thread_id: 't', stream_id: 's1', sequence: 6, text: 'a' })
      state = deriveDmState(base, chunks)
      expect(state?.visible_text).toBe('hello abc')
    })
  })
})
