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
  const beforeStr = formatValue(before)
  const afterStr = formatValue(after)
  return { label, opSymbol, beforeStr, afterStr }
}

export default function SheetProposalPopup({ proposals, sessionId, onApplied, onDismissed }) {
  const [applying, setApplying] = useState({})
  const [error, setError] = useState(null)

  if (!proposals || proposals.length === 0) return null

  const handleApply = async (proposal) => {
    setApplying((prev) => ({ ...prev, [proposal.id]: true }))
    setError(null)
    try {
      const data = await applySheetProposal(sessionId, proposal.id)
      onApplied?.(data.proposal, data.character)
    } catch (err) {
      setError(err.message || 'Failed to apply changes')
      setApplying((prev) => ({ ...prev, [proposal.id]: false }))
    }
  }

  const handleDismiss = async (proposal) => {
    setApplying((prev) => ({ ...prev, [proposal.id]: true }))
    setError(null)
    try {
      await dismissSheetProposal(sessionId, proposal.id)
      onDismissed?.(proposal)
    } catch (err) {
      setError(err.message || 'Failed to dismiss')
      setApplying((prev) => ({ ...prev, [proposal.id]: false }))
    }
  }

  return (
    <div className="modal-overlay">
      <div className="modal-panel sheet-proposal-panel">
        <div className="modal-header">
          <h2>Character Sheet Updates</h2>
        </div>
        <div className="sheet-proposal-body">
          {error && <div className="error-message">{error}</div>}
          {proposals.map((proposal) => (
            <div key={proposal.id} className="sheet-proposal-card">
              <p className="sheet-proposal-reason">{proposal.reason}</p>
              <div className="sheet-proposal-changes">
                {proposal.changes.map((change, index) => {
                  const { label, opSymbol, beforeStr, afterStr } = changeDescription(change)
                  return (
                    <div key={index} className="sheet-proposal-change">
                      <span className="sp-change-label">{label}</span>
                      <span className="sp-change-arrow">
                        {beforeStr}
                        <span className="sp-op">{opSymbol}</span>
                        {afterStr}
                      </span>
                    </div>
                  )
                })}
              </div>
              <div className="sheet-proposal-actions">
                <button
                  className="btn btn-primary small"
                  onClick={() => handleApply(proposal)}
                  disabled={applying[proposal.id]}
                >
                  {applying[proposal.id] ? 'Applying...' : 'Apply'}
                </button>
                <button
                  className="btn btn-secondary small"
                  onClick={() => handleDismiss(proposal)}
                  disabled={applying[proposal.id]}
                >
                  Dismiss
                </button>
              </div>
            </div>
          ))}
        </div>
        {proposals.length > 1 && (
          <div className="sheet-proposal-bulk-actions">
            <button
              className="btn btn-primary"
              onClick={() => proposals.forEach((p) => { if (p.status === 'pending') handleApply(p) })}
              disabled={Object.values(applying).some(Boolean)}
            >
              Apply All
            </button>
          </div>
        )}
      </div>
    </div>
  )
}