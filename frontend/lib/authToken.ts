import type { SupportedStorage } from '@supabase/supabase-js'
import { isLocalSupabase } from '@/lib/localSupabase'

/**
 * Where the Supabase session and its token mirror live. On the local stack
 * each tab keeps its own session, so one browser can seat several test players
 * at the same table; everywhere else sign-in is shared across tabs.
 */
export function authStorage(): Storage | null {
  if (typeof window === 'undefined') return null
  return isLocalSupabase ? window.sessionStorage : window.localStorage
}

const LOCAL_SESSION_KEY = 'sb-local-auth'

/**
 * Supabase client auth options for the local stack's per-tab sessions.
 * supabase-js relays sign-in/out between tabs over a BroadcastChannel named
 * after `storageKey`, which would hand one tab's player to every other tab.
 * A storage key unique to this page load gives every tab a private channel;
 * the storage adapter maps it back to one stable sessionStorage key so the
 * session survives reloads.
 */
export function localTabAuthOptions(): { storageKey: string; storage: SupportedStorage } | null {
  if (!isLocalSupabase || typeof window === 'undefined') return null
  const tabs = window.sessionStorage
  const pageLoadKey = `${LOCAL_SESSION_KEY}-${Math.random().toString(36).slice(2)}`
  const stable = (key: string) => key.replace(pageLoadKey, LOCAL_SESSION_KEY)
  return {
    storageKey: pageLoadKey,
    storage: {
      getItem: (key) => tabs.getItem(stable(key)),
      setItem: (key, value) => tabs.setItem(stable(key), value),
      removeItem: (key) => tabs.removeItem(stable(key)),
    },
  }
}

// Mirror of the Supabase access token that backend requests send as Bearer.
const TOKEN_KEY = 'token'

export function getAuthToken(): string | null {
  return authStorage()?.getItem(TOKEN_KEY) ?? null
}

export function setAuthToken(token: string): void {
  authStorage()?.setItem(TOKEN_KEY, token)
}

export function clearAuthToken(): void {
  authStorage()?.removeItem(TOKEN_KEY)
}
