// @vitest-environment jsdom
import React, { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { CapacityViewState } from '@/lib/capacity'
import { useLowCapacityDismissal } from './useLowCapacityDismissal'

let container: HTMLDivElement
let root: Root
const seen: { latest: ReturnType<typeof useLowCapacityDismissal> | null } = { latest: null }

function Harness(props: { campaignId: string | null; state: CapacityViewState }) {
  seen.latest = useLowCapacityDismissal(props.campaignId, props.state)
  return null
}

function renderHook(props: React.ComponentProps<typeof Harness>) {
  return act(async () => { root.render(<Harness {...props} />) })
}

const KEY = 'fireside:capacity-low-dismissed:c1'

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  window.localStorage.clear()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  seen.latest = null
})

afterEach(async () => {
  await act(async () => { root.unmount() })
  container.remove()
})

describe('useLowCapacityDismissal', () => {
  it('remembers a dismissal for the campaign across reloads', async () => {
    await renderHook({ campaignId: 'c1', state: 'low' })
    expect(seen.latest!.dismissed).toBe(false)
    await act(async () => { seen.latest!.dismiss() })
    expect(seen.latest!.dismissed).toBe(true)
    expect(window.localStorage.getItem(KEY)).toBe('1')

    await act(async () => { root.unmount() })
    root = createRoot(container)
    await renderHook({ campaignId: 'c1', state: 'low' })
    expect(seen.latest!.dismissed).toBe(true)
  })

  it('keeps the dismissal through loading or a failed projection', async () => {
    window.localStorage.setItem(KEY, '1')
    await renderHook({ campaignId: 'c1', state: 'unavailable' })
    expect(seen.latest!.dismissed).toBe(true)
    expect(window.localStorage.getItem(KEY)).toBe('1')
  })

  it('clears once capacity leaves the low band so the next low stretch shows again', async () => {
    for (const state of ['normal', 'grace', 'paused'] as const) {
      window.localStorage.setItem(KEY, '1')
      await renderHook({ campaignId: 'c1', state: 'low' })
      await renderHook({ campaignId: 'c1', state })
      expect(seen.latest!.dismissed, state).toBe(false)
      expect(window.localStorage.getItem(KEY), state).toBeNull()
    }
  })
})
