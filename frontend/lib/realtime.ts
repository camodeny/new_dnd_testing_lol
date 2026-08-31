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
  created_at?: string | null
  [key: string]: unknown
}

export interface SnapshotForRealtime {
  revision?: number
  reconciliation?: SnapshotReconciliation
  dm_state?: DmStateForRealtime | null
  dm_messages?: DmMessageForRealtime[]
  history?: {
    messages?: Array<{
      id: string
      sequence: number
      raw_content?: string
      thread_id?: string
      user_id?: string
      character_id?: string | null
      accepted_at?: string | null
      segments?: unknown[]
    }>
    pagination?: {
      limit?: number
      cursor?: string | null
      next_cursor?: string | null
      has_more?: boolean
      total_visible?: number
    }
  }
  active_thread_id?: string
  campaign?: { id: string; revision: number }
}

/**
 * Filter buffered realtime events to those strictly newer than the snapshot.
 * Stream-scoped reconciliation — never let a stale status/chunk for stream A
 * mutate stream B after a rollover during the subscribe→snapshot race.
 *
 * - Submissions: sequence > history_high_water_mark
 * - DM chunks: per-stream last_sequence (active stream only; stale stream ids dropped)
 * - DM status/thinking: per-stream — keep only if stream matches active or is new (not in completed)
 * - Unknown DM stream_ids (new streams created after snapshot) are kept
 */
export function reconcileBufferedEvents(
  snapshot: SnapshotForRealtime,
  buffered: RealtimeEvent[],
): RealtimeEvent[] {
  const recon = snapshot.reconciliation ?? {}
  let highWater = recon.history_high_water_mark
  if (highWater == null) highWater = recon.snapshot_sequence
  if (highWater == null) highWater = recon.snapshot_revision
  if (highWater == null) highWater = snapshot.reconciliation?.snapshot_revision ?? snapshot.revision ?? 0
  const hw = typeof highWater === 'number' ? highWater : Number(highWater) || 0

  const dmState = snapshot.dm_state
  const dmHwRaw = dmState?.last_sequence
  const dmHw = typeof dmHwRaw === 'number' ? dmHwRaw : dmHwRaw != null ? Number(dmHwRaw) : -1
  const activeStreamId = dmState?.stream_id ? String(dmState.stream_id) : dmState?.id ? String(dmState.id) : null
  const completedIds = new Set<string>((snapshot.dm_messages ?? []).map((m) => String(m.id)))
  // Also include stream_id alias if present
  for (const m of snapshot.dm_messages ?? []) {
    const sid = (m as unknown as { stream_id?: string }).stream_id
    if (sid) completedIds.add(String(sid))
  }

  return buffered.filter((ev) => {
    const t = ev.type
    const seq = ev.sequence
    const streamId = ev.stream_id ? String(ev.stream_id) : null

    // DM status/thinking without sequence are still stream-scoped
    if ((t === 'dm.status' || t === 'dm.thinking') && seq == null) {
      if (!streamId) return true
      if (activeStreamId && streamId === activeStreamId) return true
      if (!completedIds.has(streamId) && streamId !== activeStreamId) return true // new stream
      return false // stale status for old completed/active stream
    }

    if (seq == null) return true // generic non-sequenced (revision etc.)

    const n = typeof seq === 'number' ? seq : Number(seq)
    if (Number.isNaN(n)) return true

    if (t === 'dm.chunk') {
      if (!streamId) return n > dmHw
      if (activeStreamId && streamId === activeStreamId) return n > dmHw
      if (completedIds.has(streamId)) {
        // Completed stream: any chunk after snapshot is stale (snapshot already has final_text)
        // Only keep if somehow newer than completed (should not happen), but we drop to avoid regressing B.
        // Check if we have last_sequence for completed - without it, drop.
        return false
      }
      // Unknown stream (new stream B created after snapshot) — keep
      return true
    }

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

/**
 * Derive DM visible_text / last_sequence / chunk_count from authoritative snapshot base
 * plus sorted incremental chunks that are newer than the snapshot. Only contiguous
 * chunks from baseLast+1 are incorporated — gaps are buffered until the missing
 * sequence arrives, and duplicates are ignored. This makes the derived state
 * sequence-aware and persistently deduped, handling duplicate and 7-before-6 cases.
 */
export function deriveDmState(
  base: DmStateForRealtime | null | undefined,
  chunks: RealtimeEvent[],
): DmStateForRealtime | null {
  if (!base) return base ?? null
  const baseText = typeof base.visible_text === 'string' ? base.visible_text : ''
  const baseLastRaw = base.last_sequence
  const baseLast = typeof baseLastRaw === 'number' ? baseLastRaw : baseLastRaw != null ? Number(baseLastRaw) : -1
  const baseCount = typeof base.chunk_count === 'number' ? base.chunk_count : 0
  const sorted = sortEventsBySequence(dedupeEvents(chunks))
  let curText = baseText
  let expected = Number.isFinite(baseLast) ? baseLast + 1 : 0
  let contiguousCount = 0
  let lastContiguous = baseLast
  for (const ch of sorted) {
    const seq = typeof ch.sequence === 'number' ? ch.sequence : Number(ch.sequence)
    if (Number.isNaN(seq)) continue
    if (seq === expected) {
      curText += typeof ch.text === 'string' ? ch.text : ''
      expected++
      contiguousCount++
      lastContiguous = seq
    } else if (seq > expected) {
      break // gap
    }
    // seq < expected is duplicate/old — skip
  }
  return {
    ...base,
    visible_text: curText,
    last_sequence: lastContiguous >= 0 ? lastContiguous : base.last_sequence,
    chunk_count: baseCount + contiguousCount,
  }
}

/**
 * Next DM state after a status event, handling A→B rollover.
 * If status is for a new streaming stream B not in completed and mismatched with active A,
 * it replaces active state; stale old-stream statuses are dropped.
 */
export function nextDmStateForStatus(
  current: DmStateForRealtime | null,
  event: RealtimeEvent,
  snapshot: SnapshotForRealtime | null,
  runtimeTerminalIds?: Set<string>,
): DmStateForRealtime | null {
  const evSid = event.stream_id ? String(event.stream_id) : null
  const activeSid = current?.stream_id ? String(current.stream_id) : null
  const status = String(event.status ?? '')
  const completedIds = new Set<string>((snapshot?.dm_messages ?? []).map((m) => String(m.id)))
  for (const m of snapshot?.dm_messages ?? []) {
    const sid2 = (m as unknown as { stream_id?: string }).stream_id
    if (sid2) completedIds.add(String(sid2))
  }
  // Runtime terminal knowledge (completed/abandoned/failed seen in-memory) — not yet in snapshot
  const isTerminal = (id: string) => completedIds.has(id) || (runtimeTerminalIds?.has(id) ?? false)

  if (evSid && activeSid && evSid !== activeSid) {
    if (status === 'streaming' && !isTerminal(evSid)) {
      // New stream B — establish, but only if B is not older than current.
      // Use created_at ordering if available, otherwise rely on terminal set to reject old A.
      // If current is B and delayed A streaming arrives, A will be in terminal set (completed) or
      // if A was abandoned/failed (absent from dm_messages), runtimeTerminalIds will contain it.
      return {
        status: 'streaming',
        streaming: true,
        stream_id: evSid,
        turn_id: event.turn_id ? String(event.turn_id) : null,
        attempt_id: event.attempt_id ? String(event.attempt_id) : null,
        visible_text: typeof (event as { visible_text?: string }).visible_text === 'string' ? (event as { visible_text?: string }).visible_text! : '',
        last_sequence: (event as { last_sequence?: number | null }).last_sequence ?? null,
        chunk_count: typeof (event as { chunk_count?: number }).chunk_count === 'number' ? (event as { chunk_count?: number }).chunk_count! : 0,
        trace_id: (event as { trace_id?: string }).trace_id ?? null,
      } as unknown as DmStateForRealtime
    }
    return current // stale old
  }

  if (!current && evSid) {
    // No active, new status establishes
    return {
      status: status || 'streaming',
      streaming: status === 'streaming',
      stream_id: evSid,
      turn_id: event.turn_id ? String(event.turn_id) : null,
      attempt_id: event.attempt_id ? String(event.attempt_id) : null,
      visible_text: typeof (event as { visible_text?: string }).visible_text === 'string' ? (event as { visible_text?: string }).visible_text! : '',
      last_sequence: (event as { last_sequence?: number | null }).last_sequence ?? null,
      chunk_count: typeof (event as { chunk_count?: number }).chunk_count === 'number' ? (event as { chunk_count?: number }).chunk_count! : 0,
    } as unknown as DmStateForRealtime
  }

  if (!current) return current

  if (activeSid && evSid !== activeSid) return current // mismatched non-streaming status

  return {
    ...current,
    status,
    streaming: status === 'streaming',
    last_sequence: (event as { last_sequence?: number | null }).last_sequence ?? current.last_sequence,
    chunk_count: typeof (event as { chunk_count?: number }).chunk_count === 'number' ? (event as { chunk_count?: number }).chunk_count! : current.chunk_count,
    visible_text: typeof (event as { visible_text?: string }).visible_text === 'string' ? (event as { visible_text?: string }).visible_text! : current.visible_text,
    stream_id: evSid ?? current.stream_id,
  } as unknown as DmStateForRealtime
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
