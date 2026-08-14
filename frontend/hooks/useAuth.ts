'use client'

import { useState, useEffect, useCallback } from 'react'
import { auth } from '@/lib/api'
import type { User } from '@/types'

const MOCK_USER: User | null =
  process.env.NEXT_PUBLIC_MOCK_USER === 'true'
    ? { id: 1, username: 'dev', email: 'dev@fireside.local' }
    : null

export function useAuth() {
  const [user, setUser] = useState<User | null>(MOCK_USER)
  const [loading, setLoading] = useState(MOCK_USER ? false : true)

  useEffect(() => {
    if (MOCK_USER) return

    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 5000)

    auth
      .me(controller.signal)
      .then((data) => setUser(data.user))
      .catch(() => {
        localStorage.removeItem('token')
        setUser(null)
      })
      .finally(() => {
        clearTimeout(timer)
        setLoading(false)
      })

    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [])

  const logout = useCallback(async () => {
    try {
      await auth.logout()
    } catch {
      // Clear client state even if server request fails.
    }
    localStorage.removeItem('token')
    setUser(null)
  }, [])

  return { user, setUser, loading, logout }
}
