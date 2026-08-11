import { useEffect, useState } from 'react'
import {
  assignLlmPlayer,
  createAutomationKey,
  createLlmPlayer,
  deleteAutomationKey,
  deleteLlmPlayer,
  listAutomationKeys,
  listLlmPlayers,
  rotateLlmPlayerKey,
} from '../../api/client'

function classSummary(character) {
  if (!character) return 'No character assigned'
  if (character.classes?.length) {
    return character.classes.map((c) => `${c.class_name} ${c.level}`).join(', ')
  }
  return `Level ${character.total_level ?? 1}`
}

async function copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Fall through to fallback
    }
  }

  // Fallback using temporary textarea
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

export default function LlmPlayerManager({ campaignId, enabled, isOwner, onAdded }) {
  const [llmPlayers, setLlmPlayers] = useState([])
  const [availableLlmPlayers, setAvailableLlmPlayers] = useState([])
  const [automationKeys, setAutomationKeys] = useState([])
  const [loading, setLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [creatingAutomationKey, setCreatingAutomationKey] = useState(false)
  const [rotatingId, setRotatingId] = useState(null)
  const [assigningId, setAssigningId] = useState(null)
  const [deletingId, setDeletingId] = useState(null)
  const [deletingAutomationKeyId, setDeletingAutomationKeyId] = useState(null)
  const [error, setError] = useState('')
  const [latestKey, setLatestKey] = useState('')
  const [latestKeyKind, setLatestKeyKind] = useState('llm')
  const [copied, setCopied] = useState(false)

  const loadLlmPlayers = async (active = true) => {
    const [llmData, automationData] = await Promise.all([
      listLlmPlayers(campaignId),
      listAutomationKeys(),
    ])
    if (!active) return
    setLlmPlayers(llmData.llm_players || [])
    setAvailableLlmPlayers(llmData.available_llm_players || [])
    setAutomationKeys(automationData.automation_keys || [])
  }

  useEffect(() => {
    if (!enabled || !isOwner) return
    let active = true
    setLoading(true)
    loadLlmPlayers(active)
      .catch((err) => {
        if (active) setError(err.message)
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [campaignId, enabled, isOwner])

  if (!enabled || !isOwner) return null

  const handleCreate = async () => {
    setCreating(true)
    setError('')
    try {
      const data = await createLlmPlayer(campaignId)
      setLatestKey(data.api_key || '')
      setLatestKeyKind('llm')
      await loadLlmPlayers(true)
      onAdded?.(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setCreating(false)
    }
  }

  const handleCopy = async () => {
    if (!latestKey) return
    const text = latestKeyKind === 'automation'
      ? [
          `X-API-Key: ${latestKey}`,
          `/Users/cpendergrass/Programming/new_dnd_testing_lol/automation/bootstrap_llm_campaign.sh --owner-api-key '${latestKey}' --llm-count 3`,
          `/Users/cpendergrass/Programming/new_dnd_testing_lol/automation/build_llm_overseer_context.sh /Users/cpendergrass/Programming/new_dnd_testing_lol/automation/state/llm-campaign-${campaignId}.json`,
        ].join('\n')
      : [
          `X-API-Key: ${latestKey}`,
          `GET /api/campaigns`,
          `GET /api/campaigns/${campaignId}`,
          `POST /api/sessions/{sessionId}/messages {"content":"...","role":"player"}`,
        ].join('\n')
    const success = await copyToClipboard(text)
    if (success) {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  const handleRotate = async (llmPlayerId) => {
    setRotatingId(llmPlayerId)
    setError('')
    try {
      const data = await rotateLlmPlayerKey(campaignId, llmPlayerId)
      setLatestKey(data.api_key || '')
      setLatestKeyKind('llm')
      await loadLlmPlayers(true)
      onAdded?.(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setRotatingId(null)
    }
  }

  const handleAssign = async (llmPlayerId) => {
    setAssigningId(llmPlayerId)
    setError('')
    try {
      const data = await assignLlmPlayer(campaignId, llmPlayerId)
      await loadLlmPlayers(true)
      onAdded?.(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setAssigningId(null)
    }
  }

  const handleDelete = async (llmPlayerId, label) => {
    const confirmed = window.confirm(`Delete "${label}"? This removes the LLM seat, its assigned character, and its pending sheet proposals.`)
    if (!confirmed) return

    setDeletingId(llmPlayerId)
    setError('')
    try {
      await deleteLlmPlayer(campaignId, llmPlayerId)
      await loadLlmPlayers(true)
      if (latestKey) setLatestKey('')
      onAdded?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setDeletingId(null)
    }
  }

  const handleCreateAutomationKey = async () => {
    setCreatingAutomationKey(true)
    setError('')
    try {
      const data = await createAutomationKey({ label: `Campaign ${campaignId} Overseer` })
      setLatestKey(data.api_key || '')
      setLatestKeyKind('automation')
      await loadLlmPlayers(true)
    } catch (err) {
      setError(err.message)
    } finally {
      setCreatingAutomationKey(false)
    }
  }

  const handleDeleteAutomationKey = async (keyId, label) => {
    const confirmed = window.confirm(`Delete automation key "${label}"? Any scheduled fully LLM runs using it will stop working.`)
    if (!confirmed) return

    setDeletingAutomationKeyId(keyId)
    setError('')
    try {
      await deleteAutomationKey(keyId)
      await loadLlmPlayers(true)
      if (latestKeyKind === 'automation') {
        setLatestKey('')
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setDeletingAutomationKeyId(null)
    }
  }

  const assignedCount = llmPlayers.length
  const availableCount = availableLlmPlayers.length

  return (
    <section className="llm-manager-card">
      <div className="llm-manager-header">
        <div>
          <h3>LLM Players</h3>
          <p>LLM players listed here are already assigned to this campaign. Creating a new one also assigns it here immediately.</p>
        </div>
        <button className="btn btn-primary small" onClick={handleCreate} disabled={creating}>
          {creating ? 'Creating...' : 'Create And Assign LLM'}
        </button>
      </div>

      {error && <div className="lobby-error">{error}</div>}
      {loading && <div className="lobby-loading">Loading LLM players...</div>}

      {latestKey && (
        <div className="llm-manager-keybox">
          <div className="llm-manager-keyline">
            <strong>{latestKeyKind === 'automation' ? 'New automation key' : 'New API key'}</strong>
            <button className={`btn btn-secondary small ${copied ? 'copied' : ''}`} onClick={handleCopy}>
              {copied ? (
                <><i className="bi bi-check-lg"></i> Copied</>
              ) : (
                <><i className="bi bi-clipboard"></i> Copy</>
              )}
            </button>
          </div>
          <code>{latestKey}</code>
          <p>
            {latestKeyKind === 'automation'
              ? 'Use this owner-scoped key for fully LLM bootstrap and orchestration runs.'
              : 'Pass it as `X-API-Key` or `Authorization: Bearer ...`.'}
          </p>
        </div>
      )}

      <div className="llm-manager-section">
        <div className="llm-manager-header">
          <div>
            <h3>Automation Keys</h3>
            <p>Owner-scoped keys for bootstrap, oversight, and autonomous campaign runs.</p>
          </div>
          <button className="btn btn-primary small" onClick={handleCreateAutomationKey} disabled={creatingAutomationKey}>
            {creatingAutomationKey ? 'Creating...' : 'Create Automation Key'}
          </button>
        </div>
        <div className="llm-manager-list">
          {automationKeys.map((key) => (
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
                  onClick={() => handleDeleteAutomationKey(key.id, key.label)}
                  disabled={deletingAutomationKeyId === key.id}
                >
                  {deletingAutomationKeyId === key.id ? 'Deleting...' : 'Delete'}
                </button>
              </div>
            </article>
          ))}
          {!loading && automationKeys.length === 0 && (
            <div className="llm-manager-empty">No automation keys yet.</div>
          )}
        </div>
      </div>

      <div className="llm-manager-summary">
        <span>{assignedCount} in this campaign</span>
        <span>{availableCount} available to assign from your other campaigns</span>
      </div>

      {availableCount > 0 && (
        <div className="llm-manager-section">
          <div className="llm-manager-subhead">
            <strong>Available To Assign</strong>
          </div>
          <div className="llm-manager-list">
            {availableLlmPlayers.map((entry) => (
              <article key={`available-${entry.llm_player.id}`} className="llm-manager-player">
                <div className="llm-manager-player-title">
                  <strong>{entry.llm_player.label}</strong>
                  <span>{entry.assigned_campaign?.name || 'Unassigned'}</span>
                </div>
                <div className="llm-manager-player-body">
                  <span>{entry.character?.name || 'Unknown character'}</span>
                  <span>{classSummary(entry.character)}</span>
                </div>
                {entry.assigned_campaign?.has_active_session ? (
                  <div className="llm-manager-note">Locked: source campaign has an active session.</div>
                ) : (
                  <div className="llm-manager-player-actions">
                    <button
                      className="btn btn-secondary small"
                      onClick={() => handleAssign(entry.llm_player.id)}
                      disabled={assigningId === entry.llm_player.id}
                    >
                      {assigningId === entry.llm_player.id ? 'Assigning...' : 'Assign To This Campaign'}
                    </button>
                  </div>
                )}
              </article>
            ))}
          </div>
        </div>
      )}

      <div className="llm-manager-section">
        <div className="llm-manager-subhead">
          <strong>Assigned Here</strong>
        </div>
        <div className="llm-manager-list">
        {llmPlayers.map((entry) => (
          <article key={entry.llm_player.id} className="llm-manager-player">
            <div className="llm-manager-player-title">
              <strong>{entry.llm_player.label}</strong>
              <span>{entry.llm_player.api_key_prefix}...</span>
            </div>
            <div className="llm-manager-player-body">
              <span>{entry.character?.name || 'Unknown character'}</span>
              <span>{classSummary(entry.character)}</span>
            </div>
            <div className="llm-manager-player-actions">
              <button
                className="btn btn-secondary small"
                onClick={() => handleRotate(entry.llm_player.id)}
                disabled={rotatingId === entry.llm_player.id || deletingId === entry.llm_player.id}
              >
                {rotatingId === entry.llm_player.id ? 'Rotating...' : 'Rotate Key'}
              </button>
              <button
                className="btn btn-danger small"
                onClick={() => handleDelete(entry.llm_player.id, entry.llm_player.label)}
                disabled={deletingId === entry.llm_player.id || rotatingId === entry.llm_player.id}
              >
                {deletingId === entry.llm_player.id ? 'Deleting...' : 'Delete'}
              </button>
            </div>
          </article>
        ))}
        {!loading && llmPlayers.length === 0 && (
          <div className="llm-manager-empty">No LLM players are assigned to this campaign yet.</div>
        )}
        </div>
      </div>
    </section>
  )
}
