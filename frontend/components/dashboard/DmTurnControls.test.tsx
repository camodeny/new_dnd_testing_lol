// @vitest-environment jsdom
import React, { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/api'
import DmTurnControls from './DmTurnControls'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
let container: HTMLDivElement
let root: Root
const refresh = vi.fn(async () => {})
beforeEach(() => {
  vi.clearAllMocks()
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove() })

it('shows pending work even before a narration stream exists', async () => {
  await act(async () => root.render(<DmTurnControls campaignId="c" dmState={{ status: 'thinking', streaming: false }}
    rolls={[]} refresh={refresh} />))
  expect(container.textContent).toContain('considering your action')
})

it('retries the saved attempt and reuses the command after a lost acknowledgement', async () => {
  vi.mocked(apiFetch).mockRejectedValueOnce(new Error('lost response')).mockResolvedValueOnce({})
  await act(async () => root.render(<DmTurnControls campaignId="c"
    dmState={{ status: 'failed_visible', streaming: false, turn_id: 't', attempt_id: 'a', can_retry: true }}
    rolls={[]} refresh={refresh} />))
  expect(container.textContent).toContain('Your action is saved')
  await act(async () => container.querySelector('button')!.click())
  expect(container.textContent).toContain('Retry could not be confirmed')
  await act(async () => container.querySelector('button')!.click())
  const calls = vi.mocked(apiFetch).mock.calls
  expect(calls[0][0]).toBe('/campaigns/c/dm-turns/t/retry')
  expect(calls[0][1]).toEqual(calls[1][1])
  expect(JSON.parse(String(calls[0][1]?.body))).toEqual({ attempt_id: 'a' })
  expect(refresh).toHaveBeenCalledOnce()
})

it('only provides roll entry for the assigned player and never displays private DCs', async () => {
  const rolls = ['me', 'other'].map((user, index) => ({ id: String(index), requested_user_id: user,
    character_id: `pc-${user}`, status: 'pending', label: 'Perception', reason_public: 'Study the fog',
    advantage_state: 'advantage', dc_private: 17 }))
  await act(async () => root.render(<DmTurnControls campaignId="c" userId="me" dmState={{ status: 'awaiting_roll', streaming: false }}
    rolls={rolls} refresh={refresh} />))
  expect(container.querySelectorAll('form')).toHaveLength(1)
  expect(container.textContent).toContain('Waiting for a player')
  expect(container.textContent).not.toContain('17')
})
