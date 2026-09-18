import Link from 'next/link'
import { useMemo } from 'react'
import type { Campaign } from '@/types'

function formatDate(iso?: string): string {
  if (!iso) return ''
  return new Date(iso).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

function getInitials(name: string): string {
  return name
    .split(' ')
    .map((w) => w[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
}

// Ember-palette avatar gradient — avoids the old purple gradients
function getAvatarColor(str: string): string {
  let hash = 0
  for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash)
  // Warm hues only: ember, gold, moss
  const hues = [16, 24, 30, 38, 150, 160, 170]
  const h = hues[Math.abs(hash) % hues.length]
  return `hsl(${h}, 42%, 42%)`
}

interface CampaignCardProps {
  campaign: Campaign
  onDelete?: ((e: React.MouseEvent) => void) | null
  isOwner?: boolean
  onArchive?: ((e: React.MouseEvent) => void) | null
  onRestore?: ((e: React.MouseEvent) => void) | null
  actionBusy?: boolean
}

export default function CampaignCard({ campaign, onDelete, isOwner, onArchive, onRestore, actionBusy }: CampaignCardProps) {
  const initials = useMemo(() => getInitials(campaign.name), [campaign.name])
  const avatarColor = useMemo(
    () => getAvatarColor(campaign.name + (campaign.random_seed ?? '')),
    [campaign.name, campaign.random_seed],
  )
  const archived = campaign.status === 'archived'
  const archiveHandler = isOwner && !archived ? onArchive : null
  const restoreHandler = isOwner && archived ? onRestore : null
  const hasActions = Boolean(onDelete || archiveHandler || restoreHandler)

  return (
    <article className={`campaign-card-v2${hasActions ? ' has-actions' : ''}`}>
      <Link
        href={`/campaigns/${campaign.id}`}
        className="campaign-card-inner campaign-card-link"
        aria-label={`Open campaign ${campaign.name}`}
      >
        <div className="campaign-card-header">
          <div
            className="campaign-avatar"
            style={{ background: avatarColor, color: '#fff8ec' }}
            aria-hidden="true"
          >
            {initials}
          </div>
          <div className="campaign-meta">
            <h3 className="campaign-title">{campaign.name}</h3>
            {archived && (
              <span className="campaign-status-badge" title="Archived campaigns are dormant — restore to keep playing the same table.">
                Archived
              </span>
            )}
          </div>
        </div>
        {campaign.description && (
          <p className="campaign-desc">{campaign.description}</p>
        )}
        <div className="campaign-footer">
          {campaign.random_seed && (
            <span className="campaign-seed">Seed: {campaign.random_seed}</span>
          )}
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
          <i className="bi bi-trash" aria-hidden="true" />
        </button>
      )}
      {archiveHandler && (
        <button
          type="button"
          className="campaign-card-archive-btn"
          onClick={archiveHandler}
          disabled={actionBusy}
          aria-label={`Archive campaign ${campaign.name}`}
          title="Archive (dormant — nothing is deleted)"
        >
          <i className="bi bi-archive" aria-hidden="true" />
        </button>
      )}
      {restoreHandler && (
        <button
          type="button"
          className="campaign-card-restore-btn"
          onClick={restoreHandler}
          disabled={actionBusy}
          aria-label={`Restore campaign ${campaign.name}`}
          title="Restore the same table"
        >
          <i className="bi bi-arrow-counterclockwise" aria-hidden="true" />
        </button>
      )}
    </article>
  )
}
