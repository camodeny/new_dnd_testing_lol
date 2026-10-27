// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { push, replace, signUp, signInWithPassword, localStack } = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  signUp: vi.fn(),
  signInWithPassword: vi.fn(),
  localStack: { on: false },
}))

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push, replace }),
}))

vi.mock('@/lib/supabase', () => ({
  supabase: { auth: { signUp, signInWithPassword } },
  isSupabaseConfigured: () => true,
}))

vi.mock('@/lib/localSupabase', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/localSupabase')>()),
  get isLocalSupabase() {
    return localStack.on
  },
}))

import LoginPage from './LoginPage'
import { PENDING_INVITE_KEY } from '@/lib/pendingInvite'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.resetAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  sessionStorage.clear()
  localStack.on = false
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

async function submit() {
  await act(async () => {
    container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await Promise.resolve()
  })
  // Flush the async submit handler.
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
}

describe('signed-out /invite/:code registration', () => {
  it('starts in Create Account mode on an invite continuation', async () => {
    const onLogin = vi.fn()
    await act(async () => root.render(<LoginPage onLogin={onLogin} />))
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })

    expect(container.querySelector('button[type="submit"]')?.textContent).toBe('Create Account')
    // The way back to sign-in stays visible.
    expect([...container.querySelectorAll('button')].some((b) => b.textContent === 'Sign In')).toBe(true)
  })

  it('registers with Email first, then Username, then Password', async () => {
    const onLogin = vi.fn()
    await act(async () => root.render(<LoginPage onLogin={onLogin} />))
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })

    const ids = [...container.querySelectorAll('.form-group input')].map((el) => el.id)
    expect(ids).toEqual(['email', 'username', 'password'])
    const email = field('email')
    expect(email.type).toBe('email')
    expect(email.required).toBe(true)
    expect(email.getAttribute('autocomplete')).toBe('email')
    // No username-as-email juggling: the username field is plain text.
    expect(field('username').type).toBe('text')
  })

  it('keeps the same invite across an email-confirmation signup', async () => {
    signUp.mockResolvedValue({
      data: { user: { id: 'new-user', email: 'new@example.com' }, session: null },
      error: null,
    })
    const onLogin = vi.fn()
    // Already in Create Account mode from the invite continuation.
    await act(async () => root.render(<LoginPage onLogin={onLogin} />))

    setValue(field('email'), 'new@example.com')
    setValue(field('username'), 'newbie')
    setValue(field('password'), 'correct horse battery staple')
    await submit()

    expect(signUp).toHaveBeenCalledOnce()
    expect(signUp.mock.calls[0][0]).toMatchObject({ email: 'new@example.com' })
    const options = signUp.mock.calls[0][0].options as {
      emailRedirectTo?: string
      data?: Record<string, string>
    }
    // The invite code rides along so the invite-only sign-up gate admits it.
    expect(options.data).toEqual({ username: 'newbie', invite_code: 'TESTCODE8' })
    // Confirmation returns to the invite itself — never the bare site URL.
    expect(options.emailRedirectTo).toBe(`${window.location.origin}/invite/TESTCODE8`)
    // Pending code survives even if the redirect chain drops the path.
    expect(localStorage.getItem(PENDING_INVITE_KEY)).toBe('TESTCODE8')
    // No session yet: confirmation prompt, no login continuation.
    expect(container.textContent).toContain('Confirmation email sent')
    expect(onLogin).not.toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
  })

  it('carries a pending invite when signing up from the bare login page', async () => {
    signUp.mockResolvedValue({ data: { user: null, session: null }, error: null })
    localStorage.setItem(PENDING_INVITE_KEY, 'PARKED42')
    window.history.replaceState({}, '', '/login')
    await act(async () => root.render(<LoginPage onLogin={vi.fn()} />))

    await act(async () => {
      [...container.querySelectorAll('button')].find((b) => b.textContent === 'Create Account')!.click()
    })
    setValue(field('email'), 'later@example.com')
    setValue(field('password'), 'correct horse battery staple')
    await submit()

    expect(signUp.mock.calls[0][0].options.data).toMatchObject({ invite_code: 'PARKED42' })
  })

  it('sends no invite code when there is no invite to carry', async () => {
    signUp.mockResolvedValue({
      data: { user: null, session: null },
      error: new Error('Fireside is invite-only for now. Open your invite link to create an account.'),
    })
    window.history.replaceState({}, '', '/login')
    await act(async () => root.render(<LoginPage onLogin={vi.fn()} />))

    await act(async () => {
      [...container.querySelectorAll('button')].find((b) => b.textContent === 'Create Account')!.click()
    })
    setValue(field('email'), 'stranger@example.com')
    setValue(field('password'), 'correct horse battery staple')
    await submit()

    expect(signUp.mock.calls[0][0].options.data).not.toHaveProperty('invite_code')
    // The gate's rejection reads as an invite-only notice, not a crash.
    expect(container.querySelector('.error-message')?.textContent).toContain('invite-only')
  })

  it('shows a submit error right above the submit button', async () => {
    signInWithPassword.mockResolvedValue({ data: {}, error: new Error('Invalid login credentials') })
    window.history.replaceState({}, '', '/login')
    const onLogin = vi.fn()
    await act(async () => root.render(<LoginPage onLogin={onLogin} />))

    setValue(field('email'), 'someone@example.com')
    setValue(field('password'), 'wrong password')
    await submit()

    const error = container.querySelector('.error-message')
    expect(error?.textContent).toContain('Invalid login credentials')
    // The error renders directly before the submit button (above the fold anchor).
    expect(error?.nextElementSibling?.getAttribute('type')).toBe('submit')
  })
})

describe('sign-in mode', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/login')
  })

  it('asks for Email (not Username) and signs in with it', async () => {
    signInWithPassword.mockResolvedValue({
      data: {
        user: { id: 'user-1', email: 'sam@example.com', user_metadata: {} },
        session: { access_token: 'tok' },
      },
      error: null,
    })
    const onLogin = vi.fn()
    await act(async () => root.render(<LoginPage onLogin={onLogin} />))

    const email = field('email')
    expect(email.type).toBe('email')
    expect(email.getAttribute('autocomplete')).toBe('email')
    expect(container.querySelector('#username')).toBeNull()

    setValue(email, 'sam@example.com')
    setValue(field('password'), 'correct horse battery staple')
    await submit()

    expect(signInWithPassword).toHaveBeenCalledOnce()
    expect(signInWithPassword.mock.calls[0][0]).toMatchObject({ email: 'sam@example.com' })
    expect(onLogin).toHaveBeenCalledOnce()
  })
})

describe('local test players', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/login')
  })

  it('signs this tab in as a seeded player on the local stack', async () => {
    localStack.on = true
    signInWithPassword.mockResolvedValue({
      data: {
        user: { id: 'player-2', email: 'player2@local.test', user_metadata: { username: 'player2' } },
        session: { access_token: 'tok-2' },
      },
      error: null,
    })
    const onLogin = vi.fn()
    await act(async () => root.render(<LoginPage onLogin={onLogin} />))

    const player2 = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Player 2')!
    await act(async () => player2.click())
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })

    expect(signInWithPassword).toHaveBeenCalledWith({ email: 'player2@local.test', password: 'password' })
    // Per-tab: the token mirror lands in sessionStorage, not shared localStorage.
    expect(sessionStorage.getItem('token')).toBe('tok-2')
    expect(localStorage.getItem('token')).toBeNull()
    expect(onLogin).toHaveBeenCalledOnce()
  })

  it('offers no test players against a hosted project', async () => {
    await act(async () => root.render(<LoginPage onLogin={vi.fn()} />))

    expect(container.querySelector('[aria-label="Local test players"]')).toBeNull()
  })
})
