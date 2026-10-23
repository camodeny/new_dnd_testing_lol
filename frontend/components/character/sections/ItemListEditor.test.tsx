// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ItemListEditor from './ItemListEditor'

let container: HTMLDivElement
let root: Root

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

const FIELDS = [
  { key: 'class_name', label: 'Class Name' },
  { key: 'level', label: 'Level', type: 'number' },
] as const

describe('ItemListEditor', () => {
  it('saves the item on Enter instead of submitting the outer form', async () => {
    const onChange = vi.fn()
    await act(async () => {
      root.render(
        <form onSubmit={(event) => {
          event.preventDefault()
          ;(container as unknown as { submitted: boolean }).submitted = true
        }}>
          <ItemListEditor
            title="Classes"
            items={[]}
            fields={[...FIELDS] as unknown as Parameters<typeof ItemListEditor>[0]['fields']}
            emptyItem={{ class_name: '', level: 1 }}
            onChange={onChange}
          />
        </form>,
      )
    })

    const addButton = container.querySelector('.add-btn') as HTMLButtonElement
    await act(async () => {
      addButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    const nameInput = container.querySelector('.item-form input') as HTMLInputElement
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    await act(async () => {
      setter?.call(nameInput, 'Wizard')
      nameInput.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => {
      nameInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
    })

    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange.mock.calls[0][0]).toEqual([{ class_name: 'Wizard', level: 1 }])
    expect((container as unknown as { submitted?: boolean }).submitted).toBeUndefined()
  })
})
