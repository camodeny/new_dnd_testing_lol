// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import HomePage from './page'
import { campaigns } from '@/lib/api'

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('next/link', () => ({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  default: ({ children, href }: any) => <a href={typeof href === 'string' ? href : ''}>{children}</a>,
}))
vi.mock('@/contexts/AuthContext', () => {
  // Stable identity: HomePage's list effect depends on `user`, so a fresh
  // object per call would retrigger the effect forever.
  const user = { id: 'owner' }
  return { useAuthContext: () => ({ user }) }
})
vi.mock('@/lib/api', () => ({
  campaigns: { list: vi.fn(), delete: vi.fn(), transitionLifecycle: vi.fn(), restoreTarget: vi.fn() },
  campaignMembers: { lookupInvite: vi.fn() },
}))

const archivedCampaign = {
  id: 'campaign-archived',
  name: 'Sleeping Table',
  status: 'archived',
  revision: 3,
  owner_id: 'owner',
}

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.resetAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.mocked(campaigns.list).mockImplementation(async (includeArchived = false) =>
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (includeArchived ? { campaigns: [archivedCampaign] } : { campaigns: [] }) as any,
  )
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
})

async function renderHome() {
  await act(async () => {
    root.render(<HomePage />)
  })
  // Flush the list() promises + state updates.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0))
  })
}

describe('home with only archived campaigns (issue #265)', () => {
  it('renders the Archived/Restore section instead of the welcome screen', async () => {
    await renderHome()
    const text = document.body.textContent ?? ''
    expect(text).not.toContain('WELCOME TO FIRESIDE')
    expect(text).toContain('ARCHIVED')
    expect(text).toContain('Sleeping Table')
  })

  it('still shows the welcome screen when there are no campaigns at all', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    vi.mocked(campaigns.list).mockResolvedValue({ campaigns: [] } as any)
    await renderHome()
    expect(document.body.textContent ?? '').toContain('WELCOME TO FIRESIDE')
  })
})
