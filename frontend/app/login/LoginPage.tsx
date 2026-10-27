'use client'

import { useState, useEffect, useRef } from 'react'
import { useRouter } from 'next/navigation'
import { supabase, isSupabaseConfigured } from '@/lib/supabase'
import { setAuthToken } from '@/lib/authToken'
import { isLocalSupabase, LOCAL_TEST_PLAYERS, LOCAL_TEST_PLAYER_PASSWORD } from '@/lib/localSupabase'
import { inviteContinuation, readPendingInvite, storePendingInvite } from '@/lib/pendingInvite'
import type { User } from '@/types'
import './login.css'

interface LoginPageProps {
  onLogin: (user: User) => void
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
  const errorRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const authError = params.get('auth_error')
    const continuation = inviteContinuation()
    setInviteNext(continuation?.next ?? null)
    // Invite recipients are almost certainly new: start on Create Account
    // (the toggle back to Sign in stays visible).
    if (continuation?.next?.startsWith('/invite/')) setIsRegistering(true)
    // Persist the pending invite so a later auth-confirmation landing can
    // resume it even if the redirect chain drops the path.
    if (continuation?.code) storePendingInvite(continuation.code)
    if (authError) {
      setError(authError)
      params.delete('auth_error')
      const nextSearch = params.toString()
      const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ''}${window.location.hash}`
      window.history.replaceState({}, '', nextUrl)
    }
  }, [])

  // Bring a fresh submit error into view (it renders right above the
  // submit button, which may be below the fold on a phone).
  useEffect(() => {
    if (error) errorRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [error])

  // Invite-aware onboarding (#242): recipients parked here by
  // /invite/:code — or rendered here directly by AppShell — continue to
  // the same invite after signing in/up.
  const afterLogin = (continuation: ReturnType<typeof inviteContinuation>) => (user: User) => {
    onLogin(user)
    if (continuation?.next) router.push(continuation.next)
  }

  const signIn = async (emailVal: string, pw: string, continueAfterLogin: (user: User) => void) => {
    const { data, error } = await supabase.auth.signInWithPassword({
      email: emailVal,
      password: pw,
    })
    if (error) throw error
    if (data.session?.access_token) {
      setAuthToken(data.session.access_token)
    }
    const u = data.user!
    continueAfterLogin({ id: u.id as unknown as number, username: (u.user_metadata?.username as string) ?? emailVal.split('@')[0], email: u.email ?? undefined } as unknown as User)
  }

  const signInAsTestPlayer = async (testEmail: string) => {
    setError('')
    setLoading(true)
    const continueAfterLogin = afterLogin(inviteContinuation())
    try {
      await signIn(testEmail, LOCAL_TEST_PLAYER_PASSWORD, continueAfterLogin)
    } catch (err) {
      setError(`${(err as Error).message} — seed test players with scripts/local-supabase.sh up`)
    } finally {
      setLoading(false)
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setSuccess('')
    setLoading(true)

    const continuation = inviteContinuation()
    const continueAfterLogin = afterLogin(continuation)

    try {
      if (isSupabaseConfigured()) {
        // Supabase Auth path — email identifies the account in both modes;
        // username is register-only display metadata.
        const emailVal = email.trim()
        if (!emailVal) {
          throw new Error('Email is required for Supabase auth.')
        }
        if (isRegistering) {
          // Sign-up is invite-only: the Supabase before-user-created hook
          // admits a new account only with a usable invite code (or an email
          // someone invited), so carry the invite this visit came from.
          const inviteCode = continuation?.code || readPendingInvite()
          const { data, error } = await supabase.auth.signUp({
            email: emailVal,
            password,
            options: {
              data: {
                username: username.trim() || emailVal.split('@')[0],
                ...(inviteCode ? { invite_code: inviteCode } : {}),
              },
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
            setAuthToken(data.session.access_token)
          }
          const supaUser = data.user
          const u = supaUser
            ? { id: supaUser.id as unknown as number, username: username.trim() || supaUser.email?.split('@')[0] || 'adventurer', email: supaUser.email ?? emailVal }
            : { id: 0 as unknown as number, username: username.trim(), email: emailVal }
          continueAfterLogin(u as unknown as User)
        } else {
          // Try email login; Supabase requires email
          await signIn(emailVal, password, continueAfterLogin)
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
              You&apos;re accepting a campaign invite — once you&apos;re in, you&apos;ll go straight to the lobby.
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
            <div className="form-group">
              <label htmlFor="email">Email</label>
              <input
                type="email"
                id="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                placeholder="Enter your email"
                autoComplete="email"
                inputMode="email"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                enterKeyHint="next"
              />
            </div>

            {isRegistering && (
              <div className="form-group">
                <label htmlFor="username">Username</label>
                <input
                  type="text"
                  id="username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="Choose a username"
                  autoComplete="username"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  enterKeyHint="next"
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
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                enterKeyHint="go"
              />
            </div>

              {error && <div className="error-message" role="alert" ref={errorRef}>{error}</div>}
              <button type="submit" className="login-button" disabled={loading}>
                {loading ? 'Loading…' : isRegistering ? 'Create Account' : 'Sign In'}
              </button>
            </form>
          )}

          {isLocalSupabase && !success && (
            <div className="local-test-players" role="group" aria-label="Local test players">
              <span className="login-kicker">LOCAL TEST PLAYERS</span>
              <div className="local-test-players-row">
                {LOCAL_TEST_PLAYERS.map((player) => (
                  <button
                    key={player.email}
                    type="button"
                    className="toggle-button"
                    disabled={loading}
                    onClick={() => signInAsTestPlayer(player.email)}
                  >
                    {player.label}
                  </button>
                ))}
              </div>
              <p>Each tab keeps its own sign-in, so open one tab per player.</p>
            </div>
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
