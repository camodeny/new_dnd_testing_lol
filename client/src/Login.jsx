import { useEffect, useState } from 'react'
import { apiFetch } from './api/client'
import './Login.css'

function Login({ onLogin }) {
  const [isRegistering, setIsRegistering] = useState(false)
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
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
      setError(authError)
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
      <div className="login-illustration" aria-hidden="true"><span className="login-moon" /><span className="login-hill login-hill-one" /><span className="login-hill login-hill-two" /><span className="login-spark spark-one">✦</span><span className="login-spark spark-two">✧</span></div>
      <div className="login-card">
        <div className="login-brand"><span>✺</span> Campfire</div>
        <h1>{isRegistering ? 'Begin a story' : 'Welcome back'}</h1>
        <p className="login-subtitle">
          {isRegistering ? 'Make a place at the table.' : 'Your next chapter is waiting.'}
        </p>

        {ssoEnabled && (
          <>
            <button
              type="button"
              className="login-button sso-button"
              onClick={handleSsoLogin}
              disabled={loading}
            >
              Continue with Pendergrass SSO
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
    </div>
  )
}

export default Login
