// @vitest-environment jsdom
import React, { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/api'
import CapacityMeter, { CapacityMeterView } from './CapacityMeter'
import type { CapacityUiEvent } from '@/lib/capacity'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.clearAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => { root.unmount() })
  container.remove()
})

async function renderView(props: React.ComponentProps<typeof CapacityMeterView>) {
  await act(async () => { root.render(<CapacityMeterView {...props} />) })
}

const noop = () => {}

describe('CapacityMeterView', () => {
  it('shows a simple normal percentage', async () => {
    await renderView({ view: { state: 'normal', percent: 42 }, loading: false, error: null, hasProjection: true, onRetry: noop })
    expect(container.textContent).toContain('Shared campaign capacity')
    expect(container.textContent).toContain('42% used')
    expect(container.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe('42')
  })

  it('notes low capacity in a low-key campaign-level way', async () => {
    await renderView({ view: { state: 'low', percent: 85 }, loading: false, error: null, hasProjection: true, onRetry: noop })
    expect(container.textContent).toContain('85% used')
    expect(container.textContent).toContain('Shared campaign capacity is getting low.')
  })

  it('explains grace without gameplay-alarm language', async () => {
    await renderView({ view: { state: 'grace', percent: 99 }, loading: false, error: null, hasProjection: true, onRetry: noop })
    expect(container.textContent).toContain('The current moment will finish')
    expect(container.textContent).toContain('may briefly pause')
  })

  it('pauses with a neutral multiplayer notice plus add-funds/BYOK hooks', async () => {
    await renderView({ view: { state: 'paused', percent: 100 }, loading: false, error: null, hasProjection: true, onRetry: noop })
    const text = container.textContent ?? ''
    expect(text).toContain('New AI narration is paused for the campaign until shared capacity returns.')
    expect(text).toContain('History, character sheets, and player chat stay available.')
    expect(text).toContain('nothing needs to be redone')
    expect(text).toContain('added funds')
    expect(text).toContain('BYOK')
  })

  it('never invents a percentage while loading and stays neutral on failure', async () => {
    const onRetry = vi.fn()
    await renderView({ view: { state: 'unavailable', percent: null }, loading: true, error: null, hasProjection: false, onRetry })
    expect(container.textContent).not.toMatch(/\d+% used/)
    expect(container.querySelector('[role="progressbar"]')).toBeNull()

    await act(async () => { root.render(
      <CapacityMeterView view={{ state: 'unavailable', percent: null }} loading={false} error="boom" hasProjection={false} onRetry={onRetry} />,
    ) })
    expect(container.textContent).toContain('Campaign capacity is unavailable right now.')
    expect(container.textContent).not.toMatch(/\d+% used/)
    await act(async () => { container.querySelector('button')!.click() })
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it('contains no blame, upsell, raw economics, or human-DM copy', async () => {
    const states = [
      { state: 'normal', percent: 42 },
      { state: 'low', percent: 85 },
      { state: 'grace', percent: 99 },
      { state: 'paused', percent: 100 },
    ] as const
    const banned = ['upgrade', 'premium', 'smarter', 'better dm', 'human dm', 'game master',
      'dice', 'loot', 'trust', 'token', 'cents', '$', 'you ', 'your ', 'yours', 'blame', 'fault']
    for (const view of states) {
      await renderView({ view, loading: false, error: null, hasProjection: true, onRetry: noop })
      const text = (container.textContent ?? '').toLowerCase()
      for (const word of banned) {
        expect(text, `${view.state} copy must not contain ${JSON.stringify(word)}`).not.toContain(word)
      }
    }
  })
})

describe('CapacityMeter container', () => {
  it('renders the paused notice from the authoritative projection and reports the view', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ campaign_id: 'c', percent_used: 100, ai_paused: true, grace_active: false })
    const events: CapacityUiEvent[] = []
    await act(async () => { root.render(<CapacityMeter campaignId="c" onEvent={(e) => events.push(e)} />) })
    await act(async () => {})
    expect(vi.mocked(apiFetch)).toHaveBeenCalledWith('/campaigns/c/capacity-state')
    expect(container.textContent).toContain('100% used')
    expect(container.textContent).toContain('New AI narration is paused for the campaign')
    expect(events).toContainEqual({ type: 'paused_view' })
  })

  it('resyncs to normal play after funding/BYOK restores capacity', async () => {
    vi.useFakeTimers()
    try {
      vi.mocked(apiFetch)
        .mockResolvedValueOnce({ campaign_id: 'c', percent_used: 100, ai_paused: true, grace_active: false })
        .mockResolvedValueOnce({ campaign_id: 'c', percent_used: 30, ai_paused: false, grace_active: false })
      await act(async () => { root.render(<CapacityMeter campaignId="c" />) })
      await act(async () => {})
      expect(container.textContent).toContain('New AI narration is paused for the campaign')

      // Restoration arrives through the same authoritative projection on the
      // next poll — no special recovery flow, no wizard.
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
      await act(async () => {})
      expect(vi.mocked(apiFetch)).toHaveBeenCalledTimes(2)
      expect(container.textContent).toContain('30% used')
      expect(container.textContent).not.toContain('New AI narration is paused')
    } finally {
      vi.useRealTimers()
    }
  })

  it('reports projection load errors and retries without inventing a percentage', async () => {
    vi.mocked(apiFetch).mockRejectedValueOnce(new Error('ledger down'))
    const events: CapacityUiEvent[] = []
    await act(async () => { root.render(<CapacityMeter campaignId="c" onEvent={(e) => events.push(e)} />) })
    await act(async () => {})
    expect(container.textContent).toContain('Campaign capacity is unavailable right now.')
    expect(container.textContent).not.toMatch(/\d+% used/)
    expect(events).toEqual([{ type: 'projection_load_error', message: 'ledger down' }])

    vi.mocked(apiFetch).mockResolvedValueOnce({ campaign_id: 'c', percent_used: 30, ai_paused: false })
    await act(async () => { container.querySelector('button')!.click() })
    await act(async () => {})
    expect(container.textContent).toContain('30% used')
  })
})
