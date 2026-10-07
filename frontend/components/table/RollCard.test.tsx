// @vitest-environment jsdom
import React, { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/api'
import RollCard from './RollCard'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
let container: HTMLDivElement
let root: Root
const refresh = vi.fn(async () => {})
const roll = {
  id: 'r1', requested_user_id: 'me', character_id: 'pc', status: 'pending', label: 'Insight',
  reason_public: 'Is Marta hiding something?', advantage_state: 'advantage', dc_private: 15,
}

beforeEach(() => {
  vi.clearAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove() })

const button = (text: string) => [...container.querySelectorAll('button')].find((b) => b.textContent === text)!

it('asks the question in story terms and never shows the DC', async () => {
  await act(async () => root.render(<RollCard campaignId="c" roll={roll} modifier={{ modifier: 3, label: 'Insight' }} refresh={refresh} />))
  expect(container.textContent).toContain('Is Marta hiding something?')
  expect(container.textContent).toContain('add +3 for Insight')
  expect(container.textContent).toContain('keep the higher one')
  expect(container.textContent).not.toContain('15')
})

it('rolls for the player and resends the same dice after a lost acknowledgement', async () => {
  vi.mocked(apiFetch).mockRejectedValueOnce(new Error('lost')).mockResolvedValueOnce({})
  await act(async () => root.render(<RollCard campaignId="c" roll={roll} modifier={{ modifier: 3, label: 'Insight' }} refresh={refresh} />))
  await act(async () => button('Roll for me').click())
  expect(container.textContent).toContain('could not be confirmed')
  await act(async () => button('Roll for me').click())
  const calls = vi.mocked(apiFetch).mock.calls
  expect(calls[0][0]).toBe('/campaigns/c/roll-requests/r1/fulfill')
  expect(calls[0][1]).toEqual(calls[1][1])
  const body = JSON.parse(String(calls[0][1]?.body))
  expect(body.source).toBe('app')
  expect(body.raw_rolls).toHaveLength(2)
  expect(body.total).toBe(Math.max(...body.raw_rolls) + 3)
  expect(refresh).toHaveBeenCalledOnce()
})

it('only offers typed totals when the modifier is unknown', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce({})
  await act(async () => root.render(<RollCard campaignId="c" roll={roll} refresh={refresh} />))
  expect(container.textContent).not.toContain('Roll for me')
  await act(async () => button('I rolled my own dice').click())
  const input = container.querySelector('input')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '14')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[0][1]?.body))).toEqual({ source: 'physical', total: 14 })
})

it('rolls a hit\'s damage dice and sums them', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce({})
  const damage = { ...roll, label: 'Longsword damage', advantage_state: 'normal', roll_kind: 'damage' }
  const modifier = { modifier: 3, label: 'Longsword damage', dice: { count: 2, sides: 8 } }
  await act(async () => root.render(<RollCard campaignId="c" roll={damage} modifier={modifier} refresh={refresh} />))
  expect(container.textContent).toContain('Roll 2d8 and add +3 for Longsword damage.')
  await act(async () => button('Roll for me').click())
  const body = JSON.parse(String(vi.mocked(apiFetch).mock.calls[0][1]?.body))
  expect(body.raw_rolls).toHaveLength(2)
  for (const n of body.raw_rolls) expect(n >= 1 && n <= 8).toBe(true)
  expect(body.total).toBe(body.raw_rolls[0] + body.raw_rolls[1] + 3)
})
