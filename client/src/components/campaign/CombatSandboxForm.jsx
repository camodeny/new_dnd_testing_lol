import { useEffect, useMemo, useState } from 'react'
import { createCombatSandbox, listCombatSandboxMaps } from '../../api/client'

export default function CombatSandboxForm({ onCreated, onCancel, className = '' }) {
  const [name, setName] = useState('Combat Sandbox')
  const [description, setDescription] = useState('')
  const [mode, setMode] = useState('existing')
  const [requiredPlayers, setRequiredPlayers] = useState(2)
  const [maps, setMaps] = useState([])
  const [mapsLoading, setMapsLoading] = useState(true)
  const [sourceMapId, setSourceMapId] = useState('')
  const [mapTitle, setMapTitle] = useState('')
  const [mapPrompt, setMapPrompt] = useState('')
  const [terrain, setTerrain] = useState('')
  const [tacticalFeatures, setTacticalFeatures] = useState('')
  const [mood, setMood] = useState('')
  const [vttSetupNotes, setVttSetupNotes] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function loadMaps() {
      setMapsLoading(true)
      try {
        const data = await listCombatSandboxMaps()
        if (cancelled) return
        const nextMaps = data.maps || []
        setMaps(nextMaps)
        if (nextMaps.length) {
          setSourceMapId((current) => current || String(nextMaps[0].id))
        }
        if (!nextMaps.length) {
          setMode('new')
        }
      } catch (err) {
        if (!cancelled) {
          setError(err.message || 'Failed to load existing sandbox maps.')
          setMode('new')
        }
      } finally {
        if (!cancelled) setMapsLoading(false)
      }
    }

    loadMaps()
    return () => {
      cancelled = true
    }
  }, [])

  const selectedMap = useMemo(
    () => maps.find((map) => String(map.id) === String(sourceMapId)) || null,
    [maps, sourceMapId],
  )

  const handleSubmit = async (event) => {
    event.preventDefault()
    setError('')
    setLoading(true)

    try {
      const payload = {
        name,
        description,
        required_players: requiredPlayers,
      }

      if (mode === 'existing') {
        if (!sourceMapId) {
          throw new Error('Choose an existing sandbox map first.')
        }
        payload.source_map_id = Number(sourceMapId)
      } else {
        payload.map_title = mapTitle
        payload.map_prompt = mapPrompt
        payload.terrain = terrain
        payload.tactical_features = tacticalFeatures
        payload.mood = mood
        payload.vtt_setup_notes = vttSetupNotes
      }

      const data = await createCombatSandbox(payload)
      onCreated?.(data.campaign, data)
    } catch (err) {
      setError(err.message || 'Failed to create combat sandbox.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form className={`campaign-form-compact ${className}`} onSubmit={handleSubmit}>
      {error && <div className="form-error-compact">{error}</div>}

      <div className="sandbox-mode-switch">
        <button
          type="button"
          className={`sandbox-mode-chip ${mode === 'existing' ? 'active' : ''}`}
          onClick={() => setMode('existing')}
          disabled={loading || mapsLoading || maps.length === 0}
        >
          Use Existing Map
        </button>
        <button
          type="button"
          className={`sandbox-mode-chip ${mode === 'new' ? 'active' : ''}`}
          onClick={() => setMode('new')}
          disabled={loading}
        >
          Generate New Map
        </button>
      </div>

      <div className="form-row">
        <div className="form-field">
          <label htmlFor="sandbox-name">Campaign Name *</label>
          <input
            id="sandbox-name"
            type="text"
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            autoFocus
            placeholder="Tavern Ambush Sandbox"
          />
        </div>
        <div className="form-field">
          <label htmlFor="sandbox-players">Players</label>
          <select
            id="sandbox-players"
            value={requiredPlayers}
            onChange={(event) => setRequiredPlayers(Number(event.target.value))}
            className="input"
          >
            {[1, 2, 3, 4, 5, 6, 7, 8].map((count) => (
              <option key={count} value={count}>{count} {count === 1 ? 'player' : 'players'}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="form-field">
        <label htmlFor="sandbox-description">Description</label>
        <textarea
          id="sandbox-description"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          rows={2}
          placeholder="Reusable combat-only sandbox for testing map flow, initiative, movement, and invites."
        />
      </div>

      {mode === 'existing' ? (
        <>
          <div className="form-field">
            <label htmlFor="sandbox-source-map">Existing Sandbox Map</label>
            <select
              id="sandbox-source-map"
              value={sourceMapId}
              onChange={(event) => setSourceMapId(event.target.value)}
              className="input"
              disabled={mapsLoading || loading || maps.length === 0}
            >
              {mapsLoading && <option value="">Loading sandbox maps...</option>}
              {!mapsLoading && maps.length === 0 && <option value="">No sandbox maps saved yet</option>}
              {!mapsLoading && maps.map((map) => (
                <option key={map.id} value={map.id}>
                  {map.title} · {map.campaign_name}
                </option>
              ))}
            </select>
            <p className="form-help-text">
              Reuses the saved tactical map, spawn layout, and non-player placements from an earlier sandbox.
            </p>
          </div>

          {selectedMap && (
            <div className="sandbox-map-meta">
              <strong>{selectedMap.title}</strong>
              <span>{selectedMap.campaign_name}</span>
              {selectedMap.map_summary && <p>{selectedMap.map_summary}</p>}
              {selectedMap.tactical_notes?.length > 0 && (
                <ul className="sandbox-map-note-list">
                  {selectedMap.tactical_notes.slice(0, 3).map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      ) : (
        <>
          <div className="form-field">
            <label htmlFor="sandbox-map-title">Map Title *</label>
            <input
              id="sandbox-map-title"
              type="text"
              value={mapTitle}
              onChange={(event) => setMapTitle(event.target.value)}
              required={mode === 'new'}
              placeholder="Midnight Tavern Brawl"
            />
          </div>
          <div className="form-field">
            <label htmlFor="sandbox-map-prompt">Map Prompt *</label>
            <textarea
              id="sandbox-map-prompt"
              value={mapPrompt}
              onChange={(event) => setMapPrompt(event.target.value)}
              rows={4}
              required={mode === 'new'}
              placeholder="A cramped tavern interior with a long bar, central tables, a hearth, and a narrow east-side passage."
            />
          </div>
          <div className="form-row">
            <div className="form-field">
              <label htmlFor="sandbox-terrain">Terrain Notes</label>
              <input
                id="sandbox-terrain"
                type="text"
                value={terrain}
                onChange={(event) => setTerrain(event.target.value)}
                placeholder="wood floor, scattered stools, hearth"
              />
            </div>
            <div className="form-field">
              <label htmlFor="sandbox-mood">Mood</label>
              <input
                id="sandbox-mood"
                type="text"
                value={mood}
                onChange={(event) => setMood(event.target.value)}
                placeholder="warm lamp light, tense and crowded"
              />
            </div>
          </div>
          <div className="form-field">
            <label htmlFor="sandbox-tactical-features">Tactical Features</label>
            <textarea
              id="sandbox-tactical-features"
              value={tacticalFeatures}
              onChange={(event) => setTacticalFeatures(event.target.value)}
              rows={2}
              placeholder="cover behind the bar, tables as half cover, narrow chokepoint by the hearth, south door entry"
            />
          </div>
          <div className="form-field">
            <label htmlFor="sandbox-vtt-notes">Setup Notes</label>
            <textarea
              id="sandbox-vtt-notes"
              value={vttSetupNotes}
              onChange={(event) => setVttSetupNotes(event.target.value)}
              rows={2}
              placeholder="Friendly spawn near the south door. Enemy spawn behind the bar and by the stairs."
            />
          </div>
        </>
      )}

      <div className="form-actions-compact">
        {onCancel && (
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={loading}>
            Cancel
          </button>
        )}
        <button type="submit" className="btn btn-primary" disabled={loading || (mode === 'existing' && !sourceMapId)}>
          {loading ? 'Preparing Sandbox...' : 'Create Combat Sandbox'}
        </button>
      </div>
    </form>
  )
}
