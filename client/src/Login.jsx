import { useEffect, useState } from 'react'
import { apiFetch } from './api/client'
import './Login.css'

function getInitialAuthError() {
  return new URLSearchParams(window.location.search).get('auth_error') || ''
}

function Login({ onLogin }) {
  const [isRegistering, setIsRegistering] = useState(false)
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(getInitialAuthError)
  const [loading, setLoading] = useState(false)
  const [ssoEnabled, setSsoEnabled] = useState(false)

  useEffect(() => {
    let ignore = false

    apiFetch('/auth/config')
      .then((data) => {
        if (!ignore) {
          setSsoEnabled(Boolean(data.sso_enabled))
        }
      })
      .catch(() => {
        if (!ignore) {
          setSsoEnabled(false)
        }
      })

    const params = new URLSearchParams(window.location.search)
    const authError = params.get('auth_error')
    if (authError) {
      params.delete('auth_error')
      const nextSearch = params.toString()
      const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ''}${window.location.hash}`
      window.history.replaceState({}, '', nextUrl)
    }

    return () => {
      ignore = true
    }
  }, [])

  const handleSsoLogin = () => {
    const next = `${window.location.pathname}${window.location.search}${window.location.hash}`
    window.location.assign(`/api/auth/login?next=${encodeURIComponent(next || '/')}`)
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)

    const endpoint = isRegistering ? '/register' : '/login'
    const body = isRegistering
      ? { username, email, password }
      : { username, password }

    try {
      const data = await apiFetch(endpoint, { method: 'POST', body: JSON.stringify(body) })

      if (data.token) {
        localStorage.setItem('token', data.token)
      }

      if (onLogin) {
        onLogin(data.user)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-container">
      <section className="login-illustration" aria-label="Fireside">
        <div className="login-visual-brand"><span aria-hidden="true">✦</span> Fireside</div>
        <div className="login-visual-copy">
          <span className="login-visual-kicker">THE TABLE IS OPEN</span>
          <h2>Friends around the fire.<br />Adventure everywhere else.</h2>
          <p>Gather your party, keep every chapter close, and follow the story wherever it leads.</p>
        </div>
        <div className="login-visual-note"><span aria-hidden="true">↗</span> Built for stories that remember</div>
      </section>
      <main className="login-panel">
        <div className="login-card">
        <div className="login-brand"><span aria-hidden="true">✦</span> Fireside</div>
        <span className="login-kicker">YOUR CAMPAIGN WORKSPACE</span>
        <h1>{isRegistering ? 'Begin a story' : 'Welcome back'}</h1>
        <p className="login-subtitle">
          {isRegistering ? 'Create an account and take your seat.' : 'Sign in to return to your campaigns.'}
        </p>

        {ssoEnabled && (
          <>
            <button
              type="button"
              className="login-button sso-button"
              onClick={handleSsoLogin}
              disabled={loading}
            >
              <span>Continue with Pendergrass SSO</span>
              <i className="bi bi-arrow-up-right" aria-hidden="true" />
            </button>
            <div className="login-divider">or use a local account</div>
          </>
        )}

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
                required
                placeholder="Enter your email"
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
            />
          </div>

          <button type="submit" className="login-button" disabled={loading}>
            {loading ? 'Loading...' : isRegistering ? 'Create Account' : 'Sign In'}
          </button>
        </form>

        <p className="toggle-text">
          {isRegistering ? 'Already have an account?' : "Don't have an account?"}{' '}
          <button
            type="button"
            className="toggle-button"
            onClick={() => {
              setIsRegistering(!isRegistering)
              setError('')
            }}
          >
            {isRegistering ? 'Sign In' : 'Create Account'}
          </button>
        </p>
        </div>
        <p className="login-footer">Private by default · Built for the AI-led table</p>
      </main>
    </div>
  )
}

export default Login
