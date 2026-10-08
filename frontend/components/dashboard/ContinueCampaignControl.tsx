'use client'

import Link from 'next/link'
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { apiFetch } from '@/lib/api'

type AdventureSummary = {
  id: string
  title: string
  status: 'active' | 'completed'
  outcome: string | null
  public_summary: string | null
}

type AdventureList = {
  campaign_status: string
  current_adventure_id: string | null
  adventures: AdventureSummary[]
}

interface Props {
  campaignId: string
  isOwner: boolean
  /** Changes whenever the table may have moved on (e.g. a DM turn landed),
   * so a just-completed adventure is noticed without a page reload. */
  refreshKey?: string | number
}

/** Continue the campaign into a new adventure (issue #264).
 *
 * Shown on the live table once the AI DM has completed the current
 * adventure and none is open. The same world, characters, clocks and
 * fictional time carry over — only a new adventure arc opens. Opening it
 * is an owner host action; other players see a neutral note.
 */
export default function ContinueCampaignControl({ campaignId, isOwner, refreshKey }: Props) {
  const [list, setList] = useState<AdventureList | null>(null)
  const [title, setTitle] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  // One command per attempted title: a retry after a lost acknowledgement
  // replays the same key instead of opening a second adventure.
  const command = useRef<{ title: string; key: string } | null>(null)

  const load = useCallback(async () => {
    try {
      const data = await apiFetch<AdventureList>(`/campaigns/${campaignId}/adventures`)
      setList(data)
    } catch {
      // Non-critical: the table keeps working; the next refresh retries.
    }
  }, [campaignId])

  useEffect(() => { void load() }, [load, refreshKey])

  const last = list?.adventures.at(-1)
  const canContinue = Boolean(
    list && list.campaign_status === 'active' && list.current_adventure_id === null && last?.status === 'completed',
  )
  const defaultTitle = `Adventure ${(list?.adventures.length ?? 0) + 1}`

  if (!canContinue || !last) return null

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (pending) return
    const nextTitle = (title.trim() || defaultTitle).slice(0, 160)
    if (command.current?.title !== nextTitle) {
      command.current = { title: nextTitle, key: crypto.randomUUID() }
    }
    setPending(true)
    setError('')
    try {
      await apiFetch(`/campaigns/${campaignId}/adventures`, {
        method: 'POST',
        headers: { 'Idempotency-Key': command.current.key },
        body: JSON.stringify({ title: nextTitle }),
      })
      command.current = null
      setTitle('')
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not continue the campaign.')
    } finally {
      setPending(false)
    }
  }

  return <section aria-label="Adventure complete" className="continue-campaign">
    <p role="status">
      <strong>{last.title}</strong> is complete{last.outcome ? ` (${last.outcome.replace(/_/g, ' ')})` : ''}.
      {' '}<Link href={`/campaigns/${campaignId}/adventures/${last.id}`}>Review adventure</Link>
    </p>
    {isOwner ? <form onSubmit={(event) => void submit(event)}>
      <label htmlFor="continue-campaign-title">Next adventure title</label>{' '}
      <input id="continue-campaign-title" type="text" maxLength={160} value={title}
        placeholder={defaultTitle} onChange={(event) => setTitle(event.target.value)} disabled={pending} />{' '}
      <button type="submit" className="btn btn-primary small" disabled={pending} aria-busy={pending}>
        {pending ? 'Continuing…' : 'Continue campaign'}
      </button>
      <p>The world, characters, clocks and time carry over unchanged.</p>
    </form> : <p>The campaign continues when the campaign owner opens the next adventure.</p>}
    {error && <p role="alert">{error}</p>}
  </section>
}
