import { useMemo } from 'react'
import { Link } from 'react-router-dom'

const DIFFICULTY_COLORS = {
  easy: '#4ade80',
  medium: '#facc15',
  hard: '#fb923c',
  deadly: '#f87171',
}

function getDifficultyColor(difficulty) {
  if (!difficulty) return null
  const key = difficulty.toLowerCase().trim()
  return DIFFICULTY_COLORS[key] || '#a78bfa'
}

import { parseDate } from '../../utils/date'

function formatDate(iso) {
  if (!iso) return ''
  const d = parseDate(iso)
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

function getInitials(name) {
  return name
    .split(' ')
    .map((w) => w[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
}

function getGradientSeed(str) {
  let hash = 0
  for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash)
  const hues = [250, 270, 290, 310, 330, 200, 220, 180]
  const h1 = hues[Math.abs(hash) % hues.length]
  const h2 = (h1 + 40) % 360
  return `linear-gradient(135deg, hsl(${h1}, 60%, 55%), hsl(${h2}, 55%, 45%))`
}

export default function CampaignCard({ campaign, onDelete }) {
  const diffColor = useMemo(() => getDifficultyColor(campaign.difficulty), [campaign.difficulty])
  const initials = useMemo(() => getInitials(campaign.name), [campaign.name])
  const gradient = useMemo(() => getGradientSeed(campaign.name + (campaign.seed || '')), [campaign.name, campaign.seed])

  return (
    <article className="campaign-card-v2">
      <Link
        to={`/campaigns/${campaign.id}`}
        className="campaign-card-inner campaign-card-link"
        aria-label={`Open campaign ${campaign.name}`}
      >
        <div className="campaign-card-header">
          <div className="campaign-avatar" style={{ background: gradient }} aria-hidden="true">
            {initials}
          </div>
          <div className="campaign-meta">
            <h3 className="campaign-title">{campaign.name}</h3>
            {campaign.difficulty && (
              <span
                className="campaign-badge"
                style={{ '--badge-color': diffColor }}
              >
                {campaign.difficulty}
              </span>
            )}
          </div>
        </div>
        {campaign.description && (
          <p className="campaign-desc">{campaign.description}</p>
        )}
        <div className="campaign-footer">
          {campaign.seed && <span className="campaign-seed">Seed: {campaign.seed}</span>}
          <span className="campaign-date">{formatDate(campaign.created_at)}</span>
        </div>
      </Link>
      {onDelete && (
        <button
          type="button"
          className="campaign-card-delete-btn"
          onClick={onDelete}
          aria-label={`Delete campaign ${campaign.name}`}
          title={`Delete ${campaign.name}`}
        >
          <i className="bi bi-trash" aria-hidden="true"></i>
        </button>
      )}
    </article>
  )
}
