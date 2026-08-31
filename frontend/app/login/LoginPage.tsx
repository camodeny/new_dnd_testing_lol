'use client'

import { useState, useEffect } from 'react'
import { supabase, isSupabaseConfigured } from '@/lib/supabase'
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
  const [success, setSuccess] = useState('')
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const authError = params.get('auth_error')
    if (authError) {
      setError(authError)
      params.delete('auth_error')
      const nextSearch = params.toString()
      const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ''}${window.location.hash}`
      window.history.replaceState({}, '', nextUrl)
    }
  }, [])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setSuccess('')
    setLoading(true)

    try {
      if (isSupabaseConfigured()) {
        // Supabase Auth path — email is required for Supabase; username is stored in user_metadata
        const emailVal = email.trim() || (username.includes('@') ? username.trim() : '')
        if (!emailVal) {
          throw new Error('Email is required for Supabase auth.')
        }
        if (isRegistering) {
          const { data, error } = await supabase.auth.signUp({
            email: emailVal,
            password,
            options: { data: { username: username.trim() || emailVal.split('@')[0] } },
          })
          if (error) throw error
          // If email confirmation is on, user needs to confirm before signing in
          if (data.user && !data.session) {
            setSuccess(emailVal)
            return
          }
          if (data.session?.access_token) {
            localStorage.setItem('token', data.session.access_token)
          }
          const supaUser = data.user
          const u = supaUser
            ? { id: supaUser.id as unknown as number, username: username.trim() || supaUser.email?.split('@')[0] || 'adventurer', email: supaUser.email ?? emailVal }
            : { id: 0 as unknown as number, username: username.trim(), email: emailVal }
          onLogin(u as unknown as User)
        } else {
          const emailLogin = username.includes('@') ? username.trim() : email.trim() || username.trim()
          // Try email login; Supabase requires email
          const { data, error } = await supabase.auth.signInWithPassword({
            email: emailLogin.includes('@') ? emailLogin : emailVal,
            password,
          })
          if (error) throw error
          if (data.session?.access_token) {
            localStorage.setItem('token', data.session.access_token)
          }
          const u = data.user!
          onLogin({ id: u.id as unknown as number, username: (u.user_metadata?.username as string) ?? u.email?.split('@')[0] ?? username, email: u.email ?? undefined } as unknown as User)
        }
      } else {
        throw new Error('Authentication is not configured. Set the public Supabase URL and key.')
      }
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
          <h1>{success ? 'Check your email' : isRegistering ? 'Begin a story' : 'Welcome back'}</h1>
          <p className="login-subtitle">
            {success
              ? 'Your adventure is almost ready.'
              : isRegistering
                ? 'Create an account and take your seat.'
                : 'Sign in to return to your campaigns.'}
          </p>

          {success ? (
            <div className="success-message" role="status" aria-live="polite">
              <div className="success-message-icon" aria-hidden="true">✉︎</div>
              <div className="success-message-body">
                <strong>Confirmation email sent</strong>
                <p>
                  We&apos;ve sent a confirmation link to <strong>{success}</strong>. Open your inbox and click the link to confirm your account, then return here to sign in.
                </p>
              </div>
              <button
                type="button"
                className="login-button"
                onClick={() => {
                  setSuccess('')
                  setIsRegistering(false)
                  setError('')
                }}
              >
                Back to sign in
              </button>
            </div>
          ) : (
            <form onSubmit={handleSubmit}>
              {error && <div className="error-message" role="alert">{error}</div>}

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
          )}

          {!success && (
            <p className="toggle-text">
              {isRegistering ? 'Already have an account?' : "Don't have an account?"}{' '}
              <button
                type="button"
                className="toggle-button"
                onClick={() => {
                  setIsRegistering((r) => !r)
                  setError('')
                  setSuccess('')
                }}
              >
                {isRegistering ? 'Sign In' : 'Create Account'}
              </button>
            </p>
          )}
        </div>
      </main>
    </div>
  )
}
