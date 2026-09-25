import { describe, expect, it } from 'vitest'
import type { Message } from '@/types'
import { mergeOptimisticMessages } from './optimisticMessages'

function msg(overrides: Partial<Message> & { id: string }): Message {
  return {
    session_id: 's1',
    role: 'player',
    content: 'hello',
    created_at: '2026-09-25T21:00:00.000Z',
    sender_name: 'Bryn',
    ...overrides,
  }
}

describe('mergeOptimisticMessages', () => {
  it('appends an unmatched optimistic entry in time order', () => {
    const server = [msg({ id: 'a', created_at: '2026-09-25T20:59:00.000Z' })]
    const pending = [msg({ id: 'pending:1', content: 'fresh words', created_at: '2026-09-25T21:01:00.000Z' })]
    const merged = mergeOptimisticMessages(server, pending)
    expect(merged.map((m) => m.id)).toEqual(['a', 'pending:1'])
  })

  it('retires the optimistic entry once the server echo arrives', () => {
    const server = [msg({ id: 'sub-9', content: 'fresh words', created_at: '2026-09-25T21:01:02.000Z' })]
    const pending = [msg({ id: 'pending:1', content: 'fresh words', created_at: '2026-09-25T21:01:00.000Z' })]
    expect(mergeOptimisticMessages(server, pending).map((m) => m.id)).toEqual(['sub-9'])
  })

  it('does not match echoes for different content or senders', () => {
    const server = [
      msg({ id: 'sub-9', content: 'other words', created_at: '2026-09-25T21:01:02.000Z' }),
      msg({ id: 'sub-10', content: 'fresh words', sender_name: 'Other', created_at: '2026-09-25T21:01:02.000Z' }),
    ]
    const pending = [msg({ id: 'pending:1', content: 'fresh words', created_at: '2026-09-25T21:01:00.000Z' })]
    // Pending is oldest so it sorts first; the tie between the two server
    // echoes breaks on id ('sub-10' < 'sub-9').
    expect(mergeOptimisticMessages(server, pending).map((m) => m.id))
      .toEqual(['pending:1', 'sub-10', 'sub-9'])
  })

  it('retires duplicate texts together while keeping every server echo', () => {
    const server = [
      msg({ id: 'sub-9', content: 'charge!', created_at: '2026-09-25T21:01:02.000Z' }),
      msg({ id: 'sub-10', content: 'charge!', created_at: '2026-09-25T21:01:03.000Z' }),
    ]
    const pending = [
      msg({ id: 'pending:1', content: 'charge!', created_at: '2026-09-25T21:01:00.000Z' }),
      msg({ id: 'pending:2', content: 'charge!', created_at: '2026-09-25T21:01:01.000Z' }),
    ]
    expect(mergeOptimisticMessages(server, pending).map((m) => m.id)).toEqual(['sub-9', 'sub-10'])
  })

  it('never matches DM messages against player pendings', () => {
    const server = [msg({ id: 'dm:1', role: 'dm', sender_name: 'Dungeon Master', content: 'fresh words' })]
    const pending = [msg({ id: 'pending:1', content: 'fresh words' })]
    expect(mergeOptimisticMessages(server, pending).map((m) => m.id)).toEqual(['dm:1', 'pending:1'])
  })
})
