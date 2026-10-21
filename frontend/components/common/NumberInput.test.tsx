// @vitest-environment jsdom
import React, { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import NumberInput from './NumberInput'

let container: HTMLDivElement
let root: Root

function setNativeValue(element: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
  setter?.call(element, value)
  element.dispatchEvent(new Event('input', { bubbles: true }))
}

// Mirrors the sections: a cleared field falls back to its default.
function ClearingHarness({ initial = 10 }: { initial?: number }) {
  const [value, setValue] = useState<number | null>(initial)
  return <NumberInput aria-label="amount" value={value} min={0} onChange={(next) => setValue(next ?? 10)} />
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => { root.unmount() })
  container.remove()
})

async function renderHarness() {
  await act(async () => { root.render(<ClearingHarness />) })
  return container.querySelector('input') as HTMLInputElement
}

describe('NumberInput', () => {
  it('lets a value be cleared and retyped without snapping back', async () => {
    const input = await renderHarness()
    expect(input.value).toBe('10')

    await act(async () => { input.focus() })
    await act(async () => { setNativeValue(input, '') })
    // Parent coerced null back to 10, but the field stays empty mid-edit.
    expect(input.value).toBe('')

    await act(async () => { setNativeValue(input, '8') })
    expect(input.value).toBe('8')

    await act(async () => { input.blur() })
    expect(input.value).toBe('8')
  })

  it('restores the incoming value on blur when left empty', async () => {
    const input = await renderHarness()
    await act(async () => { input.focus() })
    await act(async () => { setNativeValue(input, '') })
    expect(input.value).toBe('')
    await act(async () => { input.blur() })
    expect(input.value).toBe('10')
  })

  it('uses a numeric keyboard only for fields that cannot go negative', async () => {
    await act(async () => {
      root.render(
        <>
          <NumberInput aria-label="non-negative" value={1} min={0} onChange={() => {}} />
          <NumberInput aria-label="negative" value={1} min={-30} onChange={() => {}} />
          <NumberInput aria-label="unbounded" value={1} onChange={() => {}} />
        </>,
      )
    })
    const byLabel = (label: string) => container.querySelector(`[aria-label="${label}"]`) as HTMLInputElement
    expect(byLabel('non-negative').getAttribute('inputmode')).toBe('numeric')
    expect(byLabel('negative').getAttribute('inputmode')).toBeNull()
    expect(byLabel('unbounded').getAttribute('inputmode')).toBeNull()
  })
})
