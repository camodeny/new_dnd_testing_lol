'use client'

import { useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import type { DmStateForRealtime, PlayerRollForRealtime } from '@/lib/realtime'

interface Props {
  campaignId: string
  dmState: DmStateForRealtime | null
  rolls: PlayerRollForRealtime[]
  userId?: string
  refresh: () => Promise<unknown>
}

export default function DmTurnControls({ campaignId, dmState, rolls, userId, refresh }: Props) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const retryCommand = useRef<{ attemptId: string; key: string } | null>(null)
  const retry = async () => {
    if (pending || !dmState?.turn_id || !dmState.attempt_id) return
    if (retryCommand.current?.attemptId !== dmState.attempt_id) {
      retryCommand.current = { attemptId: dmState.attempt_id, key: crypto.randomUUID() }
    }
    setPending(true)
    setError('')
    try {
      await apiFetch(`/campaigns/${campaignId}/dm-turns/${dmState.turn_id}/retry`, {
        method: 'POST', headers: { 'Idempotency-Key': retryCommand.current.key },
        body: JSON.stringify({ attempt_id: retryCommand.current.attemptId }),
      })
      await refresh()
    } catch {
      setError('Retry could not be confirmed. Your original action is still saved.')
    } finally { setPending(false) }
  }
  // The viewer's own pending roll is a card in the conversation; here the
  // table only notes that someone else's roll is outstanding.
  const awaitingOthers = rolls.filter((roll) => roll.status === 'pending' && roll.requested_user_id !== userId)
  return <div aria-label="AI DM turn status">
    {['pending', 'thinking'].includes(String(dmState?.status)) && <p role="status">The AI DM is considering your action…</p>}
    {dmState?.status === 'failed_visible' && <div role="alert">
      <p>The AI DM could not finish this turn. Your action is saved.</p>
      {dmState.can_retry === true && <button type="button" className="btn btn-secondary small" onClick={() => void retry()} disabled={pending}>
        {pending ? 'Retrying…' : 'Retry action'}
      </button>}
    </div>}
    {awaitingOthers.map((roll) => <p role="status" key={roll.id}>Waiting for a player’s {roll.label} roll.</p>)}
    {error && <p role="alert">{error}</p>}
  </div>
}
