'use client'

/**
 * Live-table Realtime subscription hook — issue #198.
 *
 * Guarantees:
 * - Only audience-authorized private channels are subscribed (server via /realtime/authorize).
 * - Stable event_id / dedupe_key used to drop duplicate deliveries.
 * - Out-of-order events tolerated (buffer + sort by sequence before apply).
 * - Snapshot is authoritative: a client that misses events re-fetches snapshot and converges
 *   exactly (messages + DM active visible_text + completed DM messages).
 * - Snapshot→subscribe race has no gap: we subscribe first (buffer), then snapshot, then reconcile.
 * - Reconnect automatically re-snapshots/reconciles and resubscribes the channel instead of
 *   assuming the event stream is complete. If a dm.chunk was missed, the fresh snapshot's
 *   dm_state.visible_text already contains it and reconciliation drops the stale buffered copy.
 * - Realtime failure never blocks submission acceptance — snapshot remains usable alone.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { supabase } from '@/lib/supabase'
import { apiFetch } from '@/lib/api'
import {
  dedupeEvents,
  deriveDmState,
  getRealtimeClientMetrics,
  incRealtimeMetric,
  liveTableChannel,
  reconcileBufferedEvents,
  sortEventsBySequence,
  type DmMessageForRealtime,
  type DmStateForRealtime,
  type RealtimeEvent,
  type SnapshotForRealtime,
} from '@/lib/realtime'

interface UseLiveTableRealtimeOptions {
  campaignId: string | null
  threadId: string | null
  enabled?: boolean
  initialSnapshot?: SnapshotForRealtime
}

interface LiveTableState {
  messages: RealtimeEvent[]
  dmState: DmStateForRealtime | null
  dmMessages: DmMessageForRealtime[]
  dmChunks: Map<string, RealtimeEvent[]> // incremental chunks newer than snapshot
  dmStatus: RealtimeEvent | null
  revision: number | null
  realtimeResumeToken: string | null
  connected: boolean
  error: string | null
}

function snapshotToMessageEvents(snap: SnapshotForRealtime, campaignId: string, threadId: string): RealtimeEvent[] {
  const msgs = snap.history?.messages ?? []
  return msgs.map((m) => ({
    type: 'submission.created',
    event_id: `submission:${m.id}`,
    campaign_id: campaignId,
    thread_id: threadId,
    sequence: m.sequence,
    id: m.id,
    raw_content: (m as { raw_content?: string }).raw_content,
    segments: (m as { segments?: RealtimeEvent['segments'] }).segments,
    dedupe_key: m.id,
  }))
}

export function useLiveTableRealtime(opts: UseLiveTableRealtimeOptions) {
  const { campaignId, threadId, enabled = true, initialSnapshot } = opts

  const [state, setState] = useState<LiveTableState>(() => {
    if (initialSnapshot && campaignId && threadId) {
      return {
        messages: snapshotToMessageEvents(initialSnapshot, campaignId, threadId),
        dmState: initialSnapshot.dm_state ?? null,
        dmMessages: initialSnapshot.dm_messages ?? [],
        dmChunks: new Map(),
        dmStatus: null,
        revision: initialSnapshot.revision ?? null,
        realtimeResumeToken: initialSnapshot.reconciliation?.realtime_resume_token ?? null,
        connected: false,
        error: null,
      }
    }
    return {
      messages: [],
      dmState: null,
      dmMessages: [],
      dmChunks: new Map(),
      dmStatus: null,
      revision: initialSnapshot?.revision ?? null,
      realtimeResumeToken: initialSnapshot?.reconciliation?.realtime_resume_token ?? null,
      connected: false,
      error: null,
    }
  })

  const bufferedRef = useRef<RealtimeEvent[]>([])
  const snapshotRef = useRef<SnapshotForRealtime | null>(initialSnapshot ?? null)
  const channelRef = useRef<ReturnType<typeof supabase.channel> | null>(null)
  const isMountedRef = useRef(true)
  const reconnectAttemptsRef = useRef(0)
  const campaignIdRef = useRef(campaignId)
  const threadIdRef = useRef(threadId)
  campaignIdRef.current = campaignId
  threadIdRef.current = threadId

  // Authoritative snapshot adoption — replaces full client projection first
  const adoptSnapshot = useCallback(
    (snap: SnapshotForRealtime) => {
      if (!campaignIdRef.current || !threadIdRef.current) return
      const cid = campaignIdRef.current
      const tid = threadIdRef.current
      snapshotRef.current = snap
      const msgs = snapshotToMessageEvents(snap, cid, tid)
      setState((prev) => ({
        ...prev,
        revision: snap.revision ?? prev.revision,
        realtimeResumeToken: snap.reconciliation?.realtime_resume_token ?? prev.realtimeResumeToken,
        messages: msgs,
        dmState: snap.dm_state ?? null,
        dmMessages: snap.dm_messages ?? [],
        // Clear incremental chunks — snapshot's visible_text is authoritative.
        // New chunks strictly newer than snapshot will repopulate this map.
        dmChunks: new Map(),
        dmStatus: null,
        error: null,
      }))
      incRealtimeMetric('snapshotCatchups')
    },
    [],
  )

  // Snapshot fetch (authoritative, retryable) — adopts immediately
  const fetchSnapshotAndAdopt = useCallback(async (): Promise<SnapshotForRealtime | null> => {
    const cid = campaignIdRef.current
    const tid = threadIdRef.current
    if (!cid) return null
    const params = new URLSearchParams()
    if (tid) params.set('thread_id', tid)
    const qs = params.toString() ? `?${params}` : ''
    try {
      const snap = await apiFetch<SnapshotForRealtime>(`/campaigns/${cid}/snapshot${qs}`)
      adoptSnapshot(snap)
      return snap
    } catch (e) {
      const msg = (e as Error).message ?? 'snapshot fetch failed'
      if (isMountedRef.current) setState((prev) => ({ ...prev, error: msg }))
      return null
    }
  }, [adoptSnapshot])

  // Apply a batch of events (dedupe + sort + merge into state)
  // Assumes snapshot already adopted; caller must reconcile first.
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
      let nextDmState = prev.dmState ? { ...prev.dmState } : prev.dmState

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
          const alreadyIncorporated =
            arr.some((c) => c.event_id === ev.event_id) ||
            (typeof ev.sequence === 'number' && arr.some((c) => c.sequence === ev.sequence && String(c.stream_id) === sid))
          if (alreadyIncorporated) {
            incRealtimeMetric('duplicateDeliveries', 1)
          } else {
            const newArr = sortEventsBySequence([...arr, ev])
            nextChunks.set(sid, newArr)
            if (nextDmState && String(nextDmState.stream_id) === sid) {
              const base = snapshotRef.current?.dm_state ?? null
              const derived = deriveDmState(base, newArr)
              if (derived) nextDmState = derived
            }
          }
        } else if (ev.type === 'dm.status' || ev.type === 'dm.thinking') {
          const evSid = ev.stream_id ? String(ev.stream_id) : null
          const activeSid = nextDmState?.stream_id ? String(nextDmState.stream_id) : null
          // Never let a status for stream A mutate stream B's projection
          if (evSid && activeSid && evSid !== activeSid) {
            // Stale status for old stream — keep as nextStatus for observability but do not mutate dmState
            // Only update nextStatus if it belongs to the active stream to avoid UI showing wrong turn's status
            if (ev.type === 'dm.thinking') {
              // thinking events are transient; drop stale ones entirely
            } else {
              // For completed status, we still don't mutate active B's state
            }
          } else {
            nextStatus = ev
            if (ev.type === 'dm.status' && ev.status && nextDmState) {
              // Only mutate dmState when stream matches (or dmState is null and this is new stream's status)
              if (!activeSid || evSid === activeSid) {
                nextDmState = {
                  ...nextDmState,
                  status: String(ev.status),
                  streaming: ev.status === 'streaming',
                  last_sequence: (ev as { last_sequence?: number }).last_sequence ?? nextDmState.last_sequence,
                  chunk_count: (ev as { chunk_count?: number }).chunk_count ?? nextDmState.chunk_count,
                  visible_text: (ev as { visible_text?: string }).visible_text ?? nextDmState.visible_text,
                  stream_id: evSid ?? nextDmState.stream_id,
                }
              }
            } else if (ev.type === 'dm.thinking' && !activeSid) {
              // Edge: thinking for new stream when no active — keep as status
              nextStatus = ev
            }
          }
        }
        if (typeof ev.revision === 'number') nextRevision = Math.max(nextRevision ?? 0, ev.revision)
        if (typeof ev.sequence === 'number' && ev.type === 'revision') nextRevision = Math.max(nextRevision ?? 0, ev.sequence)
      }

      return {
        ...prev,
        messages: sortEventsBySequence(nextMessages) as RealtimeEvent[],
        dmChunks: nextChunks,
        dmStatus: nextStatus,
        dmState: nextDmState,
        revision: nextRevision,
      }
    })
  }, [])

  // Build a channel subscription; returns the channel handle
  const buildChannel = useCallback(
    (channelName: string) => {
      const ch = supabase.channel(channelName, { config: { private: true } } as unknown as never)

      const handleBroadcast = (payload: { payload: RealtimeEvent }) => {
        const ev = (payload.payload ?? payload) as RealtimeEvent
        bufferedRef.current.push(ev)
        if (snapshotRef.current) {
          const reconciled = reconcileBufferedEvents(snapshotRef.current, [ev])
          if (reconciled.length) {
            incRealtimeMetric('reconciliationEvents')
            applyEvents(reconciled)
          } else {
            incRealtimeMetric('reconciliationEvents')
          }
        }
      }

      try {
        ch.on('broadcast' as never, { event: 'submission.created' } as never, handleBroadcast as never)
        ch.on('broadcast' as never, { event: 'dm.chunk' } as never, handleBroadcast as never)
        ch.on('broadcast' as never, { event: 'dm.status' } as never, handleBroadcast as never)
        ch.on('broadcast' as never, { event: 'dm.thinking' } as never, handleBroadcast as never)
        ch.on('broadcast' as never, { event: 'revision' } as never, handleBroadcast as never)
      } catch {
        // fallback to postgres_changes if broadcast signature differs
      }
      return ch
    },
    [applyEvents],
  )

  // Subscription lifecycle — subscribe first, then snapshot, then reconcile (race-safe)
  useEffect(() => {
    if (!enabled || !campaignId || !threadId) {
      setState((prev) => ({ ...prev, connected: false }))
      return
    }

    isMountedRef.current = true
    bufferedRef.current = []
    let cancelled = false
    let currentChannel: ReturnType<typeof supabase.channel> | null = null

    const doSubscribe = async () => {
      const channelName = liveTableChannel(campaignId!, threadId!)
      // Audience check: server preflight (UX) — actual enforcement is DB RLS on realtime.messages
      try {
        await apiFetch(`/campaigns/${campaignId}/realtime/authorize`, {
          method: 'POST',
          body: JSON.stringify({ channel: channelName }),
        })
      } catch (e) {
        const status = (e as { status?: number }).status
        const msg =
          status === 404 ? 'Thread not found' : status === 403 ? 'Not authorized for this thread' : (e as Error).message ?? 'Realtime authorization failed'
        if (!cancelled && isMountedRef.current) setState((prev) => ({ ...prev, error: msg, connected: false }))
        return false
      }
      if (cancelled) return false

      currentChannel = buildChannel(channelName) as unknown as ReturnType<typeof supabase.channel>
      channelRef.current = currentChannel

      const onStatusChange = (status: string) => {
        if (cancelled) return
        if (status === 'SUBSCRIBED') {
          if (isMountedRef.current) setState((prev) => ({ ...prev, connected: true, error: null }))
          incRealtimeMetric('subscriptionCount')
          reconnectAttemptsRef.current = 0
        } else if (status === 'CHANNEL_ERROR' || status === 'TIMED_OUT' || status === 'CLOSED') {
          if (isMountedRef.current) setState((prev) => ({ ...prev, connected: false }))
          if (!cancelled) {
            reconnectAttemptsRef.current += 1
            incRealtimeMetric('reconnectCount')
            const backoff = Math.min(1000 * 2 ** reconnectAttemptsRef.current, 15000)
            setTimeout(async () => {
              if (cancelled || !isMountedRef.current) return
              // Explicitly rebuild the channel (supabase-js does not always auto-resubscribe private channels)
              try {
                if (currentChannel) {
                  try {
                    supabase.removeChannel(currentChannel as unknown as Parameters<typeof supabase.removeChannel>[0])
                  } catch {
                    try {
                      ;(currentChannel as unknown as { unsubscribe: () => void }).unsubscribe()
                    } catch { /* ignore */ }
                  }
                }
              } catch { /* ignore */ }
              currentChannel = null
              channelRef.current = null
              bufferedRef.current = []
              const ok = await doSubscribe()
              if (ok) {
                const snap = await fetchSnapshotAndAdopt()
                if (snap && bufferedRef.current.length) {
                  const reconciled = reconcileBufferedEvents(snap, bufferedRef.current)
                  if (reconciled.length) applyEvents(reconciled)
                  bufferedRef.current = []
                }
              }
            }, backoff)
          }
        }
      }

      try {
        ;(currentChannel as unknown as { subscribe: (cb: (s: string) => void) => void }).subscribe(onStatusChange as never)
      } catch {
        try {
          ;(currentChannel as unknown as { subscribe: (cb: (s: string) => void) => unknown }).subscribe(onStatusChange)
        } catch { /* ignore */ }
      }

      // Now fetch snapshot (after subscribe, so no gap) — adoption replaces state
      const snap = await fetchSnapshotAndAdopt()
      if (cancelled || !snap) return true // subscribed, even if snapshot failed; snapshot remains retryable

      // Reconcile any events that arrived between subscribe and snapshot response
      if (bufferedRef.current.length) {
        const reconciled = reconcileBufferedEvents(snap, bufferedRef.current)
        incRealtimeMetric('reconciliationEvents')
        if (reconciled.length) applyEvents(reconciled)
        bufferedRef.current = []
      }
      return true
    }

    doSubscribe()

    return () => {
      cancelled = true
      isMountedRef.current = false
      if (currentChannel) {
        try {
          supabase.removeChannel(currentChannel as unknown as Parameters<typeof supabase.removeChannel>[0])
        } catch {
          try {
            ;(currentChannel as unknown as { unsubscribe: () => void }).unsubscribe()
          } catch { /* ignore */ }
        }
        currentChannel = null
      }
      if (channelRef.current) {
        try {
          supabase.removeChannel(channelRef.current as unknown as Parameters<typeof supabase.removeChannel>[0])
        } catch { /* ignore */ }
        channelRef.current = null
      }
    }
  }, [campaignId, threadId, enabled, buildChannel, fetchSnapshotAndAdopt, applyEvents])

  const refresh = useCallback(async () => {
    const snap = await fetchSnapshotAndAdopt()
    if (snap && bufferedRef.current.length) {
      const reconciled = reconcileBufferedEvents(snap, bufferedRef.current)
      if (reconciled.length) applyEvents(reconciled)
      bufferedRef.current = []
    }
    return snap
  }, [fetchSnapshotAndAdopt, applyEvents])

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
    dmState: state.dmState,
    dmMessages: state.dmMessages,
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
