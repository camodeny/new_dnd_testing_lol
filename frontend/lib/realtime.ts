'use client'

/**
 * Audience-safe Supabase Realtime helpers — issue #198.
 *
 * - Private channels per thread: `live-table:campaign:<cid>:thread:<tid>`
 * - Stable event ids for dedupe/reconciliation
 * - Snapshot → subscribe race handling: subscribe first, buffer, then fetch
 *   snapshot, then reconcile (drop events already in snapshot).
 * - Duplicate/out-of-order tolerance via dedupe + sequence ordering.
 * - Realtime outage never blocks authoritative snapshot fetch.
 */

export type RealtimeEventType = 'submission.created' | 'dm.chunk' | 'dm.status' | 'dm.thinking' | 'revision'

export interface RealtimeEvent {
  type: RealtimeEventType | string
  event_id: string
  campaign_id: string
  thread_id: string
  sequence?: number
  revision?: number
  timestamp?: string
  dedupe_key?: string
  channel?: string
  // payload specifics
  id?: string
  raw_content?: string
  segments?: Array<{ type: 'ic' | 'ooc'; text: string }>
  text?: string
  stream_id?: string
  status?: string
  [key: string]: unknown
}

export function liveTableChannel(campaignId: string, threadId: string): string {
  return `live-table:campaign:${campaignId.toLowerCase()}:thread:${threadId.toLowerCase()}`
}

export function parseLiveTableChannel(channel: string): { campaignId: string; threadId: string } | null {
  const parts = channel.split(':')
  if (parts.length !== 5) return null
  if (parts[0] !== 'live-table' || parts[1] !== 'campaign' || parts[3] !== 'thread') return null
  return { campaignId: parts[2], threadId: parts[4] }
}

/**
 * Deduplicate realtime events by stable key (event_id or dedupe_key).
 * Preserves first-seen order; duplicate deliveries are dropped.
 * Out-of-order delivery is tolerated — caller sorts by sequence if needed.
 */
export function dedupeEvents(events: RealtimeEvent[], key: 'event_id' | 'dedupe_key' = 'event_id'): RealtimeEvent[] {
  const seen = new Set<string>()
  const out: RealtimeEvent[] = []
  for (const ev of events) {
    const k = String((ev as unknown as Record<string, unknown>)[key] ?? ev.dedupe_key ?? '')
    if (!k) {
      out.push(ev)
      continue
    }
    if (seen.has(k)) continue
    seen.add(k)
    out.push(ev)
  }
  return out
}

/**
 * Sort events by sequence (numeric) ascending; events without sequence keep original order at end.
 */
export function sortEventsBySequence(events: RealtimeEvent[]): RealtimeEvent[] {
  return [...events].sort((a, b) => {
    const sa = typeof a.sequence === 'number' ? a.sequence : Number.MAX_SAFE_INTEGER
    const sb = typeof b.sequence === 'number' ? b.sequence : Number.MAX_SAFE_INTEGER
    if (sa === Number.MAX_SAFE_INTEGER && sb === Number.MAX_SAFE_INTEGER) return 0
    if (sa === Number.MAX_SAFE_INTEGER) return 1
    if (sb === Number.MAX_SAFE_INTEGER) return -1
    return sa - sb
  })
}

export interface SnapshotReconciliation {
  snapshot_revision?: number
  snapshot_sequence?: number
  history_high_water_mark?: number | null
  history_total?: number
  realtime_resume_token?: string
}

export interface DmStateForRealtime {
  status: string
  streaming: boolean
  active_turn?: string | null
  turn_id?: string | null
  attempt_id?: string | null
  stream_id?: string | null
  last_sequence?: number | null
  chunk_count?: number
  visible_text?: string
  started_at?: string | null
  first_chunk_at?: string | null
  last_chunk_at?: string | null
  trace_id?: string | null
  [key: string]: unknown
}

export interface DmMessageForRealtime {
  id: string
  turn_id: string
  attempt_id: string
  final_text: string
  status: string
  completed_at?: string | null
  [key: string]: unknown
}

export interface SnapshotForRealtime {
  revision?: number
  reconciliation?: SnapshotReconciliation
  dm_state?: DmStateForRealtime | null
  dm_messages?: DmMessageForRealtime[]
  history?: { messages?: Array<{ id: string; sequence: number; raw_content?: string; thread_id?: string; segments?: unknown[] }> }
  active_thread_id?: string
  campaign?: { id: string; revision: number }
}

/**
 * Filter buffered realtime events to those strictly newer than the snapshot.
 * Implements the snapshot→subscribe race fix: subscribe first (buffering),
 * then fetch snapshot, then drop buffered events already contained in snapshot.
 *
 * For submission events: keep only sequence > history_high_water_mark
 * For dm.chunk: keep only sequence > dm_state.last_sequence
 * For non-sequenced events (thinking/status): keep all (idempotent by event_id)
 */
export function reconcileBufferedEvents(
  snapshot: SnapshotForRealtime,
  buffered: RealtimeEvent[],
): RealtimeEvent[] {
  const recon = snapshot.reconciliation ?? {}
  let highWater = recon.history_high_water_mark
  if (highWater == null) highWater = recon.snapshot_sequence
  if (highWater == null) highWater = recon.snapshot_revision
  if (highWater == null) highWater = snapshot.revision ?? 0
  const hw = typeof highWater === 'number' ? highWater : Number(highWater) || 0

  const dmHwRaw = snapshot.dm_state?.last_sequence
  const dmHw = typeof dmHwRaw === 'number' ? dmHwRaw : dmHwRaw != null ? Number(dmHwRaw) : -1

  return buffered.filter((ev) => {
    const seq = ev.sequence
    if (seq == null) return true // non-sequenced: keep (dedupe handles idempotency)
    const n = typeof seq === 'number' ? seq : Number(seq)
    if (Number.isNaN(n)) return true
    if (ev.type === 'dm.chunk') return n > dmHw
    return n > hw
  })
}

/**
 * Apply reconciled events to a snapshot's message list (pure, for convergence checks).
 * Returns new message list sorted by sequence, deduped by id.
 * Used to verify that "snapshot + missed events == fresh snapshot".
 */
export function applyEventsToMessages(
  existing: Array<{ id: string; sequence: number; raw_content?: string }>,
  events: RealtimeEvent[],
): Array<{ id: string; sequence: number; raw_content?: string }> {
  const deduped = dedupeEvents(events)
  const reconciledSorted = sortEventsBySequence(deduped)
  const byId = new Set(existing.map((m) => m.id))
  const out = [...existing]
  for (const ev of reconciledSorted) {
    if (ev.type === 'submission.created' && ev.id && !byId.has(ev.id)) {
      out.push({
        id: ev.id,
        sequence: typeof ev.sequence === 'number' ? ev.sequence : 0,
        raw_content: typeof ev.raw_content === 'string' ? ev.raw_content : undefined,
      })
      byId.add(ev.id)
    }
  }
  return out.sort((a, b) => a.sequence - b.sequence)
}

// ── Observability counters (module-local, mirrors backend metrics) ──────

let _metrics = {
  publishFailures: 0,
  subscriptionCount: 0,
  reconnectCount: 0,
  duplicateDeliveries: 0,
  reconciliationEvents: 0,
  snapshotCatchups: 0,
}

export function getRealtimeClientMetrics() {
  return { ..._metrics }
}

export function incRealtimeMetric(key: keyof typeof _metrics, n = 1) {
  _metrics[key] += n
}

export function resetRealtimeMetrics() {
  _metrics = {
    publishFailures: 0,
    subscriptionCount: 0,
    reconnectCount: 0,
    duplicateDeliveries: 0,
    reconciliationEvents: 0,
    snapshotCatchups: 0,
  }
}
