import { useState, useEffect } from 'react'
import { apiFetch } from '../api/client'

export function useAuth() {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(() => Boolean(localStorage.getItem('token')))

  useEffect(() => {
    const token = localStorage.getItem('token')
    if (!token) return

    apiFetch('/me')
      .then((data) => setUser(data.user))
      .catch(() => localStorage.removeItem('token'))
      .finally(() => setLoading(false))
  }, [])

  const logout = () => {
    localStorage.removeItem('token')
    setUser(null)
  }

  return { user, setUser, loading, logout }
}
