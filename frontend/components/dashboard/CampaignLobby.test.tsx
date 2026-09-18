// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CampaignLobby from './CampaignLobby'
import { campaignMembers, campaigns, characters } from '@/lib/api'
import type { Campaign, User } from '@/types'

vi.mock('@/lib/api', () => ({
  campaignMembers: {
    getLobby: vi.fn(), listMembers: vi.fn(), listInvites: vi.fn(), createInvite: vi.fn(),
    revokeInvite: vi.fn(), sendInviteEmail: vi.fn(),
    setReadiness: vi.fn(), selectCharacter: vi.fn(),
  },
  campaigns: { transitionLifecycle: vi.fn() },
  characters: { list: vi.fn() },
}))

let container: HTMLDivElement
let root: Root
let serverRevision: number
let remoteReady: boolean
let onBegin = vi.fn<() => void>()

beforeEach(() => {
  vi.useFakeTimers()
  vi.resetAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  serverRevision = 0
  remoteReady = false
  onBegin = vi.fn()
  vi.mocked(campaignMembers.getLobby).mockImplementation(async () => ({
    campaign: { id: 'campaign', revision: serverRevision } as Campaign,
    members: [
      { user_id: 'owner', role: 'owner', username: 'Owner', is_ready: false, selected_character_id: 'hero' },
      { user_id: 'friend', role: 'player', username: 'Friend', is_ready: remoteReady },
    ],
    eligibility: { eligible: remoteReady, blockers: remoteReady ? [] : ['Friend not ready'] },
    launch_locked: false,
  }))
  vi.mocked(campaignMembers.listInvites).mockResolvedValue({ invites: [] })
  vi.mocked(characters.list).mockResolvedValue({ characters: [] })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.useRealTimers()
})

async function renderLobby() {
  await act(async () => root.render(<CampaignLobby
    campaign={{ id: 'campaign', name: 'Table', revision: 0 } as Campaign}
    currentUser={{ id: 'owner' } as User} isOwner onBegin={onBegin}
  />))
}

function button(label: string) {
  const element = [...container.querySelectorAll('button')].find((b) => b.textContent?.includes(label))
  if (!element) throw new Error(`Missing button: ${label}`)
  return element
}

describe('authoritative lobby synchronization', () => {
  it('observes another client becoming ready and starts with the refreshed revision', async () => {
    await renderLobby()
    expect(button('Not ready to begin').disabled).toBe(true)
    serverRevision = 3
    remoteReady = true
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(button('Begin adventure').disabled).toBe(false)

    let complete!: (value: { campaign: Campaign }) => void
    vi.mocked(campaigns.transitionLifecycle).mockReturnValue(new Promise((resolve) => { complete = resolve }))
    await act(async () => button('Begin adventure').click())
    expect(campaigns.transitionLifecycle).toHaveBeenCalledWith('campaign', 3, 'starting', expect.any(String))
    expect(onBegin).not.toHaveBeenCalled()
    expect(button('Begin adventure').disabled).toBe(true)
    await act(async () => complete({ campaign: { id: 'campaign', revision: 4 } as Campaign }))
    expect(onBegin).toHaveBeenCalledOnce()
  })

  it('refreshes after a revision conflict so readiness can be retried', async () => {
    await renderLobby()
    serverRevision = 2
    vi.mocked(campaignMembers.setReadiness).mockRejectedValueOnce(Object.assign(new Error('Revision conflict'), { status: 409 }))
    // Readiness lives in the character modal now (Radix portals to document.body).
    const docButton = (label: string) => {
      const element = [...document.querySelectorAll('button')].find((b) => b.textContent?.includes(label))
      if (!element) throw new Error(`Missing button: ${label}`)
      return element as HTMLButtonElement
    }
    await act(async () => { button('(you)').click() })
    await act(async () => { docButton('Mark ready').click() })
    expect(document.body.textContent).toContain('Revision conflict')
    await act(async () => { docButton('Mark ready').click() })
    expect(campaignMembers.setReadiness).toHaveBeenLastCalledWith('campaign', 2, true, expect.any(String))
  })

  it('hides bearer invite links from non-owners', async () => {
    vi.mocked(campaignMembers.getLobby).mockResolvedValueOnce({
      campaign: { id: 'campaign', revision: 0 } as Campaign,
      members: [
        { user_id: 'owner', role: 'owner', username: 'Owner', is_ready: false },
      ],
      eligibility: { eligible: false, blockers: [] },
      launch_locked: false,
      invites: [
        {
          id: 'inv-1', campaign_id: 'campaign', status: 'active', usable: true,
          recipient_label: 'Sam', intended_email_hint: 'f***@example.com',
        },
      ],
      outstanding_invites: 1,
    })
    await act(async () => root.render(<CampaignLobby
      campaign={{ id: 'campaign', name: 'Table', revision: 0 } as Campaign}
      currentUser={{ id: 'friend' } as User} isOwner={false} onBegin={onBegin}
    />))
    // Outstanding invitee is visible in the invited-players modal; the bearer link never is.
    // (Icon-only button, so query by aria-label rather than text.)
    const peopleButton = container.querySelector('button[aria-label="See who\'s been invited"]')
    if (!peopleButton) throw new Error('Missing button: See who\'s been invited')
    await act(async () => { (peopleButton as HTMLButtonElement).click() })
    expect(document.body.textContent).toContain('Sam')
    expect(document.body.textContent).toContain('invited')
    expect(document.body.textContent).not.toContain('/invite/')
  })

  it('keeps the lobby open when the lifecycle transition fails', async () => {
    remoteReady = true
    await renderLobby()
    vi.mocked(campaigns.transitionLifecycle).mockRejectedValue(new Error('Party is no longer ready'))
    await act(async () => button('Begin adventure').click())
    expect(onBegin).not.toHaveBeenCalled()
    expect(container.textContent).toContain('Party is no longer ready')
  })
})
