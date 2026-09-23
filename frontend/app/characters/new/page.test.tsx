// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { push, replace } = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
}))

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push, replace }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}))

vi.mock('@/components/character/CharacterFormLayout', () => ({
  default: ({ onDraftCreated }: { onDraftCreated?: (character: { id: string }) => void }) => (
    <button type="button" onClick={() => onDraftCreated?.({ id: 'draft-123' })}>
      Simulate draft created
    </button>
  ),
}))

import CharacterCreatePage from './page'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.resetAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  window.history.replaceState({}, '', '/characters/new?campaign=campaign-456')
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  window.history.replaceState({}, '', '/')
})

describe('character draft creation route', () => {
  it('replaces the create URL with the persistent draft URL and preserves campaign context', async () => {
    await act(async () => root.render(<CharacterCreatePage />))
    await act(async () => {
      container.querySelector('button')!.click()
    })

    expect(replace).toHaveBeenCalledWith('/characters/draft-123/edit?campaign=campaign-456')
    expect(push).not.toHaveBeenCalled()
  })
})
