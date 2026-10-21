// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useStickToBottom } from './useStickToBottom'

// jsdom has no layout: give the scroller a fixed geometry the test controls.
const geometry = { scrollHeight: 2000, clientHeight: 500 }
let resize: (() => void) | null = null
let container: HTMLDivElement
let root: Root
const seen: { latest: Pick<ReturnType<typeof useStickToBottom>, 'away' | 'scrollToBottom' | 'unpin' | 'sync'> | null } = { latest: null }

function Harness() {
  const story = useStickToBottom()
  seen.latest = story
  return <div ref={story.scrollRef} data-testid="scroller"><div ref={story.contentRef} /></div>
}

const scroller = () => container.querySelector('[data-testid="scroller"]') as HTMLDivElement

async function scrollTo(top: number) {
  await act(async () => {
    scroller().scrollTop = top
    scroller().dispatchEvent(new Event('scroll'))
  })
}

beforeEach(async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { resize = callback }
    observe() {}
    disconnect() {}
  })
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, get: () => geometry.scrollHeight })
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => geometry.clientHeight })
  HTMLElement.prototype.scrollTo = function (this: HTMLElement, options?: ScrollToOptions | number) {
    if (typeof options === 'object' && typeof options.top === 'number') this.scrollTop = options.top
  } as HTMLElement['scrollTo']
  geometry.scrollHeight = 2000
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  await act(async () => { root.render(<Harness />) })
})

afterEach(async () => {
  await act(async () => { root.unmount() })
  container.remove()
  vi.unstubAllGlobals()
})

describe('useStickToBottom', () => {
  it('starts at the latest entry and follows new content while pinned', async () => {
    expect(scroller().scrollTop).toBe(2000)
    geometry.scrollHeight = 2600
    await act(async () => resize?.())
    expect(scroller().scrollTop).toBe(2600)
    expect(seen.latest!.away).toBe(false)
  })

  it('lets go when the reader scrolls up, and offers a way back', async () => {
    await scrollTo(800)
    expect(seen.latest!.away).toBe(true)
    geometry.scrollHeight = 2600
    await act(async () => resize?.())
    expect(scroller().scrollTop).toBe(800)

    await act(async () => seen.latest!.scrollToBottom())
    expect(seen.latest!.away).toBe(false)
    expect(scroller().scrollTop).toBe(2600)
  })

  it('re-pins when the reader scrolls back down to the end', async () => {
    await scrollTo(800)
    await scrollTo(1450)
    expect(seen.latest!.away).toBe(false)
  })

  it('re-derives pinning after a jump that fires no scroll event', async () => {
    await act(async () => seen.latest!.unpin())
    expect(seen.latest!.away).toBe(true)
    // Nothing older arrived: the reader is still at the end.
    await act(async () => seen.latest!.sync())
    expect(seen.latest!.away).toBe(false)
  })

  it('ignores small nudges near the bottom', async () => {
    await scrollTo(1440)
    expect(seen.latest!.away).toBe(false)
  })
})
