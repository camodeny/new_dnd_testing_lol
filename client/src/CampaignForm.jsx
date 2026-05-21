import { useState } from 'react'
import { createCampaign } from './api/client'

function CampaignForm({ onCampaignCreated, onCancel, className = '' }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [difficulty, setDifficulty] = useState('')
  const [seed, setSeed] = useState('')
  const [requiredPlayers, setRequiredPlayers] = useState(1)
  const [lootMode, setLootMode] = useState('frequent_gamble')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)

    try {
      const data = await createCampaign({ name, description, difficulty, seed, required_players: requiredPlayers, loot_mode: lootMode })
      setName('')
      setDescription('')
      setDifficulty('')
      setSeed('')
      onCampaignCreated(data.campaign)
    } catch (err) {
      setError(err.message || 'Network error occurred')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form className={`campaign-form-compact ${className}`} onSubmit={handleSubmit}>
      {error && <div className="form-error-compact">{error}</div>}
      <div className="form-row">
        <div className="form-field">
          <label htmlFor="campaign-name">Campaign Name *</label>
          <input
            id="campaign-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            placeholder="The Lost Mines..."
            autoFocus
          />
        </div>
        <div className="form-field">
          <label htmlFor="campaign-difficulty">Difficulty</label>
          <select
            id="campaign-difficulty"
            value={difficulty}
            onChange={(e) => setDifficulty(e.target.value)}
            className="input"
          >
            <option value="">Select...</option>
            <option value="Easy">Easy</option>
            <option value="Medium">Medium</option>
            <option value="Hard">Hard</option>
            <option value="Deadly">Deadly</option>
          </select>
        </div>
        <div className="form-field">
          <label htmlFor="campaign-players">Players</label>
          <select
            id="campaign-players"
            value={requiredPlayers}
            onChange={(e) => setRequiredPlayers(Number(e.target.value))}
            className="input"
          >
            {[1, 2, 3, 4, 5, 6, 7, 8].map((n) => (
              <option key={n} value={n}>{n} {n === 1 ? 'player' : 'players'}</option>
            ))}
          </select>
        </div>
        <div className="form-field">
          <label htmlFor="campaign-loot">Loot Mode</label>
          <select
            id="campaign-loot"
            value={lootMode}
            onChange={(e) => setLootMode(e.target.value)}
            className="input"
          >
            <option value="frequent_gamble">Frequent Gamble — Lots of drops, mostly okay, sometimes amazing</option>
            <option value="rare_quality">Rare Quality — Few drops, always good, sometimes amazing</option>
          </select>
        </div>
      </div>
      <div className="form-field">
        <label htmlFor="campaign-description">Description</label>
        <textarea
          id="campaign-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="A brief tale of your adventure..."
          rows={3}
        />
      </div>
      <div className="form-field">
        <label htmlFor="campaign-seed">Seed</label>
        <input
          id="campaign-seed"
          type="text"
          value={seed}
          onChange={(e) => setSeed(e.target.value)}
          placeholder="Random seed for generation (optional)"
        />
      </div>
      <div className="form-actions-compact">
        {onCancel && (
          <button type="button" className="btn btn-secondary" onClick={onCancel}>
            Cancel
          </button>
        )}
        <button type="submit" className="btn btn-primary" disabled={loading}>
          {loading ? 'Creating...' : 'Create Campaign'}
        </button>
      </div>
    </form>
  )
}

export default CampaignForm
