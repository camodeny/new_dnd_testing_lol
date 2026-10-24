// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'

const localStack = vi.hoisted(() => ({ on: true }))
vi.mock('@/lib/localSupabase', () => ({
  get isLocalSupabase() {
    return localStack.on
  },
}))

import { getAuthToken, localTabAuthOptions, setAuthToken } from './authToken'

beforeEach(() => {
  localStack.on = true
  localStorage.clear()
  sessionStorage.clear()
})

describe('local stack per-tab auth', () => {
  it('gives each page load its own broadcast channel but one stable session', () => {
    const first = localTabAuthOptions()!
    const reload = localTabAuthOptions()!
    // Distinct storage keys -> distinct supabase-js BroadcastChannels.
    expect(first.storageKey).not.toBe(reload.storageKey)

    first.storage.setItem(first.storageKey, 'session-json')
    first.storage.setItem(`${first.storageKey}-code-verifier`, 'verifier')
    expect(reload.storage.getItem(reload.storageKey)).toBe('session-json')
    expect(reload.storage.getItem(`${reload.storageKey}-code-verifier`)).toBe('verifier')
    expect(localStorage.length).toBe(0)
  })

  it('keeps the token mirror in this tab only', () => {
    setAuthToken('tok')
    expect(sessionStorage.getItem('token')).toBe('tok')
    expect(localStorage.getItem('token')).toBeNull()
  })
})

describe('hosted project', () => {
  it('uses supabase-js defaults and a token shared across tabs', () => {
    localStack.on = false
    expect(localTabAuthOptions()).toBeNull()
    setAuthToken('tok')
    expect(localStorage.getItem('token')).toBe('tok')
    expect(getAuthToken()).toBe('tok')
  })
})
