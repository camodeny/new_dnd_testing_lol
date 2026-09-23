// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { updateDraft } = vi.hoisted(() => ({
  updateDraft: vi.fn().mockResolvedValue({ character: { id: 'draft-123' } }),
}))

vi.mock('@/lib/api', () => ({
  characters: { updateDraft, createDraft: vi.fn() },
}))

vi.mock('./CharacterAIAssist', () => ({ default: () => null }))

vi.mock('./CharacterFormPage', () => ({
  default: ({ onDraftChange }: { onDraftChange?: (draft: never) => void }) => (
    <button data-testid="edit-draft" type="button" onClick={() => onDraftChange?.({ name: 'Newest name' } as never)}>
      Edit draft
    </button>
  ),
}))

import CharacterFormLayout from './CharacterFormLayout'

let container: HTMLDivElement
let root: Root | null

beforeEach(() => {
  vi.resetAllMocks()
  updateDraft.mockResolvedValue({ character: { id: 'draft-123' } })
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  if (root) await act(async () => root?.unmount())
  container.remove()
})

describe('character draft autosave', () => {
  it('flushes the newest debounced edit when the editor unmounts', async () => {
    await act(async () => root?.render(
      <CharacterFormLayout
        characterId="draft-123"
        initial={{ id: 'draft-123', status: 'draft' }}
        onSaved={vi.fn()}
        onCancel={vi.fn()}
      />,
    ))
    await act(async () => container.querySelector('[data-testid="edit-draft"]')!.dispatchEvent(new MouseEvent('click', { bubbles: true })))
    await act(async () => {
      root?.unmount()
      root = null
    })

    expect(updateDraft).toHaveBeenCalledWith(
      'draft-123',
      expect.objectContaining({ name: 'Newest name' }),
      { keepalive: true },
    )
  })
})
