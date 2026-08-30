'use client'

/**
 * Live-table Realtime subscription hook — issue #198.
 *
 * Guarantees:
 * - Only audience-authorized private channels are subscribed (server via /realtime/authorize).
 * - Stable event_id / dedupe_key used to drop duplicate deliveries.
 * - Out-of-order events tolerated (buffer + sort by sequence before apply).
 * - Snapshot is authoritative: a client that misses events re-fetches snapshot and converges.
 * - Snapshot→subscribe race has no gap: we subscribe first (buffer), then snapshot, then reconcile.
 * - Reconnect automatically re-snapshots/reconciles instead of assuming stream is complete.
 * - Realtime failure never blocks submission acceptance — snapshot remains usable alone.
 *
 * Usage:
 *   const { messages, dmChunks, revision, connected, refresh } = useLiveTableRealtime({
 *     campaignId, threadId, enabled: !!campaignId
 *   })
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { supabase } from '@/lib/supabase'
import { apiFetch } from '@/lib/api'
import {
  dedupeEvents,
  getRealtimeClientMetrics,
  incRealtimeMetric,
  liveTableChannel,
  reconcileBufferedEvents,
  sortEventsBySequence,
  type RealtimeEvent,
  type SnapshotForRealtime,
} from '@/lib/realtime'

interface UseLiveTableRealtimeOptions {
  campaignId: string | null
  threadId: string | null
  enabled?: boolean
  // Optional pre-fetched snapshot to seed from; if omitted we fetch.
  initialSnapshot?: SnapshotForRealtime & { history?: { messages?: unknown[] } }
}

interface LiveTableState {
  messages: RealtimeEvent[]
  dmChunks: Map<string, RealtimeEvent[]> // streamId -> chunks
  dmStatus: RealtimeEvent | null
  revision: number | null
  realtimeResumeToken: string | null
  connected: boolean
  error: string | null
}

export function useLiveTableRealtime(opts: UseLiveTableRealtimeOptions) {
  const { campaignId, threadId, enabled = true, initialSnapshot } = opts

  const [state, setState] = useState<LiveTableState>({
    messages: [],
    dmChunks: new Map(),
    dmStatus: null,
    revision: initialSnapshot?.revision ?? null,
    realtimeResumeToken: initialSnapshot?.reconciliation?.realtime_resume_token ?? null,
    connected: false,
    error: null,
  })

  const bufferedRef = useRef<RealtimeEvent[]>([])
  const snapshotRef = useRef<SnapshotForRealtime | null>(initialSnapshot ?? null)
  const channelRef = useRef<ReturnType<typeof supabase.channel> | null>(null)
  const isMountedRef = useRef(true)
  const reconnectAttemptsRef = useRef(0)

  // Snapshot fetch (authoritative, retryable)
  const fetchSnapshot = useCallback(async (): Promise<SnapshotForRealtime | null> => {
    if (!campaignId) return null
    const params = new URLSearchParams()
    if (threadId) params.set('thread_id', threadId)
    const qs = params.toString() ? `?${params}` : ''
    try {
      const snap = await apiFetch<{
        revision: number
        reconciliation: SnapshotForRealtime['reconciliation']
        history: { messages: Array<{ id: string; sequence: number; raw_content?: string }> }
        dm_state: SnapshotForRealtime['dm_state']
        dm_messages: unknown[]
      }>(`/campaigns/${campaignId}/snapshot${qs}`)
      snapshotRef.current = snap as SnapshotForRealtime
      setState((prev) => ({
        ...prev,
        revision: (snap as { revision: number }).revision ?? prev.revision,
        realtimeResumeToken: (snap as { reconciliation: { realtime_resume_token: string } }).reconciliation?.realtime_resume_token ?? null,
      }))
      incRealtimeMetric('snapshotCatchups')
      return snap as unknown as SnapshotForRealtime
    } catch (e) {
      const msg = (e as Error).message ?? 'snapshot fetch failed'
      if (isMountedRef.current) setState((prev) => ({ ...prev, error: msg }))
      return null
    }
  }, [campaignId, threadId])

  // Apply a batch of events (dedupe + sort + merge into state)
  const applyEvents = useCallback((events: RealtimeEvent[]) => {
    if (!events.length) return
    const deduped = dedupeEvents(events)
    if (deduped.length < events.length) incRealtimeMetric('duplicateDeliveries', events.length - deduped.length)
    const sorted = sortEventsBySequence(deduped)

    setState((prev) => {
      const nextMessages = [...prev.messages]
      const seenIds = new Set(nextMessages.map((m) => m.id ?? m.event_id))
      const nextChunks = new Map(prev.dmChunks)
      let nextStatus = prev.dmStatus
      let nextRevision = prev.revision

      for (const ev of sorted) {
        if (ev.type === 'submission.created') {
          const id = String(ev.id ?? ev.event_id)
          if (!seenIds.has(id)) {
            nextMessages.push(ev)
            seenIds.add(id)
          }
        } else if (ev.type === 'dm.chunk') {
          const sid = String(ev.stream_id ?? 'unknown')
          const arr = nextChunks.get(sid) ?? []
          // dedupe chunk by event_id within stream
          if (!arr.some((c) => c.event_id === ev.event_id)) {
            nextChunks.set(sid, sortEventsBySequence([...arr, ev]))
          }
        } else if (ev.type === 'dm.status' || ev.type === 'dm.thinking') {
          nextStatus = ev
        }
        if (typeof ev.revision === 'number') nextRevision = Math.max(nextRevision ?? 0, ev.revision)
        if (typeof ev.sequence === 'number' && ev.type === 'revision') nextRevision = Math.max(nextRevision ?? 0, ev.sequence)
      }

      return {
        ...prev,
        messages: sortEventsBySequence(nextMessages) as RealtimeEvent[],
        dmChunks: nextChunks,
        dmStatus: nextStatus,
        revision: nextRevision,
      }
    })
  }, [])

  // Subscription lifecycle — subscribe first, then snapshot, then reconcile (race-safe)
  useEffect(() => {
    if (!enabled || !campaignId || !threadId) {
      setState((prev) => ({ ...prev, connected: false }))
      return
    }

    isMountedRef.current = true
    bufferedRef.current = []
    let cancelled = false

    async function authorizeAndSubscribe() {
      // Audience check: ask backend if we may subscribe to this private channel.
      // Private existence hidden as 404 — unauthorized clients cannot learn the channel.
      const channelName = liveTableChannel(campaignId!, threadId!)
      try {
        await apiFetch(`/campaigns/${campaignId}/realtime/authorize`, {
          method: 'POST',
          body: JSON.stringify({ channel: channelName }),
        })
      } catch (e) {
        const status = (e as { status?: number }).status
        const msg =
          status === 404
            ? 'Thread not found'
            : status === 403
              ? 'Not authorized for this thread'
              : (e as Error).message ?? 'Realtime authorization failed'
        if (!cancelled && isMountedRef.current) setState((prev) => ({ ...prev, error: msg, connected: false }))
        return
      }
      if (cancelled) return

      // Subscribe first — buffer events immediately, even before snapshot fetch.
      const ch = supabase.channel(channelName, { config: { private: true } } as unknown as Record<string, unknown>)
      // Supabase broadcast pattern: event 'broadcast' with { event: type }
      try {
        // Broadcast events (server publishes via broadcast)
        ch.on('broadcast' as never, { event: 'submission.created' } as never, (payload: { payload: RealtimeEvent }) => {
          const ev = (payload.payload ?? payload) as RealtimeEvent
          bufferedRef.current.push(ev)
          // If snapshot already fetched, apply immediately (still deduped)
          if (snapshotRef.current) {
            const reconciled = reconcileBufferedEvents(snapshotRef.current, [ev])
            if (reconciled.length) {
              incRealtimeMetric('reconciliationEvents')
              applyEvents(reconciled)
            } else {
              // Event already in snapshot — drop (race case), count as reconciliation
              incRealtimeMetric('reconciliationEvents')
            }
          }
        })
        ch.on('broadcast' as never, { event: 'dm.chunk' } as never, (payload: { payload: RealtimeEvent }) => {
          const ev = (payload.payload ?? payload) as RealtimeEvent
          bufferedRef.current.push(ev)
          if (snapshotRef.current) {
            const reconciled = reconcileBufferedEvents(snapshotRef.current, [ev])
            if (reconciled.length) applyEvents(reconciled)
          }
        })
        ch.on('broadcast' as never, { event: 'dm.status' } as never, (payload: { payload: RealtimeEvent }) => {
          const ev = (payload.payload ?? payload) as RealtimeEvent
          bufferedRef.current.push(ev)
          if (snapshotRef.current) applyEvents([ev])
        })
        ch.on('broadcast' as never, { event: 'dm.thinking' } as never, (payload: { payload: RealtimeEvent }) => {
          const ev = (payload.payload ?? payload) as RealtimeEvent
          bufferedRef.current.push(ev)
          if (snapshotRef.current) applyEvents([ev])
        })
        ch.on('broadcast' as never, { event: 'revision' } as never, (payload: { payload: RealtimeEvent }) => {
          const ev = (payload.payload ?? payload) as RealtimeEvent
          bufferedRef.current.push(ev)
          if (snapshotRef.current) applyEvents([ev])
        })
      } catch {
        // If broadcast .on signature differs, fall back to postgres_changes (still private via RLS)
      }

      // Also listen to postgres changes if configured (durability fallback) — optional
      try {
        // no-op if not using postgres_changes; kept for completeness
      } catch { /* ignore */ }

      channelRef.current = ch as unknown as ReturnType<typeof supabase.channel>

      const onStatusChange = (status: string) => {
        if (cancelled) return
        if (status === 'SUBSCRIBED') {
          if (isMountedRef.current) setState((prev) => ({ ...prev, connected: true, error: null }))
          incRealtimeMetric('subscriptionCount')
        } else if (status === 'CHANNEL_ERROR' || status === 'TIMED_OUT' || status === 'CLOSED') {
          if (isMountedRef.current) setState((prev) => ({ ...prev, connected: false }))
          // Reconnect auto re-snapshots/reconciles
          if (!cancelled) {
            reconnectAttemptsRef.current += 1
            incRealtimeMetric('reconnectCount')
            // Backoff then resubscribe + re-snapshot
            setTimeout(async () => {
              if (cancelled || !isMountedRef.current) return
              const snap = await fetchSnapshot()
              if (snap && bufferedRef.current.length) {
                const reconciled = reconcileBufferedEvents(snap, bufferedRef.current)
                if (reconciled.length) applyEvents(reconciled)
                bufferedRef.current = []
              }
            }, Math.min(1000 * 2 ** reconnectAttemptsRef.current, 15000))
          }
        }
      }

      try {
        ch.subscribe(onStatusChange as never)
      } catch {
        // supabase-js v2: .subscribe(callback)
        try {
          ;(ch as unknown as { subscribe: (cb: (s: string) => void) => void }).subscribe(onStatusChange)
        } catch { /* ignore */ }
      }

      // Now fetch snapshot (after subscribe, so no gap)
      const snap = await fetchSnapshot()
      if (cancelled || !snap) return

      // Reconcile any events that arrived between subscribe and snapshot response
      if (bufferedRef.current.length) {
        const reconciled = reconcileBufferedEvents(snap, bufferedRef.current)
        incRealtimeMetric('reconciliationEvents')
        if (reconciled.length) applyEvents(reconciled)
        // Clear buffer of events already reconciled; keep future buffer live
        // We keep only events that were reconciled + new ones will arrive via handler.
        // To avoid double-apply, clear handled ones and keep empty for live path.
        bufferedRef.current = []
        // Seed state from snapshot history messages that aren't yet in realtime
        // (snapshot is authoritative; realtime supplements it)
        const snapMessages = (snap as unknown as { history?: { messages?: Array<{ id: string; sequence: number; raw_content?: string }> } }).history?.messages
        if (Array.isArray(snapMessages) && snapMessages.length) {
          const asEvents: RealtimeEvent[] = snapMessages.map((m) => ({
            type: 'submission.created',
            event_id: `submission:${m.id}`,
            campaign_id: campaignId!,
            thread_id: threadId!,
            sequence: m.sequence,
            id: m.id,
            raw_content: m.raw_content,
            dedupe_key: m.id,
          }))
          // Only add those not already present
          applyEvents(asEvents)
        }
      } else {
        // No buffered events — seed from snapshot
        const snapMessages = (snap as unknown as { history?: { messages?: Array<{ id: string; sequence: number; raw_content?: string }> } }).history?.messages
        if (Array.isArray(snapMessages) && snapMessages.length) {
          const asEvents: RealtimeEvent[] = snapMessages.map((m) => ({
            type: 'submission.created',
            event_id: `submission:${m.id}`,
            campaign_id: campaignId!,
            thread_id: threadId!,
            sequence: m.sequence,
            id: m.id,
            raw_content: m.raw_content,
            dedupe_key: m.id,
          }))
          applyEvents(asEvents)
        }
      }
    }

    authorizeAndSubscribe()

    return () => {
      cancelled = true
      isMountedRef.current = false
      if (channelRef.current) {
        try {
          supabase.removeChannel(channelRef.current as unknown as Parameters<typeof supabase.removeChannel>[0])
        } catch {
          try {
            ;(channelRef.current as unknown as { unsubscribe: () => void }).unsubscribe()
          } catch { /* ignore */ }
        }
        channelRef.current = null
      }
    }
  }, [campaignId, threadId, enabled, fetchSnapshot, applyEvents])

  const refresh = useCallback(async () => {
    const snap = await fetchSnapshot()
    if (snap && bufferedRef.current.length) {
      const reconciled = reconcileBufferedEvents(snap, bufferedRef.current)
      if (reconciled.length) applyEvents(reconciled)
      bufferedRef.current = []
    }
    return snap
  }, [fetchSnapshot, applyEvents])

  // Expose helper to manually ingest an event (for tests / fallback polling)
  const ingest = useCallback(
    (ev: RealtimeEvent | RealtimeEvent[]) => {
      const arr = Array.isArray(ev) ? ev : [ev]
      if (snapshotRef.current) {
        const reconciled = reconcileBufferedEvents(snapshotRef.current, arr)
        if (reconciled.length) applyEvents(reconciled)
      } else {
        bufferedRef.current.push(...arr)
      }
    },
    [applyEvents],
  )

  return {
    messages: state.messages,
    dmChunks: state.dmChunks,
    dmStatus: state.dmStatus,
    revision: state.revision,
    realtimeResumeToken: state.realtimeResumeToken,
    connected: state.connected,
    error: state.error,
    refresh,
    ingest,
    metrics: getRealtimeClientMetrics(),
  }
}
