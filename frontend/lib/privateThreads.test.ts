import { afterEach, describe, expect, it, vi } from 'vitest'
import { gameplayThreads } from './api'
import { privateThreadLabel, upsertVisibleThread } from './privateThreads'
import type { CampaignMember, CampaignThread } from '@/types'

const members: CampaignMember[] = [
  { user_id: 'u1', username: 'Avery', role: 'player' },
  { user_id: 'u2', username: 'Bryn', role: 'player' },
]

function thread(overrides: Partial<CampaignThread> = {}): CampaignThread {
  return {
    id: 't1',
    campaign_id: 'c1',
    thread_type: 'private',
    private_kind: 'dm',
    members: [{ thread_id: 't1', user_id: 'u1', role: 'member' }],
    ...overrides,
  }
}

afterEach(() => vi.unstubAllGlobals())

describe('private gameplay thread UX helpers', () => {
  it('labels the AI DM without inventing a human DM participant', () => {
    expect(privateThreadLabel(thread(), members, 'u1')).toBe('AI DM')
  })

  it('labels direct threads using the other authorized player', () => {
    const direct = thread({
      private_kind: 'direct',
      members: [
        { thread_id: 't1', user_id: 'u1', role: 'member' },
        { thread_id: 't1', user_id: 'u2', role: 'member' },
      ],
    })
    expect(privateThreadLabel(direct, members, 'u1')).toBe('Bryn')
    expect(privateThreadLabel(direct, members, 'u2')).toBe('Avery')
  })

  it('upserts an idempotently returned thread instead of duplicating navigation', () => {
    const original = thread({ title: 'old' })
    const updated = thread({ title: 'new' })
    expect(upsertVisibleThread([original], updated)).toEqual([updated])
  })
})

describe('private gameplay thread API client', () => {
  it('uses the dedicated idempotent AI-DM route', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ thread: thread(), created: false }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await gameplayThreads.getOrCreateDm('c1')

    expect(fetchMock).toHaveBeenCalledWith('/api/campaigns/c1/threads/dm', expect.objectContaining({ method: 'POST' }))
  })

  it('sends direct-thread participants and submission idempotency keys', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
    vi.stubGlobal('fetch', fetchMock)

    await gameplayThreads.getOrCreateDirect('c1', 'u2')
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/campaigns/c1/threads/direct', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ participant_id: 'u2' }),
    }))

    await gameplayThreads.submit('c1', 't1', 'quiet plan', 'op-1')
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/campaigns/c1/submissions', expect.objectContaining({
      headers: expect.objectContaining({ 'Idempotency-Key': 'op-1' }),
      body: JSON.stringify({ thread_id: 't1', content: 'quiet plan', operation_id: 'op-1' }),
    }))
  })
})
