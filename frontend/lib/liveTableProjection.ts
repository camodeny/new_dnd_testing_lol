import type { Character, Message, User } from '@/types'
import type { DmMessageForRealtime, DmStateForRealtime, RealtimeEvent } from './realtime'

interface ProjectionOptions {
  submissions: RealtimeEvent[]
  dmMessages: DmMessageForRealtime[]
  characters: Character[]
  currentUser: User | null
  sessionId?: string
}

function timestamp(value: unknown): string {
  return typeof value === 'string' && value ? value : new Date(0).toISOString()
}

export function projectLiveTableMessages({
  submissions,
  dmMessages,
  characters,
  currentUser,
  sessionId = '',
}: ProjectionOptions): Message[] {
  const players = submissions.map((event): Message => {
    const character = characters.find((candidate) => String(candidate.id) === String(event.character_id ?? ''))
    const isCurrentUser = currentUser && String(event.user_id ?? '') === String(currentUser.id)
    const segments = Array.isArray(event.segments) ? event.segments : []
    return {
      id: String(event.id ?? event.event_id),
      session_id: sessionId,
      role: 'player',
      content: typeof event.raw_content === 'string' ? event.raw_content : '',
      created_at: timestamp(event.accepted_at ?? event.timestamp),
      sender_name: character?.name ?? (isCurrentUser ? currentUser.username : 'Player'),
      is_ic: segments.length > 0 && segments.every((segment) => segment.type === 'ic'),
    }
  })

  const dm = dmMessages.map((message): Message => ({
    id: `dm:${message.id}`,
    session_id: sessionId,
    role: 'dm',
    content: message.final_text,
    created_at: timestamp(message.completed_at ?? message.created_at),
    sender_name: 'Dungeon Master',
  }))

  const byId = new Map<string, Message>()
  for (const message of [...players, ...dm]) byId.set(String(message.id), message)
  return [...byId.values()].sort((a, b) => {
    const byTime = Date.parse(a.created_at) - Date.parse(b.created_at)
    if (byTime !== 0) return byTime
    return String(a.id).localeCompare(String(b.id))
  })
}

export function activeDmText(dmState: DmStateForRealtime | null, dmMessages: DmMessageForRealtime[]): string {
  if (!dmState?.stream_id || !dmState.visible_text) return ''
  const completed = dmMessages.some((message) => {
    const streamId = typeof message.stream_id === 'string' ? message.stream_id : message.id
    return String(streamId) === String(dmState.stream_id)
  })
  return completed ? '' : dmState.visible_text
}
