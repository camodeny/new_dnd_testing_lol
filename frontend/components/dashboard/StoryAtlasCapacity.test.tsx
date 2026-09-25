// @vitest-environment jsdom
import React, { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import StoryAtlas from './StoryAtlas'
import { campaignMembers, gameplayThreads } from '@/lib/api'
import type { Campaign, Character, Message, Session, User } from '@/types'

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('@/lib/api', () => ({
  campaignMembers: { listMembers: vi.fn() },
  gameplayThreads: {
    list: vi.fn(),
    getOrCreateDm: vi.fn(),
    getOrCreateDirect: vi.fn(),
    submit: vi.fn(),
  },
}))

let container: HTMLDivElement
let root: Root

// jsdom has no layout engine: stub the auto-scroll the live table performs.
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function () {}
}

const campaign = { id: 'c', name: 'Table', revision: 0, owner_id: 'owner' } as Campaign
const characters = [{ id: 'pc1', name: 'Hero', race: 'Human' }] as Character[]
const session = { id: 's', campaign_id: 'c', status: 'active', created_at: new Date().toISOString() } as Session
const currentUser = { id: 'me', username: 'Me' } as User
const messages = [{
  id: 'm1',
  session_id: 's',
  role: 'player',
  content: 'I light a torch.',
  created_at: new Date().toISOString(),
  sender_name: 'Me',
}] as Message[]

const onSendMessage = vi.fn(async () => {})
const onCapacityEvent = vi.fn()

beforeEach(() => {
  vi.clearAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.mocked(campaignMembers.listMembers).mockResolvedValue({ members: [] })
  vi.mocked(gameplayThreads.list).mockResolvedValue({ threads: [] })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => { root.unmount() })
  container.remove()
})

async function renderAtlas(aiPaused: boolean) {
  await act(async () => {
    root.render(
      <StoryAtlas
        campaign={campaign}
        characters={characters}
        session={session}
        messages={messages}
        hasOlderMessages={false}
        currentUser={currentUser}
        currentCharacter={characters[0]}
        encounterMap={null}
        aiThinking={false}
        aiThinkingStatus=""
        isOwner={false}
        aiPaused={aiPaused}
        capacitySlot={<div>Campaign capacity notice</div>}
        onCapacityEvent={onCapacityEvent}
        onSendMessage={onSendMessage}
        onLoadOlderMessages={async () => {}}
        onStartSession={async () => {}}
        onEncounterMapChange={() => {}}
        onExitToCampaigns={() => {}}
      />,
    )
  })
  await act(async () => {})
}

function textarea(): HTMLTextAreaElement {
  const el = container.querySelector('textarea')
  if (!el) throw new Error('composer textarea missing')
  return el as HTMLTextAreaElement
}

async function typeDraft(text: string) {
  const el = textarea()
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!
    setter.call(el, text)
    el.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

async function pressEnter() {
  const el = textarea()
  await act(async () => {
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
  })
  await act(async () => {})
}

describe('StoryAtlas capacity pause (#255)', () => {
  it('keeps the unsent draft editable while paused', async () => {
    await renderAtlas(true)
    expect(textarea().disabled).toBe(false)
    await typeDraft('I sneak ahead quietly.')
    expect(textarea().value).toBe('I sneak ahead quietly.')
    expect(container.textContent).toContain('your words stay here as a draft')
  })

  it('does not submit or fake-accept the draft while paused, and tracks the attempt', async () => {
    await renderAtlas(true)
    await typeDraft('I sneak ahead quietly.')
    const send = container.querySelector('button[aria-label*="paused"]')
    expect(send).not.toBeNull()
    expect((send as HTMLButtonElement).disabled).toBe(true)
    await pressEnter()
    expect(onSendMessage).not.toHaveBeenCalled()
    expect(onCapacityEvent).toHaveBeenCalledWith({ type: 'paused_submit_attempt' })
    // The draft is kept verbatim — never cleared as accepted/processing.
    expect(textarea().value).toBe('I sneak ahead quietly.')
  })

  it('keeps non-AI surfaces accessible while paused', async () => {
    await renderAtlas(true)
    const text = container.textContent ?? ''
    // Chat history, capacity notice, private-thread nav, and party roster.
    expect(text).toContain('I light a torch.')
    expect(text).toContain('Campaign capacity notice')
    expect(text).toContain('Conversations')
    expect(text).toContain('Campaign table')
    expect(text).toContain('Hero')
  })

  it('sends normally when capacity is not paused', async () => {
    await renderAtlas(false)
    await typeDraft('I charge the gate.')
    await pressEnter()
    expect(onSendMessage).toHaveBeenCalledWith('I charge the gate.')
    expect(onCapacityEvent).not.toHaveBeenCalled()
  })
})
