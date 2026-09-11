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

function RollEntry({ campaignId, roll, refresh }: { campaignId: string; roll: PlayerRollForRealtime; refresh: Props['refresh'] }) {
  const [total, setTotal] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const command = useRef<{ key: string; total: number } | null>(null)
  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (pending || !total.trim() || !Number.isInteger(Number(total))) return
    if (!command.current || command.current.total !== Number(total)) {
      command.current = { key: crypto.randomUUID(), total: Number(total) }
    }
    setPending(true)
    setError('')
    try {
      await apiFetch(`/campaigns/${campaignId}/roll-requests/${roll.id}/fulfill`, {
        method: 'POST', headers: { 'Idempotency-Key': command.current.key },
        body: JSON.stringify({ source: 'physical', total: command.current.total }),
      })
      await refresh()
    } catch {
      setError('Your roll could not be confirmed. Try submitting the same total again.')
    } finally { setPending(false) }
  }
  return <form onSubmit={submit} style={{ marginBlock: 12 }}>
    <strong>{roll.label}</strong>
    <p>{roll.reason_public}</p>
    {roll.advantage_state !== 'normal' && <p>Roll with {roll.advantage_state}.</p>}
    <label>Enter your roll total, including modifiers
      <input aria-label="Roll total" type="number" step="1" min="-10000" max="10000" required
        value={total} disabled={pending} onChange={(event) => setTotal(event.target.value)} />
    </label>{' '}
    <button className="btn btn-secondary small" type="submit" disabled={pending || !total.trim()}>
      {pending ? 'Submitting…' : 'Submit roll'}
    </button>
    {error && <p role="alert">{error}</p>}
  </form>
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
  const awaiting = rolls.filter((roll) => roll.status === 'pending')
  return <div aria-label="AI DM turn status">
    {['pending', 'thinking'].includes(String(dmState?.status)) && <p role="status">The AI DM is considering your action…</p>}
    {dmState?.status === 'failed_visible' && <div role="alert">
      <p>The AI DM could not finish this turn. Your action is saved.</p>
      {dmState.can_retry === true && <button type="button" className="btn btn-secondary small" onClick={() => void retry()} disabled={pending}>
        {pending ? 'Retrying…' : 'Retry action'}
      </button>}
    </div>}
    {awaiting.map((roll) => roll.requested_user_id === userId
      ? <RollEntry key={roll.id} campaignId={campaignId} roll={roll} refresh={refresh} />
      : <p role="status" key={roll.id}>Waiting for a player’s {roll.label}.</p>)}
    {error && <p role="alert">{error}</p>}
  </div>
}
