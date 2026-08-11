import { useState } from 'react'
import { applySheetProposal, dismissSheetProposal } from '../../api/client'

function formatValue(val) {
  if (val === null || val === undefined) return '—'
  if (typeof val === 'object' && val !== null) {
    if ('count' in val) return String(val.count)
    if ('name' in val) return val.name
    return JSON.stringify(val)
  }
  if (typeof val === 'boolean') return val ? 'Yes' : 'No'
  return String(val)
}

function changeDescription(change) {
  const { operation, before, after, label } = change
  const opSymbol = operation === 'add' ? '+' : operation === 'subtract' ? '−' : '→'
  return { label, opSymbol, beforeStr: formatValue(before), afterStr: formatValue(after) }
}

export default function SheetProposalInline({ proposal, sessionId, onApplied, onDismissed }) {
  const [applying, setApplying] = useState(false)
  const [dismissed, setDismissed] = useState(false)

  if (dismissed || proposal.status !== 'pending') {
    return (
      <div className="sheet-proposal-inline sheet-proposal-done">
        <div className="session-msg-header">
          <span className="session-msg-role"><i className="bi bi-journal-check"></i> Sheet Update</span>
          <span className="session-msg-time">{proposal.status === 'applied' ? 'Applied' : 'Dismissed'}</span>
        </div>
        <p className="sheet-proposal-reason">{proposal.reason}</p>
      </div>
    )
  }

  const handleApply = async () => {
    setApplying(true)
    try {
      const data = await applySheetProposal(sessionId, proposal.id)
      onApplied?.(data.proposal, data.character)
    } catch {
      setApplying(false)
    }
  }

  const handleDismiss = async () => {
    setApplying(true)
    try {
      await dismissSheetProposal(sessionId, proposal.id)
      setDismissed(true)
      onDismissed?.(proposal)
    } catch {
      setApplying(false)
    }
  }

  return (
    <div className="sheet-proposal-inline">
      <div className="session-msg-header">
        <span className="session-msg-role"><i className="bi bi-journal-plus"></i> Sheet Update</span>
      </div>
      <p className="sheet-proposal-reason">{proposal.reason}</p>
      <div className="sheet-proposal-changes">
        {proposal.changes.map((change, i) => {
          const { label, opSymbol, beforeStr, afterStr } = changeDescription(change)
          return (
            <div key={i} className="sheet-proposal-change">
              <span className="sp-change-label">{label}</span>
              <span className="sp-change-arrow">
                {beforeStr} <span className="sp-op">{opSymbol}</span> {afterStr}
              </span>
            </div>
          )
        })}
      </div>
      <div className="sheet-proposal-actions">
        <button className="btn btn-primary small" onClick={handleApply} disabled={applying}>
          {applying ? 'Applying...' : 'Apply'}
        </button>
        <button className="btn btn-secondary small" onClick={handleDismiss} disabled={applying}>
          Dismiss
        </button>
      </div>
    </div>
  )
}