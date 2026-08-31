'use client'

import { useState, useEffect, useCallback } from 'react'
import { auth } from '@/lib/api'
import { supabase } from '@/lib/supabase'
import type { User } from '@/types'

const MOCK_USER: User | null =
  process.env.NEXT_PUBLIC_MOCK_USER === 'true'
    ? { id: '23f3b2d1-efb6-4785-9a67-fa7ca57d72a3', username: 'dev', email: 'dev@fireside.local' }
    : null

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
  const [user, setUser] = useState<User | null>(MOCK_USER)
  const [loading, setLoading] = useState(MOCK_USER ? false : true)

  useEffect(() => {
    if (MOCK_USER) return

    let cancelled = false

    // 1. Check Supabase session first; sync token to localStorage for apiFetch (backend JWT)
    supabase.auth.getSession().then(async ({ data }) => {
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
