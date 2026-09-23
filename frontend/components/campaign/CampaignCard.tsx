import Link from 'next/link'
import type { Campaign } from '@/types'

function formatDate(iso?: string): string {
  if (!iso) return ''
  return new Date(iso).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

interface CampaignCardProps {
  campaign: Campaign
  onDelete?: ((e: React.MouseEvent) => void) | null
  isOwner?: boolean
  onArchive?: ((e: React.MouseEvent) => void) | null
  onRestore?: ((e: React.MouseEvent) => void) | null
  actionBusy?: boolean
  layout?: 'rows' | 'compact' | 'grid' | 'feature'
}

export default function CampaignCard({ campaign, onDelete, isOwner, onArchive, onRestore, actionBusy, layout = 'rows' }: CampaignCardProps) {
  const archived = campaign.status === 'archived'
  const archiveHandler = isOwner && !archived ? onArchive : null
  const restoreHandler = isOwner && archived ? onRestore : null
  const hasActions = Boolean(onDelete || archiveHandler || restoreHandler)

  return (
    <article className={`campaign-card-v2 campaign-card--${layout}${hasActions ? ' has-actions' : ''}`}>
      <Link
        href={`/campaigns/${campaign.id}`}
        className="campaign-card-inner campaign-card-link"
        aria-label={`Open campaign ${campaign.name}`}
      >
        <div className="campaign-card-header">
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
