import { describe, expect, it } from 'vitest'
import { composeForMode } from '@/components/table/Composer'
import {
  activeEncounter, buildTimeline, healthLabelFor, keptDie, orderedParticipants, panelTabs,
  rechargeSentence, rollD20s, rollDiceCount, rollInstructions, statusSentence,
  type TableCharacter, type TableProjection,
} from './table'
import type { PlayerRollForRealtime } from './realtime'
import type { Message } from '@/types'

const roll = (overrides: Partial<PlayerRollForRealtime> = {}): PlayerRollForRealtime => ({
  id: 'r', requested_user_id: 'me', character_id: 'pc1', status: 'pending', label: 'Insight',
  reason_public: 'Is she lying?', advantage_state: 'normal', ...overrides,
})

const character = (overrides: Partial<TableCharacter> = {}): TableCharacter => ({
  character_id: 'pc1', user_id: 'me', name: 'Bryn', is_self: true, health: 'hurt', conditions: [],
  hp: { current: 19, max: 28, temp: 0 }, resources: [], ...overrides,
})

describe('plain language', () => {
  it('summarises how the character is doing', () => {
    expect(statusSentence(character())).toBe('You’re hurt but steady.')
    expect(statusSentence(character({ resources: [{ name: 'Second Wind', current: 1, max: 1 }] })))
      .toBe('You’re hurt but steady. You can still use Second Wind to recover a little.')
    expect(statusSentence(character({ hp: { current: 28, max: 28, temp: 0 }, conditions: ['Poisoned'] })))
      .toBe('You’re in good shape. You’re poisoned.')
  })

  it('grades health without numbers', () => {
    expect(healthLabelFor({ current: 28, max: 28 })).toBe('unhurt')
    expect(healthLabelFor({ current: 15, max: 28 })).toBe('hurt')
    expect(healthLabelFor({ current: 14, max: 28 })).toBe('badly hurt')
    expect(healthLabelFor({ current: 0, max: 28 })).toBe('down')
  })

  it('explains recharges and rolls', () => {
    expect(rechargeSentence('short_rest')).toBe('Comes back after a short rest.')
    expect(rechargeSentence(null)).toBeNull()
    expect(rollInstructions(roll({ advantage_state: 'advantage' }), { modifier: 3, label: 'Insight' }))
      .toBe('Roll a 20-sided die and add +3 for Insight. You have advantage: roll twice and keep the higher one.')
    expect(rollInstructions(roll(), undefined)).toBe('Roll a 20-sided die and add your bonus.')
  })
})

describe('dice', () => {
  it('rolls in range and rejects biased draws', () => {
    const values = [0xffffffff, 0, 19, 39]
    const fake = (buf: Uint32Array) => { buf[0] = values.shift()!; return buf }
    // 0xffffffff is above the unbiased limit and is redrawn.
    expect(rollD20s(3, fake)).toEqual([1, 20, 20])
    for (const n of rollD20s(200)) expect(n >= 1 && n <= 20).toBe(true)
  })

  it('keeps the right die', () => {
    expect(rollDiceCount('advantage')).toBe(2)
    expect(rollDiceCount('normal')).toBe(1)
    expect(keptDie([4, 17], 'advantage')).toBe(17)
    expect(keptDie([4, 17], 'disadvantage')).toBe(4)
    expect(keptDie([9], 'normal')).toBe(9)
  })
})

describe('composer modes', () => {
  it('turns Say drafts into speech unless already quoted', () => {
    expect(composeForMode('Who rang the bell?', 'say')).toBe('"Who rang the bell?"')
    expect(composeForMode('I nod. "Hello."', 'say')).toBe('I nod. "Hello."')
    expect(composeForMode('I nod.', 'do')).toBe('I nod.')
  })
})

describe('timeline + panels', () => {
  it('interleaves public roll results with the story', () => {
    const messages = [
      { id: 'a', session_id: 's', role: 'dm', content: 'A', created_at: '2026-10-02T10:00:00Z' },
      { id: 'b', session_id: 's', role: 'dm', content: 'B', created_at: '2026-10-02T10:02:00Z' },
    ] as Message[]
    const rolls = [
      roll({ id: 'done', status: 'fulfilled', fulfilled_at: '2026-10-02T10:01:00Z', fulfillment: { total: 17 } }),
      roll({ id: 'secret', status: 'fulfilled', fulfilled_at: '2026-10-02T10:01:30Z', fulfillment: {} }),
      roll({ id: 'waiting' }),
    ]
    const party = [{ character_id: 'pc1', user_id: 'me', name: 'Bryn', is_self: true, health: null, conditions: [] }]
    const items = buildTimeline(messages, rolls, party, 'me')
    expect(items.map((i) => i.key)).toEqual(['m:a', 'r:done', 'm:b'])
    expect(items[1]).toMatchObject({ kind: 'roll', mine: true, who: 'Bryn' })
  })

  it('shows Party only with company and shops only while they are here', () => {
    expect(panelTabs(false)).toEqual(['character', 'journal'])
    expect(panelTabs(true)).toEqual(['character', 'journal', 'party'])
    expect(panelTabs(false, [{ entity_id: 's1', name: 'Hal’s' }])).toEqual(['character', 'journal', 'shop:s1'])
  })

  it('treats only an active, healthy encounter as combat', () => {
    const base = { character: null, party: [], scene: null, journal: { people: [], facts: [] } }
    const encounter = { id: 'e', status: 'active', participants: [] }
    expect(activeEncounter({ ...base, encounter } as TableProjection)).not.toBeNull()
    expect(activeEncounter({ ...base, encounter: { ...encounter, status: 'ended' } } as TableProjection)).toBeNull()
    expect(activeEncounter({ ...base, encounter: { error: 'projection_failed' } } as TableProjection)).toBeNull()
    expect(orderedParticipants({ ...encounter, participants: [
      { id: 'b', kind: 'pc', display_name: 'B', sort_order: 1 },
      { id: 'x', kind: 'pc', display_name: 'X', sort_order: null },
      { id: 'a', kind: 'monster', display_name: 'A', sort_order: 0 },
    ] }).map((p) => p.id)).toEqual(['a', 'b', 'x'])
  })
})
