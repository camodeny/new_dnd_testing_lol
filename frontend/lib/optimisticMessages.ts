import type { Message } from '@/types'

/** Clock-skew tolerance when matching an optimistic entry to its server echo. */
const ECHO_SKEW_TOLERANCE_MS = 30_000

function byTimeThenId(a: Message, b: Message): number {
  const byTime = Date.parse(a.created_at) - Date.parse(b.created_at)
  if (byTime !== 0) return byTime
  return String(a.id).localeCompare(String(b.id))
}

/**
 * Merge client-optimistic player messages into the authoritative server
 * projection. An optimistic entry retires once a server echo covers it
 * (same content + sender, server timestamp at/after the optimistic one).
 * Duplicate texts retire together — every server echo stays rendered.
 * Server state is never rewritten; unmatched entries simply append in order.
 */
export function mergeOptimisticMessages(server: Message[], pending: Message[]): Message[] {
  const uncovered = pending.filter((entry) => !server.some(
    (message) => message.role === 'player'
      && message.content === entry.content
      && (message.sender_name ?? '') === (entry.sender_name ?? '')
      && Date.parse(message.created_at) >= Date.parse(entry.created_at) - ECHO_SKEW_TOLERANCE_MS,
  ))
  return [...server, ...uncovered].sort(byTimeThenId)
}
