'use client'

import { useState, useEffect, useCallback } from 'react'
import { auth } from '@/lib/api'
import { clearAuthToken, setAuthToken } from '@/lib/authToken'
import { supabase } from '@/lib/supabase'
import type { User } from '@/types'

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
      // Check Supabase session; mirror its token for apiFetch (backend JWT)
      await supabase.auth.getSession().then(async ({ data }) => {
      const session = data.session
      if (session?.access_token) {
        setAuthToken(session.access_token)
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
      clearAuthToken()
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
        setAuthToken(session.access_token)
      } else {
        clearAuthToken()
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
    clearAuthToken()
    setUser(null)
  }, [])

  return { user, setUser, loading, logout }
}
