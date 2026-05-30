import { useState, useEffect } from 'react'
import { apiFetch } from '../api/client'

export function useAuth() {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    apiFetch('/me')
      .then((data) => setUser(data.user))
      .catch(() => {
        localStorage.removeItem('token')
        setUser(null)
      })
      .finally(() => setLoading(false))
  }, [])

  const logout = async () => {
    try {
      await apiFetch('/logout', { method: 'POST' })
    } catch {
      // Logout should still clear local client auth state if the server session is already gone.
    }
    localStorage.removeItem('token')
    setUser(null)
  }

  return { user, setUser, loading, logout }
}
