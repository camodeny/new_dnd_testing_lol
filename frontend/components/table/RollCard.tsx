'use client'

import { useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import type { PlayerRollForRealtime } from '@/lib/realtime'
import { keptDie, rollDice, rollDiceCount, rollInstructions, signed, type TableRollModifier } from '@/lib/table'

interface RollCardProps {
  campaignId: string
  roll: PlayerRollForRealtime
  /** Server-derived modifier; without it the app can't roll for you. */
  modifier?: TableRollModifier
  refresh: () => Promise<unknown>
}

type Command = { key: string; body: Record<string, unknown> }

/**
 * A roll the AI DM asked this player for, in the conversation. "Roll for me"
 * rolls fair dice on this device (d20s, or a hit's damage dice); "I rolled my
 * own dice" takes a typed total.
 * Private DCs are never part of the projection, so nothing here can show one.
 */
export default function RollCard({ campaignId, roll, modifier, refresh }: RollCardProps) {
  const [manual, setManual] = useState(false)
  const [total, setTotal] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  // Dice are rolled once per request and reused on retry: a lost
  // acknowledgement must resend the same roll, never a fresh one.
  const appCommand = useRef<Command | null>(null)
  const manualCommand = useRef<{ total: number; command: Command } | null>(null)

  const send = async (command: Command) => {
    setPending(true)
    setError('')
    try {
      await apiFetch(`/campaigns/${campaignId}/roll-requests/${roll.id}/fulfill`, {
        method: 'POST', headers: { 'Idempotency-Key': command.key }, body: JSON.stringify(command.body),
      })
      await refresh()
    } catch {
      setError('Your roll could not be confirmed. Try again — the same roll will be sent.')
    } finally {
      setPending(false)
    }
  }

  const rollForMe = () => {
    if (pending || !modifier) return
    if (!appCommand.current) {
      // Damage dice are summed; a d20 roll keeps one die.
      const { dice } = modifier
      const rawRolls = dice ? rollDice(dice.count, dice.sides) : rollDice(rollDiceCount(roll.advantage_state), 20)
      const rolled = dice ? rawRolls.reduce((sum, n) => sum + n, 0) : keptDie(rawRolls, roll.advantage_state)
      appCommand.current = {
        key: crypto.randomUUID(),
        body: { source: 'app', raw_rolls: rawRolls, modifier: modifier.modifier, total: rolled + modifier.modifier },
      }
    }
    void send(appCommand.current)
  }

  const submitManual = (event: React.FormEvent) => {
    event.preventDefault()
    const value = Number(total)
    if (pending || !total.trim() || !Number.isInteger(value)) return
    if (!manualCommand.current || manualCommand.current.total !== value) {
      manualCommand.current = { total: value, command: { key: crypto.randomUUID(), body: { source: 'physical', total: value } } }
    }
    void send(manualCommand.current.command)
  }

  return (
    <section className="tv-card tv-roll" aria-label="Roll requested by the AI DM">
      <div className="tv-card-eyebrow"><i className="bi bi-dice-5" aria-hidden="true" /> Your turn to roll</div>
      <h3>{roll.reason_public || roll.label}</h3>
      {roll.reason_public && <p className="tv-muted">{roll.label}</p>}
      <p className="tv-roll-how">{rollInstructions(roll, modifier)}</p>
      {manual ? (
        <form className="tv-roll-manual" onSubmit={submitManual}>
          <label htmlFor={`roll-total-${roll.id}`}>
            Your total{modifier ? `, including ${signed(modifier.modifier)}` : ', including your bonus'}
          </label>
          <div className="tv-row">
            <input
              id={`roll-total-${roll.id}`} aria-label="Roll total" type="number" inputMode="numeric"
              step="1" min="-10000" max="10000" required autoFocus
              value={total} disabled={pending} onChange={(e) => setTotal(e.target.value)}
            />
            <button className="tv-btn primary" type="submit" disabled={pending || !total.trim()}>
              {pending ? 'Sending…' : 'Submit'}
            </button>
            <button className="tv-btn ghost" type="button" disabled={pending} onClick={() => setManual(false)}>Back</button>
          </div>
        </form>
      ) : (
        <div className="tv-row">
          {modifier && (
            <button className="tv-btn primary" type="button" disabled={pending} onClick={rollForMe}>
              {pending ? 'Rolling…' : 'Roll for me'}
            </button>
          )}
          <button className={`tv-btn ${modifier ? 'ghost' : 'primary'}`} type="button" disabled={pending} onClick={() => setManual(true)}>
            I rolled my own dice
          </button>
        </div>
      )}
      {error && <p role="alert" className="tv-error">{error}</p>}
    </section>
  )
}
