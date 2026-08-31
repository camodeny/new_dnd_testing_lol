import { describe, it, expect } from 'vitest'
import {
  dedupeEvents,
  deriveDmState,
  nextDmStateForStatus,
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

  describe('nextDmStateForStatus — A→B rollover', () => {
    const snapWithACompleted: SnapshotForRealtime = {
      dm_messages: [{ id: 'stream-A', turn_id: 'tA', attempt_id: 'aA', final_text: 'old', status: 'completed' } as any],
      dm_state: { status: 'streaming', streaming: true, stream_id: 'stream-A', last_sequence: 5, visible_text: 'A text', chunk_count: 6 } as any,
      history: { messages: [] },
    }
    // Actually after A completes, snapshot would have A completed in dm_messages and B not yet active;
    // but for hook's in-memory state, dmState is A streaming then A completed
    it('A-active snapshot → A completed → B streaming switches active', () => {
      let dmState: any = { status: 'streaming', streaming: true, stream_id: 'stream-A', last_sequence: 5, visible_text: 'A ', chunk_count: 6 }
      const snapForACompleted: SnapshotForRealtime = {
        dm_state: dmState,
        dm_messages: [],
        history: { messages: [] },
      }
      // A completed
      const aCompleted: RealtimeEvent = { type: 'dm.status', event_id: 'sA', campaign_id: 'c', thread_id: 't', stream_id: 'stream-A', status: 'completed', last_sequence: 5 } as any
      dmState = nextDmStateForStatus(dmState, aCompleted, snapForACompleted)!
      expect(dmState.stream_id).toBe('stream-A')
      expect(dmState.status).toBe('completed')

      // Snapshot now would have A completed, but our in-memory dmState is still A completed
      // Now B streaming arrives — should replace
      const snapWithACompletedNow: SnapshotForRealtime = {
        dm_state: dmState,
        dm_messages: [{ id: 'stream-A', turn_id: 'tA', attempt_id: 'aA', final_text: 'A ', status: 'completed' } as any],
        history: { messages: [] },
      }
      const bStreaming: RealtimeEvent = { type: 'dm.status', event_id: 'sB', campaign_id: 'c', thread_id: 't', stream_id: 'stream-B', status: 'streaming', visible_text: '', last_sequence: null, chunk_count: 0 } as any
      const next = nextDmStateForStatus(dmState, bStreaming, snapWithACompletedNow)
      expect(next?.stream_id).toBe('stream-B')
      expect(next?.status).toBe('streaming')
      expect(next?.visible_text).toBe('')
    })

    it('stale A completed after snapshot already has B active does not regress B', () => {
      const dmStateB: any = { status: 'streaming', streaming: true, stream_id: 'stream-B', last_sequence: 0, visible_text: '', chunk_count: 0 }
      const snapWithBActive: SnapshotForRealtime = {
        dm_state: dmStateB,
        dm_messages: [{ id: 'stream-A', turn_id: 'tA', attempt_id: 'aA', final_text: 'old', status: 'completed' } as any],
        history: { messages: [] },
      }
      const staleA: RealtimeEvent = { type: 'dm.status', event_id: 'sA2', campaign_id: 'c', thread_id: 't', stream_id: 'stream-A', status: 'completed' } as any
      const next = nextDmStateForStatus(dmStateB, staleA, snapWithBActive)
      // stale A should not mutate B
      expect(next).toBe(dmStateB)
      expect(next?.stream_id).toBe('stream-B')
    })

    it('idle snapshot → B streaming establishes B', () => {
      const snapIdle: SnapshotForRealtime = { dm_state: { status: 'idle', streaming: false, stream_id: null } as any, dm_messages: [], history: { messages: [] } }
      const bStreaming: RealtimeEvent = { type: 'dm.status', event_id: 'sB', campaign_id: 'c', thread_id: 't', stream_id: 'stream-B', status: 'streaming', visible_text: '' } as any
      const next = nextDmStateForStatus(null, bStreaming, snapIdle)
      expect(next?.stream_id).toBe('stream-B')
      expect(next?.status).toBe('streaming')
    })

    it('A streaming → A completed → B streaming → delayed A streaming does not regress B (runtime terminal)', () => {
      let dmState: any = { status: 'streaming', streaming: true, stream_id: 'stream-A', last_sequence: 5, visible_text: 'A ', chunk_count: 6 }
      const snapAActive: SnapshotForRealtime = { dm_state: dmState, dm_messages: [], history: { messages: [] } }
      const runtimeTerminals = new Set<string>()
      // A completed — becomes terminal
      const aCompleted: RealtimeEvent = { type: 'dm.status', event_id: 'sA-completed', campaign_id: 'c', thread_id: 't', stream_id: 'stream-A', status: 'completed' } as any
      dmState = nextDmStateForStatus(dmState, aCompleted, snapAActive, runtimeTerminals)!
      runtimeTerminals.add('stream-A')
      expect(dmState.status).toBe('completed')
      // B streaming — new stream, should switch
      const snapWithACompleted: SnapshotForRealtime = {
        dm_state: dmState,
        dm_messages: [{ id: 'stream-A', turn_id: 'tA', attempt_id: 'aA', final_text: 'A ', status: 'completed' } as any],
        history: { messages: [] },
      }
      const bStreaming: RealtimeEvent = { type: 'dm.status', event_id: 'sB', campaign_id: 'c', thread_id: 't', stream_id: 'stream-B', status: 'streaming', visible_text: '' } as any
      dmState = nextDmStateForStatus(dmState, bStreaming, snapWithACompleted, runtimeTerminals)!
      expect(dmState.stream_id).toBe('stream-B')
      // Delayed duplicate A streaming — should be rejected because A is terminal (in runtime set), even though snapshot still has A active? Use old snapshot where A is still active to simulate delayed
      const snapOldAActive: SnapshotForRealtime = { dm_state: { status: 'streaming', streaming: true, stream_id: 'stream-A', last_sequence: 5 } as any, dm_messages: [], history: { messages: [] } }
      const delayedAStreaming: RealtimeEvent = { type: 'dm.status', event_id: 'sA-delayed', campaign_id: 'c', thread_id: 't', stream_id: 'stream-A', status: 'streaming' } as any
      const nextAfterDelayed = nextDmStateForStatus(dmState, delayedAStreaming, snapOldAActive, runtimeTerminals)
      expect(nextAfterDelayed).toBe(dmState) // should not regress
      expect(nextAfterDelayed?.stream_id).toBe('stream-B')
    })

    it('abandoned old stream does not regress new B (absent from dm_messages)', () => {
      let dmState: any = { status: 'streaming', streaming: true, stream_id: 'stream-B', last_sequence: 0, visible_text: '', chunk_count: 0 }
      const snapWithBActive: SnapshotForRealtime = { dm_state: dmState, dm_messages: [], history: { messages: [] } }
      const runtimeTerminals = new Set<string>(['stream-A']) // A was abandoned, not in dm_messages
      const delayedAStreaming: RealtimeEvent = { type: 'dm.status', event_id: 'sA', campaign_id: 'c', thread_id: 't', stream_id: 'stream-A', status: 'streaming' } as any
      const next = nextDmStateForStatus(dmState, delayedAStreaming, snapWithBActive, runtimeTerminals)
      expect(next).toBe(dmState)
      expect(next?.stream_id).toBe('stream-B')
    })
  })
})
