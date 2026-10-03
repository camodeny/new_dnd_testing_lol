// @vitest-environment jsdom
import React, { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import CampaignTable from './CampaignTable'
import { campaignMembers, gameplayThreads } from '@/lib/api'
import type { TableEncounter, TableProjection } from '@/lib/table'
import type { PlayerRollForRealtime } from '@/lib/realtime'
import type { Campaign, Message, Session, User } from '@/types'

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('next/link', () => ({ default: ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => <a href={href} {...rest}>{children}</a> }))
vi.mock('@/lib/api', () => ({
  apiFetch: vi.fn(),
  campaignMembers: { listMembers: vi.fn() },
  gameplayThreads: { list: vi.fn(), getOrCreateDm: vi.fn(), getOrCreateDirect: vi.fn() },
}))
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = function () {}

let container: HTMLDivElement
let root: Root
const solo = { id: 'c', name: 'Sunken Archive', revision: 0, owner_id: 'me', required_players: 1 } as Campaign
const session = { id: 's', campaign_id: 'c', status: 'active', created_at: '2026-10-02T10:00:00Z' } as Session
const me = { id: 'me', username: 'camden' } as User
const messages = [
  { id: 'd1', session_id: 's', role: 'dm', content: 'Fog rolls off the black water.', created_at: '2026-10-02T10:00:00Z' },
  { id: 'p1', session_id: 's', role: 'player', content: 'I nod.', created_at: '2026-10-02T10:01:00Z', is_own: true, sender_name: 'Bryn' },
] as Message[]

const table: TableProjection = {
  character: {
    character_id: 'pc1', user_id: 'me', name: 'Bryn Holloway', is_self: true, class_label: 'Fighter', level: 3,
    health: 'hurt', conditions: [], hp: { current: 19, max: 28, temp: 0 }, armor_class: 16, speed: 30,
    resources: [{ name: 'Second Wind', current: 1, max: 1, recharge: 'short_rest' }],
    attacks: [{ name: 'Longsword', to_hit: 5, damage: '1d8+3', damage_type: 'slashing' }],
    skills: [{ name: 'Insight', modifier: 3, proficient: true }],
    roll_modifiers: { r1: { modifier: 3, label: 'Insight' } },
  },
  party: [{ character_id: 'pc1', user_id: 'me', name: 'Bryn Holloway', is_self: true, health: 'hurt', conditions: [] }],
  scene: { location_name: 'Gallows Ferry Landing', fictional_time: 'Night' },
  journal: { people: [{ entity_id: 'n1', name: 'Marta', role: 'ferrykeeper', facts: [] }], facts: [] },
  encounter: null,
}

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.mocked(campaignMembers.listMembers).mockResolvedValue({ members: [] })
  vi.mocked(gameplayThreads.list).mockResolvedValue({ threads: [] })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove() })

async function render(props: { campaign?: Campaign; table?: TableProjection | null; rollRequests?: PlayerRollForRealtime[]; onSendMessage?: (c: string) => Promise<void> }) {
  await act(async () => {
    root.render(
      <CampaignTable
        campaign={props.campaign ?? solo} session={session} messages={messages} hasOlderMessages={false}
        currentUser={me} table={props.table ?? table} rollRequests={props.rollRequests ?? []}
        aiThinking={false} aiThinkingStatus="" isOwner
        onSendMessage={props.onSendMessage ?? (async () => {})} onLoadOlderMessages={async () => {}}
        onRefresh={async () => {}} onStartSession={async () => {}} onExitToCampaigns={() => {}}
      />,
    )
  })
  await act(async () => {})
}

const tab = (name: string) => [...container.querySelectorAll('[role="tab"]')].find((t) => t.textContent?.startsWith(name)) as HTMLButtonElement | undefined

it('reads like a story with the place in the header and the character panel open', async () => {
  await render({})
  expect(container.querySelector('.tv-place')?.textContent).toBe('Gallows Ferry LandingNight')
  expect(container.querySelector('.tv-dm')?.textContent).toContain('Fog rolls off the black water.')
  expect(container.querySelector('.tv-you')?.textContent).toContain('I nod.')
  expect(container.querySelector('.tv-view')?.textContent).toContain('You’re hurt but steady.')
  expect(container.querySelector('.tv-view')?.textContent).toContain('How hard you are to hit')
})

it('hides the conversation switcher and Party tab in solo play', async () => {
  await render({})
  expect(container.querySelector('.tv-switcher')).toBeNull()
  expect(tab('Party')).toBeUndefined()
  await render({ campaign: { ...solo, required_players: 3 } })
  expect(container.querySelector('.tv-switcher')).not.toBeNull()
  expect(tab('Party')).toBeDefined()
})

it('opens the journal', async () => {
  await render({})
  await act(async () => tab('Journal')!.click())
  expect(container.querySelector('.tv-view')?.textContent).toContain('Marta')
})

it('puts the viewer’s pending roll in the conversation', async () => {
  await render({ rollRequests: [{ id: 'r1', requested_user_id: 'me', character_id: 'pc1', status: 'pending',
    label: 'Insight', reason_public: 'Is Marta lying?', advantage_state: 'normal' }] })
  expect(container.querySelector('.tv-thread .tv-roll')?.textContent).toContain('Is Marta lying?')
  expect(container.textContent).toContain('Roll for me')
})

it('sends Say drafts as speech', async () => {
  const onSendMessage = vi.fn(async () => {})
  await render({ onSendMessage })
  const say = [...container.querySelectorAll('[role="radio"]')].find((b) => b.textContent === 'Say') as HTMLButtonElement
  await act(async () => say.click())
  const ta = container.querySelector('textarea')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(ta, 'Who rang the bell?')
    ta.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => { ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })) })
  expect(onSendMessage).toHaveBeenCalledWith('"Who rang the bell?"')
})

it('lets the fight take over the screen, with the story alongside', async () => {
  const encounter: TableEncounter = {
    id: 'e', status: 'active', round: 2, active_participant_id: 'p1',
    participants: [
      { id: 'p1', kind: 'pc', display_name: 'Bryn Holloway', controller_user_id: 'me', sort_order: 0 },
      { id: 'p2', kind: 'monster', display_name: 'Drowned Sentry', sort_order: 1 },
    ],
    turn: { round: 2, active_participant_id: 'p1', resources: { p1: {
      action_available: true, bonus_action_available: false, reaction_available: true, movement_remaining: 20, movement_max: 30,
    } } },
    map: { width: 4, height: 3, zones: [{ id: 'z', kind: 'blocked', rect: { col: 3, row: 0, width: 1, height: 1 } }],
      placements: [{ participant_id: 'p1', col: 0, row: 0 }, { participant_id: 'p2', col: 2, row: 1 }] },
    reachable: { cells: [{ col: 1, row: 0, cost_feet: 5 }] },
  }
  await render({ table: { ...table, encounter } })
  expect(container.querySelector('.tv')?.classList.contains('combat')).toBe(true)
  expect(container.querySelector('.tv-fight')).not.toBeNull()
  expect(container.querySelector('.tv-turns')?.textContent).toContain('Round 2')
  expect(container.querySelector('.tv-turns')?.textContent).toContain('your turn')
  expect(container.querySelectorAll('.tv-cell')).toHaveLength(12)
  expect(container.querySelectorAll('.tv-cell.reach')).toHaveLength(1)
  expect(container.querySelectorAll('.tv-cell.blocked')).toHaveLength(1)
  expect(container.querySelector('.tv-budget .used')?.textContent).toBe('Bonus action')
  // The story keeps going beside the map; the panel is out of the way.
  expect(container.querySelector('.tv-side-chat .tv-dm')).not.toBeNull()
  expect(container.querySelector('.tv-panel')).toBeNull()
  // Action buttons start a description in the composer; the DM resolves it.
  const attack = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Attack with longsword')!
  await act(async () => attack.click())
  expect(container.querySelector('textarea')!.value).toBe('I attack with my longsword ')
})
