'use client'

import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { supabase, isSupabaseConfigured } from '@/lib/supabase'
import type { User } from '@/types'
import './login.css'

interface LoginPageProps {
  onLogin: (user: User) => void
}

function safeNextPath(raw: string | null): string | null {
  if (!raw) return null
  // Only same-origin absolute paths continue — never open-redirect.
  if (!raw.startsWith('/') || raw.startsWith('//')) return null
  return raw
}

export const PENDING_INVITE_KEY = 'pendingInviteCode'

// Invite continuation derived from the rendered location (#242): AppShell
// renders LoginPage directly at /invite/:code for signed-out recipients,
// so there is no ?next= in that path — the pathname itself is the context.
export function inviteContinuation(): { next: string; code: string } | null {
  if (typeof window === 'undefined') return null
  const fromParam = safeNextPath(new URLSearchParams(window.location.search).get('next'))
  if (fromParam) {
    const match = /^\/invite\/([A-Za-z0-9_-]{1,20})\/?$/.exec(fromParam)
    return { next: fromParam, code: (match?.[1] ?? '').toUpperCase() || '' }
  }
  const match = /^\/invite\/([A-Za-z0-9_-]{1,20})\/?$/.exec(window.location.pathname)
  if (!match) return null
  return { next: `/invite/${match[1].toUpperCase()}`, code: match[1].toUpperCase() }
}

export default function LoginPage({ onLogin }: LoginPageProps) {
  const router = useRouter()
  const [isRegistering, setIsRegistering] = useState(false)
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [loading, setLoading] = useState(false)
  const [inviteNext, setInviteNext] = useState<string | null>(null)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const authError = params.get('auth_error')
    const continuation = inviteContinuation()
    setInviteNext(continuation?.next ?? null)
    // Persist the pending invite so a later auth-confirmation landing can
    // resume it even if the redirect chain drops the path.
    if (continuation?.code) {
      try {
        localStorage.setItem(PENDING_INVITE_KEY, continuation.code)
      } catch { /* no-op */ }
    }
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

    // Invite-aware onboarding (#242): recipients parked here by
    // /invite/:code — or rendered here directly by AppShell — continue to
    // the same invite after signing in/up.
    const continuation = inviteContinuation()
    const continueAfterLogin = (user: User) => {
      onLogin(user)
      if (continuation?.next) router.push(continuation.next)
    }

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
            options: {
              data: { username: username.trim() || emailVal.split('@')[0] },
              // Confirmation emails return to the invite itself so a
              // new-account signup never loses its table.
              ...(continuation?.next
                ? { emailRedirectTo: `${window.location.origin}${continuation.next}` }
                : {}),
            },
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
          continueAfterLogin(u as unknown as User)
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
          continueAfterLogin({ id: u.id as unknown as number, username: (u.user_metadata?.username as string) ?? u.email?.split('@')[0] ?? username, email: u.email ?? undefined } as unknown as User)
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
          {inviteNext?.startsWith('/invite/') && !success && (
            <p className="login-subtitle" role="note" style={{ marginTop: 8 }}>
              You&apos;re accepting a campaign invite — after signing in you&apos;ll continue straight to the lobby.
            </p>
          )}

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
