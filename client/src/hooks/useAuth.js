import { useState, useEffect } from 'react'
import { apiFetch } from '../api/client'

export function useAuth() {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const token = localStorage.getItem('token')
    if (token) {
      apiFetch('/me')
        .then((data) => setUser(data.user))
        .catch(() => localStorage.removeItem('token'))
        .finally(() => setLoading(false))
    } else {
      setLoading(false)
    }
  }, [])

  const logout = () => {
    localStorage.removeItem('token')
    setUser(null)
  }

  return { user, setUser, loading, logout }
}
