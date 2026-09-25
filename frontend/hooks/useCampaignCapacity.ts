'use client'

/**
 * Campaign capacity projection hook — issue #255.
 *
 * Polls the participant-safe `GET /api/campaigns/{id}/capacity-state` hook
 * (#254) and derives a simple percentage/state view via
 * `@/lib/capacity`. A projection failure never invents a percentage: the
 * view falls back to `unavailable` with a neutral retry. Callers invoke
 * `refresh()` to resync from the authoritative projection after a 409
 * pause signal or funding/BYOK changes restore capacity.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import {
  deriveCapacityView,
  type CapacityStatePayload,
  type CapacityUiEvent,
  type CapacityView,
} from '@/lib/capacity'

const REFRESH_INTERVAL_MS = 30_000

interface UseCampaignCapacityOptions {
  campaignId: string | null
  enabled?: boolean
  refreshIntervalMs?: number
  onEvent?: (event: CapacityUiEvent) => void
}

export interface CampaignCapacity {
  view: CapacityView
  payload: CapacityStatePayload | null
  loading: boolean
  error: string | null
  refresh: () => Promise<void>
}

export function useCampaignCapacity({
  campaignId,
  enabled = true,
  refreshIntervalMs = REFRESH_INTERVAL_MS,
  onEvent,
}: UseCampaignCapacityOptions): CampaignCapacity {
  const [payload, setPayload] = useState<CapacityStatePayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const eventRef = useRef(onEvent)
  eventRef.current = onEvent
  const pausedNotifiedRef = useRef(false)

  const refresh = useCallback(async () => {
    if (!campaignId || !enabled) return
    setLoading(true)
    try {
      const data = await apiFetch<CapacityStatePayload>(`/campaigns/${campaignId}/capacity-state`)
      setPayload(data)
      setError(null)
    } catch (err) {
      setError((err as Error).message || 'Campaign capacity is unavailable right now.')
      eventRef.current?.({ type: 'projection_load_error', message: (err as Error).message || 'unknown' })
    } finally {
      setLoading(false)
    }
  }, [campaignId, enabled])

  useEffect(() => {
    pausedNotifiedRef.current = false
    setPayload(null)
    setError(null)
    if (!campaignId || !enabled) return
    void refresh()
    if (refreshIntervalMs <= 0) return
    const timer = setInterval(() => { void refresh() }, refreshIntervalMs)
    return () => clearInterval(timer)
  }, [campaignId, enabled, refresh, refreshIntervalMs])

  // Before the first projection lands (and with no error), there is no
  // percentage to show yet — and none is invented. The component renders a
  // quiet placeholder until then.
  const view: CapacityView = payload
    ? deriveCapacityView(payload)
    : { state: 'unavailable', percent: null }

  useEffect(() => {
    if (view.state === 'paused' && !pausedNotifiedRef.current) {
      pausedNotifiedRef.current = true
      eventRef.current?.({ type: 'paused_view' })
    }
    if (view.state !== 'paused') {
      // Capacity restored (funding/BYOK re-credit resyncs through the same
      // authoritative projection) — re-arm the paused-view signal without a
      // special recovery flow.
      pausedNotifiedRef.current = false
    }
  }, [view.state])

  return { view, payload, loading, error, refresh }
}
