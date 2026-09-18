// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { push, replace, signUp, signInWithPassword } = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  signUp: vi.fn(),
  signInWithPassword: vi.fn(),
}))

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push, replace }),
}))

vi.mock('@/lib/supabase', () => ({
  supabase: { auth: { signUp, signInWithPassword } },
  isSupabaseConfigured: () => true,
}))

import LoginPage, { PENDING_INVITE_KEY } from './LoginPage'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.resetAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  window.history.replaceState({}, '', '/invite/TESTCODE8')
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  window.history.replaceState({}, '', '/login')
})

function field(label: string): HTMLInputElement {
  const element = container.querySelector(`#${label}`) as HTMLInputElement | null
  if (!element) throw new Error(`Missing field: ${label}`)
  return element
}

function setValue(element: HTMLInputElement, value: string) {
  // React 19 tracks the native setter; bypass it so onChange fires.
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
  setter?.call(element, value)
  element.dispatchEvent(new Event('input', { bubbles: true }))
}

describe('signed-out /invite/:code registration', () => {
  it('keeps the same invite across an email-confirmation signup', async () => {
    signUp.mockResolvedValue({
      data: { user: { id: 'new-user', email: 'new@example.com' }, session: null },
      error: null,
    })
    const onLogin = vi.fn()
    await act(async () => root.render(<LoginPage onLogin={onLogin} />))

    // AppShell renders LoginPage directly at /invite/:code (no ?next=).
    await act(async () => {
      [...container.querySelectorAll('button')].find((b) => b.textContent === 'Create Account')!.click()
    })
    setValue(field('username'), 'newbie')
    setValue(field('email'), 'new@example.com')
    setValue(field('password'), 'correct horse battery staple')
    await act(async () => {
      container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      await Promise.resolve()
    })
    // Flush the async submit handler.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })

    expect(signUp).toHaveBeenCalledOnce()
    const options = signUp.mock.calls[0][0].options as { emailRedirectTo?: string }
    // Confirmation returns to the invite itself — never the bare site URL.
    expect(options.emailRedirectTo).toBe(`${window.location.origin}/invite/TESTCODE8`)
    // Pending code survives even if the redirect chain drops the path.
    expect(localStorage.getItem(PENDING_INVITE_KEY)).toBe('TESTCODE8')
    // No session yet: confirmation prompt, no login continuation.
    expect(container.textContent).toContain('Confirmation email sent')
    expect(onLogin).not.toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
  })
})
