'use client'

import { useEffect, useState } from 'react'
import { useParams } from 'next/navigation'
import { apiFetch } from '@/lib/api'
import Loading from '@/components/common/Loading'
import ErrorMessage from '@/components/common/ErrorMessage'

type Adventure = {
  id: string
  title: string
  status: string
  outcome: string | null
  outcome_reason: string | null
}

type RecapResponse = {
  adventure: Adventure
  recap_text: string
  status: string
  version: number
  is_derived: boolean
  stale_warning: string | null
  source_event_from: number
  source_event_to: number | null
  source_revision: number | null
}

/** Review Adventure — player-facing recap surface (issue #263).
 *
 * Shows only the visibility-filtered recap projection. Derived prose is
 * labeled as such; events/facts/world remain authoritative on conflict.
 */
export default function ReviewAdventurePage() {
  const { id, adventureId } = useParams<{ id: string; adventureId: string }>()
  const [recap, setRecap] = useState<RecapResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    apiFetch<RecapResponse>(`/campaigns/${id}/adventures/${adventureId}/recap`)
      .then((data) => {
        if (!cancelled) {
          setRecap(data)
          setError('')
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load recap')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [id, adventureId])

  if (loading) return <Loading />
  if (error) return <ErrorMessage message={error} />
  if (!recap) return <ErrorMessage message="Recap not found" />

  return (
    <main className="mx-auto max-w-2xl px-4 py-8">
      <p className="text-sm uppercase tracking-wide text-gray-500">Adventure recap</p>
      <h1 className="mt-1 text-2xl font-bold">{recap.adventure.title}</h1>
      <p className="mt-1 text-sm text-gray-600">
        Outcome: {recap.adventure.outcome ?? '—'}
        {recap.adventure.outcome_reason ? ` — ${recap.adventure.outcome_reason}` : ''}
      </p>
      {recap.stale_warning && (
        <div role="alert" className="mt-4 rounded border border-amber-400 bg-amber-50 p-3 text-sm">
          {recap.stale_warning}
        </div>
      )}
      <article className="mt-4 whitespace-pre-line rounded border p-4">{recap.recap_text}</article>
      <p className="mt-3 text-xs text-gray-500">
        Derived recap (v{recap.version}
        {recap.source_event_to != null
          ? `, events ${recap.source_event_from}–${recap.source_event_to}`
          : ''}
        ). Adventure events and world facts outrank this prose on any conflict.
      </p>
    </main>
  )
}
