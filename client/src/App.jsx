import { useState, useEffect } from 'react'
import Login from './Login'
import './App.css'

function App() {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const token = localStorage.getItem('token')
    if (token) {
      fetch('http://localhost:5889/api/me', {
        headers: { Authorization: `Bearer ${token}` }
      })
        .then((res) => {
          if (!res.ok) throw new Error('Invalid token')
          return res.json()
        })
        .then((data) => setUser(data.user))
        .catch(() => localStorage.removeItem('token'))
        .finally(() => setLoading(false))
    } else {
      setLoading(false)
    }
  }, [])

  const handleLogin = (userData) => {
    setUser(userData)
  }

  const handleLogout = () => {
    localStorage.removeItem('token')
    setUser(null)
  }

  const apiFetch = (url, options = {}) => {
    const token = localStorage.getItem('token')
    const headers = options.headers || {}
    if (token) {
      headers.Authorization = `Bearer ${token}`
    }
    return fetch(url, { ...options, headers })
  }

  if (loading) {
    return <div className="loading">Loading...</div>
  }

  if (!user) {
    return <Login onLogin={handleLogin} />
  }

  return (
    <>
      <header className="app-header">
        <h1>D&D Adventure</h1>
        <div className="user-info">
          <span>Welcome, {user.username}!</span>
          <button onClick={handleLogout} className="logout-button">Logout</button>
        </div>
      </header>
      <section id="center">
        <div className="hero">
          <p>Your adventure awaits...</p>
        </div>
      </section>
    </>
  )
}

export default App