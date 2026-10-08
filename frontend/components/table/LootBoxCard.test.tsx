// @vitest-environment jsdom
import React, { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/api'
import type { TableLootBox } from '@/lib/table'
import LootBoxCard from './LootBoxCard'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
let container: HTMLDivElement
let root: Root
const refresh = vi.fn(async () => {})
const sealed: TableLootBox = {
  id: 'b1', title: "The bandit chief's strongbox", status: 'sealed', draws: 2, pool_size: 6,
  pool_rarities: { common: 4, uncommon: 1, rare: 1 },
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

it('shows the odds of a sealed box, never its items', async () => {
  await act(async () => root.render(<LootBoxCard campaignId="c" box={sealed} refresh={refresh} />))
  expect(container.textContent).toContain("You’ll find 2 of 6 possible items, plus coin.")
  expect(container.textContent).toContain('1 rare, 1 uncommon, 4 common')
})

it('opens the box, reveals the haul, and retries with the same key', async () => {
  const opened = {
    ...sealed, status: 'opened',
    contents: { items: [{ name: 'Flame Tongue', rarity: 'rare', quantity: 1 }, { name: 'Garnet', rarity: 'common', quantity: 2 }], gp: 24 },
  }
  vi.mocked(apiFetch).mockRejectedValueOnce(new Error('lost')).mockResolvedValueOnce({ loot_box: opened })
  await act(async () => root.render(<LootBoxCard campaignId="c" box={sealed} refresh={refresh} />))
  await act(async () => button('Open').click())
  expect(container.textContent).toContain('would not open')
  await act(async () => button('Open').click())
  const keys = vi.mocked(apiFetch).mock.calls.map(([, init]) => (init?.headers as Record<string, string>)['Idempotency-Key'])
  expect(keys[0]).toBe(keys[1])
  expect(vi.mocked(apiFetch).mock.calls[0][0]).toBe('/campaigns/c/loot-boxes/b1/open')
  expect(container.textContent).toContain('Flame Tongue')
  expect(container.textContent).toContain('2 × Garnet')
  expect(container.textContent).toContain('24 gold pieces')
  expect(refresh).toHaveBeenCalledTimes(1)
})
