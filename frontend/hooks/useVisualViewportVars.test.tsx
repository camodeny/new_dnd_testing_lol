// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useVisualViewportVars } from './useVisualViewportVars'

class FakeVisualViewport extends EventTarget {
  height = 800
  offsetTop = 0
  scale = 1
}

let container: HTMLDivElement
let root: Root
let vv: FakeVisualViewport

function Harness() {
  useVisualViewportVars()
  return null
}

const vars = () => ({
  height: document.documentElement.style.getPropertyValue('--vv-height'),
  top: document.documentElement.style.getPropertyValue('--vv-top'),
})

async function emit(type: 'resize' | 'scroll') {
  await act(async () => {
    vv.dispatchEvent(new Event(type))
  })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 1 })
  vi.stubGlobal('cancelAnimationFrame', () => {})
  vv = new FakeVisualViewport()
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => { root.unmount() })
  container.remove()
  vi.unstubAllGlobals()
})

describe('useVisualViewportVars', () => {
  it('tracks the visible area as the keyboard opens and the page pans', async () => {
    await act(async () => { root.render(<Harness />) })
    expect(vars()).toEqual({ height: '800px', top: '0px' })

    vv.height = 420.4
    vv.offsetTop = 96
    await emit('resize')
    expect(vars()).toEqual({ height: '420px', top: '96px' })
  })

  it('holds still while the reader is pinch-zoomed', async () => {
    await act(async () => { root.render(<Harness />) })
    vv.scale = 2
    vv.height = 400
    vv.offsetTop = 200
    await emit('scroll')
    expect(vars()).toEqual({ height: '800px', top: '0px' })
  })

  it('clears the variables on unmount', async () => {
    await act(async () => { root.render(<Harness />) })
    await act(async () => { root.unmount() })
    root = createRoot(container)
    expect(vars()).toEqual({ height: '', top: '' })
  })
})
