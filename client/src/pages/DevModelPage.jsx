import { useEffect, useState } from 'react'
import { getDevModelSettings, resetDevModel, updateDevModel } from '../api/client'
import Button from '../components/common/Button'
import ErrorMessage from '../components/common/ErrorMessage'
import Loading from '../components/common/Loading'

export default function DevModelPage() {
  const [settings, setSettings] = useState(null)
  const [model, setModel] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  useEffect(() => {
    let cancelled = false

    async function loadSettings() {
      try {
        const data = await getDevModelSettings()
        if (!cancelled) {
          setSettings(data.settings)
          setModel(data.settings?.model || '')
        }
      } catch (err) {
        if (!cancelled) setError(err.message || 'Failed to load model settings')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    loadSettings()

    return () => {
      cancelled = true
    }
  }, [])

  const handleSubmit = async (event) => {
    event.preventDefault()
    setSaving(true)
    setError('')
    setMessage('')

    try {
      const data = await updateDevModel(model)
      setSettings(data.settings)
      setModel(data.settings?.model || '')
      setMessage(data.message || 'Model updated')
    } catch (err) {
      setError(err.message || 'Failed to update model')
    } finally {
      setSaving(false)
    }
  }

  const handleReset = async () => {
    setSaving(true)
    setError('')
    setMessage('')

    try {
      const data = await resetDevModel()
      setSettings(data.settings)
      setModel(data.settings?.model || '')
      setMessage(data.message || 'Model reset')
    } catch (err) {
      setError(err.message || 'Failed to reset model')
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <Loading />

  return (
    <div className="page dev-model-page">
      <div className="dev-model-header">
        <div>
          <h2>Dev Model</h2>
          <p>Change the configured model used for new DM responses without restarting the server.</p>
        </div>
        <div className={`dev-model-source dev-model-source-${settings?.source || 'env'}`}>
          {settings?.source === 'runtime' ? 'Runtime override' : 'Environment default'}
        </div>
      </div>

      <ErrorMessage message={error} />
      {message && <div className="success-message">{message}</div>}

      <form className="dev-model-panel" onSubmit={handleSubmit}>
        <label htmlFor="dev-model-input">Model id</label>
        <div className="dev-model-control-row">
          <input
            id="dev-model-input"
            className="input dev-model-input"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder="anthropic/claude-sonnet-4.5"
            disabled={saving}
            autoComplete="off"
          />
          <Button type="submit" disabled={saving || !model.trim()}>
            <i className="bi bi-save" />
            Save
          </Button>
        </div>

        <div className="dev-model-meta">
          <div>
            <span>Provider</span>
            <strong>{settings?.provider || 'Not set'}</strong>
          </div>
          <div>
            <span>Current</span>
            <strong>{settings?.model || 'Not set'}</strong>
          </div>
          <div>
            <span>.env default</span>
            <strong>{settings?.env_model || 'Not set'}</strong>
          </div>
          <div>
            <span>API key</span>
            <strong>{settings?.api_key_configured ? 'Configured' : 'Missing'}</strong>
          </div>
          <div>
            <span>Thinking</span>
            <strong>{settings?.thinking_enabled ? settings?.reasoning_effort || 'Enabled' : 'Disabled'}</strong>
          </div>
        </div>

        <div className="dev-model-actions">
          <Button
            type="button"
            variant="secondary"
            disabled={saving || !settings?.is_overridden}
            onClick={handleReset}
          >
            <i className="bi bi-arrow-counterclockwise" />
            Reset to .env
          </Button>
        </div>
      </form>
    </div>
  )
}
