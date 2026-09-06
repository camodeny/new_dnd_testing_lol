'use client'

import { useState, useEffect, useCallback } from 'react'
import { auth } from '@/lib/api'
import { supabase } from '@/lib/supabase'
import type { User } from '@/types'

// Local-dev auto-login: signs in with the dev user from env so dogfooding
// skips the login form. Gated to non-production builds — in a production
// build these vars are ignored even if present. Never set them in a
// deployed environment (values bake into the client bundle).
const DEV_USER_EMAIL = process.env.NEXT_PUBLIC_DEV_USER_EMAIL ?? ''
const DEV_USER_PASSWORD = process.env.NEXT_PUBLIC_DEV_USER_PASSWORD ?? ''
const DEV_AUTO_LOGIN =
  process.env.NODE_ENV !== 'production' && DEV_USER_EMAIL !== '' && DEV_USER_PASSWORD !== ''

function supabaseUserToAppUser(su: { id: string; email?: string | null; user_metadata?: Record<string, unknown> }): User {
  const email = (su.email ?? null) as string | null
  const meta = su.user_metadata ?? {}
  const username =
    (meta.username as string | undefined) ??
    (meta.full_name as string | undefined) ??
    (meta.name as string | undefined) ??
    (email ? email.split('@')[0] : su.id.slice(0, 8))
  return { id: su.id, username, email: email ?? undefined }
}

export function useAuth() {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false

    const init = async () => {
      // Dev auto-login first (skipped without env creds or with a session).
      if (DEV_AUTO_LOGIN) {
        const { data } = await supabase.auth.getSession()
        if (!data.session && !cancelled) {
          const { error } = await supabase.auth.signInWithPassword({
            email: DEV_USER_EMAIL,
            password: DEV_USER_PASSWORD,
          })
          if (error && !cancelled) {
            console.error('[auth] dev auto-login failed:', error.message)
          }
        }
      }
      // Check Supabase session; sync token to localStorage for apiFetch (backend JWT)
      await supabase.auth.getSession().then(async ({ data }) => {
      const session = data.session
      if (session?.access_token) {
        localStorage.setItem('token', session.access_token)
        if (!cancelled) {
          // Prefer backend /api/me (verifies JWT + upserts profiles) but fallback to Supabase user
          try {
            const controller = new AbortController()
            const timer = setTimeout(() => controller.abort(), 5000)
            const backendUser = await auth.me(controller.signal)
            clearTimeout(timer)
            if (!cancelled) setUser(backendUser.user)
          } catch {
            const su = session.user
            if (su && !cancelled) setUser(supabaseUserToAppUser(su as never))
          } finally {
            if (!cancelled) setLoading(false)
          }
          return
        }
      }
      localStorage.removeItem('token')
      if (!cancelled) {
        setUser(null)
        setLoading(false)
      }
    })
    }

    void init()

    // 2. Keep token in sync on refresh / sign-out
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      if (session?.access_token) {
        localStorage.setItem('token', session.access_token)
      } else {
        localStorage.removeItem('token')
        setUser(null)
      }
    })

    return () => {
      cancelled = true
      subscription.unsubscribe()
    }
  }, [])

  const logout = useCallback(async () => {
    try {
      await supabase.auth.signOut()
    } catch {
      // fall through
    }
    localStorage.removeItem('token')
    setUser(null)
  }, [])

  return { user, setUser, loading, logout }
}
