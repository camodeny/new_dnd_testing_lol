import { useEffect, useState } from 'react'
import { createAutomationKey, deleteAutomationKey, listAutomationKeys } from '../../api/client'

async function copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Fall back to the textarea path below.
    }
  }

  const textArea = document.createElement('textarea')
  textArea.value = text
  textArea.style.position = 'fixed'
  textArea.style.left = '-999999px'
  textArea.style.top = '-999999px'
  document.body.appendChild(textArea)
  textArea.focus()
  textArea.select()

  try {
    const successful = document.execCommand('copy')
    document.body.removeChild(textArea)
    return successful
  } catch {
    document.body.removeChild(textArea)
    return false
  }
}

export default function AutomationKeyManager() {
  const [keys, setKeys] = useState([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [deletingId, setDeletingId] = useState(null)
  const [latestKey, setLatestKey] = useState('')
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')

  const loadKeys = async (active = true) => {
    const data = await listAutomationKeys()
    if (!active) return
    setKeys(data.automation_keys || [])
  }

  useEffect(() => {
    let active = true
    loadKeys(active)
      .catch((err) => {
        if (active) setError(err.message)
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [])

  const handleCreate = async () => {
    setCreating(true)
    setError('')
    try {
      const data = await createAutomationKey({ label: 'LLM Overseer Key' })
      setLatestKey(data.api_key || '')
      await loadKeys(true)
    } catch (err) {
      setError(err.message)
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = async (keyId, label) => {
    const confirmed = window.confirm(`Delete automation key "${label}"? Scheduled fully LLM runs using it will stop working.`)
    if (!confirmed) return

    setDeletingId(keyId)
    setError('')
    try {
      await deleteAutomationKey(keyId)
      await loadKeys(true)
      setLatestKey('')
    } catch (err) {
      setError(err.message)
    } finally {
      setDeletingId(null)
    }
  }

  const handleCopy = async () => {
    if (!latestKey) return
    const success = await copyToClipboard(latestKey)
    if (success) {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  return (
    <section className="llm-manager-card">
      <div className="llm-manager-header">
        <div>
          <h3>Automation Keys</h3>
          <p>Create an owner-scoped key for bootstrap, Antigravity, and fully LLM campaign runs.</p>
        </div>
        <button className="btn btn-primary small" onClick={handleCreate} disabled={creating}>
          {creating ? 'Creating...' : 'Create Key'}
        </button>
      </div>

      {error && <div className="lobby-error">{error}</div>}
      {loading && <div className="lobby-loading">Loading automation keys...</div>}

      {latestKey && (
        <div className="llm-manager-keybox">
          <div className="llm-manager-keyline">
            <strong>New automation key</strong>
            <button className={`btn btn-secondary small ${copied ? 'copied' : ''}`} onClick={handleCopy}>
              {copied ? (
                <><i className="bi bi-check-lg"></i> Copied</>
              ) : (
                <><i className="bi bi-clipboard"></i> Copy</>
              )}
            </button>
          </div>
          <code>{latestKey}</code>
          <p>Use this as `DND_OWNER_API_KEY` for the fully LLM bootstrap and overseer scripts.</p>
        </div>
      )}

      <div className="llm-manager-list">
        {keys.map((key) => (
          <article key={key.id} className="llm-manager-player">
            <div className="llm-manager-player-title">
              <strong>{key.label}</strong>
              <span>{key.api_key_prefix}...</span>
            </div>
            <div className="llm-manager-player-body">
              <span>Created {key.created_at ? new Date(key.created_at).toLocaleString() : 'recently'}</span>
              <span>{key.last_used_at ? `Last used ${new Date(key.last_used_at).toLocaleString()}` : 'Unused'}</span>
            </div>
            <div className="llm-manager-player-actions">
              <button
                className="btn btn-danger small"
                onClick={() => handleDelete(key.id, key.label)}
                disabled={deletingId === key.id}
              >
                {deletingId === key.id ? 'Deleting...' : 'Delete'}
              </button>
            </div>
          </article>
        ))}
        {!loading && keys.length === 0 && (
          <div className="llm-manager-empty">No automation keys yet.</div>
        )}
      </div>
    </section>
  )
}
