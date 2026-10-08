// @vitest-environment jsdom
import React, { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/api'
import ContinueCampaignControl from './ContinueCampaignControl'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
let container: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.clearAllMocks()
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove() })

const completed = {
  campaign_status: 'active',
  current_adventure_id: null,
  adventures: [{ id: 'a1', title: 'The Sunken Chapel', status: 'completed', outcome: 'villain_victory', public_summary: null }],
}
const continued = {
  ...completed,
  current_adventure_id: 'a2',
  adventures: [...completed.adventures, { id: 'a2', title: 'Ashes', status: 'active', outcome: null, public_summary: null }],
}
const button = () => container.querySelector('button[type="submit"]') as HTMLButtonElement | null

it('renders nothing while an adventure is still active', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(continued)
  await act(async () => root.render(<ContinueCampaignControl campaignId="c" isOwner />))
  expect(container.textContent).toBe('')
})

it('shows non-owners a neutral note and no control', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(completed)
  await act(async () => root.render(<ContinueCampaignControl campaignId="c" isOwner={false} />))
  expect(container.textContent).toContain('The Sunken Chapel is complete (villain victory)')
  expect(container.textContent).toContain('campaign owner opens the next adventure')
  expect(button()).toBeNull()
})

it('lets the owner continue and reuses the command after a lost acknowledgement', async () => {
  vi.mocked(apiFetch)
    .mockResolvedValueOnce(completed)
    .mockRejectedValueOnce(new Error('lost response'))
    .mockResolvedValueOnce({})
    .mockResolvedValueOnce(continued)
  await act(async () => root.render(<ContinueCampaignControl campaignId="c" isOwner />))
  expect(button()!.textContent).toBe('Continue campaign')
  await act(async () => button()!.click())
  expect(container.querySelector('[role="alert"]')!.textContent).toBe('lost response')
  await act(async () => button()!.click())
  const calls = vi.mocked(apiFetch).mock.calls
  expect(calls[1][0]).toBe('/campaigns/c/adventures')
  expect(calls[1][1]?.method).toBe('POST')
  expect(JSON.parse(String(calls[1][1]?.body))).toEqual({ title: 'Adventure 2' })
  expect(calls[1][1]?.headers).toEqual(calls[2][1]?.headers)
  expect(container.textContent).toBe('')
})

it('notices a completion when the table refreshes', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce({ ...continued, current_adventure_id: 'a1', adventures: [
    { ...completed.adventures[0], status: 'active', outcome: null }] })
  await act(async () => root.render(<ContinueCampaignControl campaignId="c" isOwner refreshKey={1} />))
  expect(button()).toBeNull()
  vi.mocked(apiFetch).mockResolvedValueOnce(completed)
  await act(async () => root.render(<ContinueCampaignControl campaignId="c" isOwner refreshKey={2} />))
  expect(button()).not.toBeNull()
})
