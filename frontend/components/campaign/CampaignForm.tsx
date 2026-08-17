'use client'

import { useState } from 'react'
import { campaigns } from '@/lib/api'
import type { Campaign } from '@/types'

const LOOT_MODES = [
  { value: 'frequent_gamble', label: 'Frequent Gamble' },
  { value: 'rare_treasure', label: 'Rare Treasure' },
  { value: 'generous', label: 'Generous' },
  { value: 'scarce', label: 'Scarce' },
]

interface CampaignFormProps {
  onCreated: (campaign: Campaign) => void
  onCancel: () => void
}

export default function CampaignForm({ onCreated, onCancel }: CampaignFormProps) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [seed, setSeed] = useState('')
  const [requiredPlayers, setRequiredPlayers] = useState(1)
  const [lootMode, setLootMode] = useState('frequent_gamble')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [quickCreating, setQuickCreating] = useState(false)

  const handleRandomize = async () => {
    setError('')
    setGenerating(true)
    try {
      const data = await campaigns.randomBrief({
        required_players: requiredPlayers,
        loot_mode: lootMode,
      })
      setName((data as { name?: string }).name ?? '')
      setDescription((data as { description?: string }).description ?? '')
      setSeed((data as { random_seed?: string }).random_seed ?? '')
    } catch (err) {
      setError((err as Error).message || 'Failed to generate brief')
    } finally {
      setGenerating(false)
    }
  }

  const handleQuickCreate = async () => {
    setError('')
    setQuickCreating(true)
    try {
      const data = await campaigns.quickCreate({
        required_players: requiredPlayers,
        loot_mode: lootMode,
      })
      onCreated((data as { campaign: Campaign }).campaign)
    } catch (err) {
      setError((err as Error).message || 'Failed to quick-create campaign')
    } finally {
      setQuickCreating(false)
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const data = await campaigns.create({
        name,
        description,
        random_seed: seed,
        required_players: requiredPlayers,
        loot_mode: lootMode,
      })
      onCreated((data as { campaign: Campaign }).campaign)
    } catch (err) {
      setError((err as Error).message || 'Network error occurred')
    } finally {
      setLoading(false)
    }
  }

  const busy = loading || generating || quickCreating

  return (
    <form onSubmit={handleSubmit} style={{ display: 'grid', gap: 16 }}>
      {error && <div className="error-message">{error}</div>}

      <div
        className="campaign-form-intro"
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr auto',
          gap: 8,
          paddingBottom: 16,
          borderBottom: '1px solid var(--line)',
        }}
      >
        <div>
          <p style={{ margin: 0, color: 'var(--ink-muted)', fontSize: '0.84rem' }}>
            Let the AI generate a campaign brief for you, or fill in the details yourself.
          </p>
        </div>
        <div className="campaign-form-quick-actions" style={{ display: 'flex', gap: 8, alignSelf: 'start' }}>
          <button
            type="button"
            className="btn btn-secondary small"
            onClick={handleRandomize}
            disabled={busy}
          >
            {generating ? 'Generating…' : <><i className="bi bi-shuffle" aria-hidden="true" /> Randomize</>}
          </button>
          <button
            type="button"
            className="btn btn-secondary small"
            onClick={handleQuickCreate}
            disabled={busy}
          >
            {quickCreating ? 'Creating…' : <><i className="bi bi-lightning" aria-hidden="true" /> Quick create</>}
          </button>
        </div>
      </div>

      <div className="form-field">
        <label htmlFor="campaign-name">Campaign Name *</label>
        <input
          id="campaign-name"
          type="text"
          className="input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          placeholder="The Lost Mines…"
          autoFocus
          disabled={busy}
        />
      </div>

      <div className="form-field">
        <label htmlFor="campaign-description">Description</label>
        <textarea
          id="campaign-description"
          className="textarea"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="A brief description of your campaign…"
          rows={3}
          disabled={busy}
          style={{ minHeight: 80 }}
        />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <div className="form-field">
          <label htmlFor="campaign-players">Required Players</label>
          <input
            id="campaign-players"
            type="number"
            className="input"
            min={1}
            max={8}
            value={requiredPlayers}
            onChange={(e) => setRequiredPlayers(Number(e.target.value))}
            disabled={busy}
          />
        </div>
        <div className="form-field">
          <label htmlFor="campaign-loot">Loot Mode</label>
          <select
            id="campaign-loot"
            className="input"
            value={lootMode}
            onChange={(e) => setLootMode(e.target.value)}
            disabled={busy}
          >
            {LOOT_MODES.map((m) => (
              <option key={m.value} value={m.value}>{m.label}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="form-field">
        <label htmlFor="campaign-seed">Genre / Theme <span style={{ fontWeight: 400, color: 'var(--ink-muted)' }}>(optional)</span></label>
        <input
          id="campaign-seed"
          type="text"
          className="input"
          placeholder="e.g. fantasy, sci-fi, nautical horror…"
          value={seed}
          onChange={(e) => setSeed(e.target.value)}
          disabled={busy}
        />
        <p style={{ margin: '5px 0 0', fontSize: '0.78rem', color: 'var(--ink-muted)' }}>
          Guides the AI DM&apos;s tone and world-building style.
        </p>
      </div>

      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, paddingTop: 4 }}>
        <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button type="submit" className="btn btn-primary" disabled={busy || !name.trim()}>
          {loading ? 'Creating…' : 'Create Campaign'}
        </button>
      </div>
    </form>
  )
}
