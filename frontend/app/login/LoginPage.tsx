'use client'

import { useState, useEffect } from 'react'
import { auth } from '@/lib/api'
import type { User } from '@/types'
import './login.css'

interface LoginPageProps {
  onLogin: (user: User) => void
}

export default function LoginPage({ onLogin }: LoginPageProps) {
  const [isRegistering, setIsRegistering] = useState(false)
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    let ignore = false

    const params = new URLSearchParams(window.location.search)
    const authError = params.get('auth_error')
    if (authError) {
      setError(authError)
      params.delete('auth_error')
      const nextSearch = params.toString()
      const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ''}${window.location.hash}`
      window.history.replaceState({}, '', nextUrl)
    }

    return () => { ignore = true }
  }, [])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)

    try {
      const data = isRegistering
        ? await auth.register(username, password)
        : await auth.login(username, password)

      if (data.token) {
        localStorage.setItem('token', data.token)
      }
      onLogin(data.user)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-container">
      <section className="login-illustration" aria-label="Fireside">
        <div className="login-visual-brand">
          <span aria-hidden="true">✦</span> Fireside
        </div>
        <div className="login-visual-copy">
          <span className="login-visual-kicker">THE TABLE IS OPEN</span>
          <h2>
            Friends around the fire.
            <br />
            Adventure everywhere else.
          </h2>
          <p>Gather your party, keep every chapter close, and follow the story wherever it leads.</p>
        </div>
        <div className="login-visual-note">
          <span aria-hidden="true">↗</span> Built for stories that remember
        </div>
      </section>

      <main className="login-panel">
        <div className="login-card">
          <div className="login-brand">
            <span aria-hidden="true">✦</span> Fireside
          </div>
          <span className="login-kicker">YOUR CAMPAIGN WORKSPACE</span>
          <h1>{isRegistering ? 'Begin a story' : 'Welcome back'}</h1>
          <p className="login-subtitle">
            {isRegistering
              ? 'Create an account and take your seat.'
              : 'Sign in to return to your campaigns.'}
          </p>

          <form onSubmit={handleSubmit}>
            {error && <div className="error-message">{error}</div>}

            <div className="form-group">
              <label htmlFor="username">Username</label>
              <input
                type="text"
                id="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
                placeholder="Enter your username"
                autoComplete="username"
              />
            </div>

            {isRegistering && (
              <div className="form-group">
                <label htmlFor="email">Email</label>
                <input
                  type="email"
                  id="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="Enter your email"
                  autoComplete="email"
                />
              </div>
            )}

            <div className="form-group">
              <label htmlFor="password">Password</label>
              <input
                type="password"
                id="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                placeholder="Enter your password"
                autoComplete={isRegistering ? 'new-password' : 'current-password'}
              />
            </div>

            <button type="submit" className="login-button" disabled={loading}>
              {loading ? 'Loading…' : isRegistering ? 'Create Account' : 'Sign In'}
            </button>
          </form>

          <p className="toggle-text">
            {isRegistering ? 'Already have an account?' : "Don't have an account?"}{' '}
            <button
              type="button"
              className="toggle-button"
              onClick={() => {
                setIsRegistering((r) => !r)
                setError('')
              }}
            >
              {isRegistering ? 'Sign In' : 'Create Account'}
            </button>
          </p>
        </div>
      </main>
    </div>
  )
}
