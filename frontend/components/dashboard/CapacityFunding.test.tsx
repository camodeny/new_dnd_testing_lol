// @vitest-environment jsdom
import React, { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CapacityMeterView, type FundingControls } from './CapacityMeter'

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

const noop = () => {}
const controls = (overrides: Partial<FundingControls> = {}): FundingControls => ({
  onStartFunding: noop,
  fundingBusy: false,
  fundingError: null,
  ...overrides,
})

async function renderView(props: React.ComponentProps<typeof CapacityMeterView>) {
  await act(async () => { root.render(<CapacityMeterView {...props} />) })
}

describe('CapacityMeterView add-funds flow', () => {
  it('shows amount choice plus Add funds in the paused view', async () => {
    await renderView({
      view: { state: 'paused', percent: 100 },
      loading: false, error: null, hasProjection: true, onRetry: noop,
      funding: controls(),
    })
    const text = container.textContent ?? ''
    expect(text).toContain('Add funds')
    expect(text).toContain('Added funds land in the shared pool.')
    expect(text).toContain('Play resumes on its own.')
    expect(container.querySelector('[aria-label="Amount to add"]')).not.toBeNull()
  })

  it('starts checkout with the chosen amount', async () => {
    const onStartFunding = vi.fn()
    await renderView({
      view: { state: 'paused', percent: 100 },
      loading: false, error: null, hasProjection: true, onRetry: noop,
      funding: controls({ onStartFunding }),
    })
    const buttons = Array.from(container.querySelectorAll('button'))
    const preset25 = buttons.find((b) => b.textContent?.includes('25'))
    expect(preset25).toBeTruthy()
    await act(async () => { preset25!.click() })
    const add = buttons.find((b) => b.textContent === 'Add funds')!
    await act(async () => { add.click() })
    expect(onStartFunding).toHaveBeenCalledOnce()
    expect(onStartFunding).toHaveBeenCalledWith(2500)
  })

  it('defaults to the standard amount without a selection', async () => {
    const onStartFunding = vi.fn()
    await renderView({
      view: { state: 'paused', percent: 100 },
      loading: false, error: null, hasProjection: true, onRetry: noop,
      funding: controls({ onStartFunding }),
    })
    const add = Array.from(container.querySelectorAll('button'))
      .find((b) => b.textContent === 'Add funds')!
    await act(async () => { add.click() })
    expect(onStartFunding).toHaveBeenCalledWith(1000)
  })

  it('disables while checkout opens and shows funding errors', async () => {
    const onStartFunding = vi.fn()
    await renderView({
      view: { state: 'low', percent: 85 },
      loading: false, error: null, hasProjection: true, onRetry: noop,
      funding: controls({ onStartFunding, fundingBusy: true, fundingError: 'Checkout is down' }),
    })
    for (const button of Array.from(container.querySelectorAll('button'))) {
      expect(button.disabled).toBe(true)
    }
    expect(container.textContent).toContain('Checkout is down')
    expect(onStartFunding).not.toHaveBeenCalled()
  })

  it('keeps the funding row out of normal/grace views and unwired meters', async () => {
    await renderView({
      view: { state: 'normal', percent: 42 },
      loading: false, error: null, hasProjection: true, onRetry: noop,
      funding: controls(),
    })
    expect(container.textContent).not.toContain('Add funds')
    await renderView({
      view: { state: 'paused', percent: 100 },
      loading: false, error: null, hasProjection: true, onRetry: noop,
    })
    expect(container.textContent).not.toContain('Add funds')
    expect(container.textContent).toContain('added funds')
  })

  it('keeps funding copy campaign-level with no blame, upsell, or quality claims', async () => {
    await renderView({
      view: { state: 'paused', percent: 100 },
      loading: false, error: null, hasProjection: true, onRetry: noop,
      funding: controls(),
    })
    const text = (container.textContent ?? '').toLowerCase()
    for (const word of ['upgrade', 'premium', 'smarter', 'better dm', 'human dm',
      'game master', 'dice', 'loot', 'trust', 'token', 'cents', 'blame', 'fault',
      'you ', 'your ', 'model', 'quality']) {
      expect(text, `funding copy must not contain ${JSON.stringify(word)}`).not.toContain(word)
    }
  })
})
