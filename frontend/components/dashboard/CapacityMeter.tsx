'use client'

/**
 * Campaign capacity meter — issue #255.
 *
 * A simple shared-campaign percentage with a low-key notice as capacity gets
 * low or pauses. Campaign-level language only: no blame toward any
 * participant, no upsell interruption, no raw cost/token/provider details.
 * Funding restoration resyncs through the authoritative projection.
 */

import { useCampaignCapacity } from '@/hooks/useCampaignCapacity'
import { DEFAULT_FUNDING_CENTS, FUNDING_PRESETS_CENTS, formatFundingAmount } from '@/lib/funding'
import type { CapacityUiEvent, CapacityView } from '@/lib/capacity'
import { useState } from 'react'

interface CapacityMeterProps {
  campaignId: string
  onEvent?: (event: CapacityUiEvent) => void
  funding?: FundingControls
}

/** Authorized add-funds entry point (#256). Amount choice only — the
 *  Stripe-hosted checkout collects payment; funding changes capacity only. */
export interface FundingControls {
  onStartFunding: (amountCents: number) => void
  fundingBusy: boolean
  fundingError: string | null
}

export default function CapacityMeter({ campaignId, onEvent, funding }: CapacityMeterProps) {
  const { view, loading, error, payload, refresh } = useCampaignCapacity({ campaignId, onEvent })
  return (
    <CapacityMeterView
      view={view}
      loading={loading}
      error={error}
      hasProjection={payload !== null}
      onRetry={() => void refresh()}
      funding={funding}
    />
  )
}

interface CapacityMeterViewProps {
  view: CapacityView
  loading: boolean
  error: string | null
  hasProjection: boolean
  onRetry: () => void
  funding?: FundingControls
}

/** Amount choice + Stripe handoff for the shared pool. Renders only when
 *  the caller wires authorized funding controls; otherwise the meter stays
 *  a pure projection. Copy stays campaign-level: no blame, no upsell, no
 *  amounts-as-quality language (funding changes capacity only). */
function AddFundsRow({ controls }: { controls: FundingControls }) {
  const [amountCents, setAmountCents] = useState<number>(DEFAULT_FUNDING_CENTS)
  return (
    <div style={{ margin: '8px auto 2px', maxWidth: 900, width: '100%' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <div role="group" aria-label="Amount to add" style={{ display: 'flex', gap: 6 }}>
          {FUNDING_PRESETS_CENTS.map((preset) => (
            <button
              key={preset}
              type="button"
              className="btn btn-secondary small"
              aria-pressed={amountCents === preset}
              aria-label={`Add ${formatFundingAmount(preset)}`}
              disabled={controls.fundingBusy}
              onClick={() => setAmountCents(preset)}
              style={amountCents === preset ? { borderColor: 'var(--color-primary)' } : undefined}
            >
              {formatFundingAmount(preset)}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="btn btn-primary small"
          disabled={controls.fundingBusy}
          aria-busy={controls.fundingBusy}
          onClick={() => controls.onStartFunding(amountCents)}
        >
          {controls.fundingBusy ? 'Opening checkout…' : 'Add funds'}
        </button>
      </div>
      <p style={{ ...styles.note, marginLeft: 0 }}>
        Added funds land in the shared pool. Play resumes on its own.
      </p>
      {controls.fundingError && (
        <p role="alert" style={{ ...styles.note, marginLeft: 0, color: 'var(--text-error, #e08080)' }}>
          {controls.fundingError}
        </p>
      )}
    </div>
  )
}

/** Pure presentational view: renders only the derived percentage/state, so
 *  tests and the live-table page can drive it without fetching. */
export function CapacityMeterView({ view, loading, error, hasProjection, onRetry, funding }: CapacityMeterViewProps) {
  // No projection yet and no failure: render a quiet placeholder rather than
  // inventing a percentage.
  if (loading && !hasProjection && !error) {
    return (
      <div aria-label="Shared campaign capacity" style={styles.wrap}>
        <span style={styles.quiet}>Shared campaign capacity…</span>
      </div>
    )
  }

  if (error && !hasProjection) {
    return (
      <div aria-label="Shared campaign capacity" style={styles.wrap}>
        <span style={styles.quiet}>Campaign capacity is unavailable right now.</span>
        <button type="button" className="btn btn-secondary small" onClick={onRetry}>
          Retry
        </button>
      </div>
    )
  }

  const percentLabel = view.percent === null ? null : `${view.percent}% used`

  return (
    <div aria-label="Shared campaign capacity" style={styles.wrap}>
      <div style={styles.row}>
        <span style={styles.label}>Shared campaign capacity</span>
        {percentLabel && <span style={styles.percent}>{percentLabel}</span>}
      </div>
      {view.percent !== null && (
        <div
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={view.percent}
          aria-label="Shared campaign capacity used"
          style={styles.track}
        >
          <div style={{ ...styles.fill, width: `${view.percent}%` }} />
        </div>
      )}
      {view.state === 'low' && (
        <>
          <p role="status" style={styles.note}>
            Shared campaign capacity is getting low.
          </p>
          {funding && <AddFundsRow controls={funding} />}
        </>
      )}
      {view.state === 'grace' && (
        <p role="status" style={styles.note}>
          Shared campaign capacity is nearly full. The current moment will finish, then new AI
          narration may briefly pause.
        </p>
      )}
      {view.state === 'paused' && (
        <div role="status" style={styles.paused}>
          <p style={{ margin: 0 }}>
            New AI narration is paused for the campaign until shared capacity returns. History,
            character sheets, and player chat stay available.
          </p>
          <p style={{ margin: '6px 0 0' }}>
            Play picks up on its own when capacity returns — nothing needs to be redone. Capacity
            can return through added funds.
          </p>
          {funding && <AddFundsRow controls={funding} />}
        </div>
      )}
    </div>
  )
}

const styles: Record<string, React.CSSProperties> = {
  wrap: {
    borderBottom: '1px solid var(--border-color)',
    background: 'var(--bg-panel)',
    padding: '8px clamp(18px, 4.5vw, 72px)',
  },
  row: {
    display: 'flex',
    alignItems: 'baseline',
    justifyContent: 'space-between',
    gap: 12,
    maxWidth: 900,
    width: '100%',
    marginInline: 'auto',
  },
  label: {
    fontSize: '0.7rem',
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    color: 'var(--text-dim)',
  },
  percent: {
    fontSize: '0.72rem',
    color: 'var(--text-muted)',
    fontVariantNumeric: 'tabular-nums',
  },
  quiet: {
    display: 'block',
    maxWidth: 900,
    width: '100%',
    marginInline: 'auto',
    fontSize: '0.72rem',
    color: 'var(--text-dim)',
  },
  track: {
    maxWidth: 900,
    width: '100%',
    margin: '6px auto 0',
    height: 4,
    borderRadius: 999,
    background: 'var(--surface-muted)',
    overflow: 'hidden',
  },
  fill: {
    height: '100%',
    borderRadius: 999,
    background: 'var(--color-primary)',
  },
  note: {
    maxWidth: 900,
    width: '100%',
    margin: '6px auto 0',
    fontSize: '0.74rem',
    color: 'var(--text-muted)',
  },
  paused: {
    maxWidth: 900,
    width: '100%',
    margin: '8px auto 2px',
    padding: '10px 12px',
    borderRadius: 'var(--radius-md)',
    border: '1px solid var(--border-color)',
    background: 'var(--bg-panel-elevated)',
    fontSize: '0.78rem',
    color: 'var(--text-muted)',
  },
}
